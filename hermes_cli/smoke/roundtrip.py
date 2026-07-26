"""Nine-stage synthetic doctrine round trip with production-safe isolation."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from hermes_constants import get_default_hermes_root
from hermes_cli.cost import gate_integration, ledger, turns_ledger
from hermes_cli.cost.errors import ProgrammeGatePausedAtIngress
from hermes_cli.cost.kill_switch import KillSwitchTripped
from hermes_cli.programme import init as programme_init
from hermes_cli.programme.ingress import admit_new_turn
from hermes_cli.routing import facade, route_context
from hermes_cli.session.controller import should_rotate
from hermes_cli.smoke.cleanup import CleanupRefused, cleanup_smoke_rows
from hermes_cli.smoke.mocks import MockLLMCall, NoOpTelegramBucket
from hermes_cli.sqlite_util import open_connection, retrying_write_txn
from hermes_cli.verdict import (
    DispatchEnvelope,
    LeafVerdict,
    record_dispatch,
    record_verdict,
)
from hermes_cli.verdict.types import canonical_strategy_hash


VALID_LANES = frozenset(
    {"green_captains", "dayroute", "tihna", "default"}
)
VALID_SCENARIOS = frozenset(
    {
        "success",
        "fallback_success",
        "cascade_exhausted",
        "cost_advisory",
        "kill_switch",
        "gate_paused",
    }
)
_SYNTHETIC_FALLBACK = {
    "provider": "openrouter",
    "model": "anthropic/claude-4.5-sonnet",
}


@dataclass(frozen=True)
class SmokeStage:
    name: str
    outcome: str
    details: dict[str, Any]
    elapsed_ms: int


@dataclass
class SmokeResult:
    overall: str
    scenario: str
    lane: str
    stages: list[SmokeStage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    task_id: str | None = None
    session_id: str | None = None
    source_db_path: str | None = None
    working_db_path: str | None = None
    commit: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["stages"] = [asdict(stage) for stage in self.stages]
        return value


def _elapsed(start: float) -> int:
    return max(0, int(round((time.perf_counter() - start) * 1_000)))


def _stage(
    stages: list[SmokeStage],
    name: str,
    outcome: str,
    details: dict[str, Any],
    started: float,
) -> None:
    stages.append(
        SmokeStage(
            name=name,
            outcome=outcome,
            details=details,
            elapsed_ms=_elapsed(started),
        )
    )


def _online_copy(source: Path) -> Path:
    handle, raw_path = tempfile.mkstemp(
        dir="/tmp",
        prefix="hermes-smoke-",
        suffix=".db",
    )
    os.close(handle)
    target = Path(raw_path)
    try:
        source_conn = sqlite3.connect(
            f"file:{source}?mode=ro",
            uri=True,
            timeout=5,
        )
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return target


def _remove_temp_copy(path: str | None) -> None:
    if not path:
        return
    target = Path(path)
    if (
        target.parent != Path("/tmp")
        or not target.name.startswith("hermes-smoke-")
        or target.suffix != ".db"
    ):
        return
    for candidate in (
        target,
        Path(f"{target}-wal"),
        Path(f"{target}-shm"),
    ):
        candidate.unlink(missing_ok=True)


def _set_temp_running(path: Path) -> None:
    programme_init.migrate(path)
    conn = programme_init.connect(path)
    try:
        with retrying_write_txn(conn):
            conn.execute(
                """
                UPDATE programme_state
                   SET state = 'RUNNING',
                       reason = 'CS-13 dry-run temp override',
                       changed_by = 'smoke_test',
                       changed_at = ?,
                       task_count_at_change = 0
                 WHERE id = 1
                """,
                (programme_init.utc_now(),),
            )
    finally:
        conn.close()


def _insert_task(path: Path, task_id: str, session_id: str) -> str:
    conn = open_connection(
        path,
        busy_timeout_ms=5_000,
        enable_wal=True,
        synchronous="FULL",
        db_label=f"CS-13 task ({path.name})",
    )
    try:
        with retrying_write_txn(conn):
            conn.execute(
                """
                INSERT INTO tasks (
                    id, title, body, status, priority, created_by,
                    created_at, workspace_kind, session_id
                ) VALUES (?, ?, ?, 'queued', 0, 'smoke_test', ?, 'scratch', ?)
                """,
                (
                    task_id,
                    "CS-13 synthetic smoke turn",
                    "smoke_test sentinel row",
                    int(time.time()),
                    session_id,
                ),
            )
    finally:
        conn.close()
    return task_id


@contextmanager
def _intercept_doctrine_alert(
    bucket: NoOpTelegramBucket,
) -> Iterator[None]:
    previous_alert = facade._maybe_emit_doctrine_live_alert

    def _capture_alert(
        _conn,
        _decision_row_id: int,
        task_id: str | None,
        lane: str,
        chosen_provider: str,
        chosen_model: str,
        doctrine_version: int | None,
    ) -> None:
        bucket.send(
            key="doctrine_live:smoke_test",
            payload={
                "task_id": task_id,
                "lane": lane,
                "provider": chosen_provider,
                "model": chosen_model,
                "doctrine_version": doctrine_version,
            },
        )

    facade._maybe_emit_doctrine_live_alert = _capture_alert
    try:
        yield
    finally:
        facade._maybe_emit_doctrine_live_alert = previous_alert


@contextmanager
def _intercept_cost_advisory(
    bucket: NoOpTelegramBucket,
) -> Iterator[None]:
    previous_advisory = gate_integration.send_task_cost_advisory

    def _capture_advisory(**payload: Any) -> bool:
        bucket.send(
            key="task_cost_advisory:smoke_test",
            payload=dict(payload),
        )
        return True

    gate_integration.send_task_cost_advisory = _capture_advisory
    try:
        yield
    finally:
        gate_integration.send_task_cost_advisory = previous_advisory


@contextmanager
def _route_environment(context: dict[str, Any]) -> Iterator[None]:
    previous_env = os.environ.get("HERMES_ROUTE_CONTEXT_JSON")
    route_context._reset_for_tests()
    os.environ["HERMES_ROUTE_CONTEXT_JSON"] = json.dumps(
        context,
        sort_keys=True,
    )
    try:
        yield
    finally:
        route_context._reset_for_tests()
        if previous_env is None:
            os.environ.pop("HERMES_ROUTE_CONTEXT_JSON", None)
        else:
            os.environ["HERMES_ROUTE_CONTEXT_JSON"] = previous_env


def _attempt_calls(
    mock: MockLLMCall,
    route: dict[str, Any],
    fallbacks: list[dict[str, str]],
) -> tuple[dict[str, Any] | None, str, str, list[dict[str, Any]]]:
    providers = [
        {"provider": str(route["provider"]), "model": str(route["model"])},
        *fallbacks,
    ]
    history: list[dict[str, Any]] = []
    response: dict[str, Any] | None = None
    chosen = providers[0]
    for attempt, candidate in enumerate(providers):
        chosen = candidate
        try:
            response = mock(
                provider=candidate["provider"],
                model=candidate["model"],
                prompt="CS-13 synthetic prompt",
                max_tokens=100,
                attempt=attempt,
            )
            break
        except KillSwitchTripped:
            raise
        except TimeoutError as exc:
            entry = {
                "provider": candidate["provider"],
                "model": candidate["model"],
                "failure_class": "timeout",
                "latency_ms": 1200 if attempt == 0 else 800,
                "error_repr": str(exc),
                "transition_reason": "mocked_timeout",
            }
            history.append(entry)
            route_context.append_failure(**entry)
    return (
        response,
        str(chosen["provider"]),
        str(chosen["model"]),
        history,
    )


def run_smoke_turn(
    *,
    scenario: str,
    lane: str,
    db_path: str | Path,
    commit: bool,
    llm_factory: Callable[..., MockLLMCall] = MockLLMCall,
    telegram_bucket: NoOpTelegramBucket | None = None,
) -> SmokeResult:
    """Run the nine-stage pipeline and capture every unexpected error."""
    normalized_scenario = str(scenario).strip().lower()
    normalized_lane = str(lane).strip().lower()
    source = Path(db_path).expanduser()
    result = SmokeResult(
        overall="FAIL",
        scenario=normalized_scenario,
        lane=normalized_lane,
        source_db_path=str(source),
        commit=bool(commit),
    )
    if normalized_scenario not in VALID_SCENARIOS:
        result.errors.append(f"invalid scenario: {scenario}")
        return result
    if normalized_lane not in VALID_LANES:
        result.errors.append(f"invalid lane: {lane}")
        return result
    try:
        working = source if commit else _online_copy(source)
        result.working_db_path = str(working)
        if not commit:
            _set_temp_running(working)
        task_id = f"smoke-t-{uuid.uuid4()}"
        session_id = f"smoke-s-{uuid.uuid4()}"
        result.task_id = task_id
        result.session_id = session_id
        bucket = telegram_bucket or NoOpTelegramBucket()
        accounting_lane = (
            "platform" if normalized_lane == "default" else normalized_lane
        )

        started = time.perf_counter()
        try:
            admit_new_turn(
                route="smoke_test",
                profile="smoke_test",
                session_id=session_id,
                task_id_hint=task_id,
                db_path=working,
            )
        except ProgrammeGatePausedAtIngress as exc:
            _stage(
                result.stages,
                "admit_new_turn",
                "blocked_by_gate",
                {"error": str(exc), "programme_state": "PAUSED"},
                started,
            )
            result.overall = "BLOCKED"
            return result
        _insert_task(working, task_id, session_id)
        _stage(
            result.stages,
            "admit_new_turn",
            "admitted",
            {
                "task_id": task_id,
                "task_status": "queued",
                "ingress_rejection_log_id": None,
            },
            started,
        )

        started = time.perf_counter()
        rotate, rotation_reason = should_rotate(
            system_prompt="CS-13 smoke",
            conversation_history=[],
            pending_user_message="synthetic turn",
        )
        _stage(
            result.stages,
            "session_rotation",
            "rotation_needed" if rotate else "no_rotation_needed",
            {
                "rotation_reason": rotation_reason or "token_count below 100000",
                "synthetic_token_count": 500,
            },
            started,
        )

        started = time.perf_counter()
        with _intercept_doctrine_alert(bucket):
            route = facade.route_for_turn(
                lane=normalized_lane,
                rung="default",
                complexity="default",
                task_id=task_id,
                session_id=session_id,
                profile="smoke_test",
                route="smoke_test",
                use_doctrine_reader=True,
                forced_legacy=False,
                db_path=working,
            )
        fallbacks = list(route.get("fallbacks") or [])
        fallback_injected = False
        if not fallbacks:
            fallbacks = [dict(_SYNTHETIC_FALLBACK)]
            fallback_injected = True
        _stage(
            result.stages,
            "route_for_turn",
            "success",
            {
                "routing_decisions.id": route["decision_row_id"],
                "chosen_provider": route["provider"],
                "chosen_model": route["model"],
                "fallback_chain": fallbacks,
                "fallback_injected_for_smoke": fallback_injected,
                "doctrine_version": route["doctrine_version"],
                "matched_rule_id": route["matched_rule_id"],
                "used_doctrine_reader": route["used_doctrine_reader"],
                "forced_legacy": route["forced_legacy"],
            },
            started,
        )

        context = {
            "schema_version": 1,
            "decision_row_id": int(route["decision_row_id"]),
            "task_id": task_id,
            "session_id": session_id,
            "primary_provider": route["provider"],
            "primary_model": route["model"],
            "fallback_chain": fallbacks,
        }
        with _route_environment(context):
            route_context.get_route_context()
            mock = llm_factory(normalized_scenario, task_id=task_id)
            started = time.perf_counter()
            try:
                response, chosen_provider, chosen_model, history = _attempt_calls(
                    mock,
                    route,
                    fallbacks,
                )
            except KillSwitchTripped as exc:
                _stage(
                    result.stages,
                    "llm_call",
                    "KillSwitchTripped",
                    {
                        "error": str(exc),
                        "attempts": mock.calls,
                        "tokens_before_exception": 20,
                    },
                    started,
                )
                result.overall = "FAIL"
                return result

            cascade_exhausted = response is None
            if cascade_exhausted:
                chosen_provider, chosen_model = "__all_failed__", "__none__"
            cost_advisory = (
                dict(response.get("cost_advisory") or {})
                if response is not None
                else {}
            )
            call_amount_aud = (
                float(response.get("amount_aud", 0.01))
                if response is not None
                else 0.01
            )
            billing_vendor = (
                str(response.get("billing_vendor") or chosen_provider)
                if response is not None
                else "openrouter"
            )
            next_admit_blocked = False
            if normalized_scenario == "gate_paused":
                conn = programme_init.connect(working)
                try:
                    with retrying_write_txn(conn):
                        conn.execute(
                            """
                            UPDATE programme_state
                               SET state = 'PAUSED',
                                   reason = 'CS-13 gate_paused scenario',
                                   changed_by = 'smoke_test',
                                   changed_at = ?
                             WHERE id = 1
                            """,
                            (programme_init.utc_now(),),
                        )
                finally:
                    conn.close()
                try:
                    admit_new_turn(
                        route="smoke_test",
                        profile="smoke_test",
                        session_id=f"{session_id}-next",
                        task_id_hint=f"{task_id}-next",
                        db_path=working,
                    )
                except ProgrammeGatePausedAtIngress:
                    next_admit_blocked = True
            _stage(
                result.stages,
                "llm_call",
                "failed" if cascade_exhausted else "success",
                {
                    "attempts": mock.calls,
                    "chosen_provider": chosen_provider,
                    "chosen_model": chosen_model,
                    "failure_history": history,
                    "output_tokens": (
                        int(response["output_tokens"]) if response else 0
                    ),
                    "next_admit_blocked": next_admit_blocked,
                    "current_turn_continued": True,
                    "cost_advisory": cost_advisory,
                },
                started,
            )

            started = time.perf_counter()
            raw_meta = {
                "scenario": normalized_scenario,
                "cascade_engaged": bool(history),
                "primary_failure_class": (
                    history[0]["failure_class"] if history else None
                ),
                "failure_history": history,
                "cost_advisory": cost_advisory,
            }
            strategy_hash = canonical_strategy_hash(raw_meta)
            verdict_id = record_verdict(
                LeafVerdict(
                    task_id=task_id,
                    attempt_number=max(1, len(mock.calls)),
                    rung_id="r0_baseline",
                    model_used=chosen_model,
                    outcome="failure" if cascade_exhausted else "success",
                    failure_class="infra" if cascade_exhausted else None,
                    failure_signals=(
                        ["cascade_exhausted"] if cascade_exhausted else []
                    ),
                    confidence=0.0 if cascade_exhausted else 1.0,
                    strategy_hash=strategy_hash,
                    cost_aud=call_amount_aud,
                    input_tokens=20,
                    output_tokens=(
                        int(response["output_tokens"]) if response else 0
                    ),
                    wall_ms=(
                        int(response["latency_ms"]) if response else 2_000
                    ),
                    error_class=(
                        "CascadeExhausted" if cascade_exhausted else None
                    ),
                    error_message=(
                        "all synthetic providers failed"
                        if cascade_exhausted
                        else None
                    ),
                    raw_meta=raw_meta,
                ),
                db_path=working,
                profile="smoke_test",
                route="smoke_test",
                session_id=session_id,
            )
            _stage(
                result.stages,
                "leaf_verdict",
                "failure" if cascade_exhausted else "success",
                {
                    "leaf_verdicts.id": verdict_id,
                    "failure_class": "infra" if cascade_exhausted else None,
                    "metadata": raw_meta,
                },
                started,
            )

            started = time.perf_counter()
            with _intercept_cost_advisory(bucket):
                cost = ledger.record_call(
                    task_id=task_id,
                    lane=accounting_lane,
                    vendor=(
                        billing_vendor
                        if not cascade_exhausted
                        else "openrouter"
                    ),
                    model_slug=chosen_model,
                    attempt_number=max(1, len(mock.calls)),
                    rung_id="r0_baseline",
                    input_tokens=20,
                    output_tokens=(
                        int(response["output_tokens"]) if response else 0
                    ),
                    latency_ms=(
                        int(response["latency_ms"]) if response else 2_000
                    ),
                    amount_aud=call_amount_aud,
                    raw_response_meta={
                        "vendor": "mock_vendor",
                        "scenario": normalized_scenario,
                        "cost_advisory": cost_advisory,
                    },
                    profile="smoke_test",
                    route="smoke_test",
                    session_id=session_id,
                    enforce_task_cap=False,
                    db_path=working,
                )
            _stage(
                result.stages,
                "cost_ledger",
                "success",
                {
                    "cost_ledger.id": cost.id,
                    "provider": cost.vendor,
                    "model": cost.model_slug,
                    "aud": cost.aud_amount,
                    "requested_lane": normalized_lane,
                    "profile": cost.profile,
                    "route": cost.route,
                    "breached_cap": cost.breached_cap,
                    "breach_reason": cost.breach_reason,
                    "transitioned_to_paused": cost.transitioned_to_paused,
                    "advisory_only": bool(
                        cost.breached_cap and not cost.transitioned_to_paused
                    ),
                },
                started,
            )

            started = time.perf_counter()
            dispatch_id = record_dispatch(
                DispatchEnvelope(
                    task_id=task_id,
                    attempt_number=max(1, len(mock.calls)),
                    rung_id="r0_baseline",
                    model_slug=chosen_model,
                    mode="single",
                    strategy_payload=raw_meta,
                    parent_verdict_id=verdict_id,
                    expected_cost_aud=call_amount_aud,
                    issued_by="smoke_test",
                ),
                db_path=working,
                profile="smoke_test",
                route="smoke_test",
                session_id=session_id,
            )
            _stage(
                result.stages,
                "dispatch_envelope",
                "success",
                {
                    "dispatch_envelopes.id": dispatch_id,
                    "rung_id": "r0_baseline",
                },
                started,
            )

            started = time.perf_counter()
            turn_id = None
            if chosen_provider == "openai-codex":
                turn_id = turns_ledger.record_turn(
                    task_id=task_id,
                    lane=accounting_lane,
                    outcome="success",
                    model_reported=chosen_model,
                    model_requested=chosen_model,
                    turns_consumed=1,
                    latency_ms=(
                        int(response["latency_ms"]) if response else None
                    ),
                    raw_response_meta={"route": "smoke_test"},
                    db_path=working,
                )
            _stage(
                result.stages,
                "subscription_turns_ledger",
                "success" if turn_id is not None else "not_applicable",
                {
                    "subscription_turns_ledger.id": turn_id,
                    "provider": chosen_provider,
                    "requested_lane": normalized_lane,
                    "accounting_lane": accounting_lane,
                },
                started,
            )

            started = time.perf_counter()
            flushed = route_context.flush_to_db(
                chosen_provider=chosen_provider,
                chosen_model=chosen_model,
                outcome="failure" if cascade_exhausted else "success",
                db_path=working,
            )
            _stage(
                result.stages,
                "route_context_flush",
                "success" if flushed else "failed",
                {
                    "routing_decisions.id": route["decision_row_id"],
                    "chosen_provider": chosen_provider,
                    "chosen_model": chosen_model,
                    "failure_history": history,
                    "env_cleared_after_read": (
                        "HERMES_ROUTE_CONTEXT_JSON" not in os.environ
                    ),
                    "telegram_bucket": list(bucket.sends),
                },
                started,
            )
        result.overall = "FAIL" if cascade_exhausted else "PASS"
        return result
    except Exception as exc:
        result.errors.append(
            f"unexpected:{type(exc).__name__}:{exc}"
        )
        result.overall = "FAIL"
        return result


def format_smoke_result(result: SmokeResult) -> str:
    lines = [
        "CS-13 SMOKE-TURN",
        f"scenario:            {result.scenario}",
        f"lane:                {result.lane}",
        f"mode:                {'commit' if result.commit else 'dry-run temp DB'}",
        f"task_id:             {result.task_id or '-'}",
        f"session_id:          {result.session_id or '-'}",
    ]
    for index, stage in enumerate(result.stages, start=1):
        lines.append(f"STAGE {index}: {stage.name}")
        lines.append(f"  outcome: {stage.outcome}")
        lines.append(f"  elapsed_ms: {stage.elapsed_ms}")
        for key, value in stage.details.items():
            rendered = (
                json.dumps(value, ensure_ascii=False, sort_keys=True)
                if isinstance(value, (dict, list))
                else str(value)
            )
            lines.append(f"  {key}: {rendered}")
    if result.errors:
        lines.append(
            "errors: " + json.dumps(result.errors, ensure_ascii=False)
        )
    lines.append(f"OVERALL: {result.overall}")
    return "\n".join(lines)


def command(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path).expanduser()
    if args.cleanup:
        try:
            counts = cleanup_smoke_rows(db_path, force=bool(args.force))
        except CleanupRefused as exc:
            print(f"SMOKE CLEANUP REFUSED: {exc}")
            return 2
        for table, count in counts.items():
            print(f"{table}: deleted {count}")
        return 0
    lane = str(args.lane).strip().lower()
    scenario = str(args.scenario).strip().lower()
    if lane not in VALID_LANES or scenario not in VALID_SCENARIOS:
        print(
            f"invalid smoke arguments: lane={lane!r} "
            f"scenario={scenario!r}"
        )
        return 3
    result = run_smoke_turn(
        scenario=scenario,
        lane=lane,
        db_path=db_path,
        commit=bool(args.commit),
    )
    try:
        if args.json:
            print(
                json.dumps(
                    result.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        else:
            print(format_smoke_result(result))
    finally:
        if not result.commit:
            _remove_temp_copy(result.working_db_path)
    if any(error.startswith("unexpected:") for error in result.errors):
        return 4
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}.get(result.overall, 4)


def cli_command(args: argparse.Namespace) -> int:
    """Bridge handler return codes through Hermes main's legacy dispatcher."""
    exit_code = command(args)
    if exit_code:
        raise SystemExit(exit_code)
    return 0


def register_cli(children: argparse._SubParsersAction) -> None:
    parser = children.add_parser(
        "smoke-turn",
        help="Run one isolated end-to-end doctrine smoke turn",
    )
    parser.add_argument("--lane", default="default")
    parser.add_argument("--scenario", default="success")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Use an online temp-DB copy (default).",
    )
    mode.add_argument(
        "--commit",
        action="store_true",
        help="Write only smoke_test sentinel rows to the selected DB.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--db-path",
        default=str(get_default_hermes_root() / "kanban.db"),
        help=argparse.SUPPRESS,
    )
    parser.set_defaults(func=cli_command)


__all__ = [
    "SmokeResult",
    "SmokeStage",
    "VALID_LANES",
    "VALID_SCENARIOS",
    "cli_command",
    "command",
    "format_smoke_result",
    "register_cli",
    "run_smoke_turn",
]
