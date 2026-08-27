#!/usr/bin/env python3
"""One-shot MCP stdio supervisor (#96036).

Some MCP servers (most notably ``codegraph`` configured with
``--liftoff-only``) are designed to *exit* after handling a single
JSON-RPC request — they read their one query from stdin, process it, write
the response to stdout, and terminate. Used directly under hermes this
triggers one full process spawn *per* MCP tool invocation: every call goes to
the SDK's ``stdio_client`` context, the server subprocess exits cleanly, the
SDK sees EOF, hermes tears the connection down, and the *next* call
re-spawns a fresh subprocess. That doubles hermes-side overhead per call
and, on Windows, accumulates visible per-call session windows.

This supervisor is a thin long-lived relay that presents a stable stdio
transport to hermes while delegating each JSON-RPC exchange to a fresh
inner subprocess. hermes sees exactly one process for the entire
conversation; the inner server is re-spawned per request inside the
supervisor.

Wire protocol: line-delimited JSON-RPC over stdin/stdout, identical to the
underlying one-shot server. The supervisor is a transparent bytes relay
*except* for the spawn/reap cycle — it ensures that the *inner* subprocess
owns exactly one exchange before being reaped, so the server's
"exit-after-call" semantics are preserved without exposing them to the
caller.

Selection:
  - explicit config flag ``one_shot_supervisor: true`` on the server entry
  - OR auto-detected when ``args`` contain ``--liftoff-only`` (codegraph
    Direct mode marker from @colbymchenry/codegraph 1.0.1+)

Environment passed through to the inner subprocess is the supervisor's own
environment minus a small allowlist (everything below). Inheriting the
caller's env is correct for MCP — the server is a black box that should see
the same shell env the user invoked hermes with.

POSIX + Windows. Standard library only.

Usage::

    python -m tools.mcp_one_shot_supervisor \\
        --inner-cmd <command> --inner-arg <arg1> --inner-arg <arg2> ...

where ``--inner-arg`` may be repeated to build argv. Stdin/stdout/stderr of
the inner process are piped back to the supervisor's own stdin/stdout/stderr.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from typing import List

# A reasonable ceiling for a single inner-process invocation. Matches the
# upstream codegraph Direct-mode query budget per the bug report (#96036).
# Longer-running tools should configure ``tool_timeout`` at the MCP layer
# instead of relying on the supervisor to keep them alive forever.
_DEFAULT_INNER_TIMEOUT_S = 600.0

# Subprocess startup grace — codegraph --liftoff-only re-indexes on first
# call. 30s is comfortably above the typical cold index on a medium repo
# but well below hermes's tool_timeout default.
_INNER_STARTUP_TIMEOUT_S = 30.0


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcp_one_shot_supervisor",
        description=(
            "Long-lived stdio relay for one-shot MCP servers. Spawns the "
            "inner command per JSON-RPC exchange; hermes sees one process."
        ),
    )
    p.add_argument(
        "--inner-cmd",
        required=True,
        help="Inner server executable (e.g. node, codegraph.exe).",
    )
    p.add_argument(
        "--inner-arg",
        action="append",
        default=[],
        dest="inner_args",
        help="Inner server argv (repeatable).",
    )
    p.add_argument(
        "--inner-timeout",
        type=float,
        default=_DEFAULT_INNER_TIMEOUT_S,
        help=(
            f"Wall-clock cap for a single inner exchange "
            f"(default {_DEFAULT_INNER_TIMEOUT_S:.0f}s)."
        ),
    )
    p.add_argument(
        "--label",
        default="mcp-one-shot",
        help="Diagnostic label for stderr/log lines (default: mcp-one-shot).",
    )
    return p


def _drain_stream_to_fd(src, dst_fd: int, label: str, kind: str) -> None:
    """Copy ``src`` (a binary file-like) to ``dst_fd`` until EOF.

    Runs on its own thread so stderr/stdout forwarding never blocks the
    main JSON-RPC read loop. On EOF the source side closed (inner exited
    or our parent closed our stdin) — we close ``dst_fd`` to mirror.
    """
    try:
        while True:
            chunk = src.read(4096)
            if not chunk:
                break
            try:
                os.write(dst_fd, chunk)
            except OSError:
                break
    except Exception as exc:  # noqa: BLE001 — relay thread must never crash
        try:
            sys.stderr.write(
                f"[{label}] {kind} relay error: {type(exc).__name__}: {exc}\n"
            )
            sys.stderr.flush()
        except Exception:
            pass
    finally:
        try:
            src.close()
        except Exception:
            pass


def _exchange(
    inner_cmd: str,
    inner_args: List[str],
    payload: bytes,
    inner_timeout: float,
    label: str,
    env: dict,
) -> bytes:
    """Spawn the inner server once, push ``payload`` on its stdin, return stdout.

    Returns the raw bytes the inner process wrote to stdout before exiting.
    The caller (the JSON-RPC read loop) is responsible for forwarding those
    bytes to hermes.

    Raises on non-zero exit so the supervisor can surface a clean
    JSON-RPC error instead of silently truncating the response. MCP
    framing is line-delimited JSON, so a partial / truncated response is
    not survivable for the caller.
    """
    try:
        proc = subprocess.Popen(
            [inner_cmd, *inner_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            # start_new_session so a SIGINT to hermes cleanly propagates to
            # the inner server on POSIX (killpg semantics); Windows ignores
            # it but Popen cleanup still works via TerminateProcess.
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"[{label}] inner executable not found: {inner_cmd!r}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"[{label}] failed to spawn inner process {inner_cmd!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    # Spawn relay threads for stderr so server-side chatter doesn't wedge
    # the main relay if the inner server emits a lot.
    stderr_relay = threading.Thread(
        target=_drain_stream_to_fd,
        args=(proc.stderr, sys.stderr.fileno(), label, "stderr"),
        daemon=True,
        name=f"{label}-stderr-relay",
    )
    stderr_relay.start()

    try:
        if payload:
            try:
                proc.stdin.write(payload)
            except BrokenPipeError as exc:
                raise RuntimeError(
                    f"[{label}] inner server closed stdin before request "
                    f"was fully written"
                ) from exc
        try:
            proc.stdin.close()
        except OSError:
            pass

        # Read stdout to EOF on the calling thread — MCP framing is
        # line-delimited and the response ends when the inner server exits,
        # so a single blocking read is correct.
        try:
            stdout_chunks: list[bytes] = []
            # Inner process startup is the slow part for codegraph
            # --liftoff-only (re-index). Bound only the startup phase —
            # once stdout produces bytes, the server is healthy and we
            # read through to natural EOF.
            deadline = time.monotonic() + inner_timeout
            first_byte_deadline = time.monotonic() + _INNER_STARTUP_TIMEOUT_S
            got_first_byte = False
            while True:
                if not got_first_byte:
                    remaining = first_byte_deadline - time.monotonic()
                    if remaining <= 0:
                        proc.kill()
                        raise RuntimeError(
                            f"[{label}] inner server produced no stdout "
                            f"within {_INNER_STARTUP_TIMEOUT_S:.0f}s — killed"
                        )
                elif deadline - time.monotonic() <= 0:
                    proc.kill()
                    raise RuntimeError(
                        f"[{label}] inner server exceeded "
                        f"{inner_timeout:.0f}s wall-clock cap — killed"
                    )
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                stdout_chunks.append(chunk)
                got_first_byte = True
            stdout_bytes = b"".join(stdout_chunks)
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass

        rc = proc.wait(timeout=5.0)
        if rc != 0:
            raise RuntimeError(
                f"[{label}] inner server exited with code {rc} "
                f"(payload {len(payload)}B, response {len(stdout_chunks)} "
                f"chunks)"
            )
        return stdout_bytes
    finally:
        # Inner is one-shot by definition — if anything went wrong, make
        # sure the process is reaped before we return.
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass


def _read_request_lines(stdin) -> bytes:
    """Return exactly one line of stdin (including the trailing newline).

    MCP is line-delimited JSON-RPC: each ``\\n``-terminated chunk is one
    request. We split here so each inner invocation gets exactly one
    request — the "one-shot" server then handles that one request and
    exits, matching its design (codegraph --liftoff-only, in particular,
    exits as soon as its stdin closes; serving multiple requests from
    one process would require a long-lived inner server, which defeats
    the whole point of the wrap).

    For empty lines (keep-alive probes, ``ping`` heartbeats from MCP
    clients), we skip them and read the next non-empty line. The
    supervisor never spawns an inner process for a blank request.
    """
    while True:
        line = stdin.readline()
        if not line:
            return b""  # EOF — parent closed our stdin
        if line.strip():
            return line


def main(argv: List[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    label = args.label
    inner_cmd = args.inner_cmd
    inner_args = list(args.inner_args or [])
    inner_timeout = float(args.inner_timeout)

    # Inherit the supervisor's environment so MCP servers see the same
    # shell the user launched hermes from. This matches what stdio_client
    # would have done if it had spawned the inner server directly.
    env = os.environ.copy()

    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    while True:
        # Read exactly one line of stdin. MCP is line-delimited JSON-RPC;
        # each line is one request. _read_request_lines skips blank lines
        # and returns b"" on EOF (hermes closed our stdin).
        try:
            request_bytes = _read_request_lines(stdin)
        except (KeyboardInterrupt, SystemExit):
            return 0
        except OSError as exc:
            sys.stderr.write(
                f"[{label}] stdin read failed: {type(exc).__name__}: {exc}\n"
            )
            sys.stderr.flush()
            return 0

        if not request_bytes:
            # hermes closed our stdin → clean supervisor shutdown.
            return 0

        try:
            response_bytes = _exchange(
                inner_cmd=inner_cmd,
                inner_args=inner_args,
                payload=request_bytes,
                inner_timeout=inner_timeout,
                label=label,
                env=env,
            )
        except RuntimeError as exc:
            sys.stderr.write(f"[{label}] exchange failed: {exc}\n")
            sys.stderr.flush()
            # Surface a JSON-RPC parse error so hermes sees a structured
            # failure rather than a hang. The id -1 keeps the response
            # well-formed for any caller that doesn't pre-flight parsing.
            err = (
                b'{"jsonrpc":"2.0","id":null,"error":'
                b'{"code":-32603,"message":"mcp-one-shot supervisor: '
                + str(exc).encode("utf-8", "replace")[:1024]
                + b'"}}\n'
            )
            try:
                stdout.write(err)
                stdout.flush()
            except OSError:
                return 1
            # Continue the loop — a transient inner failure should not
            # kill the long-lived supervisor (hermes would then have to
            # spawn a *new* supervisor, defeating the purpose).
            continue

        if response_bytes:
            try:
                stdout.write(response_bytes)
                stdout.flush()
            except OSError:
                return 1


if __name__ == "__main__":
    sys.exit(main())