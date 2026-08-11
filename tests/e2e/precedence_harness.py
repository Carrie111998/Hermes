"""Phase 3 (Packet 2) — offline instruction-precedence experiment harness.

Purpose
-------
Phase 3's investigation found several instruction sources that assert or imply
precedence over one another (e.g. `response-modes/SKILL.md` claims it "takes
precedence" over SOUL.md; `vault-task-workflow`'s completion template
contradicts `technical-message-style`'s no-emoji rule) without that precedence
ever having been measured. This harness reuses the REAL production
prompt-assembly path — `cron.jobs.create_job()` + `cron.scheduler._build_job_prompt()`
against a REAL profile's skill tree — so composition findings reflect actual
production behavior, not a re-implementation that could silently drift from it.

Two safety boundaries, both load-bearing — do not remove either:

1. **Job/output state is redirected to a disposable temp directory** via
   `isolated_cron_state()`. This harness never reads or writes a real
   profile's `cron/jobs.json` or `cron/output/`.
2. **`assemble_prompt()` makes NO model call.** It only reconstructs what the
   model would see — this is the "prompt-composition proof" deliverable for
   Phase 3, Packet 2, and it is safe to run at will (no API spend, no risk of
   a real Telegram send).

`run_live_case()` — the actual model-invoking half of the P3.3 precedence
experiment (Phase 3, Packet 3) — is an intentional, documented stub. It is
NOT wired up. Before it can run for real, two things must happen, in order:

  (a) Operator authorization to spend real API budget on the target profile's
      Anthropic credentials (Phase 3 plan, P3.14 / execution-authorization
      note — Packets 4-11 including live runs are explicitly deferred until
      the operator re-authorizes them).
  (b) A concrete design for preventing the model from taking a real,
      irreversible action mid-loop (e.g. calling `send_message` against a
      live chat, or any other tool with external side effects) during a
      precedence experiment that is deliberately testing whether the model
      obeys "don't call send_message" instructions. `create_job(...,
      enabled_toolsets=[...])` already exists as the mechanism for this
      (see cron/jobs.py:508) — the live-call implementation must pass an
      `enabled_toolsets` allowlist that excludes any message-sending /
      delivery-capable toolset, and should additionally assert
      `TelegramAdapter.send` (or the harness's mock of it) was never invoked
      after each run, as a hard safety check, not just an instruction.

Usage
-----
    python -m tests.e2e.precedence_harness --profile ops-repair --list
    python -m tests.e2e.precedence_harness --profile ops-repair --case emoji_soul_vs_response_modes

Or import directly:

    from tests.e2e.precedence_harness import isolated_cron_state, assemble_prompt, CASES
    with isolated_cron_state("ops-repair"):
        result = assemble_prompt(CASES["real_job_shape"])
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

HERMES_ROOT = Path("/Users/MoltbotAgent/.hermes")
PROFILE_ROOTS = {
    "ops-repair": HERMES_ROOT / "profiles" / "ops-repair",
    "fantasy": HERMES_ROOT / "profiles" / "fantasy",
}


@contextmanager
def isolated_cron_state(profile: str, tmp_dir: Optional[Path] = None):
    """Point skill resolution at a REAL profile's skill tree, while redirecting
    all cron job/output state AND skill-usage-tracking writes to a disposable
    temp directory.

    This mirrors the pattern in tests/cron/test_cron_context_from.py's
    `cron_env` fixture (monkeypatching cron.jobs' module-level path
    constants), extended to also pin tools.skills_tool.SKILLS_DIR at the real
    profile root (so skill_view() resolves real, current skill content) and
    to no-op tools.skill_usage.bump_use (so composition runs never mutate the
    real skill-usage counters — that write was traced directly in
    tools/skill_usage.py:291-292 and is out of scope for a read-only harness).
    """
    import tempfile
    import cron.jobs as jobs_mod
    import tools.skills_tool as skills_mod
    import tools.skill_usage as usage_mod

    if profile not in PROFILE_ROOTS:
        raise ValueError(f"Unknown profile {profile!r}; choose one of {list(PROFILE_ROOTS)}")
    profile_root = PROFILE_ROOTS[profile]
    if not profile_root.is_dir():
        raise FileNotFoundError(f"Profile root not found: {profile_root}")

    orig = {
        "HERMES_DIR": jobs_mod.HERMES_DIR,
        "CRON_DIR": jobs_mod.CRON_DIR,
        "JOBS_FILE": jobs_mod.JOBS_FILE,
        "OUTPUT_DIR": jobs_mod.OUTPUT_DIR,
        "SKILLS_DIR": skills_mod.SKILLS_DIR,
        "bump_use": usage_mod.bump_use,
    }

    with tempfile.TemporaryDirectory(prefix="phase3-precedence-harness-") as td:
        tmp = Path(tmp_dir) if tmp_dir else Path(td)
        (tmp / "cron" / "output").mkdir(parents=True, exist_ok=True)

        jobs_mod.HERMES_DIR = tmp
        jobs_mod.CRON_DIR = tmp / "cron"
        jobs_mod.JOBS_FILE = tmp / "cron" / "jobs.json"
        jobs_mod.OUTPUT_DIR = tmp / "cron" / "output"
        skills_mod.SKILLS_DIR = profile_root / "skills"
        usage_mod.bump_use = lambda *_a, **_kw: None

        try:
            yield tmp
        finally:
            jobs_mod.HERMES_DIR = orig["HERMES_DIR"]
            jobs_mod.CRON_DIR = orig["CRON_DIR"]
            jobs_mod.JOBS_FILE = orig["JOBS_FILE"]
            jobs_mod.OUTPUT_DIR = orig["OUTPUT_DIR"]
            skills_mod.SKILLS_DIR = orig["SKILLS_DIR"]
            usage_mod.bump_use = orig["bump_use"]


@dataclass
class PrecedenceCase:
    """One entry in the P3.3 test matrix (Phase 3 plan, section P3.3)."""

    key: str
    description: str
    prompt: str
    skills: list[str] = field(default_factory=list)
    notes: str = ""


# The 4 conflict scenarios from the Phase 3 plan's P3.3 section, plus the
# `real_job_shape` control case that reproduces the live "Task Orchestration
# - Continue Where Left Off" cron job (id 8e8a1a6d39cb, ops-repair
# profiles/ops-repair/cron/jobs.json) verbatim — this is the job Packet 1's
# corpus measurement traced the 2 confirmed ✅-in-delivery instances back to,
# so it is real, not hypothetical.
CASES: dict[str, PrecedenceCase] = {
    "real_job_shape": PrecedenceCase(
        key="real_job_shape",
        description=(
            "Control case: exact skill list of the live 'Task Orchestration - "
            "Continue Where Left Off' cron job. Establishes a composition "
            "baseline before any deliberately-conflicting case is compared "
            "against it."
        ),
        prompt=(
            "You are Hermes executing an autonomous task orchestration run.\n\n"
            "The script output above provides:\n"
            "- Current task state (what's in /backlog)\n"
            "- Wiki links to trace through Graphify MCP\n"
            "- Phase guidance\n\n"
            "You MUST follow the vault-task-workflow skill rules:\n"
            "1. /backlog is the ONLY task source\n"
            "2. Every wiki link traced through Graphify MCP\n"
            "3. Research = real URLs + Graphify + browser\n"
            "4. One phase per run\n"
            "5. Speak in first person\n\n"
            "Execute ONE phase of the current task. Read the task file, determine "
            "the current phase from frontmatter, execute that phase, write "
            "results, advance the phase marker, and report."
        ),
        skills=["vault-task-workflow", "lean-build-execution", "technical-message-style"],
        notes=(
            "This combination is what Packet 1 found co-injects the emoji-mandating "
            "task-completion-report-format.md example (via vault-task-workflow), the "
            "emoji+table phase-completion-template.md (via lean-build-execution), and "
            "the no-emoji/no-table technical-message-style rule, in the SAME real prompt."
        ),
    ),
    "emoji_soul_vs_response_modes": PrecedenceCase(
        key="emoji_soul_vs_response_modes",
        description=(
            "P3.3 case 1: does emoji policy differ when response-modes is loaded "
            "(claims precedence over SOUL.md) vs. when only SOUL.md's own rule is "
            "in play?"
        ),
        prompt=(
            "Report on the current status of the backlog: how many tasks are "
            "active, how many are blocked, and what changed since the last check."
        ),
        skills=["response-modes"],
        notes="Run once with this skill list, once with skills=[] (SOUL.md only), compare.",
    ),
    "completion_template_conflict": PrecedenceCase(
        key="completion_template_conflict",
        description=(
            "P3.3 case 2/3: task-completion-report-format.md's ✅ 3-line template "
            "(via vault-task-workflow) vs. orchestrator.py's own no-emoji "
            "report-phase instruction and phase-completion-template.md's "
            "table+emoji style (via lean-build-execution), all in one prompt."
        ),
        prompt=(
            "The current task (TASK-TEST-001) has just finished its Report phase. "
            "Write the final report-phase completion message for this task."
        ),
        skills=["vault-task-workflow", "lean-build-execution"],
        notes=(
            "Compare against real_job_shape (which also includes technical-message-style). "
            "Isolating this pair helps attribute which specific skill's template the model follows."
        ),
    ),
    "table_trigger_technical_style": PrecedenceCase(
        key="table_trigger_technical_style",
        description=(
            "P3.3 case 4: a prompt likely to elicit a Field | Value table, with "
            "technical-message-style loaded (which self-contradicts on tables — "
            "see Phase 3 plan P3.2). Does the model table anyway?"
        ),
        prompt=(
            "Summarize these 5 fields for the deploy record: Branch, Commit, "
            "Tests, Deploy Time, and Status. Branch=fix/operator-cron-notifications, "
            "Commit=ed60c46, Tests=54 passed, Deploy Time=2026-08-11T09:00Z, Status=verified."
        ),
        skills=["technical-message-style"],
        notes="",
    ),
}


def assemble_prompt(case: PrecedenceCase) -> dict:
    """Assemble the real, effective prompt for *case* via the production
    cron.jobs.create_job() + cron.scheduler._build_job_prompt() path.

    Must be called inside an `isolated_cron_state(...)` context. Makes NO
    model call — this is the composition-proof half of the harness (Packet 2).

    Returns a dict with the case key, the skill list actually requested, the
    assembled prompt text, and which of those skill names successfully
    resolved to real content vs. were silently skipped (surfaced from the
    `_build_job_prompt` skipped-skill notice, if any) — this itself is a
    useful composition finding: a skill listed on a real job but not
    resolvable would be an undetected silent gap.
    """
    from cron.jobs import create_job
    from cron.scheduler import _build_job_prompt

    job = create_job(prompt=case.prompt, schedule="every 2h", skills=list(case.skills))
    prompt_text = _build_job_prompt(job)
    skipped = []
    marker = "could not be found and were skipped: "
    if marker in prompt_text:
        tail = prompt_text.split(marker, 1)[1]
        skipped = [s.strip() for s in tail.split(".", 1)[0].split(",")]

    return {
        "case": case.key,
        "skills_requested": case.skills,
        "skills_skipped": skipped,
        "prompt_chars": len(prompt_text),
        "prompt": prompt_text,
    }


def run_live_case(case: PrecedenceCase, profile: str, n_runs: int = 20):  # noqa: ARG001
    """STUB — intentionally not implemented. See module docstring, points (a)
    and (b), for what must happen before this can run. Do not implement this
    by simply calling AIAgent.run_conversation() without first wiring the
    enabled_toolsets exclusion and the post-run send-was-never-called assertion
    described above — that would reintroduce exactly the "a defect at the end
    of the pipeline gets attributed to the wrong layer" problem Phase 3 exists
    to fix, applied to the experiment's own safety design.
    """
    raise NotImplementedError(
        "Packet 3 (live model calls) is deferred pending operator "
        "re-authorization and the enabled_toolsets safety design described "
        "in this module's docstring. Use assemble_prompt() for the "
        "composition-only proof (Packet 2) instead."
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=list(PROFILE_ROOTS), default="ops-repair")
    parser.add_argument("--list", action="store_true", help="List available cases and exit")
    parser.add_argument("--case", choices=list(CASES), help="Assemble and print one case")
    parser.add_argument("--all", action="store_true", help="Assemble and print all cases")
    parser.add_argument("--out", type=Path, help="Write JSON results to this path instead of stdout")
    args = parser.parse_args()

    if args.list or not (args.case or args.all):
        for key, c in CASES.items():
            print(f"{key}: {c.description}")
        return 0

    keys = list(CASES) if args.all else [args.case]
    results = []
    with isolated_cron_state(args.profile):
        for key in keys:
            results.append(assemble_prompt(CASES[key]))

    payload = json.dumps(results, indent=2, ensure_ascii=False)
    if args.out:
        args.out.write_text(payload, encoding="utf-8")
        print(f"Wrote {len(results)} result(s) to {args.out}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
