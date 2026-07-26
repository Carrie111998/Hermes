"""Strict version-one doctrine loading and one-time bootstrap."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_cli.routing import schema
from hermes_cli.sqlite_util import retrying_write_txn


DEFAULT_DOCTRINE_V1_PATH = (
    Path.home()
    / ".hermes"
    / "profiles"
    / "atlas"
    / "plugins"
    / "task-model-router"
    / "doctrine_v1.json"
)

STUB_PAYLOAD: dict[str, Any] = {
    "notes": (
        "CS-05-rev bootstrap stub. Adrian will replace with real rules per "
        "lane once ready. Version 1 intentionally defaults everything to "
        "Atlas Sol via the openai-codex Pro bridge."
    ),
    "created_by": "cs05_bootstrap",
    "rules": [
        {
            "lane": "default",
            "rung": "default",
            "complexity": "default",
            "primary_provider": "openai-codex",
            "primary_model": "gpt-5-6-sol",
            "fallback_chain": [],
            "forbid_paths": [],
            "priority": 0,
            "notes": (
                "Default fall-through for CS-05-rev. Override with hermes "
                "doctrine apply."
            ),
        },
        {
            "lane": "green_captains",
            "rung": "default",
            "complexity": "default",
            "primary_provider": "openai-codex",
            "primary_model": "gpt-5-6-sol",
            "fallback_chain": [],
            "forbid_paths": [],
            "priority": 10,
            "notes": (
                "Green Captains lane default. Replace with per-rung rules "
                "when TA-04+ ships."
            ),
        },
        {
            "lane": "dayroute",
            "rung": "default",
            "complexity": "default",
            "primary_provider": "openai-codex",
            "primary_model": "gpt-5-6-sol",
            "fallback_chain": [],
            "forbid_paths": [],
            "priority": 10,
            "notes": (
                "DayRoute lane default. Replace when TA-02a triage "
                "classifier lands."
            ),
        },
        {
            "lane": "tihna",
            "rung": "default",
            "complexity": "default",
            "primary_provider": "openai-codex",
            "primary_model": "gpt-5-6-sol",
            "fallback_chain": [],
            "forbid_paths": [],
            "priority": 10,
            "notes": (
                "Tihna lane default. Replace when TA-03a trends analyzer "
                "lands."
            ),
        },
    ],
}

_BOOTSTRAP_TOP_KEYS = frozenset({"notes", "created_by", "rules"})
_PLAN_TOP_KEYS = frozenset({"notes", "rules"})
_RULE_KEYS = frozenset(
    {
        "lane",
        "rung",
        "complexity",
        "primary_provider",
        "primary_model",
        "fallback_chain",
        "forbid_paths",
        "priority",
        "notes",
    }
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _require_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _validate_exact_keys(
    value: dict[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    actual = frozenset(value)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise ValueError(
            f"{label} keys must be exact; unknown={unknown}, missing={missing}"
        )


def validate_payload(
    payload: Any,
    *,
    require_created_by: bool,
) -> dict[str, Any]:
    """Validate and normalize a bootstrap or mutation-plan document."""
    if not isinstance(payload, dict):
        raise ValueError("doctrine document must be a JSON object")
    expected_top = _BOOTSTRAP_TOP_KEYS if require_created_by else _PLAN_TOP_KEYS
    _validate_exact_keys(payload, expected_top, "top-level")
    notes = _require_nonempty_string(payload.get("notes"), "notes")
    created_by = None
    if require_created_by:
        created_by = _require_nonempty_string(
            payload.get("created_by"),
            "created_by",
        )
    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("rules must be a non-empty list")

    normalized_rules = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f"rules[{index}] must be an object")
        _validate_exact_keys(rule, _RULE_KEYS, f"rules[{index}]")
        normalized = {
            field: _require_nonempty_string(
                rule.get(field),
                f"rules[{index}].{field}",
            )
            for field in (
                "lane",
                "rung",
                "complexity",
                "primary_provider",
                "primary_model",
            )
        }
        priority = rule.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError(f"rules[{index}].priority must be an integer")
        normalized["priority"] = priority
        normalized["notes"] = _require_nonempty_string(
            rule.get("notes"),
            f"rules[{index}].notes",
        )

        fallbacks = rule.get("fallback_chain")
        if not isinstance(fallbacks, list):
            raise ValueError(
                f"rules[{index}].fallback_chain must be a list"
            )
        normalized_fallbacks = []
        for fallback_index, fallback in enumerate(fallbacks):
            if not isinstance(fallback, dict):
                raise ValueError(
                    f"rules[{index}].fallback_chain[{fallback_index}] "
                    "must be an object"
                )
            _validate_exact_keys(
                fallback,
                frozenset({"provider", "model"}),
                f"rules[{index}].fallback_chain[{fallback_index}]",
            )
            normalized_fallbacks.append(
                {
                    "provider": _require_nonempty_string(
                        fallback.get("provider"),
                        (
                            f"rules[{index}].fallback_chain"
                            f"[{fallback_index}].provider"
                        ),
                    ),
                    "model": _require_nonempty_string(
                        fallback.get("model"),
                        (
                            f"rules[{index}].fallback_chain"
                            f"[{fallback_index}].model"
                        ),
                    ),
                }
            )
        normalized["fallback_chain"] = normalized_fallbacks

        forbid_paths = rule.get("forbid_paths")
        if not isinstance(forbid_paths, list):
            raise ValueError(f"rules[{index}].forbid_paths must be a list")
        normalized_forbid = []
        for forbid_index, forbidden in enumerate(forbid_paths):
            if not isinstance(forbidden, dict):
                raise ValueError(
                    f"rules[{index}].forbid_paths[{forbid_index}] "
                    "must be an object"
                )
            _validate_exact_keys(
                forbidden,
                frozenset({"lane", "rung"}),
                f"rules[{index}].forbid_paths[{forbid_index}]",
            )
            normalized_forbid.append(
                {
                    "lane": _require_nonempty_string(
                        forbidden.get("lane"),
                        (
                            f"rules[{index}].forbid_paths"
                            f"[{forbid_index}].lane"
                        ),
                    ),
                    "rung": _require_nonempty_string(
                        forbidden.get("rung"),
                        (
                            f"rules[{index}].forbid_paths"
                            f"[{forbid_index}].rung"
                        ),
                    ),
                }
            )
        normalized["forbid_paths"] = normalized_forbid
        normalized_rules.append(normalized)

    result: dict[str, Any] = {"notes": notes, "rules": normalized_rules}
    if created_by is not None:
        result["created_by"] = created_by
    return result


def write_stub(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(STUB_PAYLOAD, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _resolve_doctrine_path(
    doctrine_v1_path: str | Path | None,
) -> Path:
    if doctrine_v1_path is not None:
        return Path(doctrine_v1_path).expanduser()
    configured = os.environ.get("HERMES_DOCTRINE_V1_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_DOCTRINE_V1_PATH


def _insert_rule(
    conn,
    *,
    version: int,
    rule: dict[str, Any],
    created_ts: str,
) -> None:
    conn.execute(
        """
        INSERT INTO routing_doctrine (
            version, lane, rung, complexity, primary_provider,
            primary_model, fallback_chain_json, forbid_paths_json,
            notes, priority, created_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(version),
            rule["lane"],
            rule["rung"],
            rule["complexity"],
            rule["primary_provider"],
            rule["primary_model"],
            json.dumps(
                rule["fallback_chain"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(
                rule["forbid_paths"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            rule["notes"],
            int(rule["priority"]),
            created_ts,
        ),
    )


def bootstrap_if_needed(
    db_path=None,
    doctrine_v1_path: str | Path | None = None,
) -> dict[str, Any]:
    """Install version one exactly once from a strict JSON doctrine file."""
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        existing = conn.execute(
            "SELECT active_version FROM routing_doctrine_meta "
            "WHERE singleton = 1"
        ).fetchone()
    finally:
        conn.close()
    if existing is not None:
        return {"created": False}

    path = _resolve_doctrine_path(doctrine_v1_path)
    if not path.exists():
        write_stub(path)
    try:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid doctrine_v1 JSON at {path}: {exc}") from exc
    payload = validate_payload(raw_payload, require_created_by=True)
    now = utc_now()

    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            existing = conn.execute(
                "SELECT active_version FROM routing_doctrine_meta "
                "WHERE singleton = 1"
            ).fetchone()
            if existing is not None:
                return {"created": False}
            for rule in payload["rules"]:
                _insert_rule(
                    conn,
                    version=1,
                    rule=rule,
                    created_ts=now,
                )
            conn.execute(
                """
                INSERT INTO routing_doctrine_meta (
                    singleton, active_version, previous_version,
                    last_activated_ts, last_activated_by
                ) VALUES (1, 1, NULL, ?, 'cs05_bootstrap')
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO routing_doctrine_activations (
                    activated_version, deactivated_version, activated_ts,
                    activated_by, activation_type, notes
                ) VALUES (1, NULL, ?, 'cs05_bootstrap', 'bootstrap', ?)
                """,
                (now, payload["notes"]),
            )
    finally:
        conn.close()
    return {
        "created": True,
        "rule_count": len(payload["rules"]),
        "version": 1,
        "path": str(path),
    }


__all__ = [
    "DEFAULT_DOCTRINE_V1_PATH",
    "STUB_PAYLOAD",
    "_insert_rule",
    "bootstrap_if_needed",
    "utc_now",
    "validate_payload",
    "write_stub",
]
