"""swarm.integrator — M4. AI adapter synthesis + contract gate. Hub-side only.

Present now: the LLM client configuration (any OpenAI-compatible provider or
NeuralWatt, armed only when an API key is set in the environment). The
synthesis tiers and contract gate land at M4 — build order is sacred
(AGENTS.md). The adapter registry schema already exists in swarm.hub.registry.
"""

from .llm import LlmClient, LlmConfig, from_env, masked_key

__all__ = ["LlmClient", "LlmConfig", "from_env", "masked_key"]
