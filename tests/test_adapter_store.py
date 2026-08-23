"""Promoted adapter source must survive: hash -> bytes, and the schema migrates."""

import sqlite3
from pathlib import Path

from swarm.core.identity import result_hash
from swarm.hub.adapter_store import AdapterStore, store_for
from swarm.hub.registry import Registry

SOURCE = "def run(x):\n    return x * 2\n"

# The adapters table exactly as it was before source_hash existed.
_OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS adapters (
    adapter_id TEXT PRIMARY KEY,
    device_class TEXT,
    authored_by TEXT,
    probe_evidence_hash TEXT,
    gate_run_id TEXT,
    exemplar_id TEXT,
    at REAL
);
"""


def _registry(tmp_path: Path) -> Registry:
    return Registry(str(tmp_path / "hub.db"))


def test_put_get_round_trip(tmp_path: Path):
    store = AdapterStore(root=tmp_path / "adapters")
    content_hash = store.put(SOURCE)
    assert content_hash == result_hash(SOURCE.encode("utf-8"))
    assert store.exists(content_hash)
    assert store.get(content_hash) == SOURCE


def test_put_is_idempotent(tmp_path: Path):
    root = tmp_path / "adapters"
    store = AdapterStore(root=root)
    first = store.put(SOURCE)
    second = store.put(SOURCE)
    assert first == second
    blobs = [p for p in root.rglob("*") if p.is_file()]
    assert len(blobs) == 1
    assert store.get(first) == SOURCE


def test_get_unknown_hash_returns_none(tmp_path: Path):
    store = AdapterStore(root=tmp_path / "adapters")
    assert store.get("0" * 64) is None
    assert store.exists("0" * 64) is False
    # Malformed keys must not raise and must not escape the root.
    assert store.get("not-a-hash") is None
    assert store.get("../../etc/passwd") is None
    assert store.exists("") is False


def test_empty_source_still_addressable(tmp_path: Path):
    store = AdapterStore(root=tmp_path / "adapters")
    content_hash = store.put("")
    assert store.get(content_hash) == ""


def test_store_for_follows_db_directory(tmp_path: Path):
    store = store_for(str(tmp_path / "hub.db"))
    assert store.root == (tmp_path / "adapters").resolve()
    assert store_for(":memory:").root.name == "adapters"


def test_record_adapter_with_source_round_trips(tmp_path: Path):
    reg = _registry(tmp_path)
    reg.record_adapter(
        adapter_id="hw-src",
        device_class="nvidia:geforce_rtx_4090",
        authored_by="human",
        gate_run_id="gate-1",
        source=SOURCE,
    )
    assert reg.get_adapter_source("hw-src") == SOURCE
    rows = reg.list_adapters()
    assert len(rows) == 1
    assert "source_hash" in rows[0]
    assert rows[0]["source_hash"] == result_hash(SOURCE.encode("utf-8"))
    # Blobs land beside the db, not in the user's home.
    assert (tmp_path / "adapters").is_dir()
    reg.close()


def test_record_adapter_without_source(tmp_path: Path):
    reg = _registry(tmp_path)
    reg.record_adapter(
        adapter_id="hw-bare",
        device_class="cpu:x86_64",
        authored_by="human",
    )
    assert reg.get_adapter_source("hw-bare") is None
    assert reg.get_adapter_source("does-not-exist") is None
    assert reg.list_adapters()[0]["source_hash"] is None
    assert not (tmp_path / "adapters").exists()
    reg.close()


def test_source_backfills_onto_existing_row(tmp_path: Path):
    reg = _registry(tmp_path)
    reg.record_adapter(adapter_id="hw-late", device_class="cpu:x86_64", authored_by="human")
    assert reg.get_adapter_source("hw-late") is None
    reg.record_adapter(
        adapter_id="hw-late",
        device_class="cpu:x86_64",
        authored_by="human",
        source=SOURCE,
    )
    assert reg.get_adapter_source("hw-late") == SOURCE
    reg.close()


def test_migration_adds_column_and_keeps_rows(tmp_path: Path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO adapters (adapter_id, device_class, authored_by, probe_evidence_hash,"
        " gate_run_id, exemplar_id, at) VALUES (?,?,?,?,?,?,?)",
        ("hw-legacy", "cpu:x86_64", "human", "ev", "gate-0", "ex-0", 1.0),
    )
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(adapters)").fetchall()}
    assert "source_hash" not in cols
    conn.close()

    reg = Registry(str(db))
    cols = {r["name"] for r in reg._conn.execute("PRAGMA table_info(adapters)").fetchall()}
    assert "source_hash" in cols

    rows = reg.list_adapters()
    assert len(rows) == 1
    legacy = rows[0]
    assert legacy["adapter_id"] == "hw-legacy"
    assert legacy["gate_run_id"] == "gate-0"
    assert legacy["source_hash"] is None
    assert reg.get_adapter_source("hw-legacy") is None

    # The migrated db accepts source like a fresh one.
    reg.record_adapter(
        adapter_id="hw-new",
        device_class="cpu:x86_64",
        authored_by="human",
        source=SOURCE,
    )
    assert reg.get_adapter_source("hw-new") == SOURCE
    reg.close()

    # Migration is idempotent across reopens.
    reg2 = Registry(str(db))
    assert reg2.get_adapter_source("hw-new") == SOURCE
    assert len(reg2.list_adapters()) == 2
    reg2.close()


def test_injected_store_is_used(tmp_path: Path):
    store = AdapterStore(root=tmp_path / "elsewhere")
    reg = Registry(":memory:", store=store)
    reg.record_adapter(
        adapter_id="hw-injected",
        device_class="cpu:x86_64",
        authored_by="human",
        source=SOURCE,
    )
    assert reg.get_adapter_source("hw-injected") == SOURCE
    assert (tmp_path / "elsewhere").is_dir()
    reg.close()
