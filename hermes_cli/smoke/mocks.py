"""Synthetic vendor and notification doubles used only by CS-13 smoke turns."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from hermes_cli.cost.kill_switch import KillSwitchTripped, PerTaskCapExceeded


@dataclass
class NoOpTelegramBucket:
    """Capture notification intent without reserving or sending a side effect."""

    sends: list[dict[str, Any]] = field(default_factory=list)

    def send(self, *, key: str, payload: Any) -> None:
        self.sends.append({"key": str(key), "payload": payload})


class MockLLMCall:
    """Deterministic callable that models the CS-13 vendor outcomes."""

    def __init__(self, scenario: str, *, task_id: str) -> None:
        self.scenario = str(scenario)
        self.task_id = str(task_id)
        self.calls: list[dict[str, Any]] = []
        self.provider_switches = 0

    def __call__(
        self,
        *,
        provider: str,
        model: str,
        prompt: str,
        max_tokens: int,
        attempt: int = 0,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        del prompt, max_tokens
        self.calls.append(
            {
                "provider": str(provider),
                "model": str(model),
                "attempt": int(attempt),
                "ts": time.time(),
            }
        )
        if self.scenario == "cap_hit":
            raise PerTaskCapExceeded(
                task_id=self.task_id,
                current_total=0.99,
                projected_total=1.01,
                cap=1.00,
            )
        if self.scenario == "kill_switch":
            raise KillSwitchTripped(task_id=self.task_id, reason="test")
        if self.scenario == "fallback_success" and attempt == 0:
            raise TimeoutError("mocked primary timeout after 1200ms")
        if self.scenario == "cascade_exhausted":
            raise TimeoutError(
                "mocked primary timeout after 1200ms"
                if attempt == 0
                else "mocked fallback timeout after 800ms"
            )
        self.provider_switches = max(0, int(attempt))
        return {
            "text": "synthetic smoke response",
            "output_tokens": 80 if attempt else 100,
            "latency_ms": 800 if attempt else 400,
        }


__all__ = ["MockLLMCall", "NoOpTelegramBucket"]
