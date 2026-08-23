"""Adapter synthesis (M4, NeuralWatt-compatible).

The LLM writes a draft adapter. The draft means NOTHING until the contract
gate passes it. Budget is hard-capped per session — every attempt is recorded
with its verdict and its spend, and the loop stops when the cap is hit.

Fail closed: no gate pass, no promotion. Ever.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ..hub.registry import Registry
from .gate import run_gate
from .llm import LlmClient

MAX_ATTEMPTS = 5


class SynthesisLoop:
    def __init__(
        self,
        registry: Registry,
        llm: LlmClient,
        contract: Dict[str, Any],
        exemplar: Optional[str] = None,
    ) -> None:
        self.registry = registry
        self.llm = llm
        self.contract = contract
        self.attempts = 0
        self.tokens_used = 0
        # A validated sibling adapter, when one exists. Optional by design:
        # the swarm fails closed on genuinely novel silicon rather than
        # generating from nothing (measured correctness without a reference
        # is 7-17%).
        self.exemplar = exemplar

    def _prompt(self, prior_failure: Optional[str]) -> List[Dict[str, str]]:
        """Build the synthesis prompt FROM THE CONTRACT.

        Law 3 (discovery first) means synthesis is the last resort; when we do
        reach it, the contract is the only spec the model gets. Nothing here is
        hardcoded to a particular capability — swapping the contract swaps the
        task. The exemplar, when the registry has a validated sibling, is what
        moves measured correctness from 7-17% to 55-63%, so it is included
        whenever one exists.
        """
        cases = json.dumps(self.contract.get("cases", []))
        entrypoint = self.contract.get("entrypoint", "primes")
        signature = self.contract.get("signature") or f"{entrypoint}(...)"
        description = self.contract.get("description") or "implement the contract"
        device_class = self.contract.get("device_class", "unknown")

        user = (
            f"Write a Python function `{signature}`.\n"
            f"Capability: {self.contract.get('name', entrypoint)} "
            f"(device class: {device_class})\n"
            f"What it must do: {description}\n"
            f"Reference cases (input -> expected): {cases}\n"
            f"Rules: define exactly one function named `{entrypoint}`; "
            "pure Python stdlib only; no prints; no imports beyond stdlib."
        )
        if self.exemplar:
            user += (
                "\n\nA validated adapter for similar hardware is provided as a "
                "reference. Follow its structure and error handling; adapt the "
                "computation to this contract:\n"
                f"```python\n{self.exemplar}\n```"
            )
        if prior_failure:
            user += f"\n\nThe previous draft failed with: {prior_failure}\nFix it."
        return [
            {"role": "system", "content": "You write minimal, correct, pure-python adapter code."},
            {"role": "user", "content": user},
        ]

    @staticmethod
    def _extract_code(text: str) -> Optional[str]:
        m = re.search(r"```python\n(.*?)```", text, re.S)
        if m:
            return m.group(1).strip()
        m = re.search(r"```\n?(.*?)```", text, re.S)
        if m:
            return m.group(1).strip()
        return text.strip() if "def " in text else None

    def run(self) -> Dict[str, Any]:
        history: List[Dict[str, Any]] = []
        failure = None
        if not self.llm.config.armed:
            return {"passed": False, "reason": "no LLM key configured — synthesis disabled", "history": []}
        while self.attempts < MAX_ATTEMPTS:
            self.attempts += 1
            text = self.llm.chat(self._prompt(failure))
            if text is None:
                failure = "LLM returned nothing"
                history.append({"attempt": self.attempts, "reason": failure})
                continue
            source = self._extract_code(text)
            if source is None:
                failure = "no code found in draft"
                history.append({"attempt": self.attempts, "reason": failure})
                continue
            record = run_gate(source, self.contract)
            history.append(record)
            self.registry.record_gate_run(record)
            if record.get("passed"):
                # The source IS the adapter. Persisting only the hash would
                # leave a "proven adapter" pointing at nothing, and nothing
                # downstream could ever load it.
                self.registry.record_adapter(
                    adapter_id=record["adapter_id"],
                    device_class=self.contract.get("device_class", "unknown"),
                    authored_by="ai",
                    gate_run_id=record["gate_run_id"],
                    probe_evidence_hash=self.contract.get("probe_evidence_hash", ""),
                    source=source,
                )
                return {"passed": True, "adapter_id": record["adapter_id"], "history": history}
            failure = record.get("reason", "gate rejected")
        return {"passed": False, "reason": "attempt cap reached", "history": history}
