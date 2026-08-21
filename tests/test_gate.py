"""The gate proves itself before anything else gets to use it (Law 4)."""

from swarm.hub.registry import Registry
from swarm.integrator.gate import load_contract, run_gate
from swarm.integrator.synthesize import SynthesisLoop


def test_gate_accepts_hand_written_good():
    contract = load_contract("prime_contract.json")
    record = run_gate(contract["known_good"]["source"], contract)
    assert record["passed"] is True
    assert record["known_good_passed"] is True
    assert record["known_bad_rejected"] is True
    assert record["gate_run_id"].startswith("gate-")


def test_gate_rejects_known_bad():
    contract = load_contract("prime_contract.json")
    record = run_gate(contract["known_bad"]["source"], contract)
    assert record["passed"] is False
    assert any(not c["ok"] for c in record["cases"])


def test_gate_rejects_noncompiling_adapter():
    contract = load_contract("prime_contract.json")
    record = run_gate("def primes(n:  syntax error", contract)
    assert record["passed"] is False
    assert "compile" in record["reason"]


def test_gate_rejects_partially_wrong():
    contract = load_contract("prime_contract.json")
    sneaky = (
        "def primes(n):\n"
        "    def is_p(k):\n"
        "        if k < 2: return False\n"
        "        if k == 2: return True\n"
        "        if k % 2 == 0: return False\n"
        "        d = 3\n"
        "        while d*d <= k:\n"
        "            if k % d == 0: return False\n"
        "            d += 2\n"
        "        return True\n"
        "    return [p for p in range(2, n) if is_p(p)][:-1]\n"
    )
    record = run_gate(sneaky, contract)
    assert record["passed"] is False


class _FakeArmedLlm:
    class config:
        armed = True

    def __init__(self, good_after):
        self.n = 0
        self.good_after = good_after

    def chat(self, messages):
        self.n += 1
        if self.n < self.good_after:
            return "```python\ndef primes(n):\n    return list(range(n))\n```"
        return (
            "```python\ndef primes(n):\n"
            "    def is_p(k):\n"
            "        if k < 2: return False\n"
            "        if k == 2: return True\n"
            "        if k % 2 == 0: return False\n"
            "        d = 3\n"
            "        while d*d <= k:\n"
            "            if k % d == 0: return False\n"
            "            d += 2\n"
            "        return True\n"
            "    return [p for p in range(2, n) if is_p(p)]\n```"
        )


class _UnarmedLlm:
    class config:
        armed = False

    def chat(self, messages):
        return None


def test_synthesis_unarmed_fails_closed():
    reg = Registry(":memory:")
    contract = load_contract("prime_contract.json")
    out = SynthesisLoop(reg, _UnarmedLlm(), contract).run()
    assert out["passed"] is False
    assert "no LLM key" in out["reason"]


def test_synthesis_iterates_until_gate_passes():
    reg = Registry(":memory:")
    contract = load_contract("prime_contract.json")
    out = SynthesisLoop(reg, _FakeArmedLlm(good_after=3), contract).run()
    assert out["passed"] is True
    assert len(out["history"]) == 3
    runs = reg.list_gate_runs()
    assert len(runs) == 3
    assert any(r["passed"] for r in runs)
    adapters = reg.list_adapters()
    assert adapters and adapters[0]["gate_run_id"]


def test_synthesis_attempt_cap():
    reg = Registry(":memory:")
    contract = load_contract("prime_contract.json")
    out = SynthesisLoop(reg, _FakeArmedLlm(good_after=99), contract).run()
    assert out["passed"] is False
    assert len(out["history"]) == 5
