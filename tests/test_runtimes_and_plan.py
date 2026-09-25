"""Runtime discovery reads what runtimes SAY; the planner places models on
measured memory only, never shards what fits, and never double-counts a
unified-memory GPU as extra RAM."""

import struct

from swarm.hub.inference import GIB, estimate_need, node_capacity, plan_llama, usable_bytes
from swarm.probe import runtimes


def _gguf(path, arch="llama", layers=16, ctx=4096, pad=0):
    """A minimal valid GGUF v3 header (plus padding to fake a size)."""

    def s(text):
        b = text.encode()
        return struct.pack("<Q", len(b)) + b

    kvs = [
        s("general.architecture") + struct.pack("<I", 8) + s(arch),
        # an array KV the reader must skip (like tokenizer vocab)
        s("tokenizer.ggml.tokens") + struct.pack("<I", 9) + struct.pack("<IQ", 8, 3) + s("a") + s("bb") + s("c"),
        s(f"{arch}.block_count") + struct.pack("<I", 4) + struct.pack("<I", layers),
        s(f"{arch}.context_length") + struct.pack("<I", 4) + struct.pack("<I", ctx),
    ]
    header = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + struct.pack("<Q", len(kvs)) + b"".join(kvs)
    path.write_bytes(header + b"\0" * pad)


def test_device_list_parsing():
    text = """load_backend: loaded Vulkan backend
Available devices:
  Vulkan0: Intel(R) Iris(R) Xe Graphics (16235 MiB, 15467 MiB free)
  CUDA0: NVIDIA GeForce RTX 3060 (12288 MiB, 11000 MiB free)
  RPC0: 127.0.0.1:50061 (32471 MiB, 19242 MiB free)
"""
    devs = runtimes.parse_device_list(text)
    assert [d["id"] for d in devs] == ["Vulkan0", "CUDA0", "RPC0"]
    assert devs[0]["free_bytes"] == 15467 * 1024 * 1024
    assert devs[1]["name"] == "NVIDIA GeForce RTX 3060"


def test_gguf_header_is_read_not_guessed(tmp_path):
    f = tmp_path / "m.gguf"
    _gguf(f, arch="qwen35", layers=32, ctx=262144)
    meta = runtimes.gguf_metadata(f)
    assert meta == {"architecture": "qwen35", "n_layers": 32, "context_length": 262144}
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"NOPE" + b"\0" * 64)
    assert runtimes.gguf_metadata(bad) == {}


def test_local_models_offer_split_files_once(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_MODELS_DIR", str(tmp_path))
    _gguf(tmp_path / "Big-00001-of-00002.gguf", pad=100)
    (tmp_path / "Big-00002-of-00002.gguf").write_bytes(b"\0" * 300)
    _gguf(tmp_path / "Small.gguf", layers=8)
    _gguf(tmp_path / "mmproj-F16.gguf")
    models = {m["name"]: m for m in runtimes.local_gguf_models()}
    assert set(models) == {"Big", "Small"}
    assert models["Big"]["size_bytes"] > 300 and models["Small"]["n_layers"] == 8
    assert runtimes.resolve_local_model("Small") == tmp_path / "Small.gguf"
    assert runtimes.resolve_local_model("Big").name == "Big-00001-of-00002.gguf"
    assert runtimes.resolve_local_model("nothere") is None


def test_ollama_models_classed_by_reported_capability(monkeypatch):
    from tests.fake_ollama import FakeOllama

    fake = FakeOllama(
        models=[
            {"name": "chat:latest", "size": 1, "capabilities": ["completion", "tools"]},
            {"name": "emb:latest", "size": 1, "capabilities": ["embedding"]},
            {"name": "mystery:latest", "size": 1},  # no capabilities, /api/show 404s
        ]
    )
    try:
        monkeypatch.setenv("SWARM_OLLAMA_URL", fake.url)
        models, anomalies = runtimes.ollama_models()
        kinds = {(m["name"], m["kind"]) for m in models}
        assert kinds == {("chat:latest", "chat"), ("emb:latest", "embed")}
        assert any("mystery" in a.message for a in anomalies)
    finally:
        fake.stop()


def test_no_ollama_is_an_empty_answer(monkeypatch):
    monkeypatch.setenv("SWARM_OLLAMA_URL", "http://127.0.0.1:9")
    assert runtimes.ollama_models(timeout=0.5) == ([], [])


# ---------------------------------------------------------------- planner

MODEL = {"name": "m", "size_bytes": 4 * GIB, "n_layers": 32}


def _node(nid, gpu_free=None, ram_free=None):
    devices = [{"id": "Vulkan0", "free_bytes": gpu_free}] if gpu_free else []
    return {"node_id": nid, "hostname": nid, "devices": devices, "ram_free_bytes": ram_free}


def test_host_reserve_is_kept():
    assert usable_bytes(8 * GIB, "cpu") == 6 * GIB
    assert usable_bytes(1 * GIB, "cpu") == 0
    assert usable_bytes(10 * GIB, "gpu") == 9 * GIB


def test_unified_memory_is_not_counted_twice():
    cap, kind, per = node_capacity(_node("laptop", gpu_free=15 * GIB, ram_free=16 * GIB))
    assert kind == "gpu" and cap == usable_bytes(15 * GIB, "gpu")


def test_fits_one_node_is_never_sharded():
    plan = plan_llama(MODEL, [_node("big", gpu_free=24 * GIB)], [_node("phone", ram_free=6 * GIB)])
    assert plan["feasible"] and plan["mode"] == "single"
    assert plan["tensor_split"] is None and plan["participants"][0]["node_id"] == "big"
    assert "not sharding" in plan["reason"]


def test_pools_the_fewest_nodes_largest_first():
    need, _ = estimate_need(MODEL["size_bytes"])
    head = _node("head", gpu_free=3 * GIB)  # 2.5 GiB usable
    helpers = [_node("small", ram_free=4 * GIB), _node("large", ram_free=12 * GIB), _node("mid", ram_free=6 * GIB)]
    plan = plan_llama(MODEL, [head], helpers)
    assert plan["feasible"] and plan["mode"] == "pooled"
    ids = [p["node_id"] for p in plan["participants"]]
    assert ids == ["head", "large"], "one big helper beats three hops"
    assert sum(p["layers"] for p in plan["participants"]) == 32
    # GPU head: split vector = [head's Vulkan0, then each rpc endpoint]
    assert len(plan["tensor_split"]) == 2 and plan["n_gpu_layers"] == 999
    assert plan["need_bytes"] == need and "ESTIMATED" in plan["need_basis"]


def test_cpu_head_keeps_its_layers_on_cpu():
    head = _node("vps", ram_free=5 * GIB)  # 3 GiB usable, CPU only
    plan = plan_llama(MODEL, [head], [_node("laptop", gpu_free=15 * GIB)])
    assert plan["mode"] == "pooled"
    head_layers = plan["participants"][0]["layers"]
    assert plan["n_gpu_layers"] == 32 - head_layers
    assert plan["tensor_split"] == [32 - head_layers]
    assert plan["participants"][1]["device"] == "Vulkan0"


def test_infeasible_says_how_short():
    plan = plan_llama(MODEL, [_node("h", ram_free=3 * GIB)], [_node("p", ram_free=3 * GIB)])
    assert not plan["feasible"] and "short by" in plan["reason"]


def test_force_shard_spreads_even_when_it_fits():
    plan = plan_llama(
        MODEL, [_node("h", gpu_free=24 * GIB)], [_node("a", ram_free=12 * GIB), _node("b", ram_free=12 * GIB)],
        force_shard=True,
    )
    assert plan["feasible"] and plan["mode"] == "pooled" and plan["force_shard"]
    assert len(plan["participants"]) == 3
    assert sum(plan["tensor_split"]) == 32


def test_unknown_size_is_refused():
    plan = plan_llama({"name": "x"}, [_node("h", gpu_free=24 * GIB)], [])
    assert not plan["feasible"] and "unknown" in plan["reason"]


def test_build_number_parsing():
    assert runtimes.llama_build("0.5.0-dev (build 11190, commit fcc891545)") == 11190
    assert runtimes.llama_build(None) is None


def test_helpers_on_a_different_llama_build_never_pool():
    head = dict(_node("head", gpu_free=3 * GIB), build=10615)
    same = dict(_node("same", ram_free=12 * GIB), build=10615)
    other = dict(_node("other", ram_free=64 * GIB), build=11190)
    plan = plan_llama(MODEL, [head], [other, same])
    assert plan["feasible"] and [p["node_id"] for p in plan["participants"]] == ["head", "same"]
    assert "other (build 11190)" in plan["reason"]
    only_other = plan_llama(MODEL, [head], [other], force_shard=True)
    assert not only_other["feasible"] and "update llama.cpp" in only_other["reason"]
