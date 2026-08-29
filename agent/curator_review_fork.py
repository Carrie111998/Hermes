"""Curator LLM review fork — prompt, runtime resolution, and execution.

Owns the one part of the curator that talks to a model: the umbrella-
building consolidation prompt (``CURATOR_REVIEW_PROMPT`` /
``CURATOR_DRY_RUN_BANNER``), resolving which provider/model/credentials the
fork runs on (``_resolve_review_runtime`` / ``_resolve_review_model``), and
spawning + running the forked ``AIAgent`` itself (``_run_llm_review``).

Split out of ``agent/curator.py`` (which still owns scheduling, state,
deterministic lifecycle transitions, and report writing/rendering) so the
security-relevant fork-construction logic — toolset restriction, the
dispatch-time tool whitelist, credential resolution — has a single, bounded
owner instead of living inside an ever-growing orchestrator module.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, NamedTuple, Optional

# Deliberately "agent.curator", not __name__: this module's log records
# (e.g. the deprecated-config warning in _resolve_review_runtime) are part
# of the curator subsystem's operational log stream. Anyone who filters
# logs — or a test's caplog.at_level(logger="agent.curator") — by that name
# should keep seeing them across this module split.
logger = logging.getLogger("agent.curator")


def _strip_aux_credential(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class _ReviewRuntimeBinding(NamedTuple):
    """Provider/model for the curator review fork plus per-slot overrides."""

    provider: str
    model: str
    explicit_api_key: Optional[str]
    explicit_base_url: Optional[str]
    request_overrides: Dict[str, Any]


def _merge_request_overrides(
    runtime_overrides: Any,
    slot_extra_body: Any,
) -> Dict[str, Any]:
    """Merge resolver metadata with task-local request body fields."""
    merged = dict(runtime_overrides or {})
    if isinstance(slot_extra_body, dict) and slot_extra_body:
        extra_body = dict(merged.get("extra_body") or {})
        extra_body.update(slot_extra_body)
        merged["extra_body"] = extra_body
    return merged


# ---------------------------------------------------------------------------
# Review prompt for the forked agent
# ---------------------------------------------------------------------------

CURATOR_DRY_RUN_BANNER = (
    "═══════════════════════════════════════════════════════════════\n"
    "DRY-RUN — REPORT ONLY. DO NOT MUTATE THE SKILL LIBRARY.\n"
    "═══════════════════════════════════════════════════════════════\n"
    "\n"
    "This is a PREVIEW pass. Follow every instruction below EXCEPT:\n"
    "\n"
    "  • DO NOT call skill_manage with action=patch, create, delete, "
    "write_file, or remove_file.\n"
    "  • The curator has no terminal access. Do not attempt filesystem "
    "moves or shell-based skill mutations.\n"
    "  • skills_list and skill_view are FINE — read as much as you need.\n"
    "\n"
    "Your output IS the deliverable. Produce the exact same "
    "human-readable summary and structured YAML block you would "
    "produce on a live run — but describe the actions you WOULD take, "
    "not actions you took. A downstream reviewer will read the report "
    "and decide whether to approve a live run with "
    "`hermes curator run` (no flag).\n"
    "\n"
    "If you accidentally take a mutating action, say so explicitly in "
    "the summary so the reviewer can revert it.\n"
    "═══════════════════════════════════════════════════════════════"
)


CURATOR_REVIEW_PROMPT = (
    "You are running as Hermes' background skill CURATOR. This is an "
    "UMBRELLA-BUILDING consolidation pass, not a passive audit and not a "
    "duplicate-finder.\n\n"
    "The goal of the skill collection is a LIBRARY OF CLASS-LEVEL "
    "INSTRUCTIONS AND EXPERIENTIAL KNOWLEDGE. A collection of hundreds of "
    "narrow skills where each one captures one session's specific bug is "
    "a FAILURE of the library — not a feature. An agent searching skills "
    "matches on descriptions, not on exact names (note: long descriptions "
    "are truncated to 57 chars in the system prompt skill index — keep the "
    "trigger class in that window). One broad umbrella "
    "skill with labeled subsections beats five narrow siblings for "
    "discoverability, not the other way around.\n\n"
    "The right target shape is CLASS-LEVEL skills with rich SKILL.md "
    "bodies + `references/`, `templates/`, and `scripts/` subfiles for "
    "session-specific detail — not one-session-one-skill micro-entries.\n\n"
    "Hard rules — do not violate:\n"
    "1. DO NOT touch bundled, hub-installed, or external-dir skills "
    "(`skills.external_dirs`). The candidate list below is already filtered "
    "to local curator-managed skills only; external skills are externally "
    "owned and read-only to this background curator.\n"
    "2. DO NOT delete any skill. Archiving (moving the skill's directory "
    "into ~/.hermes/skills/.archive/) is the maximum destructive action. "
    "Archives are recoverable; deletion is not.\n"
    "3. DO NOT touch skills shown as pinned=yes. Skip them entirely.\n"
    "3b. DO NOT archive, delete, consolidate, move, or otherwise modify any "
    "skill named in the protected built-ins list (currently: plan). These "
    "back load-bearing UX (slash-command entry points referenced in docs and "
    "tips) and are filtered out of the candidate list below — never resurrect "
    "one as an archive or absorb target.\n"
    "3c. DO NOT archive or prune any skill marked `cron=yes` in the candidate "
    "list. A cron job depends on it and will fail to load it on its next "
    "run. You MAY still consolidate it into an umbrella — but only because "
    "the curator rewrites cron job skill references to follow consolidations; "
    "never simply prune it.\n"
    "4. DO NOT use usage counters as a reason to skip consolidation. The "
    "counters are new and often mostly zero. Judge overlap on CONTENT, "
    "not on use_count. 'use=0' is not evidence a skill is valuable; it's "
    "absence of evidence either way. Corollary: 'use=0' is ALSO not a "
    "reason to prune a skill on its own — a recently-created skill simply "
    "may not have had its trigger come up yet. This pass NEVER archives a "
    "skill with no forwarding target (see 'Your toolset' below — "
    "skill_manage(delete) always requires a real absorbed_into umbrella "
    "here). If a never-used skill is at least 30 days old AND its content "
    "is genuinely obsolete with nothing worth absorbing, do not call "
    "skill_manage on it — leave it and list it under `prunings` in the "
    "structured summary as a flagged recommendation for a human to action.\n"
    "5. DO NOT reject consolidation on the grounds that 'each skill has "
    "a distinct trigger'. Pairwise distinctness is the wrong bar. The "
    "right bar is: 'would a human maintainer write this as N separate "
    "skills, or as one skill with N labeled subsections?' When the "
    "answer is the latter, merge.\n\n"
    "How to work — not optional:\n"
    "1. Scan the full candidate list. Identify PREFIX CLUSTERS (skills "
    "sharing a first word or domain keyword). Examples you are likely "
    "to find: hermes-config-*, hermes-dashboard-*, gateway-*, codex-*, "
    "ollama-*, anthropic-*, gemini-*, mcp-*, salvage-*, pr-*, "
    "competitor-*, python-*, security-*, etc. Expect 10-25 clusters.\n"
    "2. For each cluster with 2+ members, do NOT ask 'are these pairs "
    "overlapping?' — ask 'what is the UMBRELLA CLASS these skills all "
    "serve? Would a maintainer name that class and write one skill for "
    "it?' If yes, pick (or create) the umbrella and absorb the siblings "
    "into it.\n"
    "3. Three ways to consolidate — use the right one per cluster:\n"
    "   a. MERGE INTO EXISTING UMBRELLA — one skill in the cluster is "
    "already broad enough to be the umbrella (example: `pr-triage-"
    "salvage` for the PR review cluster). Patch it to add a labeled "
    "section for each sibling's unique insight, then archive the "
    "siblings.\n"
    "   b. CREATE A NEW UMBRELLA SKILL.md — no existing member is broad "
    "enough. Use skill_manage action=create to write a new class-level "
    "skill whose SKILL.md covers the shared workflow and has short "
    "labeled subsections. Archive the now-absorbed narrow siblings.\n"
    "   c. DEMOTE TO REFERENCES/TEMPLATES/SCRIPTS — a sibling has "
    "narrow-but-valuable session-specific content. Move it into the "
    "umbrella's appropriate support directory:\n"
    "      • `references/<topic>.md` for session-specific detail OR "
    "condensed knowledge banks (quoted research, API docs excerpts, "
    "domain notes, provider quirks, reproduction recipes)\n"
    "      • `templates/<name>.<ext>` for starter files meant to be "
    "copied and modified\n"
    "      • `scripts/<name>.<ext>` for statically re-runnable actions "
    "(verification scripts, fixture generators, probes)\n"
    "      Then archive the old sibling through the guarded "
    "`skill_manage(action=delete, absorbed_into=<umbrella>)` path.\n\n"
    "Package integrity — not optional:\n"
    "Before demoting or archiving a skill, inspect it as a COMPLETE "
    "directory package, not just SKILL.md. A skill root may include "
    "`references/`, `templates/`, `scripts/`, and `assets/`; `skill_view` "
    "discovers those relative to the skill root. A reference markdown file "
    "inside another skill is NOT a new skill root and does not get its own "
    "linked-file discovery.\n"
    "If the source skill has support files OR SKILL.md contains relative "
    "links such as `references/...`, `templates/...`, `scripts/...`, or "
    "`assets/...`, DO NOT flatten only SKILL.md into "
    "`<umbrella>/references/<old>.md`. Choose one safe path instead:\n"
    "   • keep it as a standalone skill, OR\n"
    "   • fully merge it by re-homing every needed support file into the "
    "umbrella's canonical `references/`, `templates/`, `scripts/`, or "
    "`assets/` directories AND rewrite the destination instructions to "
    "the new paths, OR\n"
    "   • archive the entire original skill package unchanged.\n"
    "Never leave archived/demoted instructions pointing at files that were "
    "left behind under the old skill directory.\n"
    "4. Also flag skills whose NAME is too narrow (contains a PR number, "
    "a feature codename, a specific error string, an 'audit' / "
    "'diagnosis' / 'salvage' session artifact). These almost always "
    "belong as a subsection or support file under a class-level umbrella.\n"
    "5. Iterate. After one consolidation round, scan the remaining set "
    "and look for the NEXT umbrella opportunity. Don't stop after 3 "
    "merges.\n\n"
    "Your toolset:\n"
    "  - skills_list, skill_view        — read the current landscape\n"
    "    READ BEFORE WRITE — enforced, not advisory. Before skill_manage "
    "action=patch, action=edit, action=write_file on a file that already "
    "exists, or action=remove_file, call skill_view on that SAME target in "
    "this review turn — skill_view(name) for SKILL.md, "
    "skill_view(name, file_path=...) for a supporting file — and build the "
    "write from the content it just returned. A write without that read is "
    "REFUSED and nothing is saved.\n"
    "  - skill_manage action=patch      — add sections to the umbrella\n"
    "  - skill_manage action=create     — create a new umbrella SKILL.md\n"
    "  - skill_manage action=write_file — add a references/, templates/, "
    "or scripts/ file under an existing skill (the skill must already "
    "exist)\n"
    "  - skill_manage action=delete     — archive a skill you have merged "
    "into an umbrella. MUST pass `absorbed_into=<umbrella>` naming that "
    "umbrella (it must already exist on disk) — this drives cron-job "
    "skill-reference migration, atomically with the archive itself. This "
    "pass has no path to archive a skill with no forwarding target: the "
    "tool deterministically refuses `skill_manage(delete)` calls from this "
    "fork that omit `absorbed_into` or pass it empty, and keeps the skill "
    "active. If you believe a skill should be pruned outright with nothing "
    "to absorb it into, do NOT call skill_manage — just list it under "
    "`prunings` in the structured summary below as a recommendation; a "
    "human (or the separate deterministic inactivity sweep) decides "
    "whether to act on it.\n"
    "  - There is no shell/filesystem tool in this fork. Use "
    "`skill_manage(action=write_file)` for new support files and preserve the "
    "original package when safe re-homing cannot be completed.\n\n"
    "'keep' is a legitimate decision ONLY when the skill is already a "
    "class-level umbrella and none of the proposed merges would improve "
    "discoverability. 'This is narrow but distinct from its siblings' "
    "is NOT a reason to keep — it's a reason to move it under an "
    "umbrella as a subsection or support file.\n\n"
    "Expected output: real umbrella-ification. Process every obvious "
    "cluster. If you end the pass with fewer than 10 archives, you "
    "stopped too early — go back and look at the clusters you left "
    "alone.\n\n"
    "When done, write a human summary AND a structured machine-readable "
    "block so downstream tooling can distinguish consolidations you made "
    "from prune candidates you're only flagging. Format EXACTLY:\n\n"
    "## Structured summary (required)\n"
    "```yaml\n"
    "consolidations:\n"
    "  - from: <old-skill-name>\n"
    "    into: <umbrella-skill-name>\n"
    "    reason: <one short sentence — why merged, not just 'similar'>\n"
    "prunings:\n"
    "  - name: <skill-name>\n"
    "    reason: <one short sentence — why this looks safe to prune outright>\n"
    "```\n\n"
    "Every skill you archived via skill_manage(delete) MUST appear under "
    "`consolidations` with `into: Y` naming the umbrella you absorbed it "
    "into — that is the only kind of delete this pass can perform. "
    "`prunings` is different: it's a RECOMMENDATION list, not a record of "
    "actions taken. List a skill there when you judge it truly stale, "
    "irrelevant, or obsolete with nothing worth absorbing — but do NOT "
    "call skill_manage(delete) on it; leave it active and let a human or "
    "the separate deterministic inactivity sweep decide. Leave a list "
    "empty (`consolidations: []`) if none. Do not omit the block. The "
    "block comes AFTER your human-readable summary of clusters processed, "
    "patches made, and decisions left alone."
)


def _resolve_review_runtime(cfg: Dict[str, Any]) -> _ReviewRuntimeBinding:
    """Resolve provider/model and per-slot credentials for the curator review fork.

    Same precedence as `_resolve_review_model()`. Non-empty ``api_key`` /
    ``base_url`` from the active slot are returned as explicit overrides so
    ``resolve_runtime_provider`` does not silently reuse the main chat
    credential chain for a routed auxiliary model.
    """
    _main = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    _main_provider = _main.get("provider") or "auto"
    _main_model = _main.get("default") or _main.get("model") or ""

    # 1. Canonical aux task slot
    _aux = cfg.get("auxiliary", {}) if isinstance(cfg.get("auxiliary"), dict) else {}
    _cur_task = _aux.get("curator", {}) if isinstance(_aux.get("curator"), dict) else {}
    _task_provider = (_cur_task.get("provider") or "").strip() or None
    _task_model = (_cur_task.get("model") or "").strip() or None
    if _task_provider and _task_provider != "auto" and _task_model:
        return _ReviewRuntimeBinding(
            _task_provider,
            _task_model,
            _strip_aux_credential(_cur_task.get("api_key")),
            _strip_aux_credential(_cur_task.get("base_url")),
            _merge_request_overrides({}, _cur_task.get("extra_body")),
        )

    # 2. Legacy curator.auxiliary.{provider,model} (deprecated, pre-unification)
    _cur = cfg.get("curator", {}) if isinstance(cfg.get("curator"), dict) else {}
    _legacy = _cur.get("auxiliary", {}) if isinstance(_cur.get("auxiliary"), dict) else {}
    _legacy_provider = _legacy.get("provider") or None
    _legacy_model = _legacy.get("model") or None
    if _legacy_provider and _legacy_model:
        logger.info(
            "curator: using deprecated curator.auxiliary.{provider,model} "
            "config — please migrate to auxiliary.curator.{provider,model}"
        )
        return _ReviewRuntimeBinding(
            str(_legacy_provider),
            str(_legacy_model),
            _strip_aux_credential(_legacy.get("api_key")),
            _strip_aux_credential(_legacy.get("base_url")),
            _merge_request_overrides({}, _legacy.get("extra_body")),
        )

    # 3. Fall through to the main chat model
    return _ReviewRuntimeBinding(_main_provider, _main_model, None, None, {})


def _resolve_review_model(cfg: Dict[str, Any]) -> tuple[str, str]:
    """Pick (provider, model) for the curator review fork.

    Curator is a regular auxiliary task slot — ``auxiliary.curator.{provider,model}``
    — so it participates in the canonical aux-model plumbing (``hermes model`` →
    auxiliary picker, the dashboard Models tab, ``auxiliary.curator.{timeout,
    base_url,api_key,extra_body}``). ``provider: "auto"`` with an empty model
    means "use the main chat model" — same default as every other aux task.

    Legacy fallback: users who configured ``curator.auxiliary.{provider,model}``
    under the previous one-off schema still work. Precedence:
      1. ``auxiliary.curator.{provider,model}`` when both are set non-auto
      2. Legacy ``curator.auxiliary.{provider,model}`` when both are set
      3. Main ``model.{provider,default/model}`` pair
    """
    b = _resolve_review_runtime(cfg)
    return b.provider, b.model


def _run_llm_review(prompt: str) -> Dict[str, Any]:
    """Spawn an AIAgent fork to run the curator review prompt.

    Returns a dict with:
      - final: full (untruncated) final response from the reviewer
      - summary: short summary suitable for state file (240-char cap)
      - model, provider: what the fork actually ran on
      - tool_calls: list of {name, arguments} for every tool call made during
        the pass (arguments may be truncated for readability)
      - error: set if the pass failed mid-run; final/summary may still be empty

    Never raises; callers get a structured failure instead.
    """
    import contextlib
    result_meta: Dict[str, Any] = {
        "final": "",
        "summary": "",
        "model": "",
        "provider": "",
        "tool_calls": [],
        "error": None,
    }
    try:
        from run_agent import AIAgent
    except Exception as e:
        result_meta["error"] = f"AIAgent import failed: {e}"
        result_meta["summary"] = result_meta["error"]
        return result_meta

    # Resolve provider + model the same way the CLI does, so the curator
    # fork inherits the user's active main config rather than falling
    # through to an empty provider/model pair (which sends HTTP 400
    # "No models provided"). AIAgent() without explicit provider/model
    # arguments hits an auto-resolution path that fails for OAuth-only
    # providers and for pool-backed credentials.
    #
    # `_resolve_review_runtime()` honors `auxiliary.curator.{provider,model,...}`
    # (canonical aux-task slot, wired through `hermes model` → auxiliary
    # picker and the dashboard Models tab), with a legacy fallback to
    # `curator.auxiliary.{provider,model,...}`. See docs/user-guide/features/curator.md.
    _api_key = None
    _base_url = None
    _api_mode = None
    _resolved_provider = None
    _credential_pool = None
    _request_overrides: Dict[str, Any] = {}
    _max_tokens = None
    _acp_command = None
    _acp_args = None
    _model_name = ""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.runtime_provider import resolve_runtime_provider
        _cfg = load_config_readonly()
        _binding = _resolve_review_runtime(_cfg)
        _provider, _model_name = _binding.provider, _binding.model
        _rp = resolve_runtime_provider(
            requested=_provider,
            target_model=_model_name,
            explicit_api_key=_binding.explicit_api_key,
            explicit_base_url=_binding.explicit_base_url,
        )
        _api_key = _rp.get("api_key")
        _base_url = _rp.get("base_url")
        _api_mode = _rp.get("api_mode")
        _resolved_provider = _rp.get("provider") or _provider
        _credential_pool = _rp.get("credential_pool")
        _request_overrides = _merge_request_overrides(
            _rp.get("request_overrides"),
            _binding.request_overrides.get("extra_body"),
        )
        _max_tokens = _rp.get("max_output_tokens")
        _acp_command = _rp.get("command")
        _acp_args = list(_rp.get("args") or [])
        if isinstance(_rp.get("model"), str) and _rp["model"].strip():
            _model_name = _rp["model"].strip()
    except Exception as e:
        logger.debug("Curator provider resolution failed: %s", e, exc_info=True)

    result_meta["model"] = _model_name
    result_meta["provider"] = _resolved_provider or ""

    review_agent = None
    try:
        _agent_kwargs: Dict[str, Any] = {}
        if isinstance(_max_tokens, int):
            _agent_kwargs["max_tokens"] = _max_tokens
        if isinstance(_acp_command, str) and _acp_command:
            _agent_kwargs["acp_command"] = _acp_command
            _agent_kwargs["acp_args"] = _acp_args or []
        review_agent = AIAgent(
            model=_model_name,
            provider=_resolved_provider,
            api_key=_api_key,
            base_url=_base_url,
            api_mode=_api_mode,
            credential_pool=_credential_pool,
            request_overrides=_request_overrides,
            **_agent_kwargs,
            # Filesystem mutations must go through skill_manage so the
            # curator's consolidation guard can coordinate archive and
            # dependent-reference migration. Giving this fork terminal access
            # allowed a raw `mv` into .archive/ to bypass that path entirely.
            enabled_toolsets=["skills"],
            # enabled_toolsets=["skills"] is a security boundary here, not a
            # convenience filter — this fork must never be able to reach the
            # filesystem or network directly. The normal toolset resolution
            # merges in tools a plugin/overlay registered into "skills" via
            # the registry, which would silently widen this fork's callable
            # surface without anyone touching this file. Pin it to the
            # static ["skills_list", "skill_view", "skill_manage"] set.
            restrict_toolsets_to_static=True,
            # Umbrella-building over a large skill collection is worth a
            # high iteration ceiling — the pass typically takes 50-100
            # API calls against hundreds of candidate skills. The
            # single-session review path caps itself at a much smaller
            # number because it's not doing a curation sweep.
            max_iterations=9999,
            quiet_mode=True,
            platform="curator",
            skip_context_files=True,
            skip_memory=True,
        )
        # Disable recursive nudges — the curator must never spawn its own review.
        review_agent._memory_nudge_interval = 0
        review_agent._skill_nudge_interval = 0
        # Tag this fork as autonomous background curation so skill_manage's
        # background-review write guard fires. Without this the fork inherits
        # the default "assistant_tool" origin, is_background_review() is False,
        # and the external/bundled/hub-installed skill_manage guards never
        # trigger during the curation pass they exist to protect against.
        # turn_context.py binds this onto the write-origin ContextVar at turn
        # start (see agent/turn_context.py).
        review_agent._memory_write_origin = "background_review"

        # Runtime (dispatch-time) belt-and-suspenders on top of
        # restrict_toolsets_to_static above: even if some future change to
        # tool assembly (schema caching, provider-specific tool injection,
        # a permissive function-call parser) let a non-"skills" tool name
        # reach the model, this thread-local whitelist still refuses to
        # dispatch it. Same mechanism agent/background_review.py uses for
        # the memory/skill review fork (#15204) — checked at every tool
        # dispatch site via hermes_cli.plugins.resolve_pre_tool_block.
        from hermes_cli.plugins import (
            set_thread_tool_whitelist,
            clear_thread_tool_whitelist,
        )
        review_whitelist = {
            t["function"]["name"] for t in (getattr(review_agent, "tools", None) or [])
        }
        set_thread_tool_whitelist(
            review_whitelist,
            deny_msg_fmt=(
                "Curator consolidation pass denied non-whitelisted tool: "
                "{tool_name}. Only skill management tools are allowed."
            ),
        )

        # Redirect the forked agent's stdout/stderr to /dev/null while it
        # runs so its tool-call chatter doesn't pollute the foreground
        # terminal. The background-thread runner also hides it; this
        # belt-and-suspenders path matters when a caller invokes
        # run_curator_review(synchronous=True) from the CLI.
        try:
            with open(os.devnull, "w", encoding="utf-8") as _devnull, \
                 contextlib.redirect_stdout(_devnull), \
                 contextlib.redirect_stderr(_devnull):
                conv_result = review_agent.run_conversation(user_message=prompt)
        finally:
            clear_thread_tool_whitelist()

        final = ""
        if isinstance(conv_result, dict):
            final = str(conv_result.get("final_response") or "").strip()
        result_meta["final"] = final
        result_meta["summary"] = (final[:240] + "…") if len(final) > 240 else (final or "no change")

        # Collect tool calls for the report. Walk the forked agent's
        # session messages and extract every tool_call made during the
        # pass. Truncate argument payloads so a giant skill_manage create
        # doesn't blow up the report.
        _calls: List[Dict[str, Any]] = []
        for msg in getattr(review_agent, "_session_messages", []) or []:
            if not isinstance(msg, dict):
                continue
            tcs = msg.get("tool_calls") or []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                args_raw = fn.get("arguments") or ""
                if isinstance(args_raw, str) and len(args_raw) > 400:
                    args_raw = args_raw[:400] + "…"
                _calls.append({"name": name, "arguments": args_raw})
        result_meta["tool_calls"] = _calls
    except Exception as e:
        result_meta["error"] = f"error: {e}"
        result_meta["summary"] = result_meta["error"]
    finally:
        if review_agent is not None:
            try:
                review_agent.close()
            except Exception:
                pass
    return result_meta
