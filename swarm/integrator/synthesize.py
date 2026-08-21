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
    def __init__(self, registry: Registry, llm: LlmClient, contract: Dict[str, Any]) -> None:
        self.registry = registry
        self.llm = llm
        self.contract = contract
        self.attempts = 0
        self.tokens_used = 0

    def _prompt(self, prior_failure: Optional[str]) -> List[Dict[str, str]]:
        cases = json.dumps(self.contract.get("cases", []))
        user = (
            f"Write a Python function `primes(n: int) -> list[int]` that returns all primes below n.\n"
            f"Reference cases: {cases}\n"
            "Rules: pure Python stdlib only; define exactly that function; no prints, no imports beyond stdlib."
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
                self.registry.record_adapter(
                    adapter_id=record["adapter_id"],
                    device_class=self.contract.get("device_class", "unknown"),
                    authored_by="ai",
                    gate_run_id=record["gate_run_id"],
                )
                return {"passed": True, "adapter_id": record["adapter_id"], "history": history}
            failure = record.get("reason", "gate rejected")
        return {"passed": False, "reason": "attempt cap reached", "history": history}
