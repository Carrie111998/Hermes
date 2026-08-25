"""Persistent provider rotation cooldown state.

This module intentionally keeps the first version small: providers that expose
quota APIs can add proactive probes later, while every provider benefits from
reactive cooldown after capacity errors.
"""

from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home
from utils import atomic_json_write

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

STATE_VERSION = 1
STATE_FILE = "provider_rotation_state.json"
DEFAULT_COOLDOWN_SECONDS = 6 * 60 * 60
_MIN_DURABLE_RESET_SECONDS = 60.0


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _normalize_base_url(base_url: str | None) -> str:
    return (base_url or "").strip().rstrip("/").lower()


def provider_key(
    provider: str | None,
    model: str | None = None,
    base_url: str | None = None,
) -> str:
    """Return stable key for provider/model/base_url rotation state."""
    provider_part = _norm(provider)
    model_part = (model or "").strip()
    base_part = _normalize_base_url(base_url)
    key = f"{provider_part}:{model_part}" if model_part else provider_part
    return f"{key}@{base_part}" if base_part else key


def state_path() -> Path:
    return get_hermes_home() / STATE_FILE


def _state_lock_path() -> Path:
    path = state_path()
    return path.with_name(path.name + ".lock")


def _atomic_save_state(*, version: int, unavailable: dict[str, dict[str, Any]]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(
        path,
        {"version": version, "unavailable": unavailable},
        sort_keys=True,
    )


def _bucket_has_durable_exhaustion(bucket: Any) -> bool:
    if bucket is None:
        return False
    remaining = getattr(bucket, "remaining", None)
    reset_seconds = getattr(bucket, "remaining_seconds_now", None)
    if reset_seconds is None:
        reset_seconds = getattr(bucket, "reset_seconds", None)
    if remaining is None or reset_seconds is None:
        return False
    try:
        remaining_val = int(remaining)
        reset_val = float(reset_seconds)
    except (TypeError, ValueError):
        return False
    return remaining_val <= 0 and reset_val >= _MIN_DURABLE_RESET_SECONDS


def has_durable_rate_limit_evidence(
    *,
    headers: Any = None,
    last_known_state: Any = None,
    error_context: dict[str, Any] | None = None,
) -> bool:
    """Return True when a 429 looks like durable quota exhaustion.

    Durable cooldowns should only be written when the provider gives evidence
    that the caller's own bucket is actually exhausted. Short-lived upstream
    capacity 429s must stay session-local.
    """
    try:
        from agent.rate_limit_tracker import parse_rate_limit_headers

        if headers:
            parsed = parse_rate_limit_headers(headers, provider="")
            if parsed is not None:
                for bucket in (
                    parsed.requests_min,
                    parsed.requests_hour,
                    parsed.tokens_min,
                    parsed.tokens_hour,
                ):
                    if _bucket_has_durable_exhaustion(bucket):
                        return True
    except Exception:
        pass

    if last_known_state is not None:
        for name in ("requests_min", "requests_hour", "tokens_min", "tokens_hour"):
            if _bucket_has_durable_exhaustion(getattr(last_known_state, name, None)):
                return True

    if isinstance(error_context, dict):
        reset_at = error_context.get("reset_at")
        if reset_at not in {None, ""}:
            try:
                reset_at_val = float(reset_at if reset_at is not None else 0.0)
                if reset_at_val > time.time() + _MIN_DURABLE_RESET_SECONDS:
                    return True
            except (TypeError, ValueError):
                pass

    return False


@dataclass
class ProviderRotationState:
    """Durable cooldown records for provider rotation."""

    unavailable: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: int = STATE_VERSION

    @classmethod
    def load(cls) -> "ProviderRotationState":
        path = state_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return cls()
        unavailable = raw.get("unavailable", {})
        if not isinstance(unavailable, dict):
            unavailable = {}
        return cls(
            unavailable=unavailable,
            version=int(raw.get("version", STATE_VERSION) or STATE_VERSION),
        )

    def save(self) -> None:
        _atomic_save_state(version=self.version, unavailable=self.unavailable)

    def _locked_update(self, updater) -> Any:
        lock_path = _state_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            if fcntl is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                current = type(self).load()
                result = updater(current)
                _atomic_save_state(
                    version=current.version,
                    unavailable=current.unavailable,
                )
                self.unavailable = copy.deepcopy(current.unavailable)
                self.version = current.version
                return result
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def mark_unavailable(
        self,
        *,
        provider: str,
        model: str,
        base_url: str | None = None,
        reason: str,
        cooldown_seconds: int | float = DEFAULT_COOLDOWN_SECONDS,
        now: float | None = None,
        message: str | None = None,
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        cooldown = max(0.0, float(cooldown_seconds or 0))
        normalized_base_url = _normalize_base_url(base_url)

        def _update(current: ProviderRotationState) -> None:
            key = provider_key(provider, model, normalized_base_url)
            current.unavailable[key] = {
                "provider": (provider or "").strip(),
                "model": (model or "").strip(),
                "base_url": normalized_base_url,
                "reason": (reason or "unknown").strip() or "unknown",
                "message": (message or "").strip(),
                "unavailable_at": timestamp,
                "retry_after": timestamp + cooldown,
            }

        self._locked_update(_update)

    def is_unavailable(
        self,
        provider: str,
        model: str,
        *,
        base_url: str | None = None,
        now: float | None = None,
    ) -> bool:
        timestamp = time.time() if now is None else float(now)
        key = provider_key(provider, model, base_url)
        record = self.unavailable.get(key)
        if not isinstance(record, dict):
            return False
        retry_after = float(record.get("retry_after") or 0)
        if retry_after <= timestamp:
            self._locked_update(lambda current: current.unavailable.pop(key, None))
            return False
        return True

    def reset(self, provider: str | None = None, model: str | None = None) -> int:
        """Remove matching cooldown records. Returns count removed."""
        if not provider:
            count = len(self.unavailable)
            self.unavailable.clear()
            self.save()
            return count

        provider_norm = _norm(provider)
        model_text = (model or "").strip()

        def _update(current: ProviderRotationState) -> int:
            removed_local = 0
            for key, record in list(current.unavailable.items()):
                rec_provider = _norm(
                    record.get("provider") if isinstance(record, dict) else key.split(":", 1)[0]
                )
                rec_model = (record.get("model") if isinstance(record, dict) else "") or ""
                if rec_provider != provider_norm:
                    continue
                if model_text and rec_model != model_text:
                    continue
                current.unavailable.pop(key, None)
                removed_local += 1
            return removed_local

        removed = self._locked_update(_update)
        return int(removed or 0)


def filter_available_entries(entries: Iterable[dict[str, Any]], *, now: float | None = None) -> list[dict[str, Any]]:
    """Return entries not currently cooled down, preserving original order."""
    state = ProviderRotationState.load()
    available: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider") or ""
        model = entry.get("model") or ""
        base_url = entry.get("base_url") or ""
        if not provider or not model:
            continue
        if state.is_unavailable(provider, model, base_url=base_url, now=now):
            continue
        available.append(entry)
    return available


def is_rotation_enabled(config: dict[str, Any] | None) -> bool:
    section = (config or {}).get("provider_rotation", {})
    return isinstance(section, dict) and bool(section.get("enabled", False))


def cooldown_for_reason(config: dict[str, Any] | None, reason: str | None = None) -> int:
    section = (config or {}).get("provider_rotation", {}) if isinstance(config, dict) else {}
    if not isinstance(section, dict):
        return DEFAULT_COOLDOWN_SECONDS
    by_reason = section.get("cooldown_seconds_by_reason")
    reason_key = (reason or "").strip().lower()
    if isinstance(by_reason, dict) and reason_key in by_reason:
        try:
            return int(by_reason[reason_key])
        except (TypeError, ValueError):
            pass
    try:
        return int(section.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_SECONDS
