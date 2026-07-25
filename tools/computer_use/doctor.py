"""
`hermes computer-use doctor` — thin client for cua-driver's `health_report` MCP tool.

cua-driver owns the health model (#1908 / be761fac on `main`). This module
just drives the stdio JSON-RPC handshake, calls `health_report`, and
renders the structured response. When the driver gets new checks, they
flow through here without code changes on the Hermes side — the only
contract is the stable `schema_version="1"` payload shape.

Exit code conventions:
- 0: overall == "ok"
- 1: overall in ("degraded", "failed")
- 2: driver binary missing / unreachable / protocol error
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence


# Match the ALLOWED_STATUS_VALUES + ALLOWED_OVERALL_VALUES the cua-driver
# integration test pins. If health_report widens its vocabulary, add here.
_STATUS_GLYPH = {
    "pass": "✅",
    "fail": "❌",
    "skip": "⏭️",
}
_OVERALL_GLYPH = {
    "ok":       "✅",
    "degraded": "⚠️",
    "failed":   "❌",
}


def _cua_child_env() -> Dict[str, str]:
    """cua-driver child env with the Hermes telemetry policy applied.

    Delegates to ``cua_backend.cua_driver_child_env`` (telemetry disabled by
    default unless the user opts in). Falls back to the current environment
    if that import fails, so doctor never breaks on a telemetry-helper error.
    """
    try:
        from tools.computer_use.cua_backend import cua_driver_child_env

        return cua_driver_child_env()
    except Exception:
        return dict(os.environ)


def _sanitized_cua_env() -> Dict[str, str]:
    """Telemetry-policy env with Hermes provider secrets stripped.

    cua-driver is a third-party binary — it must never inherit provider
    API keys (#53503/#55709/#58889 lineage). Falls back to the unsanitized
    telemetry env if the sanitizer can't be imported, so doctor keeps
    working in stripped-down environments.
    """
    env = _cua_child_env()
    try:
        from tools.environments.local import _sanitize_subprocess_env

        return _sanitize_subprocess_env(env)
    except Exception:
        return env




def _read_cli_version(binary: str, *, timeout: float = 5.0) -> Optional[str]:
    """Return ``cua-driver --version`` stdout (stripped), or None on failure.

    health_report's ``driver_version`` / binary_version check can disagree
    with the actual binary (observed on Windows: health_report claims
    0.8.3 while ``--version`` and the on-disk release are 0.12.6). Doctor
    surfaces both so operators are not misled when debugging session
    issues against a "wrong" version string.
    """
    try:
        # Use Popen+communicate rather than subprocess.run so unit tests that
        # mock Popen for the MCP handshake can still patch _read_cli_version
        # independently; also swallow mock-induced ValueError/TypeError.
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=_sanitized_cua_env(),
        )
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return None
    text = (completed.stdout or completed.stderr or "").strip()
    if not text:
        return None
    # First non-empty line only — keep the banner compact.
    return text.splitlines()[0].strip()


def _normalize_version_token(text: str) -> str:
    """Pull a dotted version-ish token out of a free-form version string."""
    import re

    if not text:
        return ""
    m = re.search(r"(\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+)?)", text)
    return m.group(1) if m else text.strip().lower()


def _build_identity(binary: str, report: Dict[str, Any]) -> Dict[str, Any]:
    """Hermes-side identity block comparing resolved binary vs health_report."""
    cli = _read_cli_version(binary) or ""
    report_v = str(report.get("driver_version") or "")
    cli_tok = _normalize_version_token(cli)
    report_tok = _normalize_version_token(report_v)
    mismatch = bool(cli_tok and report_tok and cli_tok != report_tok)
    return {
        "resolved_binary": binary,
        "cli_version": cli or None,
        "health_report_driver_version": report_v or None,
        "version_mismatch": mismatch,
    }



def _drive_health_report(
    binary: str,
    *,
    include: Sequence[str] = (),
    skip: Sequence[str] = (),
    timeout: float = 12.0,
) -> Dict[str, Any]:
    """Spawn `<binary> mcp`, perform the JSON-RPC handshake, call
    `health_report`, and return the parsed `structuredContent` dict.

    Raises `RuntimeError` on a protocol-level failure (binary crash,
    malformed response, JSON-RPC error). Never raises on a `health_report`
    that has failing checks — the tool's contract is to always return a
    well-formed report with `overall` set, never to set `isError`.
    """
    args: Dict[str, Any] = {}
    if include:
        args["include"] = list(include)
    if skip:
        args["skip"] = list(skip)

    # cua-driver emits UTF-8 (containing emoji in check messages on macOS
    # and arbitrary file paths on Windows). The Python default
    # text-mode encoding follows the system locale — `cp1252` on a
    # default Windows install — which raises UnicodeDecodeError on the
    # first non-ASCII byte. Pin the codec.
    proc = subprocess.Popen(
        [binary, "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=_sanitized_cua_env(),
    )
    try:
        # 1. initialize
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "initialize", "params": {},
        }) + "\n")
        proc.stdin.flush()
        init_line = proc.stdout.readline()
        if not init_line:
            stderr_tail = (proc.stderr.read() or "").strip().splitlines()[-3:]
            raise RuntimeError(
                f"cua-driver mcp produced no initialize response. "
                f"stderr tail: {stderr_tail or '(empty)'}"
            )

        # 2. tools/call health_report
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 2,
            "method": "tools/call",
            "params": {"name": "health_report", "arguments": args},
        }) + "\n")
        proc.stdin.flush()
        call_line = proc.stdout.readline()
        if not call_line:
            raise RuntimeError("cua-driver mcp closed stdout without responding to health_report.")
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    try:
        resp = json.loads(call_line)
    except (ValueError, TypeError) as e:
        raise RuntimeError(f"health_report response was not valid JSON: {e}\nraw: {call_line[:200]}")

    if "error" in resp:
        raise RuntimeError(f"health_report JSON-RPC error: {resp['error']}")

    result = resp.get("result") or {}

    # Preferred: structuredContent (cua-driver-rs always emits it on the
    # health_report response). Fall back to parsing the first text item
    # as JSON for older cua-driver builds that didn't carry structuredContent.
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        return sc

    for item in result.get("content", []):
        if item.get("type") == "text":
            text = item.get("text", "")
            try:
                # Many health_report payloads ship JSON in the text item too.
                parsed = json.loads(text)
                if isinstance(parsed, dict) and "schema_version" in parsed:
                    return parsed
            except (ValueError, TypeError):
                pass

    raise RuntimeError(
        "health_report response carried neither structuredContent nor a parseable "
        f"JSON text block. Result keys: {list(result.keys())}"
    )


def _print_text_report(
    report: Dict[str, Any],
    color: bool,
    *,
    identity: Optional[Dict[str, Any]] = None,
) -> None:
    """Render the report in the same style as `cua-driver call health_report`
    would (one line per check + a summary footer).

    When *identity* is provided (resolved binary + ``--version``), the header
    prefers the CLI version if health_report's ``driver_version`` disagrees,
    and a short identity block is printed under the header.
    """
    schema = report.get("schema_version", "?")
    platform = report.get("platform", "?")
    report_v = report.get("driver_version", "?")
    overall = report.get("overall", "?")
    identity = identity or {}
    cli_v = identity.get("cli_version") or ""
    mismatch = bool(identity.get("version_mismatch"))
    # Prefer the binary's own --version when health_report is wrong/stale.
    header_v = cli_v or report_v

    header_glyph = _OVERALL_GLYPH.get(overall, "•")

    if color and overall in _OVERALL_GLYPH:
        # No external color library — keep ANSI inline so the doctor
        # command stays a single self-contained module.
        col_red = "\033[31m"
        col_yellow = "\033[33m"
        col_green = "\033[32m"
        col_reset = "\033[0m"
        col_dim = "\033[2m"
        col_for = {"failed": col_red, "degraded": col_yellow, "ok": col_green}.get(overall, "")
    else:
        col_red = col_yellow = col_green = col_reset = col_dim = ""
        col_for = ""

    print(
        f"{header_glyph} cua-driver {header_v} on {platform} — "
        f"{col_for}{overall}{col_reset}"
    )
    if identity.get("resolved_binary"):
        print(f"  {col_dim}binary: {identity['resolved_binary']}{col_reset}")
    if cli_v and report_v and str(report_v) not in str(cli_v) and str(cli_v) not in str(report_v):
        # Only annotate when the free-form strings clearly differ.
        print(
            f"  {col_dim}--version: {cli_v}{col_reset}"
        )
        print(
            f"  {col_dim}health_report.driver_version: {report_v}{col_reset}"
        )
    elif cli_v and not mismatch:
        # Still show the resolved path; version already matches header.
        pass
    if mismatch:
        warn = col_yellow if color else ""
        print(
            f"  {warn}⚠️ version mismatch: health_report says {report_v!r} "
            f"but binary --version is {cli_v!r}{col_reset}"
        )
        print(
            f"  {col_dim}→ trust --version / packages/current for debugging; "
            f"health_report's binary_version check can lag on Windows{col_reset}"
        )

    for check in report.get("checks", []):
        name = check.get("name", "?")
        status = check.get("status", "?")
        glyph = _STATUS_GLYPH.get(status, "•")
        message = check.get("message") or ""
        if color:
            status_col = {
                "pass": col_green, "fail": col_red, "skip": col_dim,
            }.get(status, "")
            print(f"  {glyph} {status_col}{name}{col_reset}: {message}")
        else:
            print(f"  {glyph} {name}: {message}")
        hint = check.get("hint")
        if hint:
            print(f"      → {col_dim}{hint}{col_reset}")
        # `data` is the structured payload some checks attach (bundle id,
        # AX permission state, version triple, etc.). Surface when present
        # because users / support staff frequently need it.
        data = check.get("data")
        if isinstance(data, dict) and data:
            for key, value in data.items():
                rendered = value if not isinstance(value, (dict, list)) else json.dumps(value)
                print(f"      {col_dim}{key}={rendered}{col_reset}")
    _ = schema  # acknowledge field for forward-compat readers


def run_doctor(
    driver_cmd: Optional[str] = None,
    *,
    include: Sequence[str] = (),
    skip: Sequence[str] = (),
    json_output: bool = False,
    color: Optional[bool] = None,
) -> int:
    """Resolve the cua-driver binary, call `health_report`, render the result.

    Honors `HERMES_CUA_DRIVER_CMD` via the shared runtime resolver, so the
    doctor diagnoses what your `computer_use` toolset will actually invoke.
    """
    # Windows ships stdout/stderr wrapped with the system ANSI codec
    # (`cp1252` on a US locale, `cp936` on zh-CN, etc.). The check-matrix
    # output below contains ✅ ❌ ⚠️ ⏭️ glyphs — none of them encodable
    # in those codepages. Switch stdout to UTF-8 once, idempotently: every
    # supported TextIOWrapper (Py3.7+) has `.reconfigure`, and a no-op
    # re-encode is cheap if we were already UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass
    from tools.computer_use.cua_backend import resolve_cua_driver_cmd

    binary = resolve_cua_driver_cmd(driver_cmd)
    if not binary:
        looked_for = driver_cmd or "cua-driver (PATH and canonical install paths)"
        print(f"cua-driver: not installed (looked for {looked_for!r}).")
        print("  Run: hermes computer-use install")
        return 2

    try:
        report = _drive_health_report(binary, include=include, skip=skip)
    except RuntimeError as e:
        print(f"cua-driver health_report failed: {e}", file=sys.stderr)
        return 2

    identity = _build_identity(binary, report)

    if json_output:
        # Additive envelope: preserve the upstream health_report keys and
        # attach Hermes identity under hermes_identity so existing parsers
        # that only read overall/checks keep working.
        payload = dict(report)
        payload["hermes_identity"] = identity
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        if color is None:
            color = sys.stdout.isatty()
        _print_text_report(report, color=bool(color), identity=identity)

    overall = report.get("overall")
    if overall in ("degraded", "failed"):
        return 1
    return 0
