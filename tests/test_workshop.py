"""Workshop law: sandbox gate, operator approval, exact-byte rollback,
ledger with hashes, never outside the repo."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SWARM_WORKSHOP_SANDBOX") == "1",
    reason="no nested workshops inside a sandbox test run",
)

from pathlib import Path

from swarm.hub.registry import Registry
from swarm.hub.workshop import Workshop


def _ws(repo: Path) -> Workshop:
    return Workshop(Registry(":memory:")._conn, repo)


def test_gate_rejects_syntax_error():
    ws = _ws(Path.cwd())
    bad = ws.propose("break", "proof", {"swarm/core/_t.py": "def f(:"})
    assert bad["gate_ok"] is False and bad["status"] == "rejected"
    with pytest.raises(ValueError):
        ws.approve_and_apply(bad["patch_id"])


def test_path_traversal_refused():
    ws = _ws(Path.cwd())
    with pytest.raises(ValueError):
        ws.propose("evil", "proof", {"../outside.py": "x = 1"})
    with pytest.raises(ValueError):
        ws.propose("evil", "proof", {"swarm/evil.py": "x=1", "binary.bin": "x"})


def test_good_patch_apply_and_rollback(tmp_path_factory, monkeypatch):
    # use the real repo so the test gate runs against it; sandbox copies it
    ws = _ws(Path.cwd())
    good = ws.propose(
        "add answer",
        "prove green apply",
        {
            "swarm/core/_tmp_answer.py": "def answer() -> int:\n    return 42\n",
            "tests/test__tmp_answer.py": "from swarm.core._tmp_answer import answer\n\n\ndef test_a():\n    assert answer() == 42\n",
        },
    )
    assert good["status"] == "staged" and good["gate_ok"] is True
    apply_out = ws.approve_and_apply(good["patch_id"])
    assert apply_out["status"] == "applied"
    assert Path("swarm/core/_tmp_answer.py").exists()
    rolled = ws.rollback(good["patch_id"])
    assert rolled["status"] == "rolled_back"
    assert not Path("swarm/core/_tmp_answer.py").exists()
    assert not Path("tests/test__tmp_answer.py").exists()


def test_ledger_records_everything():
    ws = _ws(Path.cwd())
    ws.propose("bad", "proof", {"swarm/core/_x.py": "def f(:"})
    rows = ws.ledger()
    assert rows and rows[0]["status"] == "rejected"
