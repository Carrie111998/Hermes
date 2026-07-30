"""
Cron subcommand for hermes CLI.

Handles standalone cron management commands like list, create, edit,
pause/resume/run/remove, status, and tick.
"""

import json
import sys
from pathlib import Path
from typing import Iterable, List, Optional

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli.colors import Colors, color

# Gateway-lifecycle command detection lives in ``cron.lifecycle_guard`` so it
# can be shared across every job-creation path (CLI + the agent's ``cronjob``
# model tool via ``cron.jobs.create_job``) without a circular import. Re-export
# ``_contains_gateway_lifecycle_command`` here for back-compat: ``tools/
# terminal_tool.py`` imports it from this module to hard-block the same
# commands at execution time when ``_HERMES_GATEWAY=1``.
from cron.lifecycle_guard import (  # noqa: F401  (re-exported for terminal_tool)
    contains_gateway_lifecycle_command as _contains_gateway_lifecycle_command,
)

_CRON_RESTORE_ERROR_CODES = frozenset({
    "CRON_RESTORE_INVALID_ID", "CRON_RESTORE_INVALID_SNAPSHOT",
    "CRON_RESTORE_INVALID_UTF8", "CRON_RESTORE_INVALID_JSON",
    "CRON_RESTORE_BOUNDS_EXCEEDED", "CRON_RESTORE_CYCLE",
    "CRON_RESTORE_NON_SERIALIZABLE", "CRON_RESTORE_INVALID_KEY",
    "CRON_RESTORE_INVALID_RECORD", "CRON_RESTORE_UNKNOWN_FIELD",
    "CRON_RESTORE_UNKNOWN_RECORD_FIELD", "CRON_RESTORE_INVALID_PRESENCE",
    "CRON_RESTORE_ID_MISMATCH", "CRON_RESTORE_RECORD_MISMATCH",
    "CRON_RESTORE_SCHEDULE_MISMATCH", "CRON_RESTORE_REPEAT_MISMATCH",
    "CRON_RESTORE_RUN_AT_MISMATCH", "CRON_RESTORE_DELIVERY_MISMATCH",
    "CRON_RESTORE_INVALID_NAME", "CRON_RESTORE_INVALID_ENABLED",
    "CRON_RESTORE_INVALID_STATE", "CRON_RESTORE_INVALID_SCHEDULE",
    "CRON_RESTORE_INVALID_REPEAT", "CRON_RESTORE_INVALID_SKILLS",
    "CRON_RESTORE_INVALID_TEXT_FIELD", "CRON_RESTORE_INVALID_NO_AGENT",
    "CRON_RESTORE_INVALID_DELIVERY", "CRON_RESTORE_INVALID_METADATA",
    "CRON_RESTORE_INVALID_NEXT_RUN", "CRON_RESTORE_SCRIPT_REQUIRED",
    "CRON_RESTORE_AGENT_INPUT_REQUIRED", "CRON_RESTORE_JOB_NOT_FOUND",
})


def _normalize_skills(single_skill=None, skills: Optional[Iterable[str]] = None) -> Optional[List[str]]:
    if skills is None:
        if single_skill is None:
            return None
        raw_items = [single_skill]
    else:
        raw_items = list(skills)

    normalized: List[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _cron_api(**kwargs):
    from tools.cronjob_tools import cronjob as cronjob_tool

    return json.loads(cronjob_tool(**kwargs))


def _active_cron_provider_name() -> str:
    """Name of the resolved cron scheduler provider ('builtin', 'chronos', …).

    Best-effort + offline (``resolve_cron_scheduler`` reads config and the
    provider's ``is_available()`` contract forbids network). Returns 'builtin'
    on any failure so callers fall back to the historical ticker-based checks.
    """
    try:
        from cron.scheduler_provider import resolve_cron_scheduler

        return resolve_cron_scheduler().name or "builtin"
    except Exception:
        return "builtin"


def _warn_if_gateway_not_running() -> None:
    """Warn that scheduled jobs won't fire unless the gateway is running.

    The cron ticker only runs inside the gateway (``_start_cron_ticker`` in
    gateway/run.py); there is no standalone cron daemon. Without a running
    gateway, ``next_run_at`` passes but jobs never fire and ``last_run_at``
    stays null — the most common cron support report (#51038). Surfacing this
    at create/list time, when the user is right there, prevents it.

    An external provider (e.g. Chronos) fires jobs via a NAS-mediated webhook,
    NOT the in-process ticker, so a momentarily-absent gateway process does not
    mean jobs won't fire — the warning would be a false alarm. Stay quiet for
    any non-builtin provider; the gateway-process heuristic only speaks to the
    built-in ticker's trigger.
    """
    try:
        if _active_cron_provider_name() != "builtin":
            return

        from hermes_cli.gateway import find_gateway_pids

        if find_gateway_pids():
            return
    except Exception:
        # If we can't determine gateway state, stay quiet rather than nag.
        return

    print(color("  ⚠  Gateway is not running — jobs won't fire automatically.", Colors.YELLOW))
    print(color("     Start it with: hermes gateway install", Colors.DIM))
    print(color("                    sudo hermes gateway install --system  # Linux servers", Colors.DIM))
    print(color("     Check status:  hermes cron status", Colors.DIM))


def _canonical_job(job: dict, *, include_snapshot: bool = False) -> dict:
    """Return the stable CLI projection, optionally with restore data."""
    from cron.jobs import PERSISTED_JOB_FIELDS

    # list_jobs() adds latest_execution as a derived display field.  Never put
    # that projection into a restore record, and never copy arbitrary keys from
    # a hand-edited jobs.json into the interchange format.
    record = {
        key: json.loads(json.dumps(job[key], ensure_ascii=False))
        for key in sorted(PERSISTED_JOB_FIELDS)
        if key in job
    }
    repeat = record.get("repeat")
    repeat_mapping = repeat if isinstance(repeat, dict) else {}
    schedule = record.get("schedule")
    raw_deliver = job.get("deliver") or job.get("delivery") or "local"
    if isinstance(raw_deliver, str):
        delivery_targets = [part.strip() for part in raw_deliver.split(",") if part.strip()]
    elif isinstance(raw_deliver, (list, tuple)):
        delivery_targets = []
        for value in raw_deliver:
            delivery_targets.extend(
                part.strip() for part in str(value).split(",") if part.strip()
            )
    else:
        delivery_targets = [str(raw_deliver).strip()] if raw_deliver else ["local"]
    if not delivery_targets:
        delivery_targets = ["local"]
    deliver = delivery_targets[0] if len(delivery_targets) == 1 else delivery_targets
    platform = recipient = thread = None
    if len(delivery_targets) == 1:
        parts = delivery_targets[0].split(":", 2)
        if len(parts) >= 2 and parts[0] in {"telegram", "discord", "signal"}:
            platform, recipient = parts[0], parts[1]
            thread = parts[2] if len(parts) == 3 else None
    result = {
        "id": record.get("id"), "name": record.get("name"),
        "enabled": record.get("enabled", True),
        "state": record.get("state", "scheduled"),
        "schedule": record.get("schedule_display") or (schedule or {}).get("display"),

        "run_at": (schedule or {}).get("run_at"), "next_run_at": record.get("next_run_at"),
        "repeat": repeat_mapping.get("times"), "delivery": deliver, "deliver": deliver,
        "platform": platform, "recipient": recipient, "thread": thread,
        "script": record.get("script"), "skills": list(record.get("skills") or []),
        "no_agent": record.get("no_agent", False),
        "prompt": record.get("prompt"), "model": record.get("model"),
        "provider": record.get("provider"), "workdir": record.get("workdir"),
    }
    if include_snapshot:
        result["schema_version"] = 2
        result["record"] = record
        result["presence"] = sorted(record)
        result["schedule_state"] = json.loads(json.dumps(schedule, ensure_ascii=False))
        result["repeat_state"] = json.loads(json.dumps(record.get("repeat"), ensure_ascii=False))
    return result


def cron_show(job_id: str, *, json_mode: bool = False) -> int:
    from cron.jobs import load_jobs
    job = next((item for item in load_jobs() if item.get("id") == job_id), None)
    if job is None:
        print(color("Job not found", Colors.RED))
        return 1
    canonical = _canonical_job(job, include_snapshot=True)
    if json_mode:
        print(json.dumps(canonical, ensure_ascii=False, sort_keys=True))
    else:
        print(f"{canonical['id']}  {canonical['name']}")
        print(f"  Schedule: {canonical.get('schedule_display', '?')}")
        print(f"  State: {canonical.get('state', '?')}")
    return 0


def cron_list(show_all: bool = False, *, json_mode: bool = False):
    """List all scheduled jobs."""
    from cron.jobs import list_jobs

    jobs = list_jobs(include_disabled=show_all)
    if json_mode:
        print(json.dumps([_canonical_job(job) for job in jobs], ensure_ascii=False, sort_keys=True))
        return

    if not jobs:
        print(color("No scheduled jobs.", Colors.DIM))
        print(color("Create one with 'hermes cron create ...' or the /cron command in chat.", Colors.DIM))
        return

    print()
    print(color("┌─────────────────────────────────────────────────────────────────────────┐", Colors.CYAN))
    print(color("│                         Scheduled Jobs                                  │", Colors.CYAN))
    print(color("└─────────────────────────────────────────────────────────────────────────┘", Colors.CYAN))
    print()

    for job in jobs:
        job_id = job.get("id", "?")
        name = job.get("name", "(unnamed)")
        schedule = job.get("schedule_display", job.get("schedule", {}).get("value", "?"))
        state = job.get("state", "scheduled" if job.get("enabled", True) else "paused")
        next_run = job.get("next_run_at", "?")

        # `repeat` may be present-but-null in the job record (e.g. a one-shot
        # job persisted with "repeat": null), so coalesce to {} rather than
        # relying on the dict-default, which only applies to a missing key.
        repeat_info = job.get("repeat") or {}
        repeat_times = repeat_info.get("times")
        repeat_completed = repeat_info.get("completed", 0)
        repeat_str = f"{repeat_completed}/{repeat_times}" if repeat_times else "∞"

        # `deliver` may be present-but-null in the job record (same pitfall as
        # `repeat` above), so coalesce to the default rather than relying on the
        # dict-default, which only applies to a missing key. A null value would
        # otherwise reach `", ".join(None)` and crash the whole listing (#32896).
        deliver = job.get("deliver") or ["local"]
        if isinstance(deliver, str):
            deliver = [deliver]
        deliver_str = ", ".join(deliver)

        skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
        if state == "paused":
            status = color("[paused]", Colors.YELLOW)
        elif state == "completed":
            status = color("[completed]", Colors.BLUE)
        elif job.get("enabled", True):
            status = color("[active]", Colors.GREEN)
        else:
            status = color("[disabled]", Colors.RED)

        print(f"  {color(job_id, Colors.YELLOW)} {status}")
        print(f"    Name:      {name}")
        print(f"    Schedule:  {schedule}")
        print(f"    Repeat:    {repeat_str}")
        print(f"    Next run:  {next_run}")
        print(f"    Deliver:   {deliver_str}")
        if skills:
            print(f"    Skills:    {', '.join(skills)}")
        script = job.get("script")
        if script:
            print(f"    Script:    {script}")
        if job.get("no_agent"):
            print(f"    Mode:      {color('no-agent', Colors.DIM)} (script stdout delivered directly)")
        workdir = job.get("workdir")
        if workdir:
            print(f"    Workdir:   {workdir}")

        # Execution history
        last_status = job.get("last_status")
        if last_status:
            last_run = job.get("last_run_at", "?")
            if last_status == "ok":
                status_display = color("ok", Colors.GREEN)
            else:
                status_display = color(f"{last_status}: {job.get('last_error', '?')}", Colors.RED)
            print(f"    Last run:  {last_run}  {status_display}")

        latest_execution = job.get("latest_execution")
        if latest_execution:
            print(
                f"    Execution: {latest_execution.get('status', '?')}  "
                f"{latest_execution.get('id', '?')}"
            )

        delivery_err = job.get("last_delivery_error")
        if delivery_err:
            print(f"    {color('⚠ Delivery failed:', Colors.YELLOW)} {delivery_err}")

        print()

    _warn_if_gateway_not_running()


def cron_tick():
    """Run due jobs once and exit."""
    from cron.scheduler import tick
    tick(verbose=True)


def cron_runs(job_id: Optional[str] = None, limit: int = 20):
    """Show indexed durable cron execution history."""
    from cron.executions import list_executions

    records = list_executions(job_id=job_id, limit=limit)
    if not records:
        print("No cron execution attempts recorded.")
        return
    for record in records:
        print(
            f"{record.get('id', '?')}  {record.get('status', '?'):<9}  "
            f"job={record.get('job_id', '?')}  source={record.get('source', '?')}  "
            f"{record.get('claimed_at', '?')}"
        )
        if record.get("error"):
            print(f"    {record['error']}")


def cron_status():
    """Show cron execution status."""
    from cron.jobs import list_jobs
    from hermes_cli.gateway import find_gateway_pids

    print()

    provider = _active_cron_provider_name()
    if provider != "builtin":
        # An external provider (e.g. Chronos) does NOT run the in-process 60s
        # ticker — it arms one external one-shot per job and is fired by a
        # NAS-mediated webhook, so between fires there is intentionally NO
        # ticker thread and NO heartbeat file. Reporting the ticker-heartbeat
        # staleness here would always say "stalled / not firing" on a perfectly
        # healthy Chronos instance. Report the provider instead and skip the
        # ticker-liveness heuristics entirely.
        print(color(
            f"✓ Cron provider: {provider} — jobs fire via the managed scheduler, "
            "not the in-process ticker.",
            Colors.GREEN,
        ))
        print(color(
            "  (No ticker heartbeat is expected for an external provider; "
            "due jobs are delivered by an authenticated webhook.)",
            Colors.DIM,
        ))
        print()
        _print_active_jobs_summary(list_jobs(include_disabled=False))
        print()
        return

    pids = find_gateway_pids()
    if pids:
        # The gateway PROCESS is alive — but the cron ticker THREAD inside it
        # can die silently, or stay alive while every tick fails. Check both
        # the liveness heartbeat and the last-successful-tick marker so we
        # don't report "will fire" when the ticker is dead or failing
        # (#32612, #32895).
        from cron.jobs import (
            get_ticker_heartbeat_age,
            get_ticker_last_error,
            get_ticker_success_age,
            TICKER_INTERVAL_SECONDS,
        )

        # Allow ~3 missed ticker iterations (+ a little slack) before declaring
        # trouble. Derived from the shared interval constant so this threshold
        # tracks the ticker cadence instead of assuming a hardcoded 60s.
        STALE_AFTER = TICKER_INTERVAL_SECONDS * 3 + 20  # = 200s at the 60s default
        hb_age = get_ticker_heartbeat_age()
        ok_age = get_ticker_success_age()

        if hb_age is not None and hb_age > STALE_AFTER:
            # No heartbeat at all → the ticker thread is gone.
            print(color(
                "⚠ Gateway is running but the cron ticker looks STALLED — "
                f"no heartbeat for {int(hb_age)}s (expected every ~60s).",
                Colors.YELLOW,
            ))
            print(f"  PID: {', '.join(map(str, pids))}")
            print("  Cron jobs may NOT be firing. Restart: hermes gateway restart")
        elif hb_age is not None and ok_age is not None and ok_age > STALE_AFTER:
            # Loop is alive (fresh heartbeat) but no tick has SUCCEEDED in a
            # long time → ticks are failing every iteration.
            print(color(
                "⚠ Gateway and cron ticker are running, but no tick has "
                f"succeeded in {int(ok_age)}s — ticks may be failing.",
                Colors.YELLOW,
            ))
            print(f"  PID: {', '.join(map(str, pids))}")
            last_error = get_ticker_last_error()
            if last_error:
                # Show WHY ticks fail — e.g. a root-rewritten jobs.json
                # (PermissionError) that silently locked out the ticker's
                # uid for ~14h in the field (#68483).
                print(color(f"  Last tick error: {last_error}", Colors.RED))
                if "Permission denied" in last_error:
                    print(color(
                        "  Hint: jobs.json may be owned by another user "
                        "(e.g. rewritten by a root `docker exec hermes "
                        "hermes cron ...`). Fix ownership to match the "
                        "gateway user, and prefer `docker exec -u <uid>:<gid>`.",
                        Colors.YELLOW,
                    ))
            print("  Check the gateway log for 'Cron tick error'.")
        else:
            print(color("✓ Gateway is running — cron jobs will fire automatically", Colors.GREEN))
            print(f"  PID: {', '.join(map(str, pids))}")
            if hb_age is not None:
                print(f"  Ticker heartbeat: {int(hb_age)}s ago")
    else:
        print(color("✗ Gateway is not running — cron jobs will NOT fire", Colors.RED))
        print()
        print("  To enable automatic execution:")
        print("    hermes gateway install    # Install as a user service")
        print("    sudo hermes gateway install --system  # Linux servers: boot-time system service")
        print("    hermes gateway            # Or run in foreground")

    print()

    _print_active_jobs_summary(list_jobs(include_disabled=False))

    print()


def _print_active_jobs_summary(jobs) -> None:
    """Print the '<N> active job(s)' + next-run line shared by every status
    path (built-in ticker AND external provider)."""
    if jobs:
        next_runs = [j.get("next_run_at") for j in jobs if j.get("next_run_at")]
        print(f"  {len(jobs)} active job(s)")
        if next_runs:
            print(f"  Next run: {min(next_runs)}")
    else:
        print("  No active jobs")


def cron_create(args):
    # The gateway-lifecycle guard lives in cron.jobs.create_job so it fires on
    # every job-creation path (this CLI subcommand AND the agent's `cronjob`
    # model tool, which calls create_job directly). When it blocks, create_job
    # raises GatewayLifecycleBlocked, the `cronjob` tool wrapper catches it and
    # returns it as result["error"], and the `if not result.get("success")`
    # branch below prints it in red and exits 1 — same UX as before.
    result = _cron_api(
        action="create",
        schedule=args.schedule,
        prompt=args.prompt,
        name=getattr(args, "name", None),
        deliver=getattr(args, "deliver", None),
        repeat=getattr(args, "repeat", None),
        skill=getattr(args, "skill", None),
        skills=_normalize_skills(getattr(args, "skill", None), getattr(args, "skills", None)),
        script=getattr(args, "script", None),
        workdir=getattr(args, "workdir", None),
        model=getattr(args, "model", None),
        provider=getattr(args, "model_provider", None),
        no_agent=getattr(args, "no_agent", False) or None,
    )
    if not result.get("success"):
        print(color(f"Failed to create job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1
    if getattr(args, "json", False):
        from cron.jobs import get_job
        job = get_job(result["job_id"])
        if not job:
            print("Failed to create job: canonical readback missing", file=sys.stderr)
            return 1
        print(json.dumps(_canonical_job(job, include_snapshot=True), ensure_ascii=False, sort_keys=True))
        return 0
    print(color(f"Created job: {result['job_id']}", Colors.GREEN))
    print(f"  Name: {result['name']}")
    print(f"  Schedule: {result['schedule']}")
    if result.get("skills"):
        print(f"  Skills: {', '.join(result['skills'])}")
    job_data = result.get("job", {})
    if job_data.get("script"):
        print(f"  Script: {job_data['script']}")
    if job_data.get("no_agent"):
        print("  Mode: no-agent (script stdout delivered directly)")
    if job_data.get("workdir"):
        print(f"  Workdir: {job_data['workdir']}")
    print(f"  Next run: {result['next_run_at']}")
    _warn_if_gateway_not_running()
    return 0


def cron_edit(args):
    from cron.jobs import AmbiguousJobReference, resolve_job_ref

    try:
        job = resolve_job_ref(args.job_id)
    except AmbiguousJobReference as exc:
        print(color(str(exc), Colors.RED))
        for m in exc.matches:
            print(f"  {m['id']}  (name: {m.get('name')!r})")
        return 1
    if not job:
        print(color(f"Job not found: {args.job_id}", Colors.RED))
        return 1

    existing_skills = list(job.get("skills") or ([] if not job.get("skill") else [job.get("skill")]))
    replacement_skills = _normalize_skills(getattr(args, "skill", None), getattr(args, "skills", None))
    add_skills = _normalize_skills(None, getattr(args, "add_skills", None)) or []
    remove_skills = set(_normalize_skills(None, getattr(args, "remove_skills", None)) or [])

    final_skills = None
    if getattr(args, "clear_skills", False):
        final_skills = []
    elif replacement_skills is not None:
        final_skills = replacement_skills
    elif add_skills or remove_skills:
        final_skills = [skill for skill in existing_skills if skill not in remove_skills]
        for skill in add_skills:
            if skill not in final_skills:
                final_skills.append(skill)

    job_id = job["id"]
    result = _cron_api(
        action="update",
        job_id=job_id,
        schedule=getattr(args, "schedule", None),
        prompt=getattr(args, "prompt", None),
        name=getattr(args, "name", None),
        deliver=getattr(args, "deliver", None),
        repeat=getattr(args, "repeat", None),
        skills=final_skills,
        script=getattr(args, "script", None),
        workdir=getattr(args, "workdir", None),
        model=getattr(args, "model", None),
        provider=getattr(args, "model_provider", None),
        no_agent=getattr(args, "no_agent", None),
    )
    if not result.get("success"):
        print(color(f"Failed to update job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1

    if getattr(args, "json", False):
        from cron.jobs import get_job
        updated_job = get_job(job_id)
        if not updated_job:
            print("Failed to update job: canonical readback missing", file=sys.stderr)
            return 1
        print(json.dumps(_canonical_job(updated_job, include_snapshot=True), ensure_ascii=False, sort_keys=True))
        return 0

    updated = result["job"]
    print(color(f"Updated job: {updated['job_id']}", Colors.GREEN))
    print(f"  Name: {updated['name']}")
    print(f"  Schedule: {updated['schedule']}")
    if updated.get("skills"):
        print(f"  Skills: {', '.join(updated['skills'])}")
    else:
        print("  Skills: none")
    if updated.get("script"):
        print(f"  Script: {updated['script']}")
    if updated.get("no_agent"):
        print("  Mode: no-agent (script stdout delivered directly)")
    if updated.get("workdir"):
        print(f"  Workdir: {updated['workdir']}")
    return 0


def _job_action(action: str, job_id: str, success_verb: str) -> int:
    result = _cron_api(action=action, job_id=job_id)
    if not result.get("success"):
        print(color(f"Failed to {action} job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1
    job = result.get("job") or result.get("removed_job") or {}
    print(color(f"{success_verb} job: {job.get('name', job_id)} ({job_id})", Colors.GREEN))
    if action in {"resume", "run"} and result.get("job", {}).get("next_run_at"):
        print(f"  Next run: {result['job']['next_run_at']}")
    if action == "run":
        job = result.get("job", {})
        if job.get("executed"):
            outcome = "succeeded" if job.get("execution_success") else "failed"
            print(f"  Ran now: {outcome}.")
        elif job.get("execution_skipped"):
            print(f"  {job['execution_skipped']}")
        else:
            print("  It will run on the next scheduler tick.")
    return 0


def cron_restore(args):
    from cron.jobs import restore_job
    try:
        if not getattr(args, "snapshot_stdin", False):
            raise ValueError("cron restore requires --snapshot-stdin")
        raw = sys.stdin.buffer.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("cron restore snapshot exceeds 64 KiB")
        snapshot_text = raw.decode("utf-8")
        restored = restore_job(args.job_id, json.loads(snapshot_text))
    except UnicodeDecodeError:
        print(color("Failed to restore job: CRON_RESTORE_INVALID_UTF8", Colors.RED))
        return 1
    except json.JSONDecodeError:
        print(color("Failed to restore job: CRON_RESTORE_INVALID_JSON", Colors.RED))
        return 1
    except (ValueError, TypeError) as exc:
        code = str(exc)
        if code not in _CRON_RESTORE_ERROR_CODES:
            code = "CRON_RESTORE_INVALID_SNAPSHOT"
        print(color(f"Failed to restore job: {code}", Colors.RED))
        return 1
    if restored is None:
        print(color("Failed to restore job: CRON_RESTORE_JOB_NOT_FOUND", Colors.RED))
        return 1
    try:
        from cron.scheduler import _notify_provider_jobs_changed
        _notify_provider_jobs_changed()
    except Exception:
        pass
    if getattr(args, "json", False):
        print(json.dumps(_canonical_job(restored, include_snapshot=True), ensure_ascii=False, sort_keys=True))
    else:
        print(color(f"Restored job: {args.job_id}", Colors.GREEN))
    return 0


def cron_command(args):
    """Handle cron subcommands."""
    subcmd = getattr(args, 'cron_command', None)

    if subcmd is None or subcmd == "list":
        show_all = getattr(args, 'all', False)
        cron_list(show_all, json_mode=bool(getattr(args, "json", False)))
        return 0

    if subcmd == "status":
        cron_status()
        return 0

    if subcmd == "tick":
        cron_tick()
        return 0

    if subcmd in {"runs", "history"}:
        cron_runs(getattr(args, "job_id", None), getattr(args, "limit", 20))
        return 0

    if subcmd in {"create", "add"}:
        return cron_create(args)

    if subcmd == "edit":
        return cron_edit(args)

    if subcmd == "restore":
        return cron_restore(args)

    if subcmd == "show":
        return cron_show(args.job_id, json_mode=bool(getattr(args, "json", False)))

    if subcmd == "pause":
        return _job_action("pause", args.job_id, "Paused")

    if subcmd == "resume":
        return _job_action("resume", args.job_id, "Resumed")

    if subcmd == "run":
        return _job_action("run", args.job_id, "Triggered")

    if subcmd in {"remove", "rm", "delete"}:
        return _job_action("remove", args.job_id, "Removed")

    print(f"Unknown cron command: {subcmd}")
    print("Usage: hermes cron [list|create|edit|pause|resume|run|remove|status|runs|tick]")
    sys.exit(1)
