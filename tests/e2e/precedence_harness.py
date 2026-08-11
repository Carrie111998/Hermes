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

`run_live_case()` (Packet 3) is now wired up, gated behind `verify_tool_isolation()`
— a HARD precondition, checked from actual constructed-object state, not
assumed from reading toolsets.py. Investigation for Packet 3 found TWO
distinct tool-injection paths in `AIAgent.__init__` (run_agent.py):

  1. `self.tools = get_tool_definitions(enabled_toolsets=..., ...)` — gated by
     the toolsets system. Passing `enabled_toolsets=[]` (an EMPTY LIST, not
     None — None means "default to everything") is confirmed by direct read
     of `model_tools.py:_compute_tool_definitions` to iterate zero times,
     producing `tools_to_include = set()` and therefore zero tool schemas.
  2. TWO post-assignment mutation sites append additional schemas to
     `self.tools` AFTER step 1, bypassing enabled_toolsets entirely
     (run_agent.py:1837, :2104): memory-provider tool schemas (gated by
     `self._memory_manager`, which `skip_memory=True` forces to `None` —
     confirmed at run_agent.py:1758) and context-engine tool schemas
     (`self.context_compressor.get_tool_schemas()` — NOT gated by
     skip_memory; the base `ContextEngine.get_tool_schemas()` returns `[]`
     by default per agent/context_engine.py:151-157, but a profile config
     could select a schema-producing engine like LCM, so this must be
     checked on the actual constructed object, not assumed).

`verify_tool_isolation(profile)` constructs a real `AIAgent` with
`enabled_toolsets=[]`, `disabled_toolsets=<every known toolset name>` (belt
and suspenders — redundant with #1 but free), and `skip_memory=True`, makes
NO model call, and inspects `agent.tools` AND
`agent.context_compressor.get_tool_schemas()` directly. `run_live_case()`
refuses to proceed unless both are empty, printing the exact
`P3 PACKET 3 BLOCKED — EXPERIMENT CANNOT ISOLATE MODEL FROM ACTION TOOLS`
sentinel and raising if not.

Credential/config resolution (`resolve_runtime_provider`, called inside
`AIAgent.__init__`) is not covered by `isolated_cron_state()`'s targeted
monkeypatches — those functions resolve `HERMES_HOME` via modules not
independently pinned. `pin_hermes_home_env(profile)` sets the `HERMES_HOME`
env var directly and MUST be called before the first import of any
`hermes_cli.*` module in the process (i.e. at the very top of `_main()`,
before any deferred import happens) — this mirrors how production
"subprocess spawners... propagate HERMES_HOME explicitly" per
hermes_constants.py's own docstring, rather than relying on any single
module's caching behavior.

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


BLOCKED_SENTINEL = "P3 PACKET 3 BLOCKED — EXPERIMENT CANNOT ISOLATE MODEL FROM ACTION TOOLS"


def pin_hermes_home_env(profile: str) -> Path:
    """Set HERMES_HOME to *profile*'s root in the process environment.

    MUST be called before the first import of any hermes_cli.* module in
    this process — credential/config resolution (resolve_runtime_provider)
    is not covered by isolated_cron_state()'s targeted monkeypatches, and
    some modules snapshot HERMES_HOME at their own import time. This mirrors
    how production subprocess spawners propagate HERMES_HOME explicitly
    (hermes_constants.py's own docstring) rather than relying on any single
    module's caching behavior.
    """
    import os

    if profile not in PROFILE_ROOTS:
        raise ValueError(f"Unknown profile {profile!r}; choose one of {list(PROFILE_ROOTS)}")
    root = PROFILE_ROOTS[profile]
    os.environ["HERMES_HOME"] = str(root)
    return root


def _resolve_experiment_runtime(profile: str) -> dict:
    """Resolve model/provider/credentials exactly as cron/scheduler.py does
    (scheduler.py:1083-1200, trimmed to what AIAgent construction needs),
    so the experiment uses the SAME model and provider production cron jobs
    for this profile actually use — not an arbitrary hardcoded model string.
    Must be called after pin_hermes_home_env(profile).
    """
    import os
    import yaml

    hermes_home = PROFILE_ROOTS[profile]
    model = os.getenv("HERMES_MODEL") or ""
    cfg = {}
    cfg_path = hermes_home / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, str):
            model = model_cfg
        elif isinstance(model_cfg, dict):
            model = model_cfg.get("default", model)

    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested=None)

    return {
        "model": model,
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"),
        "api_mode": runtime.get("api_mode"),
    }


def verify_tool_isolation(profile: str) -> dict:
    """P3.0 — construct a real AIAgent with the intended experiment
    configuration and inspect its ACTUAL tool surface. No model call is
    made. This is runtime evidence (a real constructed object, real
    toolset-registry resolution, real check_fn gating, real context-engine
    selection for this profile's config.yaml) — not an assumption from
    reading toolsets.py.

    Checks BOTH known tool-injection paths in AIAgent.__init__:
      1. self.tools, built from get_tool_definitions(enabled_toolsets=[]).
      2. self.context_compressor.get_tool_schemas() — a second injection
         point that bypasses enabled_toolsets entirely (run_agent.py:2104),
         gated by which context-engine plugin the profile's config selects,
         not by skip_memory. Must be checked on the real object.
    """
    from run_agent import AIAgent
    from toolsets import get_toolset_names

    rt = _resolve_experiment_runtime(profile)
    agent = AIAgent(
        model=rt["model"],
        api_key=rt["api_key"],
        base_url=rt["base_url"],
        provider=rt["provider"],
        api_mode=rt["api_mode"],
        enabled_toolsets=[],
        disabled_toolsets=get_toolset_names(),  # belt-and-suspenders; redundant with [] above
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        platform="cron",
        load_soul_identity=False,  # tool-isolation check only; identity irrelevant here
    )

    tool_schema_names = sorted(
        t.get("function", {}).get("name") for t in (agent.tools or [])
    )

    context_tool_schemas = []
    try:
        context_tool_schemas = agent.context_compressor.get_tool_schemas() or []
    except Exception as exc:  # pragma: no cover - diagnostic only
        context_tool_schemas = [{"error": str(exc)}]
    context_tool_names = sorted(
        s.get("name", "<unnamed>") for s in context_tool_schemas if isinstance(s, dict)
    )

    safe = not tool_schema_names and not context_tool_names
    return {
        "profile": profile,
        "safe": safe,
        "primary_tool_schema_names": tool_schema_names,
        "context_engine_tool_names": context_tool_names,
        "context_engine_class": type(agent.context_compressor).__name__,
    }


# Objective response classifiers (P3.4) — defined BEFORE any run is reviewed,
# so classification criteria cannot be adjusted post-hoc based on observed
# outputs. If criteria ever need to change after seeing results, that must
# be recorded as a new, separate analysis pass, not a silent edit here.

_STATUS_EMOJI = "✅🔵⬜❌⏳"


def classify_emoji(raw_response: str) -> str:
    return "emits_status_emoji" if any(c in raw_response for c in _STATUS_EMOJI) else "no_status_emoji"


def classify_table(raw_response: str) -> str:
    import re

    sep_re = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*){1,}\|?\s*$", re.M)
    row_re = re.compile(r"^\s*\|.*\|\s*$", re.M)
    if sep_re.search(raw_response) and row_re.search(raw_response):
        return "emits_gfm_table"
    return "no_table"


def classify_completion_shape(raw_response: str) -> str:
    """contract A = orchestrator.py's plain 3-line, no-emoji template
    ('<TASK-ID> done — ...'); contract B = vault-task-workflow's ✅-prefixed
    example ('✅ <TASK-ID> done — ...'). Ambiguous responses are retained as
    'neither_ambiguous', not forced into A or B."""
    import re

    stripped = raw_response.strip()
    if re.match(r"^✅\s*\S", stripped):
        return "contract_b_emoji_prefixed"
    if re.match(r"^[A-Za-z][\w-]*\s+done\s*[—-]", stripped) or re.match(r"^[A-Za-z][\w-]*\s+done:", stripped):
        return "contract_a_plain"
    return "neither_ambiguous"


def offline_render_chain(raw_response: str) -> dict:
    """P3.6 — apply the REAL post-generation enforcement and renderer
    functions to already-captured raw output, entirely offline. Never sends
    anything to Telegram; only calls the pure-text transform functions.
    Distinguishes model obedience (raw) from sanitizer behavior
    (post_enforcement) from renderer-introduced artifacts (telegram_rendered)."""
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location(
        "phase3_cron_reply",
        str(HERMES_ROOT / "profiles" / "ops-repair" / "plugins" / "ops-deterministic" / "cron_reply.py"),
    )
    cron_reply_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cron_reply_mod)
    post_enforcement = cron_reply_mod.sanitize_cron_reply(raw_response)

    from gateway.platforms.telegram import _wrap_markdown_tables

    telegram_rendered = _wrap_markdown_tables(post_enforcement)

    return {
        "raw": raw_response,
        "post_enforcement": post_enforcement,
        "telegram_rendered": telegram_rendered,
    }


def run_live_case(case: PrecedenceCase, profile: str, n_runs: int, *, skills_override=None):
    """P3.1/P3.3 — make N real model calls for *case* and capture raw
    final_response, refusing to proceed unless verify_tool_isolation()
    confirms zero action-capable tools. Never calls _deliver_result / any
    Telegram send — only AIAgent.run_conversation() is invoked, and its
    return value is captured directly; delivery code is never reached.

    skills_override lets a paired condition (e.g. "SOUL.md only, no
    response-modes") swap out the case's default skill list without
    redefining the whole PrecedenceCase.
    """
    import hashlib

    check = verify_tool_isolation(profile)
    if not check["safe"]:
        print(BLOCKED_SENTINEL)
        print(json.dumps(check, indent=2))
        raise RuntimeError(BLOCKED_SENTINEL)

    from run_agent import AIAgent
    from toolsets import get_toolset_names

    rt = _resolve_experiment_runtime(profile)
    skills = case.skills if skills_override is None else skills_override
    with isolated_cron_state(profile):
        assembled = assemble_prompt(PrecedenceCase(case.key, case.description, case.prompt, skills))
        prompt_text = assembled["prompt"]
        prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16]

        results = []
        for rep in range(1, n_runs + 1):
            agent = AIAgent(
                model=rt["model"],
                api_key=rt["api_key"],
                base_url=rt["base_url"],
                provider=rt["provider"],
                api_mode=rt["api_mode"],
                enabled_toolsets=[],
                disabled_toolsets=get_toolset_names(),
                skip_memory=True,
                skip_context_files=True,
                quiet_mode=True,
                platform="cron",
                load_soul_identity=True,
            )
            # Re-verify on THIS instance too — cheap, and closes any gap
            # between the pre-check instance and the one actually used.
            live_tool_names = sorted(t.get("function", {}).get("name") for t in (agent.tools or []))
            live_ctx_names = sorted(
                s.get("name", "<unnamed>")
                for s in (agent.context_compressor.get_tool_schemas() or [])
                if isinstance(s, dict)
            )
            if live_tool_names or live_ctx_names:
                print(BLOCKED_SENTINEL)
                raise RuntimeError(BLOCKED_SENTINEL)

            call_result = agent.run_conversation(prompt_text)
            raw_response = call_result.get("final_response", "") if isinstance(call_result, dict) else str(call_result)

            results.append({
                "case": case.key,
                "profile": profile,
                "repetition": rep,
                "n_runs": n_runs,
                "prompt_hash": prompt_hash,
                "skills": skills,
                "model": agent.model,
                "provider": getattr(agent, "provider", None),
                "raw_final_response": raw_response,
                "classify_emoji": classify_emoji(raw_response),
                "classify_table": classify_table(raw_response),
                "classify_completion_shape": classify_completion_shape(raw_response),
                "effective_tool_names": live_tool_names,
                "effective_context_tool_names": live_ctx_names,
            })

    return {
        "case": case.key,
        "prompt_hash": prompt_hash,
        "prompt_chars": len(prompt_text),
        "n_runs": n_runs,
        "runs": results,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=list(PROFILE_ROOTS), default="ops-repair")
    parser.add_argument("--list", action="store_true", help="List available cases and exit")
    parser.add_argument("--case", choices=list(CASES), help="Assemble and print/run one case")
    parser.add_argument("--all", action="store_true", help="Assemble and print all cases (composition-only)")
    parser.add_argument("--verify-isolation", action="store_true", help="P3.0: construct-only tool-isolation check, no model call")
    parser.add_argument("--live", type=int, metavar="N", help="P3.1/P3.3: make N real model calls for --case")
    parser.add_argument("--skills-override", nargs="*", help="Override --case's skill list for a paired condition")
    parser.add_argument("--out", type=Path, help="Write JSON results to this path instead of stdout")
    args = parser.parse_args()

    # Must happen before ANY hermes_cli-dependent import in this process.
    pin_hermes_home_env(args.profile)

    if args.verify_isolation:
        check = verify_tool_isolation(args.profile)
        print(json.dumps(check, indent=2))
        if not check["safe"]:
            print(BLOCKED_SENTINEL)
            return 1
        return 0

    if args.live is not None:
        if not args.case:
            parser.error("--live requires --case")
        result = run_live_case(CASES[args.case], args.profile, args.live, skills_override=args.skills_override)
        payload = json.dumps(result, indent=2, ensure_ascii=False)
        if args.out:
            args.out.write_text(payload, encoding="utf-8")
            print(f"Wrote live results to {args.out}")
        else:
            print(payload)
        return 0

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
