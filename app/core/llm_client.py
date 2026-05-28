"""
app/core/llm_client.py — MultiLLMClient class + _make_llm + _pricing_for + _add_usage
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import anthropic
from openai import OpenAI

from app.config import (
    _ANTHROPIC_API_KEY,
    _CLAUDE_MODEL,
    _DEEPSEEK_API_KEY,
    _DEEPSEEK_BASE_URL,
    _DEEPSEEK_MODEL,
    _DEFAULT_PROVIDER,
    _PROVIDER_CATALOG,
)


def _pricing_for(provider: str, model: str) -> dict:
    """Return {input, output} USD per 1M tokens for a given provider+model."""
    for m in _PROVIDER_CATALOG.get(provider, {}).get("models", []):
        if m["id"] == model:
            return {"input": m["input"], "output": m["output"]}
    return {"input": 0.0, "output": 0.0}


def _add_usage(session: Dict, usage: Dict, provider: str, model: str, stage: str = "L3") -> None:
    """Accumulate token usage + cost into session['usage']."""
    inp  = int(usage.get("input_tokens", 0))
    out  = int(usage.get("output_tokens", 0))
    px   = _pricing_for(provider, model)
    cost = (inp * px["input"] + out * px["output"]) / 1_000_000

    u = session.setdefault("usage", {
        "calls": 0, "input_tokens": 0, "output_tokens": 0,
        "cost_usd": 0.0, "provider": provider, "model": model, "breakdown": [],
    })
    u["calls"]         += 1
    u["input_tokens"]  += inp
    u["output_tokens"] += out
    u["cost_usd"]       = round(u["cost_usd"] + cost, 8)
    u["provider"]       = provider
    u["model"]          = model
    u["breakdown"].append({
        "stage":         stage,
        "ts":            datetime.now(timezone.utc).isoformat(),
        "input_tokens":  inp,
        "output_tokens": out,
        "cost_usd":      round(cost, 8),
    })


class MultiLLMClient:
    """Unified client: provider=claude uses anthropic SDK; others use openai SDK."""

    def __init__(
        self,
        provider: str = "claude",
        api_key: str = "",
        base_url: str = "",
        model: str = "",
    ):
        self.provider   = provider.lower()
        self.last_usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

        if not model:
            cat_models = _PROVIDER_CATALOG.get(self.provider, {}).get("models", [])
            model = cat_models[0]["id"] if cat_models else (_CLAUDE_MODEL if self.provider == "claude" else _DEEPSEEK_MODEL)
        self.model = model

        if self.provider == "claude":
            key = api_key or _ANTHROPIC_API_KEY
            if not key:
                raise ValueError("Anthropic API key not configured. Add ANTHROPIC_API_KEY to .env or enter it in Settings.")
            self._anthropic = anthropic.Anthropic(api_key=key)
            self._openai    = None
        else:
            if not api_key:
                if self.provider == "deepseek":
                    api_key = _DEEPSEEK_API_KEY
                elif self.provider == "ollama":
                    api_key = "ollama"
            if not base_url:
                base_url = _PROVIDER_CATALOG.get(self.provider, {}).get("base_url", "") or _DEEPSEEK_BASE_URL
            self._openai    = OpenAI(api_key=api_key or "placeholder", base_url=base_url)
            self._anthropic = None

    def complete(self, system: str, prompt: str, temperature: float = 0.1, max_tokens: int = 4096) -> str:
        """Run a completion and store token counts in self.last_usage."""
        if self.provider == "claude":
            resp = self._anthropic.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
            self.last_usage = {
                "input_tokens":  resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            }
            return resp.content[0].text.strip()
        else:
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": prompt})
            resp = self._openai.chat.completions.create(
                model=self.model,
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            u = resp.usage or {}
            self.last_usage = {
                "input_tokens":  getattr(u, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(u, "completion_tokens", 0) or 0,
            }
            return (resp.choices[0].message.content or "").strip()

    def complete_json(self, system: str, prompt: str) -> Any:
        """Run a completion, capture usage in self.last_usage, and parse JSON."""
        raw = self.complete(system, prompt)
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
        if m:
            raw = m.group(1).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            m2 = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
            if m2:
                return json.loads(m2.group(1))
            raise ValueError(f"LLM did not return valid JSON:\n{raw[:400]}")


def _make_llm(session: Dict) -> Optional["MultiLLMClient"]:
    """Build LLM client from session-level key overrides or global env.
    Returns None if llm_mode=='browser'."""
    cfg = session.get("api_config", {})
    if cfg.get("llm_mode") == "browser":
        return None
    provider = (cfg.get("provider") or _DEFAULT_PROVIDER).lower()
    key_required = {"openai", "groq", "mistral"}
    api_key = cfg.get("api_key", "")
    if provider in key_required and not api_key:
        raise ValueError(
            f"API key required for {provider}. Enter it in Settings → Session API Config."
        )
    return MultiLLMClient(
        provider=provider,
        api_key=api_key,
        base_url=cfg.get("base_url", ""),
        model=cfg.get("model", ""),
    )
