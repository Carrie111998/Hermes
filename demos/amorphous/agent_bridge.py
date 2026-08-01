"""Agent bridge for Amorphous Applications.

Provides two capabilities:
  chat(messages)     -> str          (invariant chat dock + workflow execution)
  json_task(prompt)  -> dict|list    (curator structured output)

Resolution order:
  1. OpenAI-compatible endpoint from env: NOUS_API_KEY -> OPENROUTER_API_KEY
     -> OPENAI_API_KEY (also loads ~/.hermes/.env if python-dotenv-style file
     exists, without extra deps).
  2. Offline deterministic fallback so the demo runs with zero credentials.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Optional

_PROVIDERS = [
    ("NOUS_API_KEY", "https://inference-api.nousresearch.com/v1", "Hermes-4.5-405B"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4.5"),
    ("OPENAI_API_KEY", "https://api.openai.com/v1", "gpt-5.2"),
]


def _load_hermes_env() -> None:
    env_file = Path.home() / ".hermes" / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass


class AgentBridge:
    def __init__(self) -> None:
        _load_hermes_env()
        self.api_key: Optional[str] = None
        self.base_url = ""
        self.model = ""
        for env, url, model in _PROVIDERS:
            key = os.getenv(env)
            if key:
                self.api_key, self.base_url, self.model = key, url, model
                break
        override_model = os.getenv("AMORPHOUS_MODEL")
        if override_model:
            self.model = override_model

    @property
    def live(self) -> bool:
        return bool(self.api_key)

    def describe(self) -> dict:
        return {"live": self.live, "model": self.model if self.live else "offline-heuristic",
                "base_url": self.base_url if self.live else None}

    # ---------- core LLM call ----------
    def _complete(self, messages: list[dict], max_tokens: int = 1200) -> str:
        req = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps({"model": self.model, "messages": messages,
                             "max_tokens": max_tokens}).encode(),
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"] or ""

    # ---------- public API ----------
    def chat(self, messages: list[dict], system: str = "") -> str:
        """Chat for the dock + workflows. messages: [{role, content}]."""
        if self.live:
            msgs = ([{"role": "system", "content": system}] if system else []) + messages
            try:
                return self._complete(msgs)
            except Exception as e:
                return f"(agent error: {e}) " + self._offline_chat(messages)
        return self._offline_chat(messages)

    def json_task(self, prompt: str, system: str = "") -> Any:
        """Structured task for the curator. Returns parsed JSON or None."""
        if self.live:
            try:
                out = self._complete(
                    ([{"role": "system", "content": system}] if system else [])
                    + [{"role": "user", "content": prompt}])
                m = re.search(r"```(?:json)?\s*(.*?)```", out, re.DOTALL)
                text = m.group(1) if m else out
                start = min([i for i in (text.find("["), text.find("{")) if i >= 0],
                            default=-1)
                if start >= 0:
                    return json.loads(text[start:])
            except Exception:
                return None
        return None

    # ---------- offline fallback ----------
    @staticmethod
    def _offline_chat(messages: list[dict]) -> str:
        last = next((m["content"] for m in reversed(messages)
                     if m.get("role") == "user"), "")
        low = last.lower()
        if "triage" in low or "incident" in low:
            return ("Triage plan (demo mode):\n"
                    "1. Likely cause: elevated 5xx correlates with the 14:20 deploy of api-gateway.\n"
                    "2. Blast radius: /v2/complete consumers, ~4% of traffic.\n"
                    "3. First mitigation: roll back api-gateway to previous release.\n"
                    "4. Page: on-call platform engineer (see runbook links).\n"
                    "5. Comms: 'We are investigating elevated error rates on the API; mitigation in progress.'")
        if "standup" in low or "summary" in low or "summarize" in low:
            return ("Standup summary (demo mode):\n"
                    "• Shipped: workflow shortcuts saw heavy use; error rate stable.\n"
                    "• At risk: INC-2411 still open on api-gateway.\n"
                    "• Needs decision: promote incident table to top row? The curator has a pending proposal.")
        if "report" in low or "service" in low:
            return ("Service health report (demo mode): latency within SLO, error budget 82% "
                    "remaining, last deploy 6h ago, no anomalies. Recommendation: close the "
                    "stale monitoring alert on the retry queue.")
        return ("(demo mode — set NOUS_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY for live "
                f"agent replies) You said: {last[:200]}")


BRIDGE = AgentBridge()
