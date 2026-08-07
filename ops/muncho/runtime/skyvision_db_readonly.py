#!/usr/bin/env python3
"""Bounded SkyVision read-only database operation.

The trusted model selects the exact normal or sensitive catalog operation and
authors the SQL plus any exact output fields that must be redacted.  This
helper does not infer sensitivity, business meaning, or data categories from
SQL, column names, or prose.  It enforces only SQL/read-only structure,
credential scope, row/time bounds, and exact model-authored redactions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import selectors
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any


DEFAULT_SSH_HELPER = "/opt/adventico-ai-platform/hermes-home/bin/ssh-alwyzon-phoenix"
DB_CONFIGS = {
    "skyvisio_laravel": {
        "secret_file": Path(
            "/opt/adventico-ai-platform/hermes-home/secrets/"
            "skyvision_db_readonly_laravel.env"
        ),
        "user_env": "SKYVISION_DB_READONLY_LARAVEL_USER",
        "password_env": "SKYVISION_DB_READONLY_LARAVEL_PASSWORD",
        "database_env": "SKYVISION_DB_READONLY_LARAVEL_DB",
        "ssh_helper_env": "SKYVISION_DB_READONLY_LARAVEL_SSH_HELPER",
        "default_user": "muncho_ro_laravel",
        "secret_handle": ("skyvision/db-readonly/skyvisio_laravel/muncho_ro_laravel"),
    },
    "skyvisio_wp64": {
        "secret_file": Path(
            "/opt/adventico-ai-platform/hermes-home/secrets/"
            "skyvision_db_readonly_wp64.env"
        ),
        "user_env": "SKYVISION_DB_READONLY_WP64_USER",
        "password_env": "SKYVISION_DB_READONLY_WP64_PASSWORD",
        "database_env": "SKYVISION_DB_READONLY_WP64_DB",
        "ssh_helper_env": "SKYVISION_DB_READONLY_WP64_SSH_HELPER",
        "default_user": "skyvisio_munro64",
        "secret_handle": ("skyvision/db-readonly/skyvisio_wp64/skyvisio_munro64"),
    },
    "skyvisio_fp": {
        "secret_file": Path(
            "/opt/adventico-ai-platform/hermes-home/secrets/"
            "skyvision_db_readonly_fp.env"
        ),
        "user_env": "SKYVISION_DB_READONLY_FP_USER",
        "password_env": "SKYVISION_DB_READONLY_FP_PASSWORD",
        "database_env": "SKYVISION_DB_READONLY_FP_DB",
        "ssh_helper_env": "SKYVISION_DB_READONLY_FP_SSH_HELPER",
        "default_user": "skyvisio_munrofp",
        "secret_handle": "skyvision/db-readonly/skyvisio_fp/skyvisio_munrofp",
    },
}
ALLOWED_DBS = tuple(sorted(DB_CONFIGS))
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
READ_CHUNK_BYTES = 64 * 1024

# These expressions validate SQL grammar/safety only.  They do not classify
# business intent or data sensitivity.
FORBIDDEN_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP|TRUNCATE|CREATE|"
    r"LOCK\s+TABLES|GRANT|REVOKE|LOAD\s+DATA|INTO\s+OUTFILE|"
    r"INTO\s+DUMPFILE|CALL)\b",
    re.IGNORECASE,
)
ALLOWED_START_RE = re.compile(
    r"^\s*(SELECT|EXPLAIN|DESCRIBE|DESC)\b",
    re.IGNORECASE,
)


class ProcessOutputBoundExceeded(RuntimeError):
    """One exact child output stream exceeded its mechanical byte bound."""

    def __init__(self, stream: str, maximum: int):
        super().__init__(f"{stream}_bound_exceeded")
        self.stream = stream
        self.maximum = maximum


def emit(data: dict[str, Any], code: int = 0) -> None:
    print(json.dumps(data, ensure_ascii=False, sort_keys=True))
    raise SystemExit(code)


def fail(reason: str, **extra: Any) -> None:
    emit({"ok": False, "status": "BLOCKED", "reason": reason, **extra}, 2)


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def normalized_query(query: str) -> str:
    return query.strip().rstrip(";")


def query_hash(query: str) -> str:
    return hashlib.sha256(normalized_query(query).encode("utf-8")).hexdigest()


def validate_query(query: str, expected_shape: str, max_rows: int) -> str:
    """Enforce the exact read-only SQL transport contract."""

    normalized = normalized_query(query)
    if ";" in normalized:
        fail("multi_statement_not_allowed")
    if ALLOWED_START_RE.search(normalized) is None:
        fail("query_not_readonly_allowed_start")
    if FORBIDDEN_SQL_RE.search(normalized) is not None:
        fail("forbidden_sql_token")
    row_returning = normalized.upper().startswith("SELECT") and expected_shape not in {
        "scalar",
        "aggregate_report",
        "schema_metadata",
    }
    if row_returning and re.search(r"\bLIMIT\s+\d+\b", normalized, re.I) is None:
        fail("limit_required_for_row_returning_query")
    if max_rows < 1 or max_rows > 500:
        fail("max_rows_out_of_bounds", hard_max_rows=500)
    return normalized


def parse_tsv(
    stdout: str,
    max_rows: int,
    redact_fields: tuple[str, ...],
) -> tuple[list[str], list[dict[str, Any]], int, bool]:
    """Parse bounded TSV and apply only exact model-authored field redactions."""

    if not stdout.strip():
        return [], [], 0, False
    rows = list(csv.reader(stdout.splitlines(), delimiter="\t"))
    if not rows:
        return [], [], 0, False
    headers = rows[0]
    unknown = sorted(set(redact_fields) - set(headers))
    if unknown:
        fail("redact_field_not_in_result", unknown_fields=unknown)
    redact = set(redact_fields)
    parsed: list[dict[str, Any]] = []
    redactions = 0
    truncated = False
    for raw_row in rows[1:]:
        if len(parsed) >= max_rows:
            truncated = True
            break
        item: dict[str, Any] = {}
        for header, value in zip(headers, raw_row):
            if header in redact:
                item[header] = "[REDACTED]"
                redactions += 1
            else:
                item[header] = value
        parsed.append(item)
    return headers, parsed, redactions, truncated


def _run_bounded_process(
    argv: list[str],
    *,
    input_text: str,
    timeout_seconds: int,
    stdout_limit: int = MAX_STDOUT_BYTES,
    stderr_limit: int = MAX_STDERR_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Run one child while enforcing hard incremental output limits."""

    process = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise RuntimeError("bounded_process_pipe_unavailable")
    streams = {
        "stdout": (process.stdout, stdout_limit),
        "stderr": (process.stderr, stderr_limit),
    }
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout_seconds
    try:
        process.stdin.write(input_text.encode("utf-8"))
        process.stdin.close()
        for name, (stream, maximum) in streams.items():
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, (name, maximum))
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout_seconds)
            events = selector.select(min(remaining, 0.25))
            for key, _mask in events:
                name, maximum = key.data
                chunk = os.read(key.fileobj.fileno(), READ_CHUNK_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                if len(buffers[name]) + len(chunk) > maximum:
                    raise ProcessOutputBoundExceeded(name, maximum)
                buffers[name].extend(chunk)
        returncode = process.wait(max(0.001, deadline - time.monotonic()))
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        for stream, _maximum in streams.values():
            if not stream.closed:
                stream.close()
        if not process.stdin.closed:
            process.stdin.close()
    try:
        stdout = bytes(buffers["stdout"]).decode("utf-8", errors="strict")
        stderr = bytes(buffers["stderr"]).decode("utf-8", errors="replace")
    except UnicodeError as exc:
        raise RuntimeError("mysql_output_encoding_invalid") from exc
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def run_mysql_over_ssh(
    query: str,
    args: argparse.Namespace,
) -> tuple[subprocess.CompletedProcess[str], int]:
    config = DB_CONFIGS.get(args.db)
    if config is None:
        fail("db_not_in_phase_scope", allowed_dbs=list(ALLOWED_DBS))
    secret_file = config["secret_file"]
    if not secret_file.exists():
        fail("secret_handle_unavailable", handle=config["secret_handle"])
    envfile = load_env_file(secret_file)
    password = envfile.get(config["password_env"])
    user = envfile.get(config["user_env"], config["default_user"])
    database = envfile.get(config["database_env"], args.db)
    ssh_helper = envfile.get(config["ssh_helper_env"], DEFAULT_SSH_HELPER)
    if not password:
        fail("secret_missing_password")
    if not Path(ssh_helper).exists():
        fail("ssh_helper_unavailable")
    # The secret is carried only on the SSH process stdin.  It never appears
    # in the local process argv, systemd journal command line, or remote shell
    # argv.  The remote shell immediately replaces itself with mysql.
    remote_script = (
        "set -eu\n"
        f"MYSQL_PWD={shlex.quote(password)}\n"
        "export MYSQL_PWD\n"
        f"exec mysql -u {shlex.quote(user)} --batch --raw "
        "--default-character-set=utf8mb4 "
        f"--database {shlex.quote(database)} -e {shlex.quote(query)}\n"
    )
    started = time.time()
    proc = _run_bounded_process(
        [ssh_helper, "sh -s"],
        input_text=remote_script,
        timeout_seconds=args.timeout_seconds,
    )
    return proc, int((time.time() - started) * 1000)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--db", required=True, choices=ALLOWED_DBS)
    result.add_argument("--query", required=True)
    result.add_argument("--case-id", required=True)
    result.add_argument("--requester", required=True)
    result.add_argument("--requester-id", default="")
    result.add_argument("--purpose", required=True)
    result.add_argument(
        "--expected-result-shape",
        default="bounded_rows",
        choices=(
            "scalar",
            "single_row",
            "bounded_rows",
            "aggregate_report",
            "schema_metadata",
        ),
    )
    result.add_argument("--max-rows", type=int, default=100)
    result.add_argument("--timeout-seconds", type=int, default=10)
    result.add_argument(
        "--sensitivity",
        required=True,
        choices=("normal", "sensitive"),
    )
    result.add_argument("--redact-field", action="append", default=[])
    return result


def main() -> None:
    args = parser().parse_args()
    query = validate_query(
        args.query,
        args.expected_result_shape,
        args.max_rows,
    )
    started = time.time()
    try:
        proc, duration_ms = run_mysql_over_ssh(query, args)
    except ProcessOutputBoundExceeded as exc:
        fail(
            "mysql_output_bound_exceeded",
            query_hash=query_hash(query),
            stream=exc.stream,
            maximum_bytes=exc.maximum,
        )
    except subprocess.TimeoutExpired:
        fail(
            "mysql_query_timeout",
            query_hash=query_hash(query),
            timeout_seconds=args.timeout_seconds,
        )
    if proc.returncode != 0:
        fail(
            "mysql_query_failed",
            query_hash=query_hash(query),
            stderr_bytes=len(proc.stderr.encode("utf-8")),
            stderr_sha256=hashlib.sha256(proc.stderr.encode("utf-8")).hexdigest(),
            duration_ms=int((time.time() - started) * 1000),
        )
    headers, rows, redactions, truncated = parse_tsv(
        proc.stdout,
        args.max_rows,
        tuple(args.redact_field),
    )
    emit({
        "ok": True,
        "status": "PASS",
        "db": args.db,
        "case_id": args.case_id,
        "requester": args.requester,
        "requester_id": args.requester_id or None,
        "purpose_hash": hashlib.sha256(args.purpose.encode("utf-8")).hexdigest(),
        "query_hash": query_hash(query),
        "sensitivity": args.sensitivity,
        "expected_result_shape": args.expected_result_shape,
        "max_rows": args.max_rows,
        "duration_ms": duration_ms,
        "headers": headers,
        "rows_returned": len(rows),
        "rows": rows,
        "truncated": truncated,
        "redactions": redactions,
        "audit": {
            "readonly": True,
            "sensitivity_selected_by_model": True,
            "exact_redaction_fields_selected_by_model": list(args.redact_field),
            "no_secret_output": True,
            "route": ("Cloud Muncho -> ssh-alwyzon-phoenix -> phoenix MySQL localhost"),
        },
    })


if __name__ == "__main__":
    main()
