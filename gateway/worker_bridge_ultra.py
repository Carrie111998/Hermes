"""Post-success independent verification for worker-bridge tasks.

Reconstructed from the deleted module's CPython 3.11 bytecode and retained
artifact contract.  Verification is deliberately outside the worker-bridge
plugin: successful tasks are reviewed by the existing panel script and receive
an artifact receipt before the gateway reports the final outcome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("gateway.run")

DEFAULT_ULTRA_ENABLED = True
DEFAULT_ULTRA_MAX_CONCURRENT = 2
MIN_ULTRA_MAX_CONCURRENT = 1
MAX_ULTRA_MAX_CONCURRENT = 4
DEFAULT_ULTRA_TIMEOUT_SECONDS = 3600.0
DEFAULT_ULTRA_STATUSES = ("succeeded", "accepted")
DEFAULT_ULTRA_TEST_CMD = "echo no-tests"
RISK_LOC_SMALL = 120
RISK_LOC_MEDIUM = 800
ULTRA_SCRIPT_RELPATH = Path("plugins") / "panel" / "ultra.py"
RECEIPT_FILENAME = "verification.json"
CRASH_FILENAME = "ultra_crash.json"
INFLIGHT_FILENAME = "ultra_inflight.json"
RECEIPT_KIND = "worker-ultra-receipt"


def resolve_hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def resolve_ultra_script(home: Path) -> Path:
    candidates = [
        home / ULTRA_SCRIPT_RELPATH,
        Path(__file__).resolve().parents[1] / ULTRA_SCRIPT_RELPATH,
    ]
    return next((path for path in candidates if path.exists()), candidates[0])


def task_artifacts_dir(home: Path, task_id: str) -> Path:
    return home / "workers" / "artifacts" / task_id


def receipt_path(home: Path, task_id: str) -> Path:
    return task_artifacts_dir(home, task_id) / RECEIPT_FILENAME


def inflight_path(home: Path, task_id: str) -> Path:
    return task_artifacts_dir(home, task_id) / INFLIGHT_FILENAME


def load_task_record(db_path: Path, task_id: str) -> Optional[dict]:
    if not db_path.exists():
        return None
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT task_id, worker, status, spec, runtime, result "
            "FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        for key in ("spec", "runtime", "result"):
            try:
                result[key] = json.loads(result[key] or "{}")
            except (TypeError, ValueError):
                result[key] = {}
        return result
    finally:
        conn.close()


def resolve_workspace(task: dict) -> Optional[Path]:
    runtime = task.get("runtime") or {}
    spec = task.get("spec") or {}
    workspace = spec.get("workspace") or {}
    value = runtime.get("path") or workspace.get("working_directory") or workspace.get(
        "repository"
    )
    return Path(value).expanduser() if value else None


def count_diff_locs(diff_path: Path) -> tuple[int, int]:
    """Return changed source LOC and raw changed lines from a unified diff."""
    try:
        lines = diff_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0, 0
    changed = [
        line
        for line in lines
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith(("+++", "---"))
    ]
    source = [
        line
        for line in changed
        if line[1:].strip() and not line[1:].lstrip().startswith(("#", "//"))
    ]
    return len(source), len(changed)


def count_diff_loc(diff_path: Path) -> int:
    return count_diff_locs(diff_path)[0]


def risk_for_loc(loc: int) -> str:
    if loc <= RISK_LOC_SMALL:
        return "small"
    if loc <= RISK_LOC_MEDIUM:
        return "medium"
    return "high"


def _read_json(path: Path) -> Optional[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temp.replace(path)


def write_inflight_marker(
    home: Path,
    task_id: str,
    *,
    queued_at: float,
    started_at: Optional[float],
    queue_position: int,
) -> Path:
    path = inflight_path(home, task_id)
    _write_json(
        path,
        {
            "task_id": task_id,
            "queued_at": queued_at,
            "started_at": started_at,
            "queue_position": queue_position,
        },
    )
    return path


def clear_inflight_marker(home: Optional[Path], task_id: str) -> None:
    if home is None:
        return
    try:
        inflight_path(home, task_id).unlink(missing_ok=True)
    except OSError:
        pass


def summarize_report(report: dict) -> dict:
    findings = report.get("findings") or []
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    if isinstance(findings, list):
        for finding in findings:
            severity = str((finding or {}).get("severity") or "").lower()
            if severity in counts:
                counts[severity] += 1
    return {
        **counts,
        "finding_count": sum(counts.values()),
        "verdict": report.get("verdict"),
    }


def write_receipt(
    home: Path,
    task_id: str,
    *,
    verdict: str,
    exit_code: int,
    workspace: str,
    risk: str,
    loc: int,
    raw_loc: int,
    summary: dict,
    manifest: Optional[dict],
) -> Path:
    path = receipt_path(home, task_id)
    _write_json(
        path,
        {
            "kind": RECEIPT_KIND,
            "task_id": task_id,
            "verdict": verdict,
            "exit_code": exit_code,
            "workspace": workspace,
            "risk": risk,
            "loc": loc,
            "raw_loc": raw_loc,
            "summary": summary,
            "manifest": manifest,
            "created_at": time.time(),
        },
    )
    return path


def write_crash_marker(home: Path, task_id: str, error: str) -> Path:
    path = task_artifacts_dir(home, task_id) / CRASH_FILENAME
    _write_json(
        path,
        {"task_id": task_id, "error": error, "created_at": time.time()},
    )
    return path


def build_ultra_argv(
    script: Path, objective: str, workspace: Path, risk: str, test_cmd: str
) -> list[str]:
    return [
        sys.executable,
        str(script),
        objective,
        "--workspace",
        str(workspace),
        "--risk",
        risk,
        "--test-cmd",
        test_cmd,
        "--json",
    ]


def _extract_report(stdout: str) -> dict:
    text = stdout.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def run_ultra_for_task(
    task_id: str,
    *,
    home: Optional[Path] = None,
    db_path: Optional[Path] = None,
    script: Optional[Path] = None,
    test_cmd: str = DEFAULT_ULTRA_TEST_CMD,
    timeout: float = DEFAULT_ULTRA_TIMEOUT_SECONDS,
) -> dict:
    home = home or resolve_hermes_home()
    db_path = db_path or home / "workers" / "bridge.db"
    script = script or resolve_ultra_script(home)
    task = load_task_record(db_path, task_id)
    if not task:
        raise KeyError(task_id)
    workspace = resolve_workspace(task)
    if workspace is None:
        raise RuntimeError("worker task has no workspace")
    artifact_dir = task_artifacts_dir(home, task_id)
    diff_path = artifact_dir / "changes.diff"
    loc, raw_loc = count_diff_locs(diff_path)
    risk = risk_for_loc(loc)
    objective = str((task.get("spec") or {}).get("objective") or task_id)
    try:
        proc = subprocess.run(
            build_ultra_argv(script, objective, workspace, risk, test_cmd),
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout)),
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            ),
        )
        report = _extract_report(proc.stdout)
        summary = summarize_report(report)
        verdict = str(report.get("verdict") or ("SHIP" if proc.returncode == 0 else "BLOCK"))
        receipt = write_receipt(
            home,
            task_id,
            verdict=verdict,
            exit_code=proc.returncode,
            workspace=str(workspace),
            risk=risk,
            loc=loc,
            raw_loc=raw_loc,
            summary=summary,
            manifest=report.get("manifest") if isinstance(report, dict) else None,
        )
        return {
            "task_id": task_id,
            "worker": task.get("worker"),
            "status": task.get("status"),
            "objective": objective,
            "verdict": verdict,
            "exit_code": proc.returncode,
            "risk": risk,
            "loc": loc,
            "summary": summary,
            "receipt": str(receipt),
            "stderr": proc.stderr[-800:],
        }
    except Exception as exc:
        marker = write_crash_marker(home, task_id, f"{type(exc).__name__}: {exc}")
        return {
            "task_id": task_id,
            "worker": task.get("worker"),
            "status": task.get("status"),
            "objective": objective,
            "crashed": True,
            "error": f"{type(exc).__name__}: {exc}",
            "crash_marker": str(marker),
        }
    finally:
        clear_inflight_marker(home, task_id)


def _severity_line(counts: dict) -> str:
    return ", ".join(
        f"{name}={int(counts.get(name, 0) or 0)}"
        for name in ("critical", "high", "medium", "low")
    )


def format_ultra_alert(outcome: dict) -> str:
    task_id = outcome.get("task_id", "?")
    worker = outcome.get("worker", "?")
    objective = outcome.get("objective") or "(no objective)"
    if outcome.get("crashed"):
        return (
            "[Worker Bridge Alert — automated notification from the gateway "
            "auto-ultra watcher, not a user message]\n\n"
            f"🛑 {task_id} ({worker}) completed, but its auto-ultra review "
            f"CRASHED — MANUAL REVIEW REQUIRED.\n"
            f"    objective: {objective}\n"
            f"    error: {outcome.get('error') or 'unknown'}"
        )
    summary = outcome.get("summary") or {}
    return (
        "[Worker Bridge Alert — automated notification from the gateway "
        "auto-ultra watcher, not a user message]\n\n"
        f"🔎 {task_id} ({worker}) independent review: "
        f"{outcome.get('verdict', 'UNKNOWN')}\n"
        f"    objective: {objective}\n"
        f"    risk/LOC: {outcome.get('risk', '?')}/{outcome.get('loc', 0)}\n"
        f"    findings: {_severity_line(summary)}\n"
        f"    receipt: {outcome.get('receipt', '(none)')}"
    )


def resolve_ultra_settings(wb_cfg: Any) -> dict:
    raw = wb_cfg.get("auto_ultra", {}) if isinstance(wb_cfg, dict) else {}
    if isinstance(raw, bool):
        raw = {"enabled": raw}
    if not isinstance(raw, dict):
        raw = {}
    try:
        maximum = max(
            MIN_ULTRA_MAX_CONCURRENT,
            min(
                MAX_ULTRA_MAX_CONCURRENT,
                int(raw.get("max_concurrent", DEFAULT_ULTRA_MAX_CONCURRENT)),
            ),
        )
    except (TypeError, ValueError):
        maximum = DEFAULT_ULTRA_MAX_CONCURRENT
    try:
        timeout = max(
            1.0,
            float(raw.get("timeout_seconds", DEFAULT_ULTRA_TIMEOUT_SECONDS)),
        )
    except (TypeError, ValueError):
        timeout = DEFAULT_ULTRA_TIMEOUT_SECONDS
    statuses = raw.get("statuses") or DEFAULT_ULTRA_STATUSES
    if isinstance(statuses, str):
        statuses = [part.strip() for part in statuses.split(",") if part.strip()]
    return {
        "enabled": raw.get("enabled", DEFAULT_ULTRA_ENABLED) is not False,
        "max_concurrent": maximum,
        "timeout": timeout,
        "statuses": frozenset(str(status) for status in statuses),
        "test_cmd": str(raw.get("test_cmd") or DEFAULT_ULTRA_TEST_CMD),
    }


class GatewayWorkerBridgeUltraMixin:
    """Queue and deliver independent review results for successful tasks."""

    def _ultra_state(self):
        state = getattr(self, "_worker_bridge_ultra_state", None)
        if state is None:
            state = {"pending": [], "running": set(), "seen": set()}
            self._worker_bridge_ultra_state = state
        return state

    def _ultra_route_transitions(
        self,
        transitions: list[dict],
        settings: dict,
        *,
        home: Optional[Path] = None,
    ) -> tuple[list[dict], list[dict]]:
        ultra = settings.get("auto_ultra") or {}
        if not ultra.get("enabled"):
            return transitions, []
        home = home or resolve_hermes_home()
        statuses = frozenset(ultra.get("statuses") or DEFAULT_ULTRA_STATUSES)
        state = self._ultra_state()
        ready: list[dict] = []
        deferred: list[dict] = []
        for item in transitions:
            if item.get("status") not in statuses:
                ready.append(item)
                continue
            task_id = item["task_id"]
            existing = _read_json(receipt_path(home, task_id))
            if existing:
                ready.append({**item, "ultra_receipt": existing})
                continue
            deferred.append(item)
            if task_id not in state["seen"]:
                state["seen"].add(task_id)
                state["pending"].append(
                    {**item, "queued_at": time.time(), "home": home}
                )
                write_inflight_marker(
                    home,
                    task_id,
                    queued_at=time.time(),
                    started_at=None,
                    queue_position=len(state["pending"]),
                )
        return ready, deferred

    def _ultra_pump(self, settings: dict) -> None:
        state = self._ultra_state()
        maximum = int(
            (settings.get("auto_ultra") or {}).get(
                "max_concurrent", DEFAULT_ULTRA_MAX_CONCURRENT
            )
        )
        while state["pending"] and len(state["running"]) < maximum:
            item = state["pending"].pop(0)
            task_id = item["task_id"]
            state["running"].add(task_id)
            asyncio.create_task(self._ultra_execute(item, settings))

    async def _ultra_execute(self, item: dict, settings: dict) -> None:
        task_id = item["task_id"]
        state = self._ultra_state()
        home = item.get("home") or resolve_hermes_home()
        ultra = settings.get("auto_ultra") or {}
        write_inflight_marker(
            home,
            task_id,
            queued_at=float(item.get("queued_at") or time.time()),
            started_at=time.time(),
            queue_position=0,
        )
        try:
            outcome = await asyncio.to_thread(
                run_ultra_for_task,
                task_id,
                home=home,
                test_cmd=ultra.get("test_cmd", DEFAULT_ULTRA_TEST_CMD),
                timeout=ultra.get("timeout", DEFAULT_ULTRA_TIMEOUT_SECONDS),
            )
            await self._ultra_alert(outcome, settings)
        finally:
            state["running"].discard(task_id)
            self._ultra_pump(settings)

    async def _ultra_alert(self, outcome: dict, settings: dict) -> None:
        target = self._resolve_worker_alert_target(settings)
        if not target:
            logger.warning(
                "worker-bridge auto-ultra result for %s has no alert target",
                outcome.get("task_id"),
            )
            return
        from gateway.platforms.base import MessageEvent, MessageType

        adapter, source = target
        await adapter.handle_message(
            MessageEvent(
                text=format_ultra_alert(outcome),
                message_type=MessageType.TEXT,
                source=source,
                internal=True,
            )
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--test-cmd", default=DEFAULT_ULTRA_TEST_CMD)
    parser.add_argument("--timeout", type=float, default=DEFAULT_ULTRA_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    outcome = run_ultra_for_task(
        args.task_id, test_cmd=args.test_cmd, timeout=args.timeout
    )
    print(json.dumps(outcome, indent=2, sort_keys=True))
    return 1 if outcome.get("crashed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
