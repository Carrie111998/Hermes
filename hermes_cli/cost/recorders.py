"""Thin, SDK-free vendor-specific cost recorder shims."""

from __future__ import annotations

from pathlib import Path

from hermes_cli.cost.ledger import record_call


def record_retell_call(
    task_id: str,
    lane: str,
    minutes: float,
    db_path: str | Path | None = None,
    *,
    profile: str | None = None,
    route: str | None = None,
    session_id: str | None = None,
) -> int:
    return record_call(
        task_id=task_id,
        lane=lane,
        vendor="retell",
        voice_minutes=minutes,
        profile=profile,
        route=route,
        session_id=session_id,
        db_path=db_path,
    ).id


def record_perplexity_call(
    task_id: str,
    lane: str,
    input_tokens: int,
    output_tokens: int,
    db_path: str | Path | None = None,
    *,
    profile: str | None = None,
    route: str | None = None,
    session_id: str | None = None,
) -> int:
    return record_call(
        task_id=task_id,
        lane=lane,
        vendor="perplexity",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        profile=profile,
        route=route,
        session_id=session_id,
        db_path=db_path,
    ).id


def record_apple_api_call(
    task_id: str,
    lane: str,
    api_call_kind: str,
    db_path: str | Path | None = None,
    *,
    profile: str | None = None,
    route: str | None = None,
    session_id: str | None = None,
) -> int:
    return record_call(
        task_id=task_id,
        lane=lane,
        vendor="apple",
        api_call_kind=api_call_kind,
        force_zero=True,
        profile=profile,
        route=route,
        session_id=session_id,
        db_path=db_path,
    ).id


def record_meta_api_call(
    task_id: str,
    lane: str,
    api_call_kind: str,
    db_path: str | Path | None = None,
    *,
    profile: str | None = None,
    route: str | None = None,
    session_id: str | None = None,
) -> int:
    return record_call(
        task_id=task_id,
        lane=lane,
        vendor="meta",
        api_call_kind=api_call_kind,
        force_zero=True,
        profile=profile,
        route=route,
        session_id=session_id,
        db_path=db_path,
    ).id


def record_github_api_call(
    task_id: str,
    lane: str,
    api_call_kind: str,
    db_path: str | Path | None = None,
    *,
    profile: str | None = None,
    route: str | None = None,
    session_id: str | None = None,
) -> int:
    return record_call(
        task_id=task_id,
        lane=lane,
        vendor="github",
        api_call_kind=api_call_kind,
        force_zero=True,
        profile=profile,
        route=route,
        session_id=session_id,
        db_path=db_path,
    ).id


__all__ = [
    "record_apple_api_call",
    "record_github_api_call",
    "record_meta_api_call",
    "record_perplexity_call",
    "record_retell_call",
]
