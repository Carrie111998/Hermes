"""Telegram/Feishu topic-to-project registry used by delegated execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml


DEFAULT_REGISTRY_PATH = Path("~/.hermes/thread_context_registry.yaml").expanduser()


class ThreadContextError(ValueError):
    """Raised when a conversation lane has no safe project binding."""


def registry_path() -> Path:
    override = os.getenv("HERMES_THREAD_CONTEXT_REGISTRY", "").strip()
    return Path(override).expanduser() if override else DEFAULT_REGISTRY_PATH


def load_thread_context_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.exists():
        return {"version": 1, "contexts": []}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, Mapping):
        raise ThreadContextError(f"invalid thread context registry: {path}")
    return dict(loaded)


def resolve_thread_context(*, platform: str, chat_id: str, thread_id: str) -> dict[str, Any]:
    """Resolve an exact lane. Never infer or fall back to a global project."""
    platform = str(platform or "").strip().lower()
    chat_id = str(chat_id or "").strip()
    thread_id = str(thread_id or "").strip()
    for item in load_thread_context_registry().get("contexts", []):
        if not isinstance(item, Mapping):
            continue
        if (
            str(item.get("platform") or "").strip().lower() == platform
            and str(item.get("chat_id") or "").strip() == chat_id
            and str(item.get("thread_id") or "").strip() == thread_id
        ):
            result = dict(item)
            if not str(result.get("project") or "").strip():
                raise ThreadContextError("registered lane is missing project")
            return result
    raise ThreadContextError(
        f"unregistered conversation lane: {platform}/{chat_id}/thread/{thread_id}; "
        "Grace must establish Topic/project context before delegating"
    )


def resolve_thread_context_alias(alias: str) -> dict[str, Any]:
    """Resolve an explicit scheduler alias without guessing a conversation lane."""
    alias = str(alias or "").strip()
    if not alias:
        raise ThreadContextError("scheduled delegation is missing context_alias")
    matches = []
    for item in load_thread_context_registry().get("contexts", []):
        if not isinstance(item, Mapping):
            continue
        aliases = item.get("aliases") or []
        if alias == str(item.get("project") or "").strip() or alias in {
            str(value).strip() for value in aliases
        }:
            matches.append(dict(item))
    if len(matches) != 1:
        raise ThreadContextError(
            f"context_alias must resolve to exactly one registered lane: {alias}"
        )
    return matches[0]


def assert_contract_matches_context(contract: Mapping[str, Any], context: Mapping[str, Any]) -> None:
    identity = contract.get("identity") if isinstance(contract, Mapping) else None
    identity = identity if isinstance(identity, Mapping) else {}
    for key in ("project", "topic_name", "thread_id"):
        if str(identity.get(key) or "").strip() != str(context.get(key) or "").strip():
            raise ThreadContextError(
                f"contract {key} does not match registered Topic context"
            )
