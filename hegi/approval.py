"""Durable professor-authorized Memory Forest approval workflow."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import HegiConfig
from .memory import (
    DraftGate,
    MCPMemoryBackend,
    MemoryEvaluator,
    validate_draft_payload,
)
from .notify import load_env_value
from .pipeline import minutes_from_dict
from .state import StateStore


TERMINAL_STATES = {
    "authentication_failed",
    "meeting_not_found",
    "duplicate_approval",
    "duplicate_memory",
    "draft_failed",
    "validation_failed",
    "approval_failed",
    "commit_failed",
    "post_commit_failed",
    "manual_review_required",
}


class ApprovalBackend(Protocol):
    def show(self, draft_id: str) -> dict[str, Any]: ...

    def approve(self, draft_id: str, *, note: str) -> dict[str, Any]: ...

    def commit(self, draft_id: str) -> dict[str, Any]: ...

    def maintenance(self, operation: str) -> dict[str, Any]: ...


def _backend(config: HegiConfig) -> MCPMemoryBackend:
    memory = config.section("memory")
    return MCPMemoryBackend(
        read_server=str(memory.get("read_server", "memory-forest-read")),
        search_tool=str(memory.get("search_tool", "")),
        draft_server=str(memory.get("draft_server", "")),
        draft_tool=str(memory.get("draft_tool", "")),
    )


def _send_status(
    config: HegiConfig,
    text: str,
    *,
    sender: Callable[..., Any] | None = None,
) -> None:
    token = load_env_value(config.curator_env, "TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("memory-curator Telegram token을 읽을 수 없습니다.")
    if sender is None:
        from tools.send_message_tool import _send_telegram

        sender = _send_telegram
    result = sender(
        token,
        config.chat_id,
        text,
        disable_link_previews=True,
    )
    if hasattr(result, "__await__"):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(result)
        else:
            loop.create_task(result)


def _approval_event(state: StateStore, platform_message_id: str) -> dict[str, str]:
    with state.connect() as connection:
        row = connection.execute(
            """
            SELECT command, user_id, raw_text FROM approval_events
            WHERE platform_message_id=?
            ORDER BY approved_at DESC LIMIT 1
            """,
            (platform_message_id,),
        ).fetchone()
    if row is None:
        raise PermissionError("승인 이벤트를 찾을 수 없습니다.")
    return {
        "command": str(row["command"]),
        "user_id": str(row["user_id"]),
        "raw_text": str(row["raw_text"]),
    }


def _payload_object(raw: str, *, command: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"명령 결과가 JSON이 아닙니다: {' '.join(command)}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"명령 결과가 object가 아닙니다: {' '.join(command)}")
    if payload.get("ok") is False or payload.get("error"):
        raise RuntimeError(str(payload.get("error", payload)))
    return payload


class CLIApprovalBackend:
    """Private subprocess boundary; never registered as an LLM tool."""

    def __init__(self, config: HegiConfig):
        memory = config.section("memory")
        approval = memory.get("approval", {})
        if not isinstance(approval, dict):
            approval = {}
        self.cli = str(approval.get("cli", "")).strip() or "memory-forest-approve"
        self.queue_root = str(approval.get("queue_root", "")).strip()
        self.timeout = int(approval.get("timeout_seconds", 120))
        self.forest_cli = str(approval.get("forest_cli", "")).strip() or "memory-forest"
        self.forest_root = str(approval.get("forest_root", "")).strip()
        self.backup_script = str(approval.get("backup_script", "")).strip()

    def _run(self, command: list[str], *, timeout: int | None = None) -> dict[str, Any]:
        env = os.environ.copy()
        if self.queue_root:
            env["MEMORY_FOREST_QUEUE_ROOT"] = self.queue_root
        completed = subprocess.run(
            command,
            check=False,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout or self.timeout,
        )
        output = completed.stdout.strip() or completed.stderr.strip()
        if completed.returncode != 0:
            try:
                detail = _payload_object(output, command=command)
            except Exception:
                detail = output
            raise RuntimeError(
                f"{Path(command[0]).name} 실패(exit={completed.returncode}): {detail}"
            )
        return _payload_object(output, command=command)

    def show(self, draft_id: str) -> dict[str, Any]:
        return self._run([self.cli, "show", draft_id])

    def approve(self, draft_id: str, *, note: str) -> dict[str, Any]:
        return self._run(
            [self.cli, "approve", draft_id, "--note", note, "--no-commit"]
        )

    def commit(self, draft_id: str) -> dict[str, Any]:
        return self._run([self.cli, "commit", draft_id], timeout=max(self.timeout, 360))

    def maintenance(self, operation: str) -> dict[str, Any]:
        if operation == "backup":
            if not self.backup_script:
                raise RuntimeError("memory.approval.backup_script가 설정되지 않았습니다.")
            completed = subprocess.run(
                [self.backup_script],
                check=False,
                capture_output=True,
                text=True,
                timeout=max(self.timeout, 180),
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"backup 실패(exit={completed.returncode}): "
                    f"{completed.stderr.strip() or completed.stdout.strip()}"
                )
            return {"ok": True, "operation": "backup"}
        if not self.forest_root:
            raise RuntimeError("memory.approval.forest_root가 설정되지 않았습니다.")
        return self._run(
            [self.forest_cli, operation, self.forest_root],
            timeout=max(self.timeout, 180),
        )


def _draft_id(draft: dict[str, Any]) -> str:
    value = draft.get("draft_id") or draft.get("id")
    if value:
        return str(value)
    structured = draft.get("structuredContent")
    if isinstance(structured, dict) and structured.get("draft_id"):
        return str(structured["draft_id"])
    raise ValueError("생성된 STM Draft ID를 찾지 못했습니다.")


def _draft_payload(result: dict[str, Any]) -> dict[str, Any]:
    draft = result.get("draft")
    if isinstance(draft, dict):
        return draft
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        nested = structured.get("draft")
        return nested if isinstance(nested, dict) else structured
    return result


def _commit_identity(result: dict[str, Any]) -> tuple[str, str]:
    forest = result.get("forest_result")
    if not isinstance(forest, dict) or forest.get("ok") is not True:
        raise RuntimeError("commit 결과에 명확한 forest_result.ok=true가 없습니다.")
    path = str(forest.get("path", "")).strip()
    memory_id = str(
        forest.get("memory_id") or forest.get("sha256") or ""
    ).strip()
    if not path or not memory_id:
        raise RuntimeError("commit 결과에 Memory ID 또는 저장 경로가 없습니다.")
    return memory_id, path


def _idempotency_key(
    professor_user_id: str,
    telegram_message_id: str,
    meeting_id: str,
    draft_id: str,
) -> str:
    material = (
        professor_user_id + telegram_message_id + meeting_id + draft_id
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _record(
    state: StateStore,
    job_id: int,
    workflow_state: str,
    **kwargs: Any,
) -> None:
    state.update_approval_workflow(job_id, workflow_state, **kwargs)


def _notify_failure(
    config: HegiConfig,
    *,
    meeting_id: str,
    successful: list[str],
    failed_step: str,
    error: str,
    sender: Callable[..., Any] | None,
) -> None:
    _send_status(
        config,
        "🌲 기억 저장 실패\n\n"
        f"성공 단계:\n{', '.join(successful) if successful else '없음'}\n\n"
        f"실패 단계:\n{failed_step}\n\n"
        f"원인:\n{error}\n\n"
        f"Meeting ID:\n{meeting_id}\n\n"
        "Draft는 가능한 경우 현재 상태로 보존했습니다.\n"
        "중복 Commit은 수행하지 않았습니다.",
        sender=sender,
    )


def process_pending_approvals(
    config: HegiConfig,
    *,
    backend: Any | None = None,
    approval_backend: ApprovalBackend | None = None,
    sender: Callable[..., Any] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Resume durable approval jobs and execute only authenticated commands."""
    state = StateStore(config.state_db)
    state.resume_recoverable_approval_jobs()
    memory_backend = backend or _backend(config)
    private_backend = approval_backend or CLIApprovalBackend(config)
    memory = config.section("memory")
    professor_ids = [str(item) for item in memory.get("professor_user_ids", [])]
    gate = DraftGate(state, memory_backend, professor_user_ids=professor_ids)
    results: list[dict[str, Any]] = []
    for _ in range(limit):
        job = state.claim_approval_job()
        if job is None:
            break
        job_id = int(job["id"])
        meeting_id = str(job["meeting_id"])
        successful: list[str] = []
        current_step = "received"
        forced_failure_state: str | None = None
        try:
            event = _approval_event(state, str(job["platform_message_id"]))
            command = event["command"]
            if event["user_id"] not in professor_ids:
                _record(state, job_id, "authentication_failed")
                raise PermissionError("설정된 교수 계정의 승인 이벤트가 아닙니다.")
            _record(state, job_id, "authenticated")
            successful.append("인증")

            row = state.episode_by_id(meeting_id)
            if (
                row is None
                or row.get("status") != "reported"
                or not row.get("minutes_json")
            ):
                _record(state, job_id, "meeting_not_found")
                raise ValueError("reported 상태의 승인 대상 회의록이 없습니다.")
            minutes = minutes_from_dict(json.loads(row["minutes_json"]))
            _record(state, job_id, "meeting_resolved")
            successful.append("회의 연결")

            workflow_state = str(job.get("workflow_state") or "received")
            draft_id = str(job.get("draft_id") or "")
            prior_committed = state.committed_approval_for_meeting(meeting_id)
            if (
                prior_committed
                and int(prior_committed["id"]) != job_id
                and prior_committed.get("memory_id")
            ):
                _record(
                    state,
                    job_id,
                    "duplicate_memory",
                    details={
                        "memory_id": prior_committed.get("memory_id"),
                        "path": prior_committed.get("memory_path"),
                    },
                )
                forced_failure_state = "duplicate_memory"
                raise RuntimeError("해당 meeting은 이미 Memory로 commit되었습니다.")
            if workflow_state not in {"approved", "committed", "post_commit_failed"}:
                current_step = "memory_researched"
                evaluation = MemoryEvaluator(memory_backend).evaluate(minutes)
                _record(
                    state,
                    job_id,
                    "memory_researched",
                    details={
                        "searched_queries": evaluation.searched_queries,
                        "search_findings": evaluation.search_findings,
                        "memory_ids": [
                            item.memory_id for item in evaluation.matched_memories
                        ],
                        "duplicate_reasons": evaluation.reasons,
                        "search_recall_warning": evaluation.search_recall_warning,
                    },
                )
                successful.append("Memory Forest 재검색")
                if evaluation.search_recall_warning:
                    _record(state, job_id, "manual_review_required")
                    forced_failure_state = "manual_review_required"
                    raise RuntimeError(
                        "키워드 검색 결과가 충분하지 않아 중복 판단의 신뢰도가 낮습니다."
                    )
                if evaluation.recommendation in {
                    "merge_existing",
                    "needs_professor_review",
                    "no_memory",
                }:
                    terminal = (
                        "duplicate_memory"
                        if evaluation.recommendation == "merge_existing"
                        else "manual_review_required"
                    )
                    _record(
                        state,
                        job_id,
                        terminal,
                        details={"reasons": evaluation.reasons},
                    )
                    forced_failure_state = terminal
                    raise RuntimeError("; ".join(evaluation.reasons))

                current_step = "draft_created"
                if command == "approve":
                    pending = [
                        item
                        for item in state.pending_drafts_for_meeting(meeting_id)
                        if int(item["id"]) != job_id
                    ]
                    if len(pending) > 1:
                        forced_failure_state = "manual_review_required"
                        raise RuntimeError(
                            "pending Draft가 둘 이상이라 승인 대상을 특정할 수 없습니다."
                        )
                    if pending:
                        draft_id = str(pending[0]["draft_id"])
                if not draft_id:
                    draft = gate.create_draft_after_recheck(
                        minutes, evaluation, project=str(job["project"])
                    )
                    draft_id = _draft_id(draft)
                _record(state, job_id, "draft_created", draft_id=draft_id)
                successful.append("Draft 생성")

                current_step = "draft_validated"
                shown = private_backend.show(draft_id)
                validate_draft_payload(_draft_payload(shown))
                _record(state, job_id, "draft_validated", draft_id=draft_id)
                successful.append("Draft 검증")

                if command in {"draft", "merge"}:
                    result = {
                        "meeting_id": meeting_id,
                        "status": (
                            "manual_review_required"
                            if command == "merge"
                            else "draft_created"
                        ),
                        "draft_id": draft_id,
                        "commit": "not_performed",
                    }
                    state.complete_approval_job(
                        job_id, status="completed", result=result
                    )
                    results.append(result)
                    _send_status(
                        config,
                        "🌲 STM Draft 생성 완료\n\n"
                        f"Draft ID:\n{draft_id}\n\n"
                        "상태:\npending\n\n"
                        "아직 approve/commit하지 않았습니다.\n"
                        "“@헤기 승인하고 저장해”라고 답하면 최종 저장합니다.",
                        sender=sender,
                    )
                    continue
            if not draft_id:
                raise RuntimeError("승인 이벤트에 연결된 Draft ID가 없습니다.")

            key = str(job.get("idempotency_key") or "") or _idempotency_key(
                event["user_id"],
                str(job["platform_message_id"]),
                meeting_id,
                draft_id,
            )
            memory_id = str(job.get("memory_id") or "")
            memory_path = str(job.get("memory_path") or "")
            live_draft = private_backend.show(draft_id)
            live_state = str(live_draft.get("state", "")).strip()
            if live_state == "committed":
                persisted_draft = _draft_payload(live_draft)
                recovered_commit = {
                    "ok": True,
                    "draft_id": draft_id,
                    "state": "committed",
                    "forest_result": persisted_draft.get("forest_result"),
                }
                memory_id, memory_path = _commit_identity(recovered_commit)
                _record(
                    state,
                    job_id,
                    "committed",
                    details={"recovered_from_approval_queue": True},
                    draft_id=draft_id,
                    idempotency_key=key,
                    memory_id=memory_id,
                    memory_path=memory_path,
                )
                workflow_state = "committed"
                successful.append("commit 상태 복구")
            if workflow_state not in {"approved", "committed", "post_commit_failed"}:
                current_step = "approved"
                if live_state == "approved":
                    approval_result = {
                        "ok": True,
                        "draft_id": draft_id,
                        "state": "approved",
                        "recovered": True,
                    }
                else:
                    approval_result = private_backend.approve(
                        draft_id,
                        note=(
                            "HEGI professor-authorized Telegram approval "
                            f"for {meeting_id}; idempotency={key}"
                        ),
                    )
                    if approval_result.get("state") != "approved":
                        raise RuntimeError("Draft approve 결과가 명확하지 않습니다.")
                _record(
                    state,
                    job_id,
                    "approved",
                    draft_id=draft_id,
                    idempotency_key=key,
                )
                workflow_state = "approved"
                successful.append("approve")

            if workflow_state not in {"committed", "post_commit_failed"}:
                current_step = "committed"
                commit_result = private_backend.commit(draft_id)
                if commit_result.get("state") != "committed":
                    raise RuntimeError("Draft commit 결과가 명확하지 않습니다.")
                memory_id, memory_path = _commit_identity(commit_result)
                _record(
                    state,
                    job_id,
                    "committed",
                    details={"commit": commit_result},
                    draft_id=draft_id,
                    idempotency_key=key,
                    memory_id=memory_id,
                    memory_path=memory_path,
                )
                workflow_state = "committed"
                successful.append("commit")

            maintenance_results: dict[str, str] = {}
            completed_transitions = set(state.approval_transitions(job_id))
            for operation, completed_state in (
                ("validate", "validated"),
                ("audit", "audited"),
                ("index", "indexed"),
                ("backup", "backed_up"),
            ):
                if completed_state in completed_transitions:
                    maintenance_results[operation] = "PASS"
                    continue
                current_step = completed_state
                maintenance = private_backend.maintenance(operation)
                if maintenance.get("ok") is not True:
                    raise RuntimeError(f"{operation} 결과가 PASS가 아닙니다.")
                maintenance_results[operation] = "PASS"
                _record(
                    state,
                    job_id,
                    completed_state,
                    details={operation: maintenance},
                )
                successful.append(operation)

            _record(state, job_id, "completed")
            result = {
                "meeting_id": meeting_id,
                "status": "completed",
                "draft_id": draft_id,
                "memory_id": memory_id,
                "path": memory_path,
                "maintenance": maintenance_results,
                "idempotency_key": key,
            }
            state.complete_approval_job(job_id, status="completed", result=result)
            results.append(result)
            _send_status(
                config,
                "🌲 기억 저장 완료\n\n"
                f"회의:\n{minutes.title}\n\n"
                f"Meeting ID:\n{meeting_id}\n\n"
                f"Draft ID:\n{draft_id}\n\n"
                f"Memory ID:\n{memory_id}\n\n"
                f"저장 경로:\n{memory_path}\n\n"
                "후속 검증:\n"
                "- validate: PASS\n"
                "- audit: PASS\n"
                "- index: PASS\n"
                "- backup: PASS\n\n"
                "자동 판단이 아니라 교수님의 명시적 “기억해” 승인에 따라 저장했습니다.",
                sender=sender,
            )
        except Exception as exc:
            failure_state = forced_failure_state or {
                "received": "authentication_failed",
                "memory_researched": "manual_review_required",
                "draft_created": "draft_failed",
                "draft_validated": "validation_failed",
                "approved": "approval_failed",
                "committed": "commit_failed",
                "validated": "post_commit_failed",
                "audited": "post_commit_failed",
                "indexed": "post_commit_failed",
                "backed_up": "post_commit_failed",
            }.get(current_step, "manual_review_required")
            if forced_failure_state is None and str(job.get("workflow_state")) in {
                "approved",
                "committed",
                "post_commit_failed",
            }:
                failure_state = (
                    "post_commit_failed"
                    if str(job.get("workflow_state")) in {
                        "committed",
                        "post_commit_failed",
                    }
                    else "commit_failed"
                )
            _record(
                state,
                job_id,
                failure_state,
                details={"error": str(exc), "failed_step": current_step},
            )
            state.complete_approval_job(job_id, status="failed", error=str(exc))
            if failure_state not in {"manual_review_required", "duplicate_memory"}:
                state.add_dead_letter(
                    "approval",
                    {
                        "job_id": job_id,
                        "platform_message_id": str(job["platform_message_id"]),
                        "workflow_state": failure_state,
                    },
                    str(exc),
                    meeting_id,
                )
            try:
                _notify_failure(
                    config,
                    meeting_id=meeting_id,
                    successful=successful,
                    failed_step=current_step,
                    error=str(exc),
                    sender=sender,
                )
            except Exception:
                pass
            results.append(
                {
                    "meeting_id": meeting_id,
                    "status": failure_state,
                    "error": str(exc),
                    "commit": "not_repeated",
                }
            )
    return results
