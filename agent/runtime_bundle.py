"""Resolved provider runtimes and wire-client construction.

This module is the boundary between three deliberately separate operations:

* provider resolution produces :class:`ResolvedRuntime`;
* :func:`build_client_bundle` constructs a complete client bundle without
  mutating an ``AIAgent``;
* the agent installs the finished bundle in one later commit step.

Bedrock and MoA keep their existing construction paths for now.  Their runtime
metadata is preserved by ``ResolvedRuntime`` so they can move behind this
factory without changing the public value contract.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Optional


class _FrozenList(tuple):
    """Tuple-backed list value that preserves legacy sequence equality."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (list, tuple)):
            return tuple(self) == tuple(other)
        return NotImplemented


def _freeze(value: Any) -> Any:
    """Freeze JSON-like containers while preserving opaque runtime objects."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


_CORE_RUNTIME_FIELDS = frozenset(
    {
        "provider",
        "model",
        "api_mode",
        "api_key",
        "base_url",
        "requested_provider",
        "source",
        "extra_headers",
        "ssl_ca_cert",
        "ssl_verify",
    }
)


@dataclass(frozen=True, slots=True, eq=False)
class ResolvedRuntime(Mapping[str, Any]):
    """Immutable, mapping-compatible provider resolution result.

    Mapping compatibility is intentional: existing callers can migrate away
    from ``runtime.get(...)`` incrementally, while the public resolver boundary
    no longer exposes a mutable dictionary.  Only keys present in the original
    resolver result participate in iteration and ``dict(runtime)``.
    """

    provider: str
    model: str = ""
    api_mode: str = "chat_completions"
    api_key: Any = ""
    base_url: str = ""
    requested_provider: str = ""
    source: str = ""
    extra_headers: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({})
    )
    ssl_ca_cert: Optional[str] = None
    ssl_verify: Optional[bool] = None
    metadata: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({}), repr=False
    )
    _keys: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | "ResolvedRuntime") -> "ResolvedRuntime":
        if isinstance(value, cls):
            return value
        raw = dict(value)
        headers = raw.get("extra_headers")
        frozen_headers = _freeze(headers) if isinstance(headers, Mapping) else MappingProxyType({})
        metadata = MappingProxyType(
            {
                key: _freeze(item)
                for key, item in raw.items()
                if key not in _CORE_RUNTIME_FIELDS
            }
        )
        ssl_verify = raw.get("ssl_verify")
        if not isinstance(ssl_verify, bool):
            ssl_verify = None
        ssl_ca_cert = raw.get("ssl_ca_cert")
        if not isinstance(ssl_ca_cert, str) or not ssl_ca_cert.strip():
            ssl_ca_cert = None
        return cls(
            provider=str(raw.get("provider") or ""),
            model=str(raw.get("model") or ""),
            api_mode=str(raw.get("api_mode") or "chat_completions"),
            api_key=raw.get("api_key", ""),
            base_url=str(raw.get("base_url") or ""),
            requested_provider=str(raw.get("requested_provider") or ""),
            source=str(raw.get("source") or ""),
            extra_headers=frozen_headers,
            ssl_ca_cert=ssl_ca_cert,
            ssl_verify=ssl_verify,
            metadata=metadata,
            _keys=tuple(raw.keys()),
        )

    def __getitem__(self, key: str) -> Any:
        if key not in self._keys:
            raise KeyError(key)
        if key in _CORE_RUNTIME_FIELDS:
            return getattr(self, key)
        return self.metadata[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Mapping):
            return NotImplemented
        return dict(self.items()) == dict(other.items())

    def as_dict(self) -> dict[str, Any]:
        """Return a mutable, shallow compatibility copy."""
        return dict(self.items())

    def with_updates(self, **updates: Any) -> "ResolvedRuntime":
        raw = self.as_dict()
        raw.update(updates)
        return type(self).from_mapping(raw)


@dataclass(frozen=True, slots=True)
class ClientBundle:
    """A completely constructed wire client plus its immutable runtime."""

    runtime: ResolvedRuntime
    client: Any = None
    anthropic_client: Any = None
    client_kwargs: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    anthropic_api_key: Any = ""
    anthropic_base_url: str = ""
    is_anthropic_oauth: bool = False

    @property
    def active_client(self) -> Any:
        return self.anthropic_client if self.anthropic_client is not None else self.client


class RuntimeWireNotMigratedError(NotImplementedError):
    """The runtime is valid, but its wire still uses the legacy builder."""


OpenAIBuilder = Callable[[dict[str, Any]], Any]
AnthropicBuilder = Callable[..., Any]


def _default_openai_builder(client_kwargs: dict[str, Any]) -> Any:
    from openai import OpenAI

    return OpenAI(**client_kwargs)


def build_client_bundle(
    runtime: Mapping[str, Any] | ResolvedRuntime,
    *,
    openai_builder: Optional[OpenAIBuilder] = None,
    anthropic_builder: Optional[AnthropicBuilder] = None,
    timeout: Optional[float] = None,
) -> ClientBundle:
    """Build an OpenAI-wire or Anthropic-wire client off-agent.

    The optional builders are dependency-injection seams, not extension hooks:
    the primary agent supplies its existing transport-aware OpenAI constructor,
    while auxiliary callers and tests can supply their established constructor.
    Provider behavior remains declared by the resolved runtime.
    """
    resolved = ResolvedRuntime.from_mapping(runtime)
    provider = resolved.provider.strip().lower()
    api_mode = resolved.api_mode.strip().lower()
    if provider in {"bedrock", "moa"} or api_mode == "bedrock_converse":
        raise RuntimeWireNotMigratedError(
            f"{provider or api_mode} still uses its existing runtime builder"
        )

    effective_timeout = timeout
    if effective_timeout is None:
        raw_timeout = resolved.get("timeout")
        if isinstance(raw_timeout, (int, float)) and not isinstance(raw_timeout, bool):
            effective_timeout = float(raw_timeout)

    declared_headers = resolved.get("default_headers")
    headers = (
        dict(declared_headers)
        if isinstance(declared_headers, Mapping)
        else {}
    )
    # ``extra_headers`` is the provider-entry override layer.  Keep it last so
    # gateway credentials such as Cloudflare Access service tokens cannot be
    # shadowed by a profile/default header with the same name.
    headers.update(resolved.extra_headers)
    if api_mode == "anthropic_messages":
        if anthropic_builder is None:
            from agent.anthropic_adapter import build_anthropic_client

            anthropic_builder = build_anthropic_client
        client = anthropic_builder(
            resolved.api_key,
            resolved.base_url or None,
            timeout=effective_timeout,
            default_headers=headers or None,
        )
        is_oauth = False
        if provider == "anthropic" and isinstance(resolved.api_key, str):
            from agent.anthropic_adapter import _is_oauth_token

            is_oauth = _is_oauth_token(resolved.api_key)
        return ClientBundle(
            runtime=resolved,
            anthropic_client=client,
            client_kwargs=MappingProxyType({}),
            anthropic_api_key=resolved.api_key,
            anthropic_base_url=resolved.base_url,
            is_anthropic_oauth=is_oauth,
        )

    client_kwargs: dict[str, Any] = {
        "api_key": resolved.api_key,
        "base_url": resolved.base_url,
    }
    if headers:
        client_kwargs["default_headers"] = headers
    if effective_timeout is not None:
        client_kwargs["timeout"] = effective_timeout
    if resolved.ssl_ca_cert:
        client_kwargs["ssl_ca_cert"] = resolved.ssl_ca_cert
    if resolved.ssl_verify is not None:
        client_kwargs["ssl_verify"] = resolved.ssl_verify
    default_query = resolved.get("default_query")
    if isinstance(default_query, Mapping) and default_query:
        client_kwargs["default_query"] = dict(default_query)
    command = resolved.get("command")
    if isinstance(command, str) and command:
        client_kwargs["command"] = command
    args = resolved.get("args")
    if isinstance(args, (list, tuple)):
        client_kwargs["args"] = list(args)

    builder = openai_builder or _default_openai_builder
    client = builder(dict(client_kwargs))
    return ClientBundle(
        runtime=resolved,
        client=client,
        client_kwargs=MappingProxyType(dict(client_kwargs)),
    )


__all__ = [
    "ClientBundle",
    "ResolvedRuntime",
    "RuntimeWireNotMigratedError",
    "build_client_bundle",
]
