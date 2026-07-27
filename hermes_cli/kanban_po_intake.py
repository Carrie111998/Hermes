"""Direct-primary Product Owner execution for inert Work Inbox intake."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from hermes_cli import kanban_db

PRODUCT_OWNER_PROFILE = "productowner"
PRODUCT_OWNER_PROMPT = (
    "Assess the claimed Work Inbox intake. Use work_inbox_show first, then "
    "finish with exactly one work_inbox_decide call."
)


def _is_new_work(intake: dict[str, Any]) -> bool:
    try:
        payload = json.loads(str(intake.get("raw_request") or ""))
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("kind") == "task_create"


def route_pending_intake(
    conn,
    *,
    board: str,
    intake: dict[str, Any],
) -> dict[str, Any]:
    """Send only new product work to the primary PO; preserve requalification."""

    if _is_new_work(intake):
        return dispatch_product_owner_intake(
            conn, board=board, intake_id=str(intake["id"])
        )
    from hermes_cli.kanban_qualifier import qualify_intake

    return qualify_intake(conn, board=board, intake_id=str(intake["id"]))


def dispatch_product_owner_intake(
    conn,
    *,
    board: str,
    intake_id: str,
    spawn_fn: Optional[Callable[..., Optional[int]]] = None,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Claim and launch one direct Product Owner attempt without waiting."""

    identity = kanban_db.resolve_profile_runtime_identity(
        PRODUCT_OWNER_PROFILE,
        source="work_inbox_intake",
        surface="work_inbox_intake",
    )
    if identity is None:
        raise RuntimeError(
            "Product Owner profile must resolve an explicit provider, model, and effort"
        )
    run = kanban_db.claim_qualification_intake(
        conn,
        intake_id,
        profile=PRODUCT_OWNER_PROFILE,
        runtime_identity=identity,
        now=now,
    )
    if run is None:
        return {"status": "not_pending", "intake_id": intake_id}
    spawn = spawn_fn or _spawn_product_owner_intake
    try:
        pid = spawn(run, board=board)
        if pid:
            if not kanban_db.set_qualification_intake_worker_pid(
                conn,
                intake_id=intake_id,
                run_id=int(run["id"]),
                claim_lock=str(run["claim_lock"]),
                worker_pid=int(pid),
            ):
                raise RuntimeError("Product Owner intake claim changed during spawn")
    except Exception as exc:
        kanban_db.finish_qualification_intake_run(
            conn,
            intake_id=intake_id,
            run_id=int(run["id"]),
            claim_lock=str(run["claim_lock"]),
            intake_status="pending",
            outcome="spawn_failed",
            error=str(exc),
            now=now,
        )
        raise
    return {
        "status": "running",
        "intake_id": intake_id,
        "run_id": int(run["id"]),
        "profile": identity["profile"],
        "provider": identity["provider"],
        "model": identity["model"],
        "effort": identity["effort"],
    }


def _spawn_product_owner_intake(
    run: dict[str, Any], *, board: str
) -> Optional[int]:
    """Fire-and-forget one intake-scoped Hermes primary process."""

    from hermes_cli.profiles import resolve_profile_env

    profile = PRODUCT_OWNER_PROFILE
    env = dict(os.environ)
    for name in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_BRANCH",
    ):
        env.pop(name, None)
    env["HERMES_HOME"] = resolve_profile_env(profile)
    env["HERMES_PROFILE"] = profile
    env["HERMES_WORK_INBOX_INTAKE"] = str(run["intake_id"])
    env["HERMES_WORK_INBOX_RUN_ID"] = str(run["id"])
    env["HERMES_WORK_INBOX_CLAIM_LOCK"] = str(run["claim_lock"])
    env["HERMES_DISABLE_PROVIDER_FALLBACK"] = "1"
    env["HERMES_INFERENCE_PROFILE"] = profile
    env["HERMES_INFERENCE_PROVIDER"] = str(run.get("provider") or "")
    env["HERMES_INFERENCE_MODEL"] = str(run.get("model") or "")
    env["HERMES_INFERENCE_EFFORT"] = str(run.get("effort") or "")
    env["HERMES_KANBAN_DB"] = str(kanban_db.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(
        kanban_db.workspaces_root(board=board)
    )
    env["HERMES_KANBAN_BOARD"] = board
    env.pop("HERMES_TUI", None)

    try:
        from hermes_cli.agent_memory_protocol import configured_outbox_path
        from hermes_cli.agent_memory_vault import configured_vault_path

        vault = configured_vault_path()
        if vault is not None:
            env["HERMES_AGENT_MEMORY_VAULT"] = str(vault)
        env["HERMES_AGENT_MEMORY_OUTBOX"] = str(configured_outbox_path())
    except Exception:
        pass

    cwd: Optional[str] = None
    metadata = kanban_db.product_board_metadata(board) or {}
    candidate = str(metadata.get("default_workdir") or "").strip()
    if candidate and Path(candidate).is_dir():
        cwd = candidate
        env["TERMINAL_CWD"] = candidate

    cmd = [
        *kanban_db._resolve_hermes_argv(),
        "-p",
        profile,
        "--cli",
        "--accept-hooks",
        "--toolsets",
        "kanban",
        "chat",
        "-q",
        PRODUCT_OWNER_PROMPT,
    ]
    log_dir = kanban_db.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{run['intake_id']}-run-{run['id']}.log"
    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
    finally:
        log_f.close()
    return int(proc.pid)
