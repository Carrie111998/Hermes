#!/usr/bin/env python3
"""Replay TGG WhatsApp bridge rows through the real Hermes gateway path.

This is intentionally a proof harness, not a production importer. It takes
stored WhatsApp bridge rows, rebuilds the bridge message shape, feeds them
through WhatsAppAdapter's replay debounce logic, and dispatches resulting turns
to GatewayRunner._handle_message so the normal PA session, tools, memory, and
transcript machinery are exercised.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path("/tmp/tgg-christopher-replay-69533/tenants/tgg.db")
DEFAULT_CHAT = "120363403845802098@g.us"
DEFAULT_SINCE = "2026-05-24 00:00:00 SGT"
DEFAULT_SECRETS = Path.home() / ".marshal" / "secrets.env"
TGG_CONFIG = REPO_ROOT / "deploy" / "tgg" / "christopher" / "config.yaml"
TGG_CONSTITUTION = REPO_ROOT / "deploy" / "tgg" / "christopher" / "christopher_tgg_constitution.yaml"
DOCS_DIR = Path.home() / "pcl-docs" / "records"


@dataclass
class ReplayRecord:
    source_ref: str
    chat_jid: str
    chat_name: str
    sender_id: str
    ts: int
    sgt: str
    text: str
    message_kind: str
    has_media: bool
    media_refs: list[dict[str, Any]]
    quoted_text: str
    reply_to_source_ref: str
    raw_json: dict[str, Any]


@dataclass
class PublishedTurn:
    turn_id: str
    event: Any
    segment: list[dict[str, Any]]
    source_refs: list[str]
    session_id: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    estimated_cost_usd: float
    model: str
    provider: str
    assistant: str


def _load_secrets(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _mint_local_christopher_token(ps_data_dir: Path) -> str | None:
    spine = ps_data_dir / "spine.db"
    if not spine.exists():
        return None
    import sqlite3

    token = f"pcl_pa_tgg_christopher_replay"
    scopes = json.dumps(
        [
            "cases:read",
            "cases:write",
            "observations:write",
            "attention:write",
            "state:write",
            "christopher:read",
            "christopher:write",
            "agent-config:read",
            "agent-config:write",
        ]
    )
    with sqlite3.connect(spine) as conn:
        conn.execute(
            """
            INSERT INTO ps_service_tokens
              (token, tenant_slug, agent_name, scopes_json, created_at, revoked_at)
            VALUES (?, 'tgg', 'christopher', ?, strftime('%s','now'), NULL)
            ON CONFLICT(token) DO UPDATE SET
              scopes_json = excluded.scopes_json,
              revoked_at = NULL
            """,
            (token, scopes),
        )
        conn.commit()
    return token


def _prepare_env(hermes_home: Path, *, secrets: Path) -> None:
    _load_secrets(secrets)
    os.environ.setdefault("PS_DATA_DIR", str(Path(DEFAULT_DB).parent.parent))
    # Christopher's deploy config expects this name. Studio secrets currently
    # still carry a legacy Bobby token, but local replay can mint a
    # Christopher-scoped token against the copied PS database.
    if "CHRISTOPHER_TGG_PS_SERVICE_TOKEN" not in os.environ:
        ps_data_dir = os.environ.get("PS_DATA_DIR")
        local = _mint_local_christopher_token(Path(ps_data_dir)) if ps_data_dir else None
        if local:
            os.environ["CHRISTOPHER_TGG_PS_SERVICE_TOKEN"] = local
        else:
            legacy = os.environ.get("BOBBY_TGG_PS_SERVICE_TOKEN")
            if legacy:
                os.environ["CHRISTOPHER_TGG_PS_SERVICE_TOKEN"] = legacy
    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ["HERMES_PA_BUSINESS_DRY_RUN"] = "1"
    os.environ["HERMES_PA_AGENT_ACTION_DRY_RUN"] = "1"
    os.environ.setdefault("HERMES_OPENAI_CAPTURE_DIR", str(hermes_home / "openai-captures"))
    os.environ["HERMES_TIMEZONE"] = "Asia/Singapore"
    os.environ.setdefault("TERMINAL_CWD", str(REPO_ROOT))


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} did not load as a mapping")
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _set_nested(mapping: dict[str, Any], keys: list[str], value: Any) -> None:
    current = mapping
    for key in keys[:-1]:
        child = current.setdefault(key, {})
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[keys[-1]] = value


def _prepare_hermes_home(
    hermes_home: Path,
    *,
    chat_id: str,
    model: str,
    debounce_seconds: int,
    business_base_url: str | None,
    vision_provider: str | None,
    vision_model: str | None,
    vision_concurrency: int,
) -> None:
    config = _load_yaml(TGG_CONFIG)
    constitution = _load_yaml(TGG_CONSTITUTION)

    provider_name = "openai-direct-primary"
    config["providers"] = {
        provider_name: {
            "name": "OpenAI Direct Primary",
            "api": "https://api.openai.com/v1",
            "key_env": "OPENAI_API_KEY",
            "default_model": model,
            "transport": "codex_responses",
        }
    }
    _set_nested(config, ["model", "provider"], provider_name)
    _set_nested(config, ["model", "default"], model)
    _set_nested(config, ["agent", "profile"], "pa")
    _set_nested(config, ["agent", "max_turns"], 12)
    _set_nested(config, ["display", "tool_progress"], "off")
    _set_nested(config, ["streaming", "enabled"], False)
    # Christopher reasons about a maintenance group as one conversation.
    # Hermes defaults to per-participant group sessions, which is right for
    # many assistants but wrong for replay/live ledger perception here.
    config["group_sessions_per_user"] = False
    config["thread_sessions_per_user"] = False

    local_constitution = hermes_home / "christopher_tgg_constitution.yaml"
    _set_nested(config, ["pa", "constitution_path"], str(local_constitution))
    auxiliary = config.setdefault("auxiliary", {})
    if isinstance(auxiliary, dict):
        for value in auxiliary.values():
            if isinstance(value, dict):
                value["provider"] = "main"
                value["model"] = model
        vision = auxiliary.setdefault("vision", {})
        if isinstance(vision, dict):
            vision["provider"] = vision_provider or "main"
            vision["model"] = vision_model or model
            vision["max_concurrency"] = max(1, int(vision_concurrency or 1))

    if business_base_url:
        bridge = (
            config.setdefault("pa", {})
            .setdefault("overlay", {})
            .setdefault("client", {})
            .setdefault("business_bridge", {})
        )
        operations = bridge.get("operations")
        if isinstance(operations, dict):
            base = business_base_url.rstrip("/")
            for operation in operations.values():
                if not isinstance(operation, dict):
                    continue
                url = str(operation.get("url") or "")
                if url.startswith("https://systems.papercut-labs.com"):
                    operation["url"] = url.replace("https://systems.papercut-labs.com", base, 1)

    platform = config.setdefault("platforms", {}).setdefault("whatsapp", {})
    platform["enabled"] = True
    extra = platform.setdefault("extra", {})
    extra.update(
        {
            "require_mention": True,
            "group_policy": "allowlist",
            "group_allow_from": [chat_id],
            "ingest_chats": [chat_id],
            "turn_policy": {
                chat_id: {
                    "process_all": True,
                    "debounce_seconds": debounce_seconds,
                    "direct_mention_immediate": True,
                }
            },
            "pa_job_type": "tgg_ops_ingest",
            "pa": {"enabled": True, "job_type": "tgg_ops_ingest"},
        }
    )

    _set_nested(constitution, ["runtime", "provider"], provider_name)
    _set_nested(constitution, ["runtime", "model"], model)
    for brief in (constitution.get("job_briefs") or {}).values():
        if isinstance(brief, dict):
            runtime = brief.setdefault("runtime", {})
            runtime["provider"] = provider_name
            runtime["model"] = model

    _write_yaml(hermes_home / "config.yaml", config)
    _write_yaml(local_constitution, constitution)
    (hermes_home / "sessions").mkdir(parents=True, exist_ok=True)
    (hermes_home / "cache").mkdir(parents=True, exist_ok=True)


def _parse_media_refs(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _parse_raw_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_records(
    db_path: Path,
    *,
    chat_id: str,
    since_sgt: str,
    until_sgt: str | None,
    limit: int | None,
    skip_messages: int = 0,
) -> list[ReplayRecord]:
    clauses = ["chat_jid = ?", "sgt >= ?"]
    params: list[Any] = [chat_id, since_sgt]
    if until_sgt:
        clauses.append("sgt < ?")
        params.append(until_sgt)

    sql = f"""
        SELECT source_ref, chat_jid, chat_name, sender_id, ts, sgt, text,
               message_kind, has_media, media_refs, quoted_text,
               reply_to_source_ref, raw_json
        FROM bridge_message_log
        WHERE {' AND '.join(clauses)}
        ORDER BY ts, source_ref
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    elif skip_messages:
        sql += " LIMIT -1"
    if skip_messages:
        sql += " OFFSET ?"
        params.append(int(skip_messages))

    rows = []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for row in conn.execute(sql, params):
            rows.append(
                ReplayRecord(
                    source_ref=str(row["source_ref"] or ""),
                    chat_jid=str(row["chat_jid"] or ""),
                    chat_name=str(row["chat_name"] or row["chat_jid"] or ""),
                    sender_id=str(row["sender_id"] or ""),
                    ts=int(row["ts"] or 0),
                    sgt=str(row["sgt"] or ""),
                    text=str(row["text"] or ""),
                    message_kind=str(row["message_kind"] or "text"),
                    has_media=bool(row["has_media"]),
                    media_refs=_parse_media_refs(row["media_refs"]),
                    quoted_text=str(row["quoted_text"] or ""),
                    reply_to_source_ref=str(row["reply_to_source_ref"] or ""),
                    raw_json=_parse_raw_json(row["raw_json"]),
                )
            )
    return rows


def _media_paths(refs: list[dict[str, Any]]) -> list[str]:
    paths = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        candidate = ref.get("local_path") or ref.get("path") or ref.get("file_path")
        if candidate:
            paths.append(str(candidate))
    return paths


def _record_to_bridge_message(record: ReplayRecord) -> dict[str, Any]:
    raw = dict(record.raw_json)
    message_id = (
        raw.get("id")
        or str(record.source_ref).rsplit("::", 1)[-1]
        or record.source_ref
    )
    media_paths = _media_paths(record.media_refs)
    body = record.text
    message_kind = record.message_kind or ""
    if not body and record.has_media:
        body = ""
    bridge = {
        **raw,
        "messageId": message_id,
        "chatId": record.chat_jid,
        "chatName": record.chat_name,
        "senderId": record.sender_id,
        "senderName": record.sender_id.split("@", 1)[0] if record.sender_id else "",
        "isGroup": record.chat_jid.endswith("@g.us"),
        "timestamp": record.ts,
        "sgt": record.sgt,
        "body": body,
        "hasMedia": bool(record.has_media),
        "mediaType": message_kind,
        "mediaUrls": media_paths,
        "quotedText": record.quoted_text,
        "quotedMessageId": record.reply_to_source_ref,
        "fromMe": bool(raw.get("fromMe", False)),
        "_tgg_source_ref": record.source_ref,
        "_tgg_sgt": record.sgt,
        "_hermes_pa_job_type": "tgg_ops_ingest",
        "_hermes_pa_context": {
            "tenant": "tgg",
            "agent_id": "christopher",
            "job_type": "tgg_ops_ingest",
        },
    }
    return bridge


def _extract_latest_assistant(messages: list[dict[str, Any]], start: int = 0) -> str:
    for msg in reversed(messages[start:]):
        if msg.get("role") == "assistant" and msg.get("content"):
            return str(msg.get("content"))
    return ""


def _extract_tool_names(messages: list[dict[str, Any]], start: int = 0) -> list[str]:
    out: list[str] = []
    for msg in messages[start:]:
        name = msg.get("tool_name")
        if name:
            out.append(str(name))
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            if isinstance(function, dict) and function.get("name"):
                out.append(str(function["name"]))
    seen = set()
    deduped = []
    for name in out:
        if name not in seen:
            deduped.append(name)
            seen.add(name)
    return deduped


def _message_tool_calls(msg: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for call in msg.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not name:
            continue
        raw_args = function.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            args = {"_raw": raw_args}
        out.append(
            {
                "id": call.get("id") or call.get("call_id"),
                "name": str(name),
                "arguments": args if isinstance(args, dict) else {"value": args},
            }
        )
    return out


def _tool_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "assistant":
            for call in _message_tool_calls(msg):
                call_id = str(call.get("id") or "")
                if call_id:
                    calls[call_id] = call
                ordered.append({"call": call, "result": None})
        elif msg.get("role") == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            call = calls.get(call_id, {"id": call_id, "name": msg.get("name") or "tool", "arguments": {}})
            content = msg.get("content") or ""
            try:
                parsed = json.loads(content) if isinstance(content, str) else content
            except Exception:
                parsed = {"_raw": content}
            paired = False
            for item in reversed(ordered):
                if item.get("result") is None and item.get("call", {}).get("id") == call_id:
                    item["result"] = parsed
                    paired = True
                    break
            if not paired:
                ordered.append({"call": call, "result": parsed})
    return ordered


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _tool_result_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        payload = result.get("payload")
        if isinstance(payload, dict):
            return payload
        data = result.get("data")
        if isinstance(data, dict):
            return data
    return {}


def _lookup_candidates(pair: dict[str, Any]) -> list[dict[str, Any]]:
    call = pair.get("call") or {}
    name = str(call.get("name") or "")
    if name not in {"tgg_case_lookup", "tgg_case_search", "case_lookup", "case_search"}:
        return []
    result = pair.get("result")
    data = result.get("data") if isinstance(result, dict) else None
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "job_no": _first_text(row.get("jobNo"), row.get("job_no"), row.get("normalized_job_no")),
                "address": _first_text(row.get("address"), row.get("unitAddress"), row.get("unit_address")),
                "match_reasons": [
                    str(v)
                    for v in (
                        row.get("matchReasons")
                        or row.get("match_reasons")
                        or row.get("reasons")
                        or []
                    )
                    if v
                ],
            }
        )
    return out


def _status_effect_from_text(text: str) -> str:
    lower = text.lower()
    if any(word in lower for word in ("done", "complete", "completed", "install done", "replaced", "rectified")):
        return "reported_complete"
    if any(word in lower for word in ("new job", "job no", "assist", "please assist")):
        return "new_job_or_request"
    if any(word in lower for word in ("update", "arrange", "when", "follow up", "follow-up")):
        return "followup"
    return "observation"


def _case_effect_from_pair(pair: dict[str, Any], *, source_refs: list[str], assistant: str) -> dict[str, Any] | None:
    call = pair.get("call") or {}
    name = str(call.get("name") or "")
    if name not in {"tgg_case_observation", "case_observation", "tgg_case_create", "case_create"}:
        return None
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    result = pair.get("result")
    payload = _tool_result_payload(result)
    job_no = _first_text(
        payload.get("jobNo"),
        payload.get("job_no"),
        args.get("jobNo"),
        args.get("job_no"),
        args.get("case_id"),
    )
    notes = _first_text(payload.get("notes"), args.get("notes"), args.get("messageText"), args.get("message_text"), assistant)
    effect = _status_effect_from_text(notes or assistant or "")
    if "create" in name:
        effect = "new_case_dry_run"
    confidence = _first_text(payload.get("confidence"), args.get("confidence")) or ("high" if job_no else "low")
    case_match = "tool_lookup" if job_no else "unmatched"
    return {
        "status_effect": effect,
        "confidence": confidence,
        "case_match": case_match,
        "normalized_job_no": job_no,
        "summary": notes or assistant or "",
        "evidence_source_refs": source_refs,
        "needs_human_confirmation": not bool(job_no) or "create" in name,
        "reason": _first_text(
            payload.get("reason"),
            result.get("message") if isinstance(result, dict) else None,
            "dry-run tool call captured from Hermes replay",
        ),
    }


def _action_from_pair(pair: dict[str, Any], *, assistant: str) -> dict[str, Any] | None:
    call = pair.get("call") or {}
    name = str(call.get("name") or "")
    if name not in {"tgg_case_observation", "case_observation", "tgg_case_create", "case_create"}:
        return None
    args = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    result = pair.get("result")
    payload = _tool_result_payload(result)
    return {
        "action_type": "create_case_dry_run" if "create" in name else "record_observation",
        "should_send": False,
        "needs_human_approval": False,
        "draft_message": None,
        "reason": _first_text(
            payload.get("reason"),
            result.get("message") if isinstance(result, dict) else None,
            args.get("notes"),
            assistant,
        ),
    }


def _pending_questions(assistant: str) -> list[dict[str, Any]]:
    lower = assistant.lower()
    if "confirm" not in lower and "which job" not in lower and "can you" not in lower:
        return []
    return [{"question": assistant.strip()}] if assistant.strip() else []


def _source_refs_from_event(event: Any) -> list[str]:
    raw = event.raw_message if isinstance(event.raw_message, dict) else {}
    refs: list[str] = []
    messages = raw.get("messages") if raw.get("bundle") else [raw]
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        ref = message.get("_tgg_source_ref") or message.get("source_ref")
        if ref:
            refs.append(str(ref))
    if not refs:
        for value in raw.get("sourceMessageIds") or []:
            if value:
                refs.append(str(value))
    if not refs and getattr(event, "message_id", None):
        refs.append(str(event.message_id))
    return refs


def _build_review_result(
    *,
    turn: PublishedTurn,
) -> dict[str, Any]:
    pairs = _tool_pairs(turn.segment)
    lookup = []
    case_effects = []
    actions = []
    for pair in pairs:
        lookup.extend(_lookup_candidates(pair))
        effect = _case_effect_from_pair(pair, source_refs=turn.source_refs, assistant=turn.assistant)
        if effect:
            case_effects.append(effect)
        action = _action_from_pair(pair, assistant=turn.assistant)
        if action:
            actions.append(action)
    return {
        "run_id": None,
        "turn_id": turn.turn_id,
        "processor_version": "hermes-gateway-replay-v1",
        "provider": turn.provider or "openai-direct-primary",
        "model": turn.model or "gpt-5.4-mini",
        "status": "ok",
        "turn_summary": turn.assistant,
        "case_effects_json": json.dumps(case_effects, ensure_ascii=False),
        "actions_json": json.dumps(actions, ensure_ascii=False),
        "pending_questions_json": json.dumps(_pending_questions(turn.assistant), ensure_ascii=False),
        "lookup_json": json.dumps(lookup, ensure_ascii=False),
        "model_input_json": json.dumps(
            {
                "source_refs": turn.source_refs,
                "input_tokens": turn.input_tokens,
                "cached_input_tokens": turn.cached_input_tokens,
                "output_tokens": turn.output_tokens,
                "reasoning_output_tokens": turn.reasoning_output_tokens,
                "estimated_cost_usd": turn.estimated_cost_usd,
            },
            ensure_ascii=False,
        ),
        "model_output_json": json.dumps(
            {
                "assistant": turn.assistant,
                "tools": pairs,
            },
            ensure_ascii=False,
        ),
        "error": None,
    }


def _result_usage(messages: list[dict[str, Any]], start: int = 0) -> tuple[int, int]:
    input_tokens = 0
    output_tokens = 0
    for msg in messages[start:]:
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            continue
        input_tokens += int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return input_tokens, output_tokens


def _capture_response_usage(files: list[Path]) -> dict[str, int]:
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        payload = data.get("payload") if isinstance(data, dict) else {}
        raw_usage = payload.get("usage") if isinstance(payload, dict) else {}
        if not isinstance(raw_usage, dict):
            continue
        usage["input_tokens"] += _as_int(raw_usage.get("input_tokens") or raw_usage.get("prompt_tokens"))
        usage["cached_input_tokens"] += _as_int(
            (raw_usage.get("input_tokens_details") or {}).get("cached_tokens")
        )
        usage["output_tokens"] += _as_int(raw_usage.get("output_tokens") or raw_usage.get("completion_tokens"))
        usage["reasoning_output_tokens"] += _as_int(
            (raw_usage.get("output_tokens_details") or {}).get("reasoning_tokens")
        )
    return usage


def _publish_review_run(
    *,
    db_path: Path,
    run_id: str,
    records: list[ReplayRecord],
    turn_results: list[dict[str, Any]],
    run_label: str,
    model: str,
    debounce_seconds: int,
    turn_offset: int = 0,
) -> dict[str, Any]:
    if not records:
        raise RuntimeError("Cannot publish empty replay run")
    now_ts = int(datetime.now().timestamp())
    chat_scope = sorted({record.chat_jid for record in records})
    turn_policy = {
        chat_id: {
            "process_all": True,
            "debounce_seconds": debounce_seconds,
            "direct_mention_immediate": True,
        }
        for chat_id in chat_scope
    }
    published_turns: list[PublishedTurn] = []
    for index, result in enumerate(turn_results, start=turn_offset + 1):
        event = result["event"]
        source_refs = _source_refs_from_event(event)
        if not source_refs:
            source_refs = [f"turn-{index}"]
        start_record = next((record for record in records if record.source_ref == source_refs[0]), None)
        end_record = next((record for record in reversed(records) if record.source_ref == source_refs[-1]), None)
        turn_id = f"{run_id}:turn:{index:04d}"
        published_turns.append(
            PublishedTurn(
                turn_id=turn_id,
                event=event,
                segment=result.get("segment") or [],
                source_refs=source_refs,
                session_id=str(result.get("session_id") or ""),
                input_tokens=_as_int(result.get("input_tokens")),
                cached_input_tokens=_as_int(result.get("cached_input_tokens")),
                output_tokens=_as_int(result.get("output_tokens")),
                reasoning_output_tokens=_as_int(result.get("reasoning_output_tokens")),
                estimated_cost_usd=_as_number(result.get("estimated_cost_usd")),
                model=str(result.get("model") or model),
                provider=str(result.get("provider") or "openai-direct-primary"),
                assistant=str(result.get("assistant") or ""),
            )
        )

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                INSERT INTO tgg_christopher_runs
                  (run_id, mode, clock_mode, status, source_adapter, chat_scope_json,
                   replay_window_start_ts, replay_window_end_ts, debounce_enabled,
                   quiet_window_seconds, direct_mention_immediate, detect_only,
                   turn_policy_json, metadata_json, created_at, started_at, ended_at, updated_at)
                VALUES (?, 'replay', 'virtual', 'settled', 'hermes-whatsapp-replay',
                        ?, ?, ?, 1, ?, 1, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  status = excluded.status,
                  chat_scope_json = excluded.chat_scope_json,
                  replay_window_start_ts = MIN(COALESCE(tgg_christopher_runs.replay_window_start_ts, excluded.replay_window_start_ts), excluded.replay_window_start_ts),
                  replay_window_end_ts = MAX(COALESCE(tgg_christopher_runs.replay_window_end_ts, excluded.replay_window_end_ts), excluded.replay_window_end_ts),
                  quiet_window_seconds = excluded.quiet_window_seconds,
                  turn_policy_json = excluded.turn_policy_json,
                  metadata_json = excluded.metadata_json,
                  started_at = MIN(COALESCE(tgg_christopher_runs.started_at, excluded.started_at), excluded.started_at),
                  ended_at = MAX(COALESCE(tgg_christopher_runs.ended_at, excluded.ended_at), excluded.ended_at),
                  updated_at = excluded.updated_at
                """,
                (
                    run_id,
                    json.dumps(chat_scope),
                    records[0].ts,
                    records[-1].ts,
                    debounce_seconds,
                    json.dumps(turn_policy),
                    json.dumps(
                        {
                            "label": run_label,
                            "publisher": "tgg_christopher_hermes_replay.py",
                            "processor_version": "hermes-gateway-replay-v1",
                            "model": model,
                        },
                        ensure_ascii=False,
                    ),
                    now_ts,
                    records[0].ts,
                    records[-1].ts,
                    now_ts,
                ),
            )
            queue_ids: dict[str, int] = {}
            for record in records:
                cursor = conn.execute(
                    """
                    INSERT INTO tgg_christopher_message_queue
                      (run_id, mode, chat_id, chat_name, source_ref, source_kind,
                       sender_id, sender_label, sender_role_guess, from_me, ts, sgt,
                       text, message_kind, has_media, media_refs_json, reply_to_source_ref,
                       quoted_text, mentioned_ids_json, raw_json, state, turn_id, error,
                       ingested_at, updated_at)
                    VALUES (?, 'replay', ?, ?, ?, 'hermes_replay', ?, ?, NULL, 0, ?, ?,
                            ?, ?, ?, ?, ?, ?, '[]', ?, 'queued', NULL, NULL, ?, ?)
                    ON CONFLICT(run_id, source_ref) DO UPDATE SET
                      chat_id = excluded.chat_id,
                      chat_name = excluded.chat_name,
                      sender_id = excluded.sender_id,
                      sender_label = excluded.sender_label,
                      ts = excluded.ts,
                      sgt = excluded.sgt,
                      text = excluded.text,
                      message_kind = excluded.message_kind,
                      has_media = excluded.has_media,
                      media_refs_json = excluded.media_refs_json,
                      reply_to_source_ref = excluded.reply_to_source_ref,
                      quoted_text = excluded.quoted_text,
                      raw_json = excluded.raw_json,
                      updated_at = excluded.updated_at
                    """,
                    (
                        run_id,
                        record.chat_jid,
                        record.chat_name,
                        record.source_ref,
                        record.sender_id or None,
                        record.sender_id or None,
                        record.ts,
                        record.sgt,
                        record.text,
                        record.message_kind or "text",
                        1 if record.has_media else 0,
                        json.dumps(record.media_refs, ensure_ascii=False),
                        record.reply_to_source_ref or None,
                        record.quoted_text or None,
                        json.dumps(record.raw_json, ensure_ascii=False),
                        now_ts,
                        now_ts,
                    ),
                )
                if int(cursor.lastrowid or 0):
                    queue_ids[record.source_ref] = int(cursor.lastrowid)
                else:
                    row = conn.execute(
                        """
                        SELECT id FROM tgg_christopher_message_queue
                        WHERE run_id = ? AND source_ref = ?
                        """,
                        (run_id, record.source_ref),
                    ).fetchone()
                    if row:
                        queue_ids[record.source_ref] = int(row[0])

            record_by_ref = {record.source_ref: record for record in records}
            for turn in published_turns:
                turn_records = [record_by_ref[ref] for ref in turn.source_refs if ref in record_by_ref]
                if not turn_records:
                    continue
                conn.execute(
                    """
                    INSERT INTO tgg_christopher_turns
                      (turn_id, run_id, chat_id, chat_name, turn_start_ts, turn_end_ts,
                       turn_start_sgt, turn_end_sgt, message_count, media_count,
                       closed_reason, debounce_enabled, quiet_window_seconds,
                       direct_mention, policy_json, summary_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'quiet_window', 1, ?, 0, ?, ?, ?)
                    ON CONFLICT(turn_id) DO UPDATE SET
                      chat_id = excluded.chat_id,
                      chat_name = excluded.chat_name,
                      turn_start_ts = excluded.turn_start_ts,
                      turn_end_ts = excluded.turn_end_ts,
                      turn_start_sgt = excluded.turn_start_sgt,
                      turn_end_sgt = excluded.turn_end_sgt,
                      message_count = excluded.message_count,
                      media_count = excluded.media_count,
                      closed_reason = excluded.closed_reason,
                      debounce_enabled = excluded.debounce_enabled,
                      quiet_window_seconds = excluded.quiet_window_seconds,
                      direct_mention = excluded.direct_mention,
                      policy_json = excluded.policy_json,
                      summary_json = excluded.summary_json
                    """,
                    (
                        turn.turn_id,
                        run_id,
                        turn_records[0].chat_jid,
                        turn_records[0].chat_name,
                        turn_records[0].ts,
                        turn_records[-1].ts,
                        turn_records[0].sgt,
                        turn_records[-1].sgt,
                        len(turn_records),
                        sum(1 for record in turn_records if record.has_media),
                        debounce_seconds,
                        json.dumps(turn_policy.get(turn_records[0].chat_jid, {})),
                        json.dumps(
                            {
                                "session_id": turn.session_id,
                                "input_tokens": turn.input_tokens,
                                "cached_input_tokens": turn.cached_input_tokens,
                                "output_tokens": turn.output_tokens,
                                "reasoning_output_tokens": turn.reasoning_output_tokens,
                                "estimated_cost_usd": turn.estimated_cost_usd,
                            },
                            ensure_ascii=False,
                        ),
                        now_ts,
                    ),
                )
                for record in turn_records:
                    queue_id = queue_ids.get(record.source_ref)
                    if not queue_id:
                        continue
                    conn.execute(
                        """
                        DELETE FROM tgg_christopher_turn_messages
                        WHERE run_id = ? AND turn_id = ? AND source_ref = ?
                        """,
                        (run_id, turn.turn_id, record.source_ref),
                    )
                    conn.execute(
                        """
                        INSERT INTO tgg_christopher_turn_messages
                          (run_id, turn_id, queue_id, source_ref, ts)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT DO NOTHING
                        """,
                        (run_id, turn.turn_id, queue_id, record.source_ref, record.ts),
                    )
                    conn.execute(
                        """
                        UPDATE tgg_christopher_message_queue
                        SET state = 'turn_processed', turn_id = ?, updated_at = ?
                        WHERE run_id = ? AND id = ?
                        """,
                        (turn.turn_id, now_ts, run_id, queue_id),
                    )
                review = _build_review_result(turn=turn)
                review["run_id"] = run_id
                conn.execute(
                    """
                    INSERT INTO tgg_christopher_turn_results
                      (run_id, turn_id, processor_version, provider, model, status,
                       turn_summary, case_effects_json, actions_json,
                       pending_questions_json, lookup_json, model_input_json,
                       model_output_json, error, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, turn_id, processor_version) DO UPDATE SET
                      provider = excluded.provider,
                      model = excluded.model,
                      status = excluded.status,
                      turn_summary = excluded.turn_summary,
                      case_effects_json = excluded.case_effects_json,
                      actions_json = excluded.actions_json,
                      pending_questions_json = excluded.pending_questions_json,
                      lookup_json = excluded.lookup_json,
                      model_input_json = excluded.model_input_json,
                      model_output_json = excluded.model_output_json,
                      error = excluded.error,
                      updated_at = excluded.updated_at
                    """,
                    (
                        run_id,
                        turn.turn_id,
                        review["processor_version"],
                        review["provider"],
                        review["model"],
                        review["status"],
                        review["turn_summary"],
                        review["case_effects_json"],
                        review["actions_json"],
                        review["pending_questions_json"],
                        review["lookup_json"],
                        review["model_input_json"],
                        review["model_output_json"],
                        review["error"],
                        now_ts,
                        now_ts,
                    ),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        message_count = conn.execute(
            """
            SELECT COUNT(*) FROM tgg_christopher_message_queue
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
        turn_count = conn.execute(
            """
            SELECT COUNT(*) FROM tgg_christopher_turns
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
        result_count = conn.execute(
            """
            SELECT COUNT(*) FROM tgg_christopher_turn_results
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]
    return {
        "run_id": run_id,
        "messages": int(message_count),
        "turns": int(turn_count),
        "results": int(result_count),
        "db": str(db_path),
    }


def _as_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any) -> int:
    return int(_as_number(value, 0.0))


def _html_report(
    *,
    output_path: Path,
    run_label: str,
    model: str,
    db_path: Path,
    records: list[ReplayRecord],
    turn_results: list[dict[str, Any]],
    hermes_home: Path,
    session_id: str,
) -> None:
    rows = []
    for index, result in enumerate(turn_results, start=1):
        event = result["event"]
        text = str(event.text or "")
        source_ids = []
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        if raw.get("bundle"):
            source_ids = [str(v) for v in raw.get("sourceMessageIds") or []]
        elif event.message_id:
            source_ids = [str(event.message_id)]
        rows.append(
            f"""
            <section class="turn">
              <div class="turn-head">
                <span>turn {index}</span>
                <span>{html.escape(str(event.message_id or ''))}</span>
              </div>
              <div class="meta">{len(source_ids)} source message(s) · tools: {html.escape(', '.join(result['tools']) or 'none')} · {_as_int(result.get('input_tokens')):,} in ({_as_int(result.get('cached_input_tokens')):,} cached) / {_as_int(result.get('output_tokens')):,} out ({_as_int(result.get('reasoning_output_tokens')):,} reasoning) · ${_as_number(result.get('estimated_cost_usd')):.6f}</div>
              <pre class="inbound">{html.escape(text)}</pre>
              <pre class="assistant">{html.escape(result['assistant'] or '[no assistant transcript row]')}</pre>
            </section>
            """
        )
    first = records[0].sgt if records else "n/a"
    last = records[-1].sgt if records else "n/a"
    total_in = sum(_as_int(r.get("input_tokens")) for r in turn_results)
    total_cached = sum(_as_int(r.get("cached_input_tokens")) for r in turn_results)
    total_out = sum(_as_int(r.get("output_tokens")) for r in turn_results)
    total_reasoning = sum(_as_int(r.get("reasoning_output_tokens")) for r in turn_results)
    total_cost = sum(_as_number(r.get("estimated_cost_usd")) for r in turn_results)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{html.escape(run_label)}</title>
  <style>
    body {{ margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f4ef; color: #171717; }}
    header {{ padding: 28px 32px; background: #123c42; color: white; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; padding: 20px 32px; background: white; border-bottom: 1px solid #dedbd2; }}
    .summary div {{ padding: 12px; background: #f2f7f6; border: 1px solid #d7e6e3; border-radius: 8px; }}
    .summary b {{ display: block; font-size: 18px; }}
    main {{ padding: 24px 32px 40px; max-width: 1180px; margin: 0 auto; }}
    .turn {{ background: white; border: 1px solid #dedbd2; border-radius: 8px; margin-bottom: 18px; overflow: hidden; }}
    .turn-head {{ display: flex; justify-content: space-between; gap: 16px; background: #e6eee9; padding: 10px 14px; font-weight: 700; }}
    .meta {{ padding: 8px 14px; color: #555; border-bottom: 1px solid #eee9df; }}
    pre {{ margin: 0; padding: 14px; white-space: pre-wrap; word-break: break-word; font: 13px/1.42 ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .inbound {{ background: #fff; border-bottom: 1px solid #eee9df; }}
    .assistant {{ background: #f4f0fb; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(run_label)}</h1>
    <div>actual Hermes gateway replay · dry-run business writes · no prod mutation</div>
  </header>
  <section class="summary">
    <div><span>model</span><b>{html.escape(model)}</b></div>
    <div><span>messages</span><b>{len(records)}</b></div>
    <div><span>turns</span><b>{len(turn_results)}</b></div>
    <div><span>window</span><b>{html.escape(first)} → {html.escape(last)}</b></div>
    <div><span>tokens</span><b>{total_in:,} in / {total_out:,} out</b></div>
    <div><span>cache</span><b>{total_cached:,} cached in / {total_reasoning:,} reasoning out</b></div>
    <div><span>cost</span><b>${total_cost:.6f}</b></div>
    <div><span>session</span><b>{html.escape(session_id)}</b></div>
  </section>
  <main>
    <p><b>DB:</b> {html.escape(str(db_path))}<br><b>Hermes home:</b> {html.escape(str(hermes_home))}</p>
    {''.join(rows)}
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )


def _html_report_from_published_run(*, db_path: Path, run_id: str, output_path: Path) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            """
            SELECT * FROM tgg_christopher_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if not run:
            raise RuntimeError(f"No tgg_christopher_runs row for {run_id}")
        turns = conn.execute(
            """
            SELECT t.*, r.provider, r.model, r.status, r.turn_summary,
                   r.case_effects_json, r.actions_json, r.pending_questions_json,
                   r.lookup_json, r.model_input_json, r.model_output_json, r.error
            FROM tgg_christopher_turns t
            LEFT JOIN tgg_christopher_turn_results r
              ON r.run_id = t.run_id AND r.turn_id = t.turn_id
            WHERE t.run_id = ?
            ORDER BY t.turn_start_ts, t.turn_id
            """,
            (run_id,),
        ).fetchall()
        queue_rows = conn.execute(
            """
            SELECT * FROM tgg_christopher_message_queue
            WHERE run_id = ?
            ORDER BY ts, id
            """,
            (run_id,),
        ).fetchall()
        messages_by_turn: dict[str, list[sqlite3.Row]] = {}
        for row in queue_rows:
            turn_id = str(row["turn_id"] or "")
            if turn_id:
                messages_by_turn.setdefault(turn_id, []).append(row)

    def parse_json(raw: Any, fallback: Any) -> Any:
        if not raw:
            return fallback
        try:
            return json.loads(str(raw))
        except Exception:
            return fallback

    total_messages = len(queue_rows)
    total_media = sum(1 for row in queue_rows if int(row["has_media"] or 0))
    total_in = 0
    total_cached = 0
    total_out = 0
    total_reasoning = 0
    total_cost = 0.0
    session_ids: set[str] = set()
    row_html = []
    for index, turn in enumerate(turns, start=1):
        summary = parse_json(turn["summary_json"], {})
        model_input = parse_json(turn["model_input_json"], {})
        model_output = parse_json(turn["model_output_json"], {})
        input_tokens = _as_int(model_input.get("input_tokens") or summary.get("input_tokens"))
        cached_tokens = _as_int(model_input.get("cached_input_tokens") or summary.get("cached_input_tokens"))
        output_tokens = _as_int(model_input.get("output_tokens") or summary.get("output_tokens"))
        reasoning_tokens = _as_int(
            model_input.get("reasoning_output_tokens") or summary.get("reasoning_output_tokens")
        )
        cost = _as_number(model_input.get("estimated_cost_usd") or summary.get("estimated_cost_usd"))
        total_in += input_tokens
        total_cached += cached_tokens
        total_out += output_tokens
        total_reasoning += reasoning_tokens
        total_cost += cost
        session_id = str(summary.get("session_id") or "")
        if session_id:
            session_ids.add(session_id)
        messages = messages_by_turn.get(str(turn["turn_id"]), [])
        message_blocks = []
        for message in messages:
            media_refs = parse_json(message["media_refs_json"], [])
            media_label = f" · media {len(media_refs)}" if media_refs else ""
            text = str(message["text"] or "")
            message_blocks.append(
                f"""
                <div class="message">
                  <div class="msg-meta">{html.escape(str(message['sgt']))} · {html.escape(str(message['sender_label'] or message['sender_id'] or 'unknown'))}{html.escape(media_label)}</div>
                  <pre>{html.escape(text or '[media only]')}</pre>
                </div>
                """
            )
        assistant = str(model_output.get("assistant") or turn["turn_summary"] or "[no assistant output]")
        tools = model_output.get("tools") if isinstance(model_output.get("tools"), list) else []
        tool_names = []
        for pair in tools:
            call = pair.get("call") if isinstance(pair, dict) else None
            name = call.get("name") if isinstance(call, dict) else None
            if name:
                tool_names.append(str(name))
        effects = parse_json(turn["case_effects_json"], [])
        actions = parse_json(turn["actions_json"], [])
        questions = parse_json(turn["pending_questions_json"], [])
        row_html.append(
            f"""
            <section class="turn">
              <div class="turn-head">
                <div>
                  <span class="turn-num">turn {index}</span>
                  <b>{html.escape(str(turn['turn_start_sgt']))} → {html.escape(str(turn['turn_end_sgt']))}</b>
                </div>
                <div>{int(turn['message_count'] or 0)} msg · {int(turn['media_count'] or 0)} media</div>
              </div>
              <div class="meta">
                tools: {html.escape(', '.join(tool_names) or 'none')} ·
                {input_tokens:,} in ({cached_tokens:,} cached) / {output_tokens:,} out ({reasoning_tokens:,} reasoning)
              </div>
              <div class="cols">
                <div>
                  <h2>WhatsApp input</h2>
                  {''.join(message_blocks) or '<p class="empty">no source messages attached</p>'}
                </div>
                <div>
                  <h2>Christopher read</h2>
                  <pre class="assistant">{html.escape(assistant)}</pre>
                  <div class="chips">
                    <span>{len(effects)} case effect(s)</span>
                    <span>{len(actions)} action(s)</span>
                    <span>{len(questions)} pending question(s)</span>
                  </div>
                </div>
              </div>
            </section>
            """
        )

    first = str(queue_rows[0]["sgt"]) if queue_rows else "n/a"
    last = str(queue_rows[-1]["sgt"]) if queue_rows else "n/a"
    run_meta = parse_json(run["metadata_json"], {})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>{html.escape(run_id)} · Christopher replay review</title>
  <style>
    :root {{ color-scheme: light; --ink: #18211f; --muted: #66716d; --line: #d9ded8; --paper: #f5f2eb; --panel: #fffdf8; --wa: #0b7a67; --read: #f2edf9; --read-line: #d6c9ee; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--paper); color: var(--ink); }}
    header {{ padding: 28px 32px; background: var(--wa); color: white; }}
    h1 {{ margin: 0 0 6px; font-size: 24px; letter-spacing: 0; }}
    h2 {{ margin: 0 0 10px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
    .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; padding: 18px 32px; background: white; border-bottom: 1px solid var(--line); }}
    .summary div {{ padding: 10px 12px; background: #f3f8f6; border: 1px solid #d6e8e3; border-radius: 8px; }}
    .summary span {{ display: block; color: var(--muted); font-size: 12px; }}
    .summary b {{ display: block; margin-top: 4px; font-size: 17px; }}
    main {{ padding: 24px 32px 44px; max-width: 1320px; margin: 0 auto; }}
    .note {{ margin: 0 0 18px; color: var(--muted); }}
    .turn {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 18px; overflow: hidden; }}
    .turn-head {{ display: flex; justify-content: space-between; gap: 16px; padding: 11px 14px; background: #e7f1ee; border-bottom: 1px solid var(--line); }}
    .turn-num {{ display: block; color: var(--muted); font-size: 12px; }}
    .meta {{ padding: 8px 14px; color: var(--muted); border-bottom: 1px solid #eee8dd; }}
    .cols {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0; }}
    .cols > div {{ padding: 14px; }}
    .cols > div + div {{ border-left: 1px solid #eee8dd; background: var(--read); }}
    .message {{ background: white; border: 1px solid #e6e1d7; border-radius: 8px; margin-bottom: 10px; overflow: hidden; }}
    .msg-meta {{ padding: 7px 10px; background: #f8f7f3; color: var(--muted); font-size: 12px; border-bottom: 1px solid #eee8dd; }}
    pre {{ margin: 0; padding: 10px; white-space: pre-wrap; word-break: break-word; font: 13px/1.42 ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .assistant {{ background: var(--read); border: 1px solid var(--read-line); border-radius: 8px; }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }}
    .chips span {{ padding: 5px 8px; border-radius: 999px; background: white; border: 1px solid var(--read-line); color: #513e7c; font-size: 12px; }}
    .empty {{ color: var(--muted); margin: 0; }}
    @media (max-width: 820px) {{ .cols {{ grid-template-columns: 1fr; }} .cols > div + div {{ border-left: 0; border-top: 1px solid #eee8dd; }} header, .summary, main {{ padding-left: 16px; padding-right: 16px; }} }}
  </style>
</head>
<body>
  <header>
    <h1>Christopher replay review · MM2-SK</h1>
    <div>Hermes replay path · dry-run business writes · copied database only</div>
  </header>
  <section class="summary">
    <div><span>run</span><b>{html.escape(run_id)}</b></div>
    <div><span>model</span><b>{html.escape(str(run_meta.get('model') or 'gpt-5.4-mini'))}</b></div>
    <div><span>messages</span><b>{total_messages}</b></div>
    <div><span>turns</span><b>{len(turns)}</b></div>
    <div><span>media</span><b>{total_media}</b></div>
    <div><span>window</span><b>{html.escape(first)} → {html.escape(last)}</b></div>
    <div><span>session</span><b>{html.escape(', '.join(sorted(session_ids)) or 'n/a')}</b></div>
    <div><span>tokens</span><b>{total_in:,} in / {total_out:,} out</b></div>
    <div><span>cache</span><b>{total_cached:,} cached / {total_reasoning:,} reasoning</b></div>
    <div><span>cost</span><b>{'$' + format(total_cost, '.6f') if total_cost else 'not computed'}</b></div>
  </section>
  <main>
    <p class="note">Source DB: {html.escape(str(db_path))}. This is a local replay artifact; no production mutation.</p>
    {''.join(row_html)}
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )
    return {
        "run_id": run_id,
        "html": str(output_path),
        "messages": total_messages,
        "turns": len(turns),
        "media": total_media,
        "session_ids": sorted(session_ids),
        "input_tokens": total_in,
        "cached_input_tokens": total_cached,
        "output_tokens": total_out,
        "reasoning_output_tokens": total_reasoning,
        "estimated_cost_usd": total_cost,
    }


async def _run(args: argparse.Namespace) -> int:
    run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_label = args.run_label or f"tgg-hermes-replay-{args.chat_id.split('@', 1)[0]}-{run_stamp}"
    hermes_home = Path(args.hermes_home) if args.hermes_home else Path(tempfile.mkdtemp(prefix="tgg-hermes-replay-"))
    output_path = Path(args.output) if args.output else DOCS_DIR / f"{run_label}.html"

    _prepare_env(hermes_home, secrets=Path(args.secrets))
    _prepare_hermes_home(
        hermes_home,
        chat_id=args.chat_id,
        model=args.model,
        debounce_seconds=args.debounce_seconds,
        business_base_url=args.business_base_url,
        vision_provider=args.vision_provider,
        vision_model=args.vision_model,
        vision_concurrency=args.vision_concurrency,
    )

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not available in environment or secrets file")
    if not os.environ.get("CHRISTOPHER_TGG_PS_SERVICE_TOKEN"):
        raise RuntimeError("CHRISTOPHER_TGG_PS_SERVICE_TOKEN/BOBBY_TGG_PS_SERVICE_TOKEN is not available")

    # Import after HERMES_HOME is set; gateway/run reads it at module load.
    import gateway.run as gateway_run
    from gateway.config import Platform, load_gateway_config
    from gateway.platforms.whatsapp import WhatsAppAdapter
    from gateway.run import GatewayRunner

    records = _load_records(
        Path(args.db),
        chat_id=args.chat_id,
        since_sgt=args.since_sgt,
        until_sgt=args.until_sgt,
        limit=args.limit_messages,
        skip_messages=args.skip_messages,
    )
    if not records:
        raise RuntimeError("No bridge_message_log rows matched replay criteria")

    config = load_gateway_config()
    runner = GatewayRunner(config)
    runner._is_user_authorized = lambda source: True  # type: ignore[method-assign]

    adapter = WhatsAppAdapter(config.platforms[Platform.WHATSAPP])
    runner.adapters[Platform.WHATSAPP] = adapter

    turn_results: list[dict[str, Any]] = []
    captured_agent_actions: list[dict[str, Any]] = []
    original_record_pa_agent_action = gateway_run._record_pa_agent_action

    def capture_record_pa_agent_action(*args, **kwargs):
        captured_agent_actions.append(
            {
                "action_type": kwargs.get("action_type"),
                "status": kwargs.get("status"),
                "turn_id": kwargs.get("turn_id"),
                "cost_usd": kwargs.get("cost_usd", 0.0),
                "tokens_input": kwargs.get("tokens_input", 0),
                "tokens_output": kwargs.get("tokens_output", 0),
            }
        )
        return original_record_pa_agent_action(*args, **kwargs)

    gateway_run._record_pa_agent_action = capture_record_pa_agent_action

    handled_turns = 0

    async def handle_turn(event):
        nonlocal handled_turns
        source = event.source
        before = []
        session_id = ""
        capture_dir_raw = os.environ.get("HERMES_OPENAI_CAPTURE_DIR") or ""
        capture_dir = Path(capture_dir_raw) if capture_dir_raw else None
        response_captures_before = set(capture_dir.glob("*-response.json")) if capture_dir else set()
        if source is not None:
            entry = runner.session_store.get_or_create_session(source)
            if args.rotate_session_every_turns and handled_turns > 0 and handled_turns % args.rotate_session_every_turns == 0:
                runner.session_store.reset_session(entry.session_key, display_name=source.chat_name or source.chat_id)
                runner._evict_cached_agent(entry.session_key)
                runner._release_running_agent_state(entry.session_key)
                entry = runner.session_store.get_or_create_session(source)
            session_id = entry.session_id
            before = runner.session_store.load_transcript(session_id)
        action_start = len(captured_agent_actions)
        returned = await runner._handle_message(event)
        after = runner.session_store.load_transcript(session_id) if session_id else []
        segment = after[len(before):]
        assistant = _extract_latest_assistant(after, start=len(before))
        tools = _extract_tool_names(after, start=len(before))
        input_tokens, output_tokens = _result_usage(after, start=len(before))
        cached_input_tokens = 0
        reasoning_output_tokens = 0
        if capture_dir:
            response_captures_after = set(capture_dir.glob("*-response.json"))
            new_response_captures = sorted(response_captures_after - response_captures_before)
            capture_usage = _capture_response_usage(new_response_captures)
            if capture_usage["input_tokens"] or capture_usage["output_tokens"]:
                input_tokens = capture_usage["input_tokens"]
                cached_input_tokens = capture_usage["cached_input_tokens"]
                output_tokens = capture_usage["output_tokens"]
                reasoning_output_tokens = capture_usage["reasoning_output_tokens"]
        estimated_cost_usd = 0.0
        result_model = None
        result_provider = None
        if isinstance(returned, dict):
            input_tokens = _as_int(returned.get("input_tokens") or returned.get("prompt_tokens") or input_tokens)
            output_tokens = _as_int(returned.get("output_tokens") or returned.get("completion_tokens") or output_tokens)
            estimated_cost_usd = _as_number(returned.get("estimated_cost_usd"))
            result_model = returned.get("model")
            result_provider = returned.get("provider")
        if not input_tokens and not output_tokens:
            for action in reversed(captured_agent_actions[action_start:]):
                if action.get("action_type") != "dry-run-reply":
                    continue
                input_tokens = _as_int(action.get("tokens_input"))
                output_tokens = _as_int(action.get("tokens_output"))
                estimated_cost_usd = _as_number(action.get("cost_usd"))
                break
        turn_results.append(
            {
                "event": event,
                "returned": returned,
                "segment": segment,
                "assistant": assistant,
                "tools": tools,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": reasoning_output_tokens,
                "estimated_cost_usd": estimated_cost_usd,
                "model": result_model,
                "provider": result_provider,
                "session_id": session_id,
            }
        )
        handled_turns += 1
        if args.publish_review_run:
            _publish_review_run(
                db_path=Path(args.publish_review_db or args.db),
                run_id=args.publish_review_run,
                records=records,
                turn_results=turn_results,
                run_label=run_label,
                model=args.model,
                debounce_seconds=args.debounce_seconds,
                turn_offset=args.turn_offset,
            )

    adapter.handle_message = handle_turn  # type: ignore[method-assign]
    messages = [_record_to_bridge_message(record) for record in records]
    try:
        processed = await adapter.replay_bridge_messages(messages)
    finally:
        gateway_run._record_pa_agent_action = original_record_pa_agent_action
    if processed != len(records):
        print(f"processed {processed}/{len(records)} bridge rows", file=sys.stderr)

    session_ids = sorted({str(r.get("session_id") or "") for r in turn_results if r.get("session_id")})
    session_id = session_ids[-1] if session_ids else ""
    _html_report(
        output_path=output_path,
        run_label=run_label,
        model=args.model,
        db_path=Path(args.db),
        records=records,
        turn_results=turn_results,
        hermes_home=hermes_home,
        session_id=session_id,
    )
    published = None
    if args.publish_review_run:
        published = _publish_review_run(
            db_path=Path(args.publish_review_db or args.db),
            run_id=args.publish_review_run,
            records=records,
            turn_results=turn_results,
            run_label=run_label,
            model=args.model,
            debounce_seconds=args.debounce_seconds,
            turn_offset=args.turn_offset,
        )

    summary = {
        "run_label": run_label,
        "chat_id": args.chat_id,
        "since_sgt": args.since_sgt,
        "until_sgt": args.until_sgt,
        "messages": len(records),
        "skip_messages": args.skip_messages,
        "processed": processed,
        "turns": len(turn_results),
        "turn_offset": args.turn_offset,
        "model": args.model,
        "session_id": session_id,
        "session_ids": session_ids,
        "session_count": len(session_ids),
        "hermes_home": str(hermes_home),
        "openai_capture_dir": os.environ.get("HERMES_OPENAI_CAPTURE_DIR"),
        "html": str(output_path),
        "input_tokens": sum(_as_int(r.get("input_tokens")) for r in turn_results),
        "cached_input_tokens": sum(_as_int(r.get("cached_input_tokens")) for r in turn_results),
        "output_tokens": sum(_as_int(r.get("output_tokens")) for r in turn_results),
        "reasoning_output_tokens": sum(_as_int(r.get("reasoning_output_tokens")) for r in turn_results),
        "estimated_cost_usd": sum(_as_number(r.get("estimated_cost_usd")) for r in turn_results),
        "published": published,
    }
    print(json.dumps(summary, indent=2))
    if args.cleanup_hermes_home and not args.hermes_home:
        shutil.rmtree(hermes_home, ignore_errors=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--chat-id", default=DEFAULT_CHAT)
    parser.add_argument("--since-sgt", default=DEFAULT_SINCE)
    parser.add_argument("--until-sgt")
    parser.add_argument("--limit-messages", type=int)
    parser.add_argument("--skip-messages", type=int, default=0)
    parser.add_argument("--turn-offset", type=int, default=0)
    parser.add_argument("--debounce-seconds", type=int, default=300)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--vision-provider")
    parser.add_argument("--vision-model")
    parser.add_argument("--vision-concurrency", type=int, default=1)
    parser.add_argument("--run-label")
    parser.add_argument("--output")
    parser.add_argument("--hermes-home")
    parser.add_argument("--business-base-url")
    parser.add_argument("--publish-review-run")
    parser.add_argument("--publish-review-db")
    parser.add_argument("--render-review-run", help="Render a previously published tgg_christopher_* run without replaying")
    parser.add_argument("--rotate-session-every-turns", type=int)
    parser.add_argument("--secrets", default=str(DEFAULT_SECRETS))
    parser.add_argument("--cleanup-hermes-home", action="store_true")
    args = parser.parse_args()
    if args.render_review_run:
        output_path = Path(args.output) if args.output else DOCS_DIR / f"{args.render_review_run}.html"
        summary = _html_report_from_published_run(
            db_path=Path(args.publish_review_db or args.db),
            run_id=args.render_review_run,
            output_path=output_path,
        )
        print(json.dumps(summary, indent=2))
        return 0
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
