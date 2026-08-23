"""End-to-end proof that a promoted adapter survives as executable code.

Before this, `record_adapter` had nowhere to put the source: a "proven
adapter" was a content hash pointing at nothing, and no code path could ever
load one. These tests assert the whole chain instead of any single link —
synthesis produces source, the gate passes it, the registry persists it, and
the source that comes back out is byte-identical and still runs.

That last clause is the point. A stored blob that no longer executes would
satisfy a round-trip assertion while failing the thing the store exists for.
"""

from __future__ import annotations

import json

import pytest

from swarm.hub.registry import Registry
from swarm.integrator.gate import load_contract, run_gate
from swarm.integrator.synthesize import SynthesisLoop

GOOD_PRIMES = (
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
    "    return [p for p in range(2, n) if is_p(p)]\n"
)


class _ScriptedLlm:
    """Returns a fixed draft. Synthesis quality is not what is under test."""

    class config:
        armed = True

    def __init__(self, source: str = GOOD_PRIMES) -> None:
        self.source = source
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return "```python\n" + self.source + "```"


@pytest.fixture
def registry(tmp_path):
    return Registry(str(tmp_path / "hub.db"))


def test_promoted_adapter_source_survives(registry):
    """The bug this file exists for: promotion must not discard the code."""
    contract = load_contract("prime_contract.json")
    loop = SynthesisLoop(registry, _ScriptedLlm(), contract)
    result = loop.run()

    assert result["passed"] is True, result.get("reason")
    adapter_id = result["adapter_id"]

    stored = registry.get_adapter_source(adapter_id)
    assert stored is not None, "promoted adapter has no retrievable source"
    # Synthesis strips the markdown fence, so compare the code itself rather
    # than surrounding whitespace. What must survive is every byte that runs.
    assert stored.strip() == GOOD_PRIMES.strip()


def test_retrieved_source_still_executes(registry):
    """A blob that no longer runs would pass a round-trip test and be useless."""
    contract = load_contract("prime_contract.json")
    loop = SynthesisLoop(registry, _ScriptedLlm(), contract)
    adapter_id = loop.run()["adapter_id"]

    source = registry.get_adapter_source(adapter_id)
    namespace: dict = {}
    exec(compile(source, "<stored-adapter>", "exec"), namespace)
    fn = namespace["primes"]
    assert fn(10) == [2, 3, 5, 7]
    assert len(fn(100)) == 25


def test_retrieved_source_repasses_the_gate(registry):
    """Round-tripping through the store must not change the verdict."""
    contract = load_contract("prime_contract.json")
    loop = SynthesisLoop(registry, _ScriptedLlm(), contract)
    adapter_id = loop.run()["adapter_id"]

    source = registry.get_adapter_source(adapter_id)
    assert run_gate(source, contract)["passed"] is True


def test_adapter_row_carries_provenance(registry):
    """Law: a human must be able to audit why the machine trusted this code."""
    contract = load_contract("prime_contract.json")
    loop = SynthesisLoop(registry, _ScriptedLlm(), contract)
    adapter_id = loop.run()["adapter_id"]

    row = next(r for r in registry.list_adapters() if r["adapter_id"] == adapter_id)
    assert row["authored_by"] == "ai"
    assert row["gate_run_id"], "no gate run recorded for a promoted adapter"
    assert row["source_hash"], "no source hash recorded"


def test_rejected_adapter_is_never_promoted(registry):
    """Fail closed: a draft that loses to the gate leaves no adapter behind."""
    before = len(registry.list_adapters())
    contract = load_contract("prime_contract.json")
    # Length-correct for n=10 but the values are wrong — exactly the class of
    # bug a length-only comparison used to wave through.
    loop = SynthesisLoop(registry, _ScriptedLlm("def primes(n):\n    return list(range(4))\n"), contract)
    result = loop.run()

    assert result["passed"] is False
    assert len(registry.list_adapters()) == before, "rejected draft was promoted anyway"


def test_synthesis_prompt_is_contract_driven(registry):
    """Swapping the contract must swap the task — nothing hardcoded to primes."""
    contract = json.loads(json.dumps(load_contract("matmul_contract.json")))
    loop = SynthesisLoop(registry, _ScriptedLlm(), contract)
    prompt = loop._prompt(None)
    text = " ".join(m["content"] for m in prompt)

    assert "matmul" in text, "matmul contract did not reach the prompt"
    assert "primes(n: int)" not in text, "prime-specific text leaked into a matmul prompt"


def test_exemplar_is_included_when_present(registry):
    """The exemplar is the 7-17% -> 55-63% lever; it must reach the model."""
    contract = load_contract("prime_contract.json")
    loop = SynthesisLoop(registry, _ScriptedLlm(), contract, exemplar=GOOD_PRIMES)
    text = " ".join(m["content"] for m in loop._prompt(None))
    assert GOOD_PRIMES.strip() in text
