"""Typed failure raised after repeated provider stream stalls."""

from __future__ import annotations

from agent.provider_health_probe import ProbeOutcome


class ProviderStalledError(TimeoutError):
    """A repeated provider stall with sanitized probe evidence."""

    provider: str
    model: str
    silent_seconds: float
    attempt: int
    probe: ProbeOutcome

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        silent_seconds: float,
        attempt: int,
        probe: ProbeOutcome,
    ) -> None:
        self.provider = provider
        self.model = model
        self.silent_seconds = float(silent_seconds)
        self.attempt = int(attempt)
        self.probe = probe
        super().__init__(
            "provider stalled with no response chunks for "
            f"{int(self.silent_seconds)}s on attempt {self.attempt}; "
            f"probe={self.probe.status}"
        )
