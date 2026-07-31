"""Track non-secret environment values written by Hermes from YAML."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import os
import threading
from typing import Callable, Iterator, Mapping


_MISSING = object()
_LOCK = threading.RLock()


@dataclass(frozen=True)
class YamlEnvRecord:
    owner: str
    key: str
    value: str
    source: str


@dataclass(frozen=True)
class YamlEnvContext:
    """Scoped provenance state made available to a two-argument YAML hook."""

    load: "YamlEnvLoad"
    source_prefix: str
    source_paths: Mapping[str, str]

    def source_for(self, leaf: str) -> str:
        """Return the source path that supplied an effective leaf."""
        return self.source_paths.get(leaf, f"{self.source_prefix}.{leaf}")


_RECORDS: dict[str, YamlEnvRecord] = {}
_ACTIVE_CONTEXT: ContextVar[YamlEnvContext | None] = ContextVar(
    "hermes_yaml_env_context", default=None
)


@contextmanager
def yaml_env_context(
    load: "YamlEnvLoad",
    source_prefix: str,
    source_paths: Mapping[str, str] | None = None,
) -> Iterator[None]:
    """Expose load provenance to a plugin without changing its hook contract."""
    token = _ACTIVE_CONTEXT.set(
        YamlEnvContext(load, source_prefix, dict(source_paths or {}))
    )
    try:
        yield
    finally:
        _ACTIVE_CONTEXT.reset(token)


def get_yaml_env_context() -> YamlEnvContext | None:
    """Return the active scoped YAML hook context, if one is installed."""
    return _ACTIVE_CONTEXT.get()


def _is_secret_key(name: str) -> bool:
    try:
        from hermes_cli.agent_import import is_secret_key

        return is_secret_key(name)
    except Exception:
        return True


def _source_label(
    source: str,
    resolver: Callable[[str], str] | None,
) -> str:
    kind = resolver(source) if resolver is not None else "user-config"
    if kind not in {"user-config", "managed-config"}:
        kind = "user-config"
    return f"{kind}:{source}"


class YamlEnvLoad:
    """Own one complete source-resolved YAML environment load."""

    def __init__(
        self,
        owner: str,
        source_resolver: Callable[[str], str] | None = None,
    ) -> None:
        self.owner = owner
        self._source_resolver = source_resolver
        self._writers: set[str] = set()
        self._aborted = False
        self._finished = False

    @property
    def writer_names(self) -> frozenset[str]:
        return frozenset(self._writers)

    def set_env_from_yaml(
        self,
        name: str,
        value: str,
        source: str,
        *,
        predicate: Callable[[], bool],
    ) -> bool:
        """Offer a source-resolved key, then preserve its caller predicate."""
        if _is_secret_key(name):
            return False
        with _LOCK:
            self._writers.add(name)
            record = _RECORDS.get(name)
            live = os.environ.get(name, _MISSING)
            if record is not None and record.owner == self.owner:
                if live == record.value:
                    os.environ[name] = value
                    _RECORDS[name] = YamlEnvRecord(
                        self.owner,
                        name,
                        value,
                        _source_label(source, self._source_resolver),
                    )
                    return True
                _RECORDS.pop(name, None)

            if not predicate():
                return False
            os.environ[name] = value
            _RECORDS[name] = YamlEnvRecord(
                self.owner,
                name,
                value,
                _source_label(source, self._source_resolver),
            )
            return True

    def finish(self) -> None:
        """Reconcile removed writers after a complete successful load."""
        if self._finished or self._aborted:
            return
        with _LOCK:
            for name, record in list(_RECORDS.items()):
                if record.owner != self.owner or name in self._writers:
                    continue
                live = os.environ.get(name, _MISSING)
                if live is not _MISSING and live == record.value:
                    os.environ.pop(name, None)
                _RECORDS.pop(name, None)
        self._finished = True

    def abort(self) -> None:
        """End an incomplete load without absence-based cleanup."""
        self._aborted = True


def without_yaml_generated_env(
    env: Mapping[str, str],
) -> dict[str, str]:
    """Copy an environment without exact matching recorded YAML values."""
    result = dict(env)
    with _LOCK:
        for name, record in _RECORDS.items():
            if result.get(name, _MISSING) == record.value:
                result.pop(name, None)
    return result


def records_snapshot() -> dict[str, YamlEnvRecord]:
    """Return records for focused contract tests without exposing them in logs."""
    with _LOCK:
        return dict(_RECORDS)


def clear_records() -> None:
    """Clear process-local records for isolated tests."""
    with _LOCK:
        _RECORDS.clear()
