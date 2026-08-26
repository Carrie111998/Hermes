"""``hermes cron`` subcommand parser.

Extracted verbatim from ``hermes_cli/main.py:main()`` — same arguments, same
``func=cmd_cron`` dispatch. The handler is injected so this module does not
import ``main`` (cycle avoidance).
"""

from __future__ import annotations

from typing import Callable

from hermes_cli.subcommands._shared import add_accept_hooks_flag


def build_cron_parser(subparsers, *, cmd_cron: Callable) -> None:
    """Attach the ``cron`` subcommand (and its sub-actions) to ``subparsers``."""
    cron_parser = subparsers.add_parser(
        "cron", help="Cron job management", description="Manage scheduled tasks"
    )
    cron_subparsers = cron_parser.add_subparsers(dest="cron_command")

    # cron list
    cron_list = cron_subparsers.add_parser("list", help="List scheduled jobs")
    cron_list.add_argument("--all", action="store_true", help="Include disabled jobs")

    # cron create/add
    cron_create = cron_subparsers.add_parser(
        "create", aliases=["add"], help="Create a scheduled job"
    )
    cron_create.add_argument(
        "schedule", help="Schedule like '30m', 'every 2h', or '0 9 * * *'"
    )
    cron_create.add_argument(
        "prompt", nargs="?", help="Optional self-contained prompt or task instruction"
    )
    cron_create.add_argument("--name", help="Optional human-friendly job name")
    cron_create.add_argument(
        "--deliver",
        help="Delivery target: origin, local, telegram, discord, signal, or platform:chat_id",
    )
    cron_create.add_argument("--repeat", type=int, help="Optional repeat count")
    cron_create.add_argument(
        "--skill",
        dest="skills",
        action="append",
        help="Attach a skill. Repeat to add multiple skills.",
    )
    cron_create.add_argument(
        "--script",
        help=(
            "Path to a script under ~/.hermes/scripts/. Default mode: "
            "script stdout is injected into the agent's prompt each run. "
            "With --no-agent: the script IS the job and its stdout is "
            "delivered verbatim. .sh/.bash files run via bash, everything "
            "else via Python."
        ),
    )
    cron_create.add_argument(
        "--no-agent",
        dest="no_agent",
        action="store_true",
        default=False,
        help=(
            "Skip the LLM entirely — run --script on schedule and deliver "
            "its stdout directly. Empty stdout = silent. Classic watchdog "
            "pattern (memory alerts, disk alerts, CI pings)."
        ),
    )
    cron_create.add_argument(
        "--workdir",
        help="Absolute path for the job to run from. Injects AGENTS.md / CLAUDE.md / .cursorrules from that directory and uses it as the cwd for terminal/file/code_exec tools. Omit to preserve old behaviour (no project context files).",
    )
    cron_create.add_argument(
        "--model",
        help=(
            "Pin the model this job runs on. Highest-precedence override — "
            "beats HERMES_MODEL and config.yaml `model:` — and is re-read from "
            "the cron store every tick. Pinning at creation also exempts the "
            "job from the unpinned-drift skip, which otherwise refuses to run "
            "a job whose global config changed since it was created."
        ),
    )
    cron_create.add_argument(
        "--provider",
        help=(
            "Pin the inference provider this job runs on (anthropic, openai, "
            "a configured custom provider, ...). Same precedence and "
            "drift-exemption as --model."
        ),
    )

    # cron edit
    cron_edit = cron_subparsers.add_parser(
        "edit", help="Edit an existing scheduled job"
    )
    cron_edit.add_argument("job_id", help="Job ID to edit")
    cron_edit.add_argument("--schedule", help="New schedule")
    cron_edit.add_argument("--prompt", help="New prompt/task instruction")
    cron_edit.add_argument("--name", help="New job name")
    cron_edit.add_argument("--deliver", help="New delivery target")
    cron_edit.add_argument("--repeat", type=int, help="New repeat count")
    cron_edit.add_argument(
        "--skill",
        dest="skills",
        action="append",
        help="Replace the job's skills with this set. Repeat to attach multiple skills.",
    )
    cron_edit.add_argument(
        "--add-skill",
        dest="add_skills",
        action="append",
        help="Append a skill without replacing the existing list. Repeatable.",
    )
    cron_edit.add_argument(
        "--remove-skill",
        dest="remove_skills",
        action="append",
        help="Remove a specific attached skill. Repeatable.",
    )
    cron_edit.add_argument(
        "--clear-skills",
        action="store_true",
        help="Remove all attached skills from the job",
    )
    cron_edit.add_argument(
        "--script",
        help=(
            "Path to a script under ~/.hermes/scripts/. Pass empty string to clear. "
            "With --no-agent the script IS the job; otherwise its stdout is "
            "injected into the agent's prompt each run."
        ),
    )
    cron_edit.add_argument(
        "--no-agent",
        dest="no_agent",
        action="store_const",
        const=True,
        default=None,
        help=(
            "Enable no-agent mode on this job (requires --script or an "
            "existing script on the job)."
        ),
    )
    cron_edit.add_argument(
        "--agent",
        dest="no_agent",
        action="store_const",
        const=False,
        help="Disable no-agent mode on this job (reverts to LLM-driven execution).",
    )
    cron_edit.add_argument(
        "--workdir",
        help="Absolute path for the job to run from (injects AGENTS.md etc. and sets terminal cwd). Pass empty string to clear.",
    )
    cron_edit.add_argument(
        "--model",
        help=(
            "Pin the model this job runs on. Highest-precedence override — "
            "beats HERMES_MODEL and config.yaml `model:` — and is re-read from "
            "the cron store every tick, so it takes effect without a restart. "
            "Pass empty string to clear (job falls back to env/config)."
        ),
    )
    cron_edit.add_argument(
        "--provider",
        help=(
            "Pin the inference provider this job runs on (anthropic, openai, "
            "a configured custom provider, ...). Pass empty string to clear. "
            "A job that already carries a base_url is re-validated against the "
            "new provider and the edit is refused if the pair would send that "
            "provider's stored credential off-host."
        ),
    )

    # lifecycle actions
    cron_pause = cron_subparsers.add_parser("pause", help="Pause a scheduled job")
    cron_pause.add_argument("job_id", help="Job ID to pause")
    cron_pause.add_argument(
        "--reason",
        help=(
            "Why the job is being paused. Persisted as the job's paused_reason "
            "so a later reader can tell a deliberate pause from a broken job. "
            "Moved to paused_history on resume."
        ),
    )

    cron_resume = cron_subparsers.add_parser("resume", help="Resume a paused job")
    cron_resume.add_argument("job_id", help="Job ID to resume")

    cron_barrier = cron_subparsers.add_parser(
        "barrier",
        help="Show, set, or clear a job's resume authorization barrier",
    )
    cron_barrier.add_argument("job_id", help="Job ID or name")
    cron_barrier_mode = cron_barrier.add_mutually_exclusive_group()
    cron_barrier_mode.add_argument(
        "--set",
        dest="barrier_set",
        action="store_true",
        help=(
            "Attach a barrier. The job then refuses to resume, to be triggered, "
            "and to be admitted by the scheduler until the barrier is cleared - "
            "un-pausing it by any route is no longer enough."
        ),
    )
    cron_barrier_mode.add_argument(
        "--clear",
        dest="barrier_clear",
        action="store_true",
        help="Lift the barrier. Does NOT resume the job; resume it separately.",
    )
    cron_barrier.add_argument(
        "--reason",
        help=(
            "Required with --set (the condition that must be met before this "
            "job may run) and with --clear (the evidence that it now is). Both "
            "are archived to the job's resume_barrier_history."
        ),
    )
    cron_barrier.add_argument(
        "--caller",
        help=(
            "Who is making this change, e.g. 'diego:gate2-landed'. Required: an "
            "unattributable barrier lift is the 2026-08-26 bypass this whole "
            "mechanism exists to prevent."
        ),
    )

    cron_run = cron_subparsers.add_parser(
        "run", help="Run a job on the next scheduler tick"
    )
    cron_run.add_argument("job_id", help="Job ID to trigger")
    cron_run.add_argument(
        "--reason",
        help=(
            "Why the job is being triggered. Recorded on the cron_triggered "
            "audit event alongside the caller, so an off-schedule fire can be "
            "attributed after the fact."
        ),
    )
    add_accept_hooks_flag(cron_run)

    cron_remove = cron_subparsers.add_parser(
        "remove", aliases=["rm", "delete"], help="Remove a scheduled job"
    )
    cron_remove.add_argument("job_id", help="Job ID to remove")

    # cron status
    cron_subparsers.add_parser("status", help="Check if cron scheduler is running")

    cron_runs = cron_subparsers.add_parser(
        "runs", aliases=["history"], help="Show durable execution attempts"
    )
    cron_runs.add_argument("job_id", nargs="?", help="Optional job ID filter")
    cron_runs.add_argument("--limit", type=int, default=20, help="Rows to show (1-500)")

    # cron tick (mostly for debugging)
    cron_tick = cron_subparsers.add_parser("tick", help="Run due jobs once and exit")
    add_accept_hooks_flag(cron_tick)
    add_accept_hooks_flag(cron_parser)
    cron_parser.set_defaults(func=cmd_cron)
