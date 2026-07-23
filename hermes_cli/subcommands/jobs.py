"""``hermes jobs`` read-only diagnostics parser."""

from __future__ import annotations

from collections.abc import Callable


def _add_json_flag(parser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON",
    )


def build_jobs_parser(subparsers, *, cmd_jobs: Callable) -> None:
    parser = subparsers.add_parser(
        "jobs",
        help="Inspect long-running job timing, blockers, and safe resume state",
        description=(
            "Read profile-scoped Hermes job diagnostics. These commands never "
            "launch, retry, stop, or mutate a job."
        ),
    )
    actions = parser.add_subparsers(dest="jobs_action")

    status = actions.add_parser(
        "status",
        aliases=["dashboard"],
        help="Show active, blocked, long-running, and idle jobs",
    )
    _add_json_flag(status)

    show = actions.add_parser("show", help="Show one job and all of its lanes")
    show.add_argument("job_id")
    _add_json_flag(show)

    why = actions.add_parser(
        "why-slow",
        aliases=["why"],
        help="Explain where one job is spending time",
    )
    why.add_argument("job_id")
    why.add_argument(
        "--lane", help="Inspect one lane instead of the latest active lane"
    )

    parallel = actions.add_parser(
        "parallel",
        help="Recommend safe parallel work without launching anything",
    )
    parallel.add_argument("job_id", nargs="?", help="Limit analysis to one job")

    resume = actions.add_parser(
        "resume-plan",
        aliases=["resume"],
        help="Validate and print the last safe resume checkpoint",
    )
    resume.add_argument("job_id")
    resume.add_argument("--lane", help="Validate a specific lane")
    _add_json_flag(resume)

    parser.set_defaults(func=cmd_jobs)
