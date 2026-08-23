"""The brain router. Three lanes, honest about which one a thought came from:

  lanes:
    0. abliterated local model  (default; no network; the soul of the swarm)
    1. constitutional local     (fallback if 0 unavailable)
    2. frontier API             (NeuralWatt/OpenAI — gated, logged, killable)

Frontier is OFF by default (enabled=False) unless an admin turns it on, and a
kill switch can hard-disable it at runtime from the dashboard. Sensitivity
(0.0 - 1.0) decides how hard a task has to look before the router escalates:
low sensitivity = abliterated stays in charge for almost everything.
Every routing decision is recorded with its lane and why.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from typing import Any, Dict, Optional

from ..hub.registry import Registry
from .llm import LlmClient, LlmConfig

LANE_ABLITERATED = 0
LANE_CONSTITUTIONAL = 1
LANE_FRONTIER = 2

LANE_NAMES = {0: "abliterated-local", 1: "constitutional-local", 2: "frontier-api"}

DEFAULT_SENSITIVITY = 0.7
DEFAULT_LOCAL_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LOCAL_MODEL = "huihui-ai/Huihui-Qwen3-14B-abliterated"
DEFAULT_CONSTITUTIONAL_MODEL = "Qwen/Qwen3-14B"


class BrainRouter:
    def __init__(
        self,
        registry: Registry,
        frontier: LlmConfig,
        local: Optional[LlmConfig] = None,
        constitutional: Optional[LlmConfig] = None,
        enabled: bool = False,
        sensitivity: float = DEFAULT_SENSITIVITY,
    ) -> None:
        self.registry = registry
        self.frontier = frontier
        self.local = local
        self.constitutional = constitutional
        state = self._load_state()
        self.enabled = bool(state.get("enabled", enabled))
        self.kill_switch = bool(state.get("kill_switch", False))
        self.sensitivity = float(state.get("sensitivity", sensitivity))

    def _load_state(self) -> Dict[str, Any]:
        row = (
            self.registry._conn.execute("SELECT value FROM admin_config WHERE key='brain'").fetchone()
            if self._has_admin_table()
            else None
        )
        if row:
            import json

            try:
                return json.loads(row["value"])
            except ValueError:
                return {}
        return {}

    def _has_admin_table(self) -> bool:
        row = self.registry._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='admin_config'"
        ).fetchone()
        if row is None:
            self.registry._conn.execute(
                "CREATE TABLE IF NOT EXISTS admin_config (key TEXT PRIMARY KEY, value TEXT)"
            )
            self.registry._conn.commit()
            return False
        return True

    def _save_state(self) -> None:
        import json

        self._has_admin_table()
        self.registry._conn.execute(
            "INSERT OR REPLACE INTO admin_config (key, value) VALUES ('brain', ?)",
            (
                json.dumps(
                    {
                        "enabled": self.enabled,
                        "kill_switch": self.kill_switch,
                        "sensitivity": self.sensitivity,
                    }
                ),
            ),
        )
        self.registry._conn.commit()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self._save_state()

    def set_kill_switch(self, on: bool) -> None:
        self.kill_switch = on
        self._save_state()

    def set_sensitivity(self, value: float) -> None:
        self.sensitivity = max(0.0, min(1.0, value))
        self._save_state()

    @staticmethod
    def estimate_difficulty(
        contract: Dict[str, Any],
        prior_failures: int = 0,
        has_exemplar: bool = False,
    ) -> float:
        """Estimate how hard a synthesis task is, from the contract itself.

        This is a HEURISTIC, not a measurement, and it is labelled as such
        wherever it surfaces — the swarm does not dress an estimate up as a
        benchmark. It exists because the alternative in place was a hardcoded
        constant, which meant every request escalated to the frontier lane
        regardless of difficulty.

        The signals are the ones with evidence behind them:
        - no exemplar is the single largest correctness predictor
          (7-17% without a reference vs 55-63% with one), so it dominates
        - each gate rejection is direct evidence this task is harder than
          the last estimate assumed
        - elementwise/numeric comparison is a stricter bar than a count
        """
        score = 0.35
        if not has_exemplar:
            score += 0.30
        compare = str(contract.get("compare", "length")).lower()
        score += {"length": 0.0, "exact": 0.08, "allclose": 0.15}.get(compare, 0.08)
        n_cases = len(contract.get("cases") or [])
        if n_cases >= 8:
            score += 0.08
        elif n_cases >= 4:
            score += 0.04
        # Each failed attempt is measured evidence, not a guess.
        score += min(0.25, 0.08 * max(0, prior_failures))
        return max(0.0, min(1.0, score))

    def route(
        self,
        task_difficulty: float,
        has_local: bool = True,
        difficulty_source: str = "caller",
    ) -> Dict[str, Any]:
        """Never returns lane 2 unless: enabled, not kill-switched, escalation
        required by difficulty, and the frontier config is armed.

        `difficulty_source` is recorded with the decision so an auditor can
        tell an estimated difficulty from one a caller asserted.
        """
        difficulty = max(0.0, min(1.0, task_difficulty))
        if self.enabled and not self.kill_switch and difficulty > self.sensitivity and self.frontier.armed:
            lane = LANE_FRONTIER
        elif has_local and (self.local is not None):
            lane = LANE_ABLITERATED
        else:
            lane = LANE_CONSTITUTIONAL
        decision = {
            "lane": lane,
            "lane_name": LANE_NAMES[lane],
            "difficulty": difficulty,
            "difficulty_source": difficulty_source,
            "sensitivity": self.sensitivity,
            "frontier_enabled": self.enabled,
            "kill_switch": self.kill_switch,
        }
        self._log_route(decision)
        return decision

    def _log_route(self, decision: Dict[str, Any]) -> None:
        self._has_admin_table()
        self.registry._conn.execute(
            "CREATE TABLE IF NOT EXISTS brain_routes (id INTEGER PRIMARY KEY AUTOINCREMENT, lane INTEGER, lane_name TEXT, difficulty REAL, sensitivity REAL, frontier_enabled INTEGER, kill_switch INTEGER, at REAL)"
        )
        # Older hubs have this table without difficulty_source. Add it in
        # place so an existing routing ledger keeps its history.
        cols = {
            row[1]
            for row in self.registry._conn.execute("PRAGMA table_info(brain_routes)")
        }
        if "difficulty_source" not in cols:
            with contextlib.suppress(sqlite3.OperationalError):
                self.registry._conn.execute(
                    "ALTER TABLE brain_routes ADD COLUMN difficulty_source TEXT"
                )
        self.registry._conn.execute(
            "INSERT INTO brain_routes (lane, lane_name, difficulty, difficulty_source,"
            " sensitivity, frontier_enabled, kill_switch, at) VALUES (?,?,?,?,?,?,?,?)",
            (
                decision["lane"],
                decision["lane_name"],
                decision["difficulty"],
                decision.get("difficulty_source", "caller"),
                decision["sensitivity"],
                int(decision["frontier_enabled"]),
                int(decision["kill_switch"]),
                time.time(),
            ),
        )
        self.registry._conn.commit()

    def client_for(self, lane: int) -> Optional[LlmClient]:
        cfg = None
        if lane == LANE_FRONTIER:
            cfg = self.frontier
        elif lane == LANE_CONSTITUTIONAL:
            cfg = self.constitutional
        if lane == LANE_ABLITERATED:
            cfg = self.local
        if cfg is None or not cfg.armed:
            return None
        return LlmClient(cfg)

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "kill_switch": self.kill_switch,
            "sensitivity": self.sensitivity,
            "frontier_armed": self.frontier.armed,
            "local_armed": bool(self.local and self.local.armed),
        }


def local_brain_config_from_env(env: Optional[Dict[str, str]] = None) -> Optional[LlmConfig]:
    """Local abliterated brain config: armed only when SWARM_LOCAL_BRAIN_URL
    is set (we never assume a local server exists). Local servers ignore
    auth, but a placeholder key keeps http clients happy."""
    import os

    env = env if env is not None else dict(os.environ)
    url = env.get("SWARM_LOCAL_BRAIN_URL")
    if not url:
        return None
    return LlmConfig(
        api_key=env.get("SWARM_LOCAL_BRAIN_KEY", "local"),
        base_url=url,
        model=env.get("SWARM_LOCAL_BRAIN_MODEL", DEFAULT_LOCAL_MODEL),
        provider="abliterated-local",
    )
