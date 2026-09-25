"""`swarm map`: the owner's own Python function, fanned out across workers,
results back in input order; a raising item is reported, not hidden."""

import json

from swarm import cli
from swarm.agent.daemon import Agent
from swarm.hub.server import Hub
from swarm.probe import runtimes as runtimes_mod

FN = '''
def run(params):
    if params.get("item") == "boom":
        raise ValueError("bad input")
    return {"square": params["n"] ** 2} if "n" in params else {"echo": params["item"]}
'''


def test_map_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runtimes_mod, "find_llama_binaries", lambda: {})
    monkeypatch.setenv("SWARM_OLLAMA_URL", "http://127.0.0.1:9")
    hub = Hub(host="127.0.0.1", port=0)
    _, port = hub.start_background()
    agent = Agent(hub_url=f"http://127.0.0.1:{port}", bench=False, ignore_welfare=True)
    try:
        assert agent.run_once()
        agent.start_worker(poll_seconds=0.2)
        fn = tmp_path / "fn.py"
        fn.write_text(FN)
        inputs = tmp_path / "in.jsonl"
        inputs.write_text("\n".join([json.dumps({"n": i}) for i in range(5)] + ['"hello"', '"boom"']))
        out = tmp_path / "out.jsonl"
        code = cli.main(["--hub", f"http://127.0.0.1:{port}", "map", str(fn), str(inputs), "-o", str(out)])
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        assert [r["index"] for r in rows] == list(range(7))
        assert [r["result"]["square"] for r in rows[:5]] == [0, 1, 4, 9, 16]
        assert rows[5]["result"] == {"echo": "hello"}
        assert rows[6]["ok"] is False and "bad input" in rows[6]["error"]
        assert code == 1, "a failed item makes the command exit non-zero"
    finally:
        agent.stop_worker()
        hub.stop()
