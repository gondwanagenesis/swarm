from pathlib import Path

from swarm.core.identity import adapter_id, get_node_id, result_hash


def test_node_id_persists(tmp_path: Path):
    state = tmp_path / "node_id"
    first = get_node_id(state_file=state)
    second = get_node_id(state_file=state)
    assert first == second


def test_node_id_reseeds_on_hostname_change(tmp_path: Path, monkeypatch):
    state = tmp_path / "node_id"
    monkeypatch.setattr("swarm.core.identity.machine_hostname", lambda: "host-a")
    first = get_node_id(state_file=state)
    monkeypatch.setattr("swarm.core.identity.machine_hostname", lambda: "host-b")
    second = get_node_id(state_file=state)
    assert first != second


def test_adapter_id_prefix_and_content_addressing():
    a = adapter_id("source-code-1", origin="ai")
    b = adapter_id("source-code-1", origin="ai")
    c = adapter_id("source-code-2", origin="ai")
    assert a == b != c
    assert a.startswith("ai-")
    human = adapter_id("source-code-1", origin="human")
    assert human.startswith("hw-") and human != a


def test_result_hash():
    assert result_hash(b"payload") == result_hash(b"payload")
    assert result_hash(b"payload") != result_hash(b"payload2")
