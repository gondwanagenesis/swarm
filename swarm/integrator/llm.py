"""LLM access for the integrator (M4 groundwork). Hub-side only.

Provider-agnostic: any OpenAI-compatible /v1/chat/completions endpoint —
OpenAI, Anthropic-via-proxy, OpenRouter, local vLLM, or NeuralWatt — works by
setting env vars. The integration is ARMED ONLY when an API key is present;
without one the integrator cleanly reports "no key configured" and nothing
calls the network. Keys are never logged or returned in full.

Configuration (all optional until you want AI features):
    SWARM_LLM_API_KEY   — the key (any provider)
    SWARM_LLM_BASE_URL  — default https://api.openai.com/v1
    SWARM_LLM_MODEL     — provider's model id
    (NeuralWatt convenience: SWARM_NEURALWATT_API_KEY + SWARM_NEURALWATT_BASE_URL
     are honored first when set)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 60.0


class LlmConfig:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        provider: str = "openai-compatible",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.provider = provider

    @property
    def armed(self) -> bool:
        return bool(self.api_key)

    def status(self) -> Dict[str, Any]:
        return {
            "armed": self.armed,
            "provider": self.provider if self.armed else None,
            "base_url": self.base_url if self.armed else None,
            "model": self.model if self.armed else None,
            "key_hint": masked_key(self.api_key),
        }


def masked_key(key: str) -> Optional[str]:
    if not key:
        return None
    if len(key) <= 8:
        return "****"
    return key[:4] + "…" + key[-4:]


def from_env(env: Optional[Dict[str, str]] = None) -> LlmConfig:
    env = env if env is not None else dict(os.environ)
    neuralwatt_key = env.get("SWARM_NEURALWATT_API_KEY", "")
    if neuralwatt_key:
        return LlmConfig(
            api_key=neuralwatt_key,
            base_url=env.get("SWARM_NEURALWATT_BASE_URL", "https://api.neuralwatt.com/v1"),
            model=env.get("SWARM_LLM_MODEL", DEFAULT_MODEL),
            provider="neuralwatt",
        )
    key = env.get("SWARM_LLM_API_KEY", "")
    return LlmConfig(
        api_key=key,
        base_url=env.get("SWARM_LLM_BASE_URL", DEFAULT_BASE_URL),
        model=env.get("SWARM_LLM_MODEL", DEFAULT_MODEL),
        provider="openai-compatible" if key else "",
    )


class LlmClient:
    def __init__(self, config: LlmConfig, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.config = config
        self.timeout = timeout

    def chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 2048,
        temperature: float = 0.2,
    ) -> Optional[str]:
        """One chat completion via the provider's OpenAI-compatible API.
        Returns assistant text, or None on any failure. Never raises."""
        if not self.config.armed:
            return None
        url = self.config.base_url + "/chat/completions"
        body = json.dumps(
            {
                "model": self.config.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.config.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, OSError):
            return None
        choices = data.get("choices")
        if not choices:
            return None
        message = choices[0].get("message") or {}
        content = message.get("content")
        return content if isinstance(content, str) else None
