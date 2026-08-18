"""Metadata-only Kanban summary delivery.

This module is deliberately a small read-only boundary around the existing
Kanban CLI and ``hermes send`` command.  It never forwards task bodies,
summaries, raw run metadata, credentials, or filesystem paths.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterator, Optional

from hermes_cli.kanban_db import kanban_db_path
from hermes_cli.config import load_config_readonly


SUMMARY_SCHEMA = "hermes.kanban.summary.v1"
STATE_SCHEMA = "hermes.kanban.summary.state.v1"
TOKEN_SCHEMA = "hermes.worker.v1"
MAX_RUNS = 10_000
WINDOW_DAYS = 90
MAX_STATE_BYTES = 512
MAX_STRING_CHARS = 128
MAX_STRING_BYTES = 512
MAX_INTEGER = 10**12
MAX_COST_MICROS = 10**15
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class SummaryError(ValueError):
    """Expected, non-sensitive operator/configuration error."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _safe_string(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = unicodedata.normalize("NFKC", value)
    if not value or len(value) > MAX_STRING_CHARS or len(value.encode("utf-8")) > MAX_STRING_BYTES:
        return None
    if _CONTROL_RE.search(value):
        return None
    return value


def _nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_INTEGER:
        return None
    return value


def _cost_micros(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    micros = int((parsed * Decimal("1000000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return micros if micros <= MAX_COST_MICROS else None


def validate_token_usage(raw: Any) -> tuple[Optional[dict[str, Any]], str | None]:
    """Validate a complete worker token record or reject it as a whole."""
    if not isinstance(raw, dict):
        return None, "not_object"
    if raw.get("schema") != TOKEN_SCHEMA:
        return None, "schema"
    provider = _safe_string(raw.get("provider"))
    model = _safe_string(raw.get("model"))
    source = raw.get("source")
    if provider is None or model is None or source != "worker_reported":
        return None, "identity"
    required = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        parsed = _nonnegative_int(raw.get(key))
        if parsed is None:
            return None, f"{key}"
        required[key] = parsed
    optional: dict[str, int] = {}
    for key in ("cache_read_tokens", "cache_write_tokens"):
        if key in raw:
            parsed = _nonnegative_int(raw[key])
            if parsed is None:
                return None, key
            optional[key] = parsed
    cost = _cost_micros(raw.get("estimated_cost_usd"))
    if "estimated_cost_usd" in raw and raw["estimated_cost_usd"] is not None and cost is None:
        return None, "estimated_cost_usd"
    record: dict[str, Any] = {
        "schema": TOKEN_SCHEMA,
        "provider": provider,
        "model": model,
        **required,
        **optional,
        "source": "worker_reported",
    }
    if cost is not None:
        record["estimated_cost_usd_micros"] = cost
    return record, None


def _run_token_rollup(rows: list[dict[str, Any]], *, truncated: bool) -> dict[str, Any]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    optional_totals = {"cache_read_tokens": 0, "cache_write_tokens": 0}
    by_provider_model: dict[str, dict[str, int]] = {}
    invalid = 0
    anomaly = 0
    cost_by_provider_model: dict[str, int] = {}
    for row in rows:
        metadata = row.get("metadata")
        usage = metadata.get("token_usage") if isinstance(metadata, dict) else None
        if usage is None:
            continue
        valid, _reason = validate_token_usage(usage)
        if valid is None:
            invalid += 1
            continue
        for key in totals:
            totals[key] += valid[key]
        for key in optional_totals:
            optional_totals[key] += valid.get(key, 0)
        key = f"{valid['provider']}/{valid['model']}"
        bucket = by_provider_model.setdefault(key, {**totals.fromkeys(totals, 0), **optional_totals.fromkeys(optional_totals, 0)})
        for name in (*totals, *optional_totals):
            bucket[name] += valid.get(name, 0)
        if "estimated_cost_usd_micros" in valid:
            cost_by_provider_model[key] = cost_by_provider_model.get(key, 0) + valid["estimated_cost_usd_micros"]
        expected = valid["input_tokens"] + valid["output_tokens"]
        expected += valid.get("cache_read_tokens", 0) + valid.get("cache_write_tokens", 0)
        if abs(valid["total_tokens"] - expected) > max(int(max(valid["total_tokens"], expected) * 0.10), 1024):
            anomaly += 1
    result: dict[str, Any] = {
        "window_days": WINDOW_DAYS,
        "max_runs": MAX_RUNS,
        "scanned_runs": len(rows),
        "truncated": truncated,
        "valid_records": sum(1 for row in rows if isinstance(row.get("metadata"), dict) and row["metadata"].get("token_usage") is not None) - invalid,
        "invalid_token_usage_count": invalid,
        "accounting_anomaly_count": anomaly,
        **totals,
        **optional_totals,
        "by_provider_model": {k: by_provider_model[k] for k in sorted(by_provider_model)},
    }
    if cost_by_provider_model:
        result["estimated_cost_usd_micros_by_provider_model"] = {
            k: cost_by_provider_model[k] for k in sorted(cost_by_provider_model)
        }
    return result


def load_recent_run_metadata(board: str, *, now: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    """Read only the bounded task-run metadata projection from SQLite."""
    path = kanban_db_path(board)
    if not path.exists():
        return [], False
    now = int(time.time()) if now is None else int(now)
    cutoff = now - WINDOW_DAYS * 86400
    rows: list[dict[str, Any]] = []
    truncated = False
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            query = (
                "SELECT r.metadata, r.ended_at FROM task_runs r "
                "JOIN tasks t ON t.id = r.task_id "
                "WHERE t.status != 'archived' AND r.ended_at IS NOT NULL "
                "AND r.ended_at >= ? ORDER BY r.ended_at DESC, r.id DESC LIMIT ?"
            )
            fetched = conn.execute(query, (cutoff, MAX_RUNS + 1)).fetchall()
            truncated = len(fetched) > MAX_RUNS
            for row in fetched[:MAX_RUNS]:
                try:
                    metadata = json.loads(row["metadata"]) if row["metadata"] else None
                except (TypeError, ValueError):
                    metadata = None
                rows.append({"metadata": metadata})
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return [], False
    return rows, truncated


def _run_hermes_json(kind: str, board: str) -> Any:
    command = [sys.executable, "-m", "hermes_cli.main", "kanban", "--board", board, kind, "--json"]
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
    if completed.returncode != 0:
        raise SummaryError(f"{kind}_failed")
    try:
        return json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise SummaryError(f"{kind}_invalid_json") from exc


def _safe_task_id(value: Any) -> Optional[str]:
    return _safe_string(value)


def build_summary(board: str, *, now: int | None = None) -> dict[str, Any]:
    stats = _run_hermes_json("stats", board)
    if not isinstance(stats, dict) or not isinstance(stats.get("by_status"), dict):
        raise SummaryError("stats_invalid")
    queue_counts: dict[str, int] = {}
    for status, count in stats["by_status"].items():
        safe_status = _safe_string(status)
        safe_count = _nonnegative_int(count)
        if safe_status is None or safe_count is None:
            raise SummaryError("stats_invalid")
        queue_counts[safe_status] = safe_count
    diagnostics = _run_hermes_json("diagnostics", board)
    if not isinstance(diagnostics, list):
        raise SummaryError("diagnostics_invalid")
    projected: list[dict[str, str]] = []
    for group in diagnostics:
        if not isinstance(group, dict):
            continue
        task_id = _safe_task_id(group.get("task_id"))
        entries = group.get("diagnostics")
        if task_id is None or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            severity = _safe_string(entry.get("severity"))
            rule = _safe_string(entry.get("kind"))
            if severity in {"warning", "error", "critical"} and rule is not None:
                projected.append({"task_id": task_id, "severity": severity, "rule": rule})
    projected.sort(key=lambda item: (item["task_id"], item["severity"], item["rule"]))
    run_rows, truncated = load_recent_run_metadata(board, now=now)
    return {
        "schema": SUMMARY_SCHEMA,
        "board": _safe_string(board) or "default",
        "queue_counts": {key: queue_counts[key] for key in sorted(queue_counts)},
        "diagnostics": projected,
        "token_rollup": _run_token_rollup(run_rows, truncated=truncated),
    }


def summary_fingerprint(summary: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(summary)).hexdigest()


def _target_from_config(config: dict[str, Any]) -> Optional[str]:
    section = config.get("kanban", {}) if isinstance(config, dict) else {}
    summary = section.get("summary", {}) if isinstance(section, dict) else {}
    target = summary.get("telegram_target") if isinstance(summary, dict) else None
    return target if isinstance(target, str) else None


def validate_target(target: Any) -> Optional[str]:
    target = _safe_string(target)
    if target is None or not target.startswith("telegram"):
        return None
    if target != "telegram" and not target.startswith("telegram:"):
        return None
    return target


def _instance_key(board: str, target: str) -> str:
    return hashlib.sha256(f"{board}\0{target}".encode("utf-8")).hexdigest()[:32]


def _state_paths(state_dir: Path, board: str, target: str) -> tuple[Path, Path]:
    key = _instance_key(board, target)
    return state_dir / f"{key}.json", state_dir / f"{key}.lock"


def _read_state(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        if path.stat().st_size > MAX_STATE_BYTES:
            raise SummaryError("state_oversized")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except SummaryError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise SummaryError("state_unreadable") from exc
    if not isinstance(raw, dict) or raw.get("schema") != STATE_SCHEMA:
        raise SummaryError("state_schema")
    fingerprint = raw.get("fingerprint")
    last_success = raw.get("last_success_at")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise SummaryError("state_fingerprint")
    if _nonnegative_int(last_success) is None:
        raise SummaryError("state_timestamp")
    return {"schema": STATE_SCHEMA, "fingerprint": fingerprint, "last_success_at": last_success}


def _write_state(path: Path, fingerprint: str, now: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    payload = {"schema": STATE_SCHEMA, "fingerprint": fingerprint, "last_success_at": int(now)}
    encoded = _canonical_json(payload) + b"\n"
    if len(encoded) > MAX_STATE_BYTES:
        raise SummaryError("state_oversized")
    fd, temp_name = tempfile.mkstemp(prefix=".summary-", dir=path.parent)
    try:
        os.chmod(temp_name, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if fd != -1:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _instance_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (BlockingIOError, OSError):
            locked = False
        yield locked
    finally:
        if locked:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _send(target: str, message: str) -> bool:
    command = [sys.executable, "-m", "hermes_cli.main", "send", "--to", target, message, "--quiet", "--json"]
    completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
    return completed.returncode == 0


def format_message(summary: dict[str, Any]) -> str:
    counts = summary["queue_counts"]
    queue = ", ".join(f"{key}={counts[key]}" for key in sorted(counts)) or "none"
    token = summary["token_rollup"]
    diagnostics = summary["diagnostics"]
    lines = [
        "Hermes Kanban summary",
        f"board={summary['board']}",
        f"queue: {queue}",
        f"diagnostics: {len(diagnostics)}",
        "tokens: "
        f"input={token['input_tokens']} output={token['output_tokens']} "
        f"total={token['total_tokens']} valid={token['valid_records']} "
        f"invalid={token['invalid_token_usage_count']}",
    ]
    if token["truncated"]:
        lines.append(f"token window truncated at {token['max_runs']} runs")
    return "\n".join(lines)


def run_once(*, board: str, target: str, state_dir: Path, now: int | None = None) -> str:
    target = validate_target(target)
    board = _safe_string(board)
    if target is None or board is None:
        return "invalid_target_or_board"
    now = int(time.time()) if now is None else int(now)
    state_path, lock_path = _state_paths(state_dir, board, target)
    with _instance_lock(lock_path) as locked:
        if not locked:
            return "lock_held"
        summary = build_summary(board, now=now)
        fingerprint = summary_fingerprint(summary)
        try:
            state = _read_state(state_path)
        except SummaryError as exc:
            return str(exc)
        if state is not None and state["fingerprint"] == fingerprint:
            return "unchanged"
        if not _send(target, format_message(summary)):
            return "send_failed"
        _write_state(state_path, fingerprint, now)
        return "sent"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send a metadata-only Kanban summary when state changes.")
    parser.add_argument("--board", default="default")
    parser.add_argument("--target", default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    config = load_config_readonly()
    target = validate_target(args.target or _target_from_config(config))
    if target is None:
        print("kanban_summary: target_missing_or_invalid", file=sys.stderr)
        return 2
    state_dir = args.state_dir or (Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "kanban" / "summary-state")
    try:
        result = run_once(board=args.board, target=target, state_dir=state_dir)
    except SummaryError as exc:
        print(f"kanban_summary: {exc}", file=sys.stderr)
        return 1
    print(result)
    return 0 if result in {"sent", "unchanged", "lock_held"} else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
