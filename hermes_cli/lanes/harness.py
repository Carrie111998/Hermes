"""Sanctioned infrastructure boundary for future business-lane modules."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from hermes_cli.cost.ledger import record_call
from hermes_cli.lanes import approvals, metrics, rate_limit, schema
from hermes_cli.lanes.channels import (
    DashboardApprovalChannel,
    TelegramApprovalChannel,
)
from hermes_cli.lanes.contracts import (
    AdmitResult,
    ApprovalRequest,
    ApprovalStatus,
    LLMResult,
    LaneDraft,
    LaneTask,
    PublishResult,
)
from hermes_cli.lanes.errors import (
    ApprovalExpired,
    ApprovalNotGranted,
    PublishDisabled,
)
from hermes_cli.lanes.registry import LaneRegistry
from hermes_cli.programme.ingress import admit_new_turn
from hermes_cli.routing.facade import route_for_turn
from hermes_cli.routing import route_context
from hermes_cli.side_effects import confirm, mark_in_flight, reserve
from hermes_cli.side_effects import schema as side_effects_schema
from hermes_cli.sqlite_util import retrying_write_txn
from hermes_cli.verdict import (
    DispatchEnvelope,
    LeafVerdict,
    record_dispatch,
    record_verdict,
)
from hermes_cli.verdict.types import canonical_strategy_hash

_PURPOSES = frozenset({"draft", "classification", "summary", "refine"})


class DryRunViolation(RuntimeError):
    """Raised when a dry-run path attempts a real external or durable write."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _publisher_action(external_target: str) -> str:
    prefix = str(external_target).split(":", 1)[0].lower()
    return {
        "gbp": "gbp.reply",
        "email": "email.send",
        "telegram": "telegram.send",
    }.get(prefix, "test.action")


class LaneHarness:
    def __init__(
        self,
        *,
        lane_id: str,
        db_path: str | Path,
        dry_run: bool = False,
        manifest_path: str | Path | None = None,
        llm_caller: Callable[..., dict[str, Any]] | None = None,
        publisher: Callable[[dict[str, Any]], str] | None = None,
        telegram_sender: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.lane_id = str(lane_id).strip().lower()
        self.db_path = Path(db_path).expanduser()
        self.dry_run = bool(dry_run)
        self.manifest_path = manifest_path
        self.llm_caller = llm_caller
        self.publisher = publisher
        self.telegram_sender = telegram_sender
        self.registry = LaneRegistry(
            manifest_path=manifest_path,
            db_path=self.db_path,
        )
        self.config = self.registry.config(self.lane_id)

    def persist_task(self, task: LaneTask) -> LaneTask:
        """Idempotently persist an ingested task by lane/external identifier."""
        if self.dry_run:
            return task
        schema.ensure_migrated(self.db_path)
        conn = schema.connect(self.db_path)
        try:
            with retrying_write_txn(conn):
                conn.execute(
                    """INSERT OR IGNORE INTO lane_task(
                         lane_id,external_id,task_id,ingested_at,status,
                         payload_json)
                       VALUES(?,?,?,?,'ingested',?)""",
                    (
                        self.lane_id,
                        task.external_id,
                        task.task_id,
                        _utc_now(),
                        json.dumps(
                            task.payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
                row = conn.execute(
                    """SELECT id,task_id,status,payload_json FROM lane_task
                       WHERE lane_id=? AND external_id=?""",
                    (self.lane_id, task.external_id),
                ).fetchone()
        finally:
            conn.close()
        return LaneTask(
            lane_id=self.lane_id,
            external_id=task.external_id,
            payload=json.loads(row["payload_json"]),
            id=int(row["id"]),
            task_id=row["task_id"],
            status=str(row["status"]),
        )

    def find_task(self, *, external_id: str) -> LaneTask | None:
        """Return one lane task by its stable external identifier."""
        if self.dry_run:
            return None
        schema.ensure_migrated(self.db_path)
        conn = schema.connect(self.db_path)
        try:
            row = conn.execute(
                """SELECT id,external_id,task_id,status,payload_json
                     FROM lane_task WHERE lane_id=? AND external_id=?""",
                (self.lane_id, external_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return LaneTask(
            lane_id=self.lane_id,
            external_id=str(row["external_id"]),
            payload=json.loads(row["payload_json"]),
            id=int(row["id"]),
            task_id=row["task_id"],
            status=str(row["status"]),
        )

    def list_tasks(
        self,
        *,
        status: str | None = None,
        ingested_since: str | None = None,
    ) -> list[LaneTask]:
        """Read this lane's tasks without exposing its SQLite connection."""
        if self.dry_run:
            return []
        schema.ensure_migrated(self.db_path)
        clauses = ["lane_id=?"]
        params: list[object] = [self.lane_id]
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if ingested_since is not None:
            clauses.append("ingested_at>=?")
            params.append(ingested_since)
        conn = schema.connect(self.db_path)
        try:
            rows = conn.execute(
                f"""SELECT id,external_id,task_id,status,payload_json
                      FROM lane_task WHERE {' AND '.join(clauses)}
                     ORDER BY id""",
                tuple(params),
            ).fetchall()
        finally:
            conn.close()
        return [
            LaneTask(
                lane_id=self.lane_id,
                external_id=str(row["external_id"]),
                payload=json.loads(row["payload_json"]),
                id=int(row["id"]),
                task_id=row["task_id"],
                status=str(row["status"]),
            )
            for row in rows
        ]

    def update_task(
        self,
        *,
        task: LaneTask,
        payload: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> LaneTask:
        """Update payload/status for a persisted lane task atomically."""
        if task.id is None:
            raise ValueError("lane task update requires a persisted task")
        if self.dry_run:
            return LaneTask(
                lane_id=task.lane_id,
                external_id=task.external_id,
                payload=payload if payload is not None else task.payload,
                id=task.id,
                task_id=task.task_id,
                status=status if status is not None else task.status,
            )
        allowed_statuses = {
            "ingested",
            "drafting",
            "drafted",
            "awaiting_approval",
            "publishing",
            "published",
            "failed",
            "expired",
        }
        if status is not None and status not in allowed_statuses:
            raise ValueError(f"invalid lane task status: {status}")
        schema.ensure_migrated(self.db_path)
        assignments = []
        params: list[object] = []
        if payload is not None:
            assignments.append("payload_json=?")
            params.append(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )
        if status is not None:
            assignments.append("status=?")
            params.append(status)
        if not assignments:
            return task
        params.append(int(task.id))
        conn = schema.connect(self.db_path)
        try:
            with retrying_write_txn(conn):
                cursor = conn.execute(
                    f"UPDATE lane_task SET {','.join(assignments)} WHERE id=?",
                    tuple(params),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"lane task does not exist: {task.id}")
        finally:
            conn.close()
        return LaneTask(
            lane_id=task.lane_id,
            external_id=task.external_id,
            payload=payload if payload is not None else task.payload,
            id=task.id,
            task_id=task.task_id,
            status=status if status is not None else task.status,
        )

    def check_rate_limit(
        self,
        *,
        window_kind: str,
        increment: float = 1,
    ) -> None:
        """Apply one manifest-scoped lane rate-limit increment."""
        if self.dry_run:
            return
        caps = {
            "hourly_ingest": self.config.per_lane_hourly_ingest_cap,
            "daily_task": self.config.per_lane_daily_task_cap,
            "daily_cost": self.config.per_lane_daily_cost_cap_aud,
        }
        try:
            cap = caps[window_kind]
        except KeyError as exc:
            raise ValueError(
                f"unknown lane rate-limit window: {window_kind}"
            ) from exc
        rate_limit.enforce(
            lane_id=self.lane_id,
            window_kind=window_kind,
            increment=increment,
            cap=cap,
            db_path=self.db_path,
        )

    def lint_draft(self, text: str) -> str:
        """Run CS-11a's routing-intent lint over drafted text."""
        from hermes_cli.skills.lint import lint_skill_body

        return lint_skill_body(str(text)).linted_body

    def _mark_failed(self, task: LaneTask) -> None:
        if task.id is None or self.dry_run:
            return
        conn = schema.connect(self.db_path)
        try:
            with retrying_write_txn(conn):
                conn.execute(
                    "UPDATE lane_task SET status='failed' WHERE id=?",
                    (int(task.id),),
                )
        finally:
            conn.close()

    def admit(
        self,
        *,
        task: LaneTask,
        apply_rate_limits: bool = True,
    ) -> AdmitResult:
        if self.dry_run:
            return AdmitResult(admitted=True, dry_run=True)
        admit_new_turn(
            route="single",
            profile=f"lane:{self.lane_id}",
            task_id_hint=task.task_id or task.external_id,
            db_path=self.db_path,
        )
        if not apply_rate_limits:
            return AdmitResult(admitted=True, dry_run=False)
        try:
            rate_limit.enforce(
                lane_id=self.lane_id,
                window_kind="hourly_ingest",
                increment=1,
                cap=self.config.per_lane_hourly_ingest_cap,
                db_path=self.db_path,
            )
            rate_limit.enforce(
                lane_id=self.lane_id,
                window_kind="daily_task",
                increment=1,
                cap=self.config.per_lane_daily_task_cap,
                db_path=self.db_path,
            )
        except Exception:
            self._mark_failed(task)
            raise
        return AdmitResult(admitted=True, dry_run=False)

    def call_llm(
        self,
        *,
        task: LaneTask,
        prompt: str,
        max_tokens: int,
        purpose: str,
    ) -> LLMResult:
        if purpose not in _PURPOSES:
            raise ValueError(f"unsupported lane LLM purpose: {purpose}")
        if self.llm_caller is None:
            raise RuntimeError("lane LLM calls require an injected caller")
        task_id = task.task_id or f"lane-{self.lane_id}-{task.external_id}"
        decision = route_for_turn(
            lane=self.lane_id,
            rung="default",
            complexity="default",
            task_id=task_id,
            profile=f"lane:{self.lane_id}",
            route="single",
            use_doctrine_reader=True,
            db_path=self.db_path,
        )
        context = {
            "schema_version": 1,
            "decision_row_id": int(decision["decision_row_id"]),
            "task_id": task_id,
            "session_id": None,
            "primary_provider": decision["provider"],
            "primary_model": decision["model"],
            "fallback_chain": list(decision.get("fallbacks") or []),
        }
        route_context._reset_for_tests()
        os.environ["HERMES_ROUTE_CONTEXT_JSON"] = json.dumps(
            context,
            sort_keys=True,
            separators=(",", ":"),
        )
        result: dict[str, Any]
        try:
            result = self.llm_caller(
                prompt=prompt,
                max_tokens=int(max_tokens),
                route=decision,
                task=task,
                purpose=purpose,
            )
            chosen_provider = str(
                result.get("provider") or decision["provider"]
            )
            chosen_model = str(result.get("model") or decision["model"])
            route_context.flush_to_db(
                chosen_provider=chosen_provider,
                chosen_model=chosen_model,
                outcome=str(result.get("outcome") or "success"),
                db_path=self.db_path,
            )
        finally:
            os.environ.pop("HERMES_ROUTE_CONTEXT_JSON", None)

        provider = str(result.get("provider") or decision["provider"])
        model = str(result.get("model") or decision["model"])
        input_tokens = int(result.get("input_tokens") or 0)
        output_tokens = int(result.get("output_tokens") or 0)
        ledger_entry = record_call(
            task_id=task_id,
            lane=self.lane_id,
            vendor=provider,
            model=model,
            attempt_number=1,
            rung_id="r0_baseline",
            input_tokens=(
                max(1, input_tokens)
                if provider == "perplexity"
                else input_tokens
            ),
            output_tokens=(
                max(1, output_tokens)
                if provider == "perplexity"
                else output_tokens
            ),
            reported_usd=float(result.get("reported_usd") or 0.0),
            latency_ms=int(result.get("latency_ms") or 0),
            profile=f"lane:{self.lane_id}",
            route="single",
            db_path=self.db_path,
        )

        strategy = {
            "lane": self.lane_id,
            "purpose": purpose,
            "decision_row_id": int(decision["decision_row_id"]),
        }
        dispatch_id = record_dispatch(
            DispatchEnvelope(
                task_id=task_id,
                attempt_number=1,
                rung_id="r0_baseline",
                model_slug=model,
                mode="single",
                strategy_payload=strategy,
                expected_cost_aud=float(ledger_entry.aud_amount),
                issued_by=f"lane:{self.lane_id}",
            ),
            db_path=self.db_path,
            profile=f"lane:{self.lane_id}",
            route="single",
        )
        verdict_id = record_verdict(
            LeafVerdict(
                task_id=task_id,
                attempt_number=1,
                rung_id="r0_baseline",
                dispatch_envelope_id=dispatch_id,
                model_used=model,
                outcome="success",
                confidence=float(result.get("confidence") or 1.0),
                cost_aud=float(ledger_entry.aud_amount),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                wall_ms=int(result.get("latency_ms") or 0),
                strategy_hash=canonical_strategy_hash(strategy),
                raw_meta={
                    "lane": self.lane_id,
                    "purpose": purpose,
                    "decision_row_id": int(decision["decision_row_id"]),
                },
            ),
            db_path=self.db_path,
            profile=f"lane:{self.lane_id}",
            route="single",
        )
        rate_limit.record_cost_advisory(
            lane_id=self.lane_id,
            increment=float(ledger_entry.aud_amount),
            cap=self.config.per_lane_daily_cost_cap_aud,
            db_path=self.db_path,
        )
        return LLMResult(
            text=str(result.get("text") or ""),
            provider=provider,
            model=model,
            decision_row_id=int(decision["decision_row_id"]),
            verdict_id=verdict_id,
            cost_ledger_id=int(ledger_entry.id),
        )

    def enqueue_approval(
        self,
        *,
        task: LaneTask,
        draft: LaneDraft,
    ) -> ApprovalRequest:
        if self.dry_run:
            return ApprovalRequest(
                token="DRYRUN000000",
                lane_task_id=int(task.id or 0),
                status="pending",
                expires_at="dry-run",
            )
        request = approvals.enqueue(
            task=task,
            draft=draft,
            channel=self.config.approval_channel,
            timeout_hours=self.config.approval_timeout_hours,
            db_path=self.db_path,
        )
        payload = {
            "lane_id": self.lane_id,
            "lane_task_id": request.lane_task_id,
            "approval_token": request.token,
            "draft": draft.content,
            "reply_codes": {
                "grant": f"GRANT {request.token}",
                "reject": f"REJECT {request.token} <reason>",
            },
        }
        if self.config.approval_channel == "telegram":
            channel = TelegramApprovalChannel(
                lane_id=self.lane_id,
                db_path=self.db_path,
                sender=self.telegram_sender,
            )
        else:
            channel = DashboardApprovalChannel()
        channel.emit(request=request, payload=payload)
        return request

    def check_approval(self, *, approval_token: str) -> ApprovalStatus:
        return approvals.check(approval_token, db_path=self.db_path)

    def publish_with_ledger(
        self,
        *,
        task: LaneTask,
        external_target: str,
        payload: dict[str, Any],
        side_effect_key: str | None = None,
        publisher: Callable[[dict[str, Any]], str] | None = None,
    ) -> PublishResult:
        if self.dry_run:
            return PublishResult(outcome="success")
        if not self.config.publish_enabled:
            raise PublishDisabled(f"publishing is disabled: {self.lane_id}")
        if task.id is None:
            raise ValueError("publish requires a persisted lane task")
        schema.ensure_migrated(self.db_path)
        side_effects_schema.ensure_migrated(self.db_path)
        target_hash = hashlib.sha256(
            external_target.encode("utf-8")
        ).hexdigest()[:16]
        key = side_effect_key or (
            f"lane:{self.lane_id}:task:{task.id}:"
            f"target:{target_hash}:v1"
        )
        conn = schema.connect(self.db_path)
        try:
            with retrying_write_txn(conn):
                duplicate = conn.execute(
                    "SELECT id FROM lane_publish_log WHERE side_effect_key=?",
                    (key,),
                ).fetchone()
                if duplicate is not None:
                    return PublishResult(
                        outcome="skipped_duplicate",
                        log_id=int(duplicate["id"]),
                    )
                approval = conn.execute(
                    """SELECT approval_token,status,expires_at
                         FROM lane_approval_queue
                        WHERE lane_task_id=?
                        ORDER BY id DESC LIMIT 1""",
                    (int(task.id),),
                ).fetchone()
                if approval is None or approval["status"] != "granted":
                    raise ApprovalNotGranted(
                        f"task {task.id} has no granted approval"
                    )
                if str(approval["expires_at"]) <= _utc_now():
                    raise ApprovalExpired(
                        f"approval token expired: {approval['approval_token']}"
                    )
                reused = conn.execute(
                    """SELECT 1 FROM lane_publish_log
                       WHERE approval_token=? LIMIT 1""",
                    (approval["approval_token"],),
                ).fetchone()
                if reused is not None:
                    raise ApprovalNotGranted(
                        "approval token has already been consumed"
                    )
                reservation = reserve(
                    task_id=task.task_id or str(task.id),
                    lane=self.lane_id,
                    action_type=_publisher_action(external_target),
                    payload=payload,
                    idempotency_key=key,
                    db_path=self.db_path,
                    conn=conn,
                )
                if reservation.already_done is not None:
                    return PublishResult(
                        outcome="skipped_duplicate",
                        side_effect_id=int(reservation.already_done["id"]),
                    )
                if reservation.already_in_flight is not None:
                    return PublishResult(
                        outcome="skipped_duplicate",
                        side_effect_id=int(
                            reservation.already_in_flight["id"]
                        ),
                    )
                reserved_id = int(reservation.reserved_id)
                mark_in_flight(reserved_id=reserved_id, conn=conn)
                active_publisher = publisher or self.publisher
                if active_publisher is None:
                    raise RuntimeError(
                        "lane publish requires an injected publisher"
                    )
                external_ref = active_publisher(payload)
                confirm(
                    reserved_id=reserved_id,
                    external_ref=external_ref,
                    result_summary="business lane publish succeeded",
                    conn=conn,
                )
                cursor = conn.execute(
                    """INSERT INTO lane_publish_log(
                         lane_id,lane_task_id,approval_token,external_target,
                         side_effect_key,payload_json,published_at,outcome)
                       VALUES(?,?,?,?,?,?,?,'success')""",
                    (
                        self.lane_id,
                        int(task.id),
                        approval["approval_token"],
                        external_target,
                        key,
                        json.dumps(
                            payload,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        _utc_now(),
                    ),
                )
                conn.execute(
                    "UPDATE lane_task SET status='published' WHERE id=?",
                    (int(task.id),),
                )
                return PublishResult(
                    outcome="success",
                    log_id=int(cursor.lastrowid),
                    side_effect_id=reserved_id,
                )
        finally:
            conn.close()

    def record_metric(
        self,
        *,
        task: LaneTask,
        metric_name: str,
        value: float,
    ) -> None:
        if self.dry_run:
            return
        metrics.record_metric(
            lane_id=self.lane_id,
            lane_task_id=task.id,
            metric_name=metric_name,
            value=value,
            db_path=self.db_path,
        )


class DryRunHarness(LaneHarness):
    """In-memory harness that exercises lane contracts without real writes."""

    def __init__(
        self,
        *,
        lane_id: str,
        db_path: str | Path,
        manifest_path: str | Path | None = None,
        llm_caller: Callable[..., dict[str, Any]] | None = None,
        publisher: Callable[[dict[str, Any]], str] | None = None,
        telegram_sender: Callable[[dict[str, Any]], str] | None = None,
        task_cap_aud: float | None = None,
    ) -> None:
        import yaml

        from hermes_cli.cost.task_caps_config import (
            default_task_cap_for_lane,
            validate_task_cap,
        )
        from hermes_cli.lanes.manifest import (
            default_path,
            validate_manifest,
        )

        self.lane_id = str(lane_id).strip().lower()
        self.db_path = Path(db_path).expanduser()
        self.dry_run = True
        self.manifest_path = (
            Path(manifest_path).expanduser()
            if manifest_path is not None
            else default_path()
        )
        manifest = validate_manifest(
            yaml.safe_load(
                self.manifest_path.read_text(encoding="utf-8")
            )
        )
        try:
            self.config = manifest.by_id()[self.lane_id]
        except KeyError as exc:
            raise ValueError(f"unknown lane: {self.lane_id}") from exc
        self.registry = None
        self.llm_caller = llm_caller
        self.publisher = publisher
        self.telegram_sender = telegram_sender
        self.task_cap_aud = validate_task_cap(
            task_cap_aud
            if task_cap_aud is not None
            else default_task_cap_for_lane(self.lane_id)
        )
        self.tasks: list[LaneTask] = []
        self.approvals: list[ApprovalRequest] = []
        self.metrics_log: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.llm_calls: list[dict[str, Any]] = []
        self.rate_counters: dict[str, float] = {}
        self.task_costs: dict[str, float] = {}
        self.killed_tasks: dict[str, str] = {}
        self.simulated_cost_aud = 0.0
        self.simulated_write_calls = 0
        self.write_calls = 0
        self.side_effect_writes = 0
        self.cost_ledger_writes = 0
        self.publish_would_have_been_called = False

    def _call(self, method: str, **details: Any) -> None:
        self.calls.append({"method": method, **details})

    def _simulated_write(self, method: str, **details: Any) -> None:
        self.simulated_write_calls += 1
        self._call(method, **details)

    def attempt_real_write(self, operation: str) -> None:
        self.write_calls += 1
        raise DryRunViolation(
            f"dry-run blocked real write attempt: {operation}"
        )

    def assert_zero_real_writes(self) -> None:
        if self.write_calls:
            raise DryRunViolation(
                f"dry-run observed {self.write_calls} real write attempt(s)"
            )

    def kill_task(self, task_id: str, *, reason: str = "test") -> None:
        self.killed_tasks[str(task_id)] = str(reason)

    @staticmethod
    def _task_key(task: LaneTask) -> str:
        return str(task.task_id or task.external_id)

    def _check_kill(self, task: LaneTask) -> None:
        from hermes_cli.cost.kill_switch import KillSwitchTripped

        task_id = self._task_key(task)
        reason = self.killed_tasks.get(task_id)
        if reason is not None:
            raise KillSwitchTripped(task_id=task_id, reason=reason)

    def persist_task(self, task: LaneTask) -> LaneTask:
        existing = self.find_task(external_id=task.external_id)
        if existing is not None:
            return existing
        persisted = LaneTask(
            lane_id=self.lane_id,
            external_id=task.external_id,
            payload=dict(task.payload),
            id=len(self.tasks) + 1,
            task_id=task.task_id,
            status=task.status,
        )
        self.tasks.append(persisted)
        self._simulated_write(
            "persist_task",
            external_id=task.external_id,
        )
        return persisted

    def find_task(self, *, external_id: str) -> LaneTask | None:
        self._call("find_task", external_id=external_id)
        return next(
            (
                task
                for task in self.tasks
                if task.external_id == external_id
            ),
            None,
        )

    def list_tasks(
        self,
        *,
        status: str | None = None,
        ingested_since: str | None = None,
    ) -> list[LaneTask]:
        del ingested_since
        self._call("list_tasks", status=status)
        return [
            task
            for task in self.tasks
            if status is None or task.status == status
        ]

    def update_task(
        self,
        *,
        task: LaneTask,
        payload: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> LaneTask:
        updated = LaneTask(
            lane_id=task.lane_id,
            external_id=task.external_id,
            payload=dict(payload if payload is not None else task.payload),
            id=task.id,
            task_id=task.task_id,
            status=status if status is not None else task.status,
        )
        self.tasks = [
            updated if existing.id == task.id else existing
            for existing in self.tasks
        ]
        self._simulated_write(
            "update_task",
            external_id=task.external_id,
            status=updated.status,
        )
        return updated

    def check_rate_limit(
        self,
        *,
        window_kind: str,
        increment: float = 1,
    ) -> None:
        from hermes_cli.lanes.errors import LaneRateLimitExceeded

        caps = {
            "hourly_ingest": self.config.per_lane_hourly_ingest_cap,
            "daily_task": self.config.per_lane_daily_task_cap,
            "daily_cost": self.config.per_lane_daily_cost_cap_aud,
        }
        try:
            cap = float(caps[window_kind])
        except KeyError as exc:
            raise ValueError(
                f"unknown lane rate-limit window: {window_kind}"
            ) from exc
        projected = self.rate_counters.get(window_kind, 0.0) + float(
            increment
        )
        if projected > cap:
            raise LaneRateLimitExceeded(
                f"lane rate limit exceeded: {self.lane_id} "
                f"{window_kind}"
            )
        self.rate_counters[window_kind] = projected
        self._call(
            "check_rate_limit",
            window_kind=window_kind,
            increment=float(increment),
        )

    def lint_draft(self, text: str) -> str:
        from hermes_cli.skills.lint import lint_skill_body

        self._call("lint_draft")
        return lint_skill_body(str(text)).linted_body

    def _mark_failed(self, task: LaneTask) -> None:
        if task.id is not None:
            self.update_task(task=task, status="failed")

    def admit(
        self,
        *,
        task: LaneTask,
        apply_rate_limits: bool = True,
    ) -> AdmitResult:
        self._check_kill(task)
        self._call(
            "admit",
            task_id=self._task_key(task),
            apply_rate_limits=apply_rate_limits,
        )
        if apply_rate_limits:
            self.check_rate_limit(window_kind="hourly_ingest")
            self.check_rate_limit(window_kind="daily_task")
        return AdmitResult(admitted=True, dry_run=True)

    def call_llm(
        self,
        *,
        task: LaneTask,
        prompt: str,
        max_tokens: int,
        purpose: str,
    ) -> LLMResult:
        from hermes_cli.cost.kill_switch import PerTaskCapExceeded

        if purpose not in _PURPOSES:
            raise ValueError(f"unsupported lane LLM purpose: {purpose}")
        if self.llm_caller is None:
            raise RuntimeError("dry-run LLM calls require an injected fake")
        self._check_kill(task)
        self.llm_calls.append(
            {
                "purpose": purpose,
                "max_tokens": int(max_tokens),
                "prompt": prompt,
            }
        )
        result = self.llm_caller(
            prompt=prompt,
            max_tokens=int(max_tokens),
            route={
                "provider": "dry-run",
                "model": "fixture",
                "fallbacks": [],
            },
            task=task,
            purpose=purpose,
        )
        cost = float(result.get("simulated_cost_aud") or 0.0)
        task_id = self._task_key(task)
        current = self.task_costs.get(task_id, 0.0)
        projected = current + cost
        if projected > self.task_cap_aud:
            self.killed_tasks[task_id] = "per_task_cap"
            raise PerTaskCapExceeded(
                task_id=task_id,
                current_total=current,
                projected_total=projected,
                cap=self.task_cap_aud,
            )
        self.task_costs[task_id] = projected
        self.simulated_cost_aud += cost
        self._call(
            "call_llm",
            purpose=purpose,
            simulated_cost_aud=cost,
        )
        return LLMResult(
            text=str(result.get("text") or ""),
            provider=str(result.get("provider") or "dry-run"),
            model=str(result.get("model") or "fixture"),
            decision_row_id=None,
            verdict_id=None,
            cost_ledger_id=None,
        )

    def enqueue_approval(
        self,
        *,
        task: LaneTask,
        draft: LaneDraft,
    ) -> ApprovalRequest:
        del draft
        request = ApprovalRequest(
            token=f"DRYRUN{len(self.approvals) + 1:06d}",
            lane_task_id=int(task.id or 0),
            status="pending",
            expires_at="9999-12-31T23:59:59Z",
        )
        self.approvals.append(request)
        if task.id is not None:
            self.update_task(task=task, status="awaiting_approval")
        self._simulated_write(
            "enqueue_approval",
            lane_task_id=request.lane_task_id,
        )
        return request

    def check_approval(self, *, approval_token: str) -> ApprovalStatus:
        self._call("check_approval", approval_token=approval_token)
        for request in self.approvals:
            if request.token == approval_token:
                return ApprovalStatus(
                    token=request.token,
                    status=request.status,
                    expires_at=request.expires_at,
                )
        raise ApprovalNotGranted(
            f"unknown dry-run approval token: {approval_token}"
        )

    def publish_with_ledger(
        self,
        *,
        task: LaneTask,
        external_target: str,
        payload: dict[str, Any],
        side_effect_key: str | None = None,
        publisher: Callable[[dict[str, Any]], str] | None = None,
    ) -> PublishResult:
        del payload, side_effect_key, publisher
        if not self.config.publish_enabled:
            raise PublishDisabled(f"publishing is disabled: {self.lane_id}")
        self._check_kill(task)
        self.publish_would_have_been_called = True
        self._simulated_write(
            "publish_with_ledger",
            external_target=external_target,
        )
        return PublishResult(outcome="success")

    def record_metric(
        self,
        *,
        task: LaneTask,
        metric_name: str,
        value: float,
    ) -> None:
        self.metrics_log.append(
            {
                "lane_task_id": task.id,
                "metric_name": metric_name,
                "value": float(value),
            }
        )
        self._simulated_write(
            "record_metric",
            metric_name=metric_name,
            value=float(value),
        )


__all__ = ["DryRunHarness", "DryRunViolation", "LaneHarness"]
