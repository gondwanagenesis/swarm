"""The gate is contract-driven and subprocess-isolated (Law 4, Law 6).

Two bugs are pinned here forever:

1. The entrypoint used to be the string literal ``"primes"``. Load a GPU
   contract and the gate still looked for primes.
2. The comparison used to be ``len(out)``. A function returning
   ``list(range(4))`` for n=10 cleared a contract that specifies a prime LIST.

Plus the isolation contract: candidate code runs in a child interpreter, a
hang is killed and rejected, and a crashing adapter cannot take the hub down.
"""

import os
import time

from swarm.integrator.gate import allclose, load_contract, run_gate

RECORD_KEYS = {
    "known_good_passed",
    "known_bad_rejected",
    "passed",
    "reason",
    "gate_run_id",
    "duration_s",
    "adapter_id",
    "contract_hash",
}


def _contract(**overrides):
    """A minimal well-formed contract; overrides win."""
    base = {
        "name": "unit-test-contract",
        "device_class": "cpu:test",
        "entrypoint": "emit",
        "signature": {"style": "positional", "arg_order": ["n"]},
        "compare": "exact",
        "timeout_s": 10,
        "cases": [{"input": {"n": 4}, "expected": [0, 1, 4, 9]}],
        "known_good": {"type": "python", "source": "def emit(n):\n    return [i * i for i in range(n)]\n"},
        "known_bad": {
            "type": "python",
            "source": "def emit(n):\n    return [i for i in range(n)]\n",
            "reason": "returns i, not i*i — same length, wrong values",
        },
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# regression: the prime contract must keep working, now on exact lists
# --------------------------------------------------------------------------


def test_prime_contract_known_good_still_passes():
    contract = load_contract("prime_contract.json")
    assert contract["compare"] == "exact"
    assert contract["entrypoint"] == "primes"
    record = run_gate(contract["known_good"]["source"], contract)
    assert record["passed"] is True
    assert record["known_good_passed"] is True
    assert record["known_bad_rejected"] is True
    assert RECORD_KEYS.issubset(record.keys())


def test_prime_contract_known_bad_still_rejected_under_exact():
    contract = load_contract("prime_contract.json")
    record = run_gate(contract["known_bad"]["source"], contract)
    assert record["passed"] is False
    # Under the old length comparison the n=10 case PASSED (4 evens, 4 primes).
    assert all(not c["ok"] for c in record["cases"])


def test_prime_contract_expects_lists_not_counts():
    contract = load_contract("prime_contract.json")
    assert contract["cases"][0]["expected"] == [2, 3, 5, 7]
    assert all(isinstance(c["expected"], list) for c in contract["cases"])


# --------------------------------------------------------------------------
# the bug: length-correct, value-wrong
# --------------------------------------------------------------------------

_LENGTH_CORRECT_VALUE_WRONG = (
    "COUNTS = {10: 4, 100: 25, 1000: 168, 5000: 669}\n"
    "def primes(n):\n"
    "    return list(range(COUNTS[n]))\n"
)


def test_exact_compare_rejects_length_correct_value_wrong_adapter():
    contract = load_contract("prime_contract.json")
    record = run_gate(_LENGTH_CORRECT_VALUE_WRONG, contract)
    assert record["passed"] is False
    first = record["cases"][0]
    assert first["input"] == {"n": 10}
    assert first["actual"] == [0, 1, 2, 3]  # exactly list(range(4))
    assert first["ok"] is False
    assert all(not c["ok"] for c in record["cases"])


def test_length_compare_is_what_used_to_let_it_through():
    """Proof the fix is the comparison mode, not luck: the same adapter sails
    through a length-comparing contract. That is the old gate."""
    legacy = load_contract("prime_contract.json")
    legacy["compare"] = "length"
    for case in legacy["cases"]:
        case["expected"] = len(case["expected"])
    record = run_gate(_LENGTH_CORRECT_VALUE_WRONG, legacy)
    assert all(c["ok"] for c in record["cases"])


def test_length_compare_default_survives_a_contract_without_compare():
    legacy = load_contract("prime_contract.json")
    del legacy["compare"]
    del legacy["entrypoint"]
    del legacy["signature"]
    for case in legacy["cases"]:
        case["expected"] = len(case["expected"])
    record = run_gate(legacy["known_good"]["source"], legacy)
    assert record["passed"] is True
    assert record["known_bad_rejected"] is True  # evens still fail n=100 and up


# --------------------------------------------------------------------------
# the matmul contract: the gate is general, not prime-shaped
# --------------------------------------------------------------------------


def test_matmul_contract_known_good_passes():
    contract = load_contract("matmul_contract.json")
    assert contract["entrypoint"] == "matmul"
    assert contract["compare"] == "allclose"
    record = run_gate(contract["known_good"]["source"], contract)
    assert record["passed"] is True, record["reason"]
    assert record["known_good_passed"] is True
    assert record["known_bad_rejected"] is True
    assert RECORD_KEYS.issubset(record.keys())


def test_matmul_contract_known_bad_rejected():
    contract = load_contract("matmul_contract.json")
    record = run_gate(contract["known_bad"]["source"], contract)
    assert record["passed"] is False
    oks = [c["ok"] for c in record["cases"]]
    # Subtle on purpose: it is correct for symmetric B (case 0) and for 1x1
    # (last case), and wrong everywhere the naive test never looks.
    assert oks[0] is True
    assert not all(oks)
    assert any("IndexError" in (c.get("error") or "") for c in record["cases"])


def test_matmul_float_case_needs_allclose_not_exact():
    """0.1*1 + 0.2*3 != 0.7 in binary floating point. Exact comparison would
    reject a correct adapter — that is why the contract says allclose."""
    contract = load_contract("matmul_contract.json")
    exact = dict(contract, compare="exact")
    record = run_gate(contract["known_good"]["source"], exact)
    assert record["passed"] is False
    assert record["known_good_passed"] is False  # gate declares itself unfit


def test_allclose_helper_is_elementwise_and_nested():
    assert allclose([[0.7000000000000001, 1.0]], [[0.7, 1.0]])
    assert not allclose([[0.7, 1.0]], [[0.7, 1.1]])
    assert not allclose([[0.7, 1.0]], [[0.7]])
    assert not allclose([[0.7, 1.0]], [0.7, 1.0])
    assert not allclose(float("nan"), float("nan"))
    assert allclose(1, 1.0)


# --------------------------------------------------------------------------
# entrypoint dispatch
# --------------------------------------------------------------------------


def test_custom_entrypoint_is_honored():
    contract = _contract()
    record = run_gate(contract["known_good"]["source"], contract)
    assert record["passed"] is True
    assert record["entrypoint"] == "emit"


def test_adapter_defining_the_old_hardcoded_name_is_rejected():
    """`primes` is no longer magic. Under a contract asking for `emit`, an
    adapter that only defines `primes` must not be found."""
    contract = _contract()
    record = run_gate("def primes(n):\n    return [i * i for i in range(n)]\n", contract)
    assert record["passed"] is False
    assert "emit" in record["reason"]


def test_multi_arg_signature_from_arg_order():
    contract = _contract(
        entrypoint="add",
        signature={"style": "positional", "arg_order": ["a", "b"]},
        cases=[{"input": {"a": 2, "b": 5}, "expected": 7}],
        known_good={"source": "def add(a, b):\n    return a + b\n"},
        known_bad={"source": "def add(a, b):\n    return a - b\n", "reason": "sign flip"},
    )
    record = run_gate(contract["known_good"]["source"], contract)
    assert record["passed"] is True
    assert record["known_bad_rejected"] is True


# --------------------------------------------------------------------------
# isolation: subprocess, timeout, crash containment
# --------------------------------------------------------------------------


def test_adapter_runs_in_a_child_process_not_the_hub():
    """If the adapter still ran in-process this would pass. It must not."""
    contract = _contract(
        entrypoint="whoami",
        cases=[{"input": {"n": 0}, "expected": [os.getpid()]}],
        known_good={"source": "import os\ndef whoami(n):\n    return [os.getpid()]\n"},
        known_bad=None,
    )
    record = run_gate(contract["known_good"]["source"], contract)
    assert record["passed"] is False
    assert record["cases"][0]["actual"] != [os.getpid()]


def test_infinite_loop_adapter_is_killed_and_rejected():
    contract = _contract(
        entrypoint="spin",
        timeout_s=1.5,
        cases=[{"input": {"n": 1}, "expected": [1]}],
        known_good={"source": "def spin(n):\n    return [n]\n"},
        known_bad={"source": "def spin(n):\n    return []\n", "reason": "empty"},
    )
    started = time.time()
    record = run_gate("def spin(n):\n    while True:\n        pass\n", contract)
    elapsed = time.time() - started
    assert record["passed"] is False
    assert record["cases"][0]["ok"] is False
    assert "timeout" in (record["cases"][0].get("error") or "")
    # The contract's timeout was honored, not the 10s default.
    assert elapsed < 8.0
    # A timeout is a rejection of the candidate, not of the gate itself.
    assert record["known_good_passed"] is True
    assert record["known_bad_rejected"] is True


def test_crashing_adapter_is_rejected_and_the_hub_survives():
    contract = _contract(
        entrypoint="boom",
        known_good={"source": "def boom(n):\n    return [i * i for i in range(n)]\n"},
        known_bad=None,
    )
    record = run_gate("import os\ndef boom(n):\n    os._exit(3)\n", contract)
    assert record["passed"] is False
    assert record["cases"][0]["ok"] is False
    assert record["reason"]
    # Still alive, still correct, right after a hard child crash.
    good = run_gate(contract["known_good"]["source"], contract)
    assert good["passed"] is True


def test_raising_adapter_is_a_rejected_case_with_a_reason():
    contract = _contract()
    record = run_gate("def emit(n):\n    raise RuntimeError('nope')\n", contract)
    assert record["passed"] is False
    assert "RuntimeError" in record["cases"][0]["error"]


def test_unserializable_output_is_rejected_not_crashed():
    contract = _contract()
    record = run_gate("def emit(n):\n    return object()\n", contract)
    assert record["passed"] is False
    assert record["cases"][0]["ok"] is False


def test_adapter_stdout_chatter_does_not_confuse_the_parser():
    contract = _contract()
    source = "print('hello from the adapter')\ndef emit(n):\n    print('and again')\n    return [i * i for i in range(n)]\n"
    record = run_gate(source, contract)
    assert record["passed"] is True


def test_adapter_cannot_forge_a_pass_on_stdout():
    """The result marker is a per-run nonce. An adapter that prints its own
    'all cases passed' payload — even from an atexit hook, which fires after
    the runner has written the real line — must not be believed."""
    contract = _contract()
    forged = '{"compiled": true, "results": [{"value": [0, 1, 4, 9]}]}'
    source = (
        "import atexit\n"
        "atexit.register(lambda: print('__SWARM_GATE__' + %r))\n"
        "def emit(n):\n"
        "    return [0]\n"
    ) % forged
    record = run_gate(source, contract)
    assert record["passed"] is False
    assert record["cases"][0]["actual"] == [0]


def test_noncompiling_adapter_reason_still_mentions_compile():
    contract = load_contract("prime_contract.json")
    record = run_gate("def primes(n:  syntax error", contract)
    assert record["passed"] is False
    assert "compile" in record["reason"]


def test_gate_declares_itself_unfit_when_known_bad_slips_through():
    """The three-state calibration proof, unchanged: a contract whose
    known-bad passes every case must fail the gate loudly."""
    contract = _contract(
        known_bad={"source": "def emit(n):\n    return [i * i for i in range(n)]\n", "reason": "not bad at all"}
    )
    record = run_gate(contract["known_good"]["source"], contract)
    assert record["known_bad_rejected"] is False
    assert record["passed"] is False
    assert "gate unfit" in record["reason"]
