"""The owner should never notice the swarm: the agent yields CPU to them,
and a watchdog restart can never produce two agents for one node."""

import subprocess
import sys

from swarm.agent import daemon


def test_second_agent_for_the_same_node_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "_state_dir", lambda: tmp_path)
    first = daemon.SingleInstance("agent-x")
    assert first.acquire(wait_s=0)
    code = (
        "import sys; from pathlib import Path; from swarm.agent import daemon; "
        f"daemon._state_dir = lambda: Path(r'{tmp_path}'); "
        "sys.exit(0 if daemon.SingleInstance('agent-x').acquire(wait_s=0) else 3)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 3
    other = subprocess.run(
        [sys.executable, "-c", code.replace("agent-x", "agent-y")]
    ).returncode
    assert other == 0, "a different node identity is a different lock"


def test_agent_lowers_its_own_priority_in_a_child():
    code = "from swarm.agent.daemon import lower_own_priority; import sys; sys.exit(0 if lower_own_priority() else 4)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
