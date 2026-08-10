"""
Cron job scheduler - executes due jobs.

Provides tick() which checks for due jobs and runs them. The gateway
calls this every 60 seconds from a background thread.

Uses a file-based lock (~/.hermes/cron/.tick.lock) so only one tick
runs at a time if multiple processes overlap.
"""

import asyncio
import atexit
import concurrent.futures
import contextvars
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

# fcntl is Unix-only; on Windows use msvcrt for file locking
try:
    import fcntl
except ImportError:
    fcntl = None
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
from pathlib import Path
from typing import Any, List, Optional

# Add parent directory to path for imports BEFORE repo-level imports.
# Without this, standalone invocations (e.g. after `hermes update` reloads
# the module) fail with ModuleNotFoundError for hermes_time et al.
sys.path.insert(0, str(Path(__file__).parent.parent))

from hermes_constants import get_hermes_home
from hermes_cli._subprocess_compat import windows_hide_flags
from hermes_cli.config import (
    _expand_env_vars,
    cron_model_drift_guard_enabled,
    load_config,
)
from hermes_cli.fallback_config import get_fallback_chain
from hermes_time import now as _hermes_now
from agent.interrupt_compat import request_hard_interrupt
from agent.delegation_context import (
    enter_non_dispatcher_owned_context,
    exit_non_dispatcher_owned_context,
)

logger = logging.getLogger(__name__)


def _close_late_session_db(future) -> None:
    """Close SessionDB returned after the cron init wait timed out."""
    try:
        session_db = future.result()
    except Exception:
        return
    try:
        session_db.close()
    except Exception:
        logger.debug("Late cron SessionDB init returned an uncloseable handle", exc_info=True)


def _set_cron_session_title(session_db, session_id, base_title):
    """Robustly title a finished cron session before it is closed.

    Centralizes the title write so the cron finally block can guarantee a
    non-blank, unique title is persisted before end_session()/close() tear
    the connection down (issues #50535, #50536, #50537):

    - #50535: never leaves the session blank. base_title already carries a
      cron-id fallback for nameless jobs; this also guards a failed write.
    - #50537: a duplicate title makes set_session_title raise ValueError (the
      unique-title index). Recover by appending a #N suffix via
      get_next_title_in_lineage() when supported, instead of swallowing the
      error and ending up untitled. If lineage dedup is unavailable, raise.
    - #50536: this runs synchronously in the cron finally block ahead of the
      session close, so no in-flight title write can race the close.

    Returns the title actually persisted, or None if nothing could be set.
    """
    if not session_db or not session_id:
        return None
    title = (base_title or "").strip()
    if not title:
        return None
    try:
        session_db.set_session_title(session_id, title)
        return title
    except ValueError:
        # Title collision against the unique-title index. Fall back to the
        # next title in the lineage (base #2, base #3, ...) when supported.
        next_title_fn = getattr(session_db, "get_next_title_in_lineage", None)
        if next_title_fn is None:
            raise
        deduped = next_title_fn(title)
        if not deduped or deduped == title:
            raise
        session_db.set_session_title(session_id, deduped)
        return deduped


def _summarize_cron_failure_for_delivery(job: dict, error: str | None) -> str:
    """Return a compact one-line failure message for chat delivery.

    Full details stay in the cron output directory and the logs. Chat should
    show the operator what broke without dumping provider JSON, retry noise, or
    stack traces into the delivery channel.
    """
    job_name = job.get("name") or job.get("id") or "cron job"
    text = (error or "unknown error").strip()
    lower = text.lower()

    # Provider/API failures are the common noisy path. Keep these short.
    if "429" in text or "rate limit" in lower or "usage limit" in lower:
        reason = "rate limit"
        if "weekly usage limit" in lower:
            reason = "weekly usage limit"
        elif "quota" in lower:
            reason = "quota limit"
        return (
            f"âš ï¸ Cron '{job_name}' failed: provider {reason}. "
            "Fallback chain was exhausted or unavailable. "
            "Full details saved in cron output."
        )

    if "readtimeout" in lower or "timed out" in lower or "timeout" in lower:
        return (
            f"âš ï¸ Cron '{job_name}' failed: provider timeout. "
            "Fallback chain was exhausted or unavailable. "
            "Full details saved in cron output."
        )

    # Match authentication/authorization wording at a word boundary and the
    # 401/403 status codes as whole tokens, so "oauth", "4015" and similar do
    # not trip a misleading auth message.
    if re.search(r"authenticat|authoriz", lower) or re.search(r"\b(401|403)\b", text):
        return (
            f"âš ï¸ Cron '{job_name}' failed: provider authentication error. "
            "Full details saved in cron output."
        )

    # Strip common exception wrappers and collapse provider payloads. Bound
    # the input first so a multi-KB provider blob cannot slow the
    # substitutions.
    cleaned = re.sub(
        r"^(RuntimeError|Exception|ValueError|HTTPStatusError):\s*",
        "", text[:2000],
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 180:
        cleaned = cleaned[:177].rstrip() + "..."
    return f"âš ï¸ Cron '{job_name}' failed: {cleaned}"


class CronPromptInjectionBlocked(Exception):
    """Raised by _build_job_prompt when the fully-assembled prompt trips the
    injection scanner. Caught in run_job so the operator sees a clean
    "job blocked" delivery instead of the scheduler crashing.

    Assembled-prompt scanning (including loaded skill content) plugs the
    gap from #3968: create-time scanning only covers the user-supplied
    prompt field; skill content loaded at runtime was never scanned, so a
    malicious skill could carry an injection payload that reached the
    non-interactive (auto-approve) cron agent.
    """


def _resolve_cron_disabled_toolsets(cfg: dict) -> list[str]:
    """Toolsets a cron-spawned agent must never receive.

    Four protected toolsets are always disabled in cron context:
      - ``cronjob`` â€” would let a cron-spawned agent schedule more cron jobs
      - ``messaging`` â€” interactive, needs a live gateway session
      - ``clarify`` â€” interactive, blocks waiting for user input
      - ``memory`` â€” cron agents are constructed with ``skip_memory=True``, so
        exposing this tool only gives the model an unbacked tool that fails

    User-level ``agent.disabled_toolsets`` from config.yaml is layered on top
    so per-job ``enabled_toolsets`` cannot bypass policy that applies to
    ordinary agent runs (#25752 â€” LLM-supplied enabled_toolsets was widening
    past config.yaml's denylist).
    """
    disabled = ["cronjob", "messaging", "clarify", "memory"]
    agent_cfg = (cfg or {}).get("agent") or {}
    user_disabled = agent_cfg.get("disabled_toolsets") or []
    for name in user_disabled:
        name = str(name).strip()
        if name and name not in disabled:
            disabled.append(name)
    return disabled


def _merge_mcp_into_per_job_toolsets(per_job: list[str], cfg: dict) -> list[str]:
    """Layer enabled MCP servers onto a per-job ``enabled_toolsets`` allowlist.

    A per-job list scopes the *native* toolsets, but on its own it silently
    drops every MCP server: ``discover_mcp_tools()`` registers the tools into
    the global registry, yet ``get_tool_definitions(enabled_toolsets=...)``
    only keeps toolsets named in the list. The agent then rejects every
    ``mcp_*`` call with "Unknown tool". This restores parity with
    ``_get_platform_tools`` MCP semantics:

      * ``no_mcp`` sentinel present  -> no MCP servers (sentinel stripped)
      * one or more MCP server names already listed -> treat as an allowlist,
        add nothing further (the user named exactly the servers they want)
      * otherwise -> union in every globally-enabled MCP server
    """
    result = [t for t in per_job if t != "no_mcp"]
    if "no_mcp" in per_job:
        return result
    # lazy import: avoid heavy hermes_cli import at cron module load (matches
    # _resolve_cron_enabled_toolsets' fallback) and share one MCP-membership
    # computation with the gateway/CLI platform resolver.
    from hermes_cli.tools_config import enabled_mcp_server_names
    enabled_mcp = enabled_mcp_server_names(cfg)
    if set(result) & enabled_mcp:
        return result
    for name in sorted(enabled_mcp):
        if name not in result:
            result.append(name)
    return result


def _resolve_cron_enabled_toolsets(job: dict, cfg: dict) -> list[str] | None:
    """Resolve the toolset list for a cron job.

    Precedence:
    1. Per-job ``enabled_toolsets`` (set via ``cronjob`` tool on create/update).
       Keeps the agent's job-scoped toolset override intact â€” #6130. Enabled
       MCP servers are layered on per ``_merge_mcp_into_per_job_toolsets`` so a
       native-toolset allowlist does not silently strip MCP tools.
    2. Per-platform ``hermes tools`` config for the ``cron`` platform.
       Mirrors gateway behavior (``_get_platform_tools(cfg, platform_key)``)
       so users can gate cron toolsets globally without recreating every job.
    3. ``None`` on any lookup failure â€” AIAgent loads the full default set
       (legacy behavior before this change, preserved as the safety net).

    _DEFAULT_OFF_TOOLSETS ({moa, homeassistant, rl}) are removed by
    ``_get_platform_tools`` for unconfigured platforms, so fresh installs
    get cron WITHOUT ``moa`` by default (issue reported by Norbert â€”
    surprise $4.63 run).
    """
    per_job = job.get("enabled_toolsets")
    if per_job:
        return _merge_mcp_into_per_job_toolsets(list(per_job), cfg or {})
    try:
        from hermes_cli.tools_config import _get_platform_tools  # lazy: avoid heavy import at cron module load
        return sorted(_get_platform_tools(cfg or {}, "cron"))
    except Exception as exc:
        logger.warning(
            "Cron toolset resolution failed, falling back to full default toolset: %s",
            exc,
        )
        return None

# Valid delivery platforms â€” used to validate user-supplied platform names
# in cron delivery targets, preventing env var enumeration via crafted names.
_KNOWN_DELIVERY_PLATFORMS = frozenset({
    "telegram", "discord", "slack", "whatsapp", "signal",
    "matrix", "mattermost", "homeassistant", "dingtalk", "feishu",
    "wecom", "wecom_callback", "weixin", "sms", "email", "webhook", "bluebubbles",
    "qqbot", "yuanbao",
})

# Platforms that support a configured cron/notification home target, mapped to
# the environment variable used by gateway setup/runtime config.
_HOME_TARGET_ENV_VARS = {
    "matrix": "MATRIX_HOME_ROOM",
    "telegram": "TELEGRAM_HOME_CHANNEL",
    "discord": "DISCORD_HOME_CHANNEL",
    "slack": "SLACK_HOME_CHANNEL",
    "signal": "SIGNAL_HOME_CHANNEL",
    "mattermost": "MATTERMOST_HOME_CHANNEL",
    "sms": "SMS_HOME_CHANNEL",
    "email": "EMAIL_HOME_ADDRESS",
    "dingtalk": "DINGTALK_HOME_CHANNEL",
    "feishu": "FEISHU_HOME_CHANNEL",
    "wecom": "WECOM_HOME_CHANNEL",
    "weixin": "WEIXIN_HOME_CHANNEL",
    "bluebubbles": "BLUEBUBBLES_HOME_CHANNEL",
    "qqbot": "QQBOT_HOME_CHANNEL",
    "whatsapp": "WHATSAPP_HOME_CHANNEL",
    "whatsapp_cloud": "WHATSAPP_CLOUD_HOME_CHANNEL",
}

# Legacy env var names kept for back-compat.  Each entry is the current
# primary env var â†’ the previous name.  _get_home_target_chat_id falls
# back to the legacy name if the primary is unset, so users who set the
# old name before the rename keep working until they migrate.
_LEGACY_HOME_TARGET_ENV_VARS = {
    "QQBOT_HOME_CHANNEL": "QQ_HOME_CHANNEL",
}

from cron.jobs import get_due_jobs, mark_job_run, save_job_output, advance_next_runs, claim_dispatch, heartbeat_run_claim
from cron.executions import create_execution, finish_execution, mark_execution_running

# Sentinel: when a cron agent has nothing new to report, it can start its
# response with this marker to suppress delivery.  Output is still saved
# locally for audit.
SILENT_MARKER = "[SILENT]"

# Canonical silence tokens recognized in cron output.  Cron's contract is
# intentionally looser than the gateway's exact-whole-response rule: the cron
# system prompt *instructs* the agent to emit "[SILENT]", and real agents often
# bracket it with a short note or trailing newline.  We therefore suppress when
# a marker is the entire response OR appears as its own first/last line â€” but
# NOT when a token merely appears mid-sentence in a genuine report (e.g.
# "I considered staying [SILENT] but here is the summaryâ€¦" must deliver).
# The actual matcher is shared with the webhook lane â€”
# gateway.response_filters.is_autonomous_silence_response â€” so the two
# autonomous lanes cannot drift apart.


def _is_cron_silence_response(text: str) -> bool:
    """Return True when a cron final response should suppress delivery.

    Recognizes the bracketed ``[SILENT]`` sentinel (whole-response, first line,
    or last line) plus the bracketless ``SILENT`` / ``NO_REPLY`` / ``NO REPLY``
    variants the model emits when it drops the brackets (#51438, #46917).
    Whitespace-trimmed and case-insensitive.  A token buried mid-sentence is
    treated as real content and delivered.

    Delegates to the shared autonomous-lane matcher in
    :mod:`gateway.response_filters` (also used by the webhook adapter).
    """
    from gateway.response_filters import is_autonomous_silence_response

    return is_autonomous_silence_response(text)

# ---------------------------------------------------------------------------
# Persistent thread pool for parallel cron jobs.
# The tick function submits jobs here and returns immediately so the ticker
# thread is never blocked by long-running jobs (e.g. the fixer running 15+ min).
# ---------------------------------------------------------------------------
_parallel_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
_parallel_pool_max_workers: Optional[int] = None
_running_job_ids: set = set()
_running_lï]¸ÖÚ$z{-®éÜj×6öâV6‚F–6²’Â6òF†RFVfVÇ@Ð¢F‚—2Væ6†ævVBâÆ—fW2†W&R†æ÷B–â7&öâö¦ö'2ç’’Fò¶VWF†R7F÷&Rg&VPÐ¢öb&÷f–FW"–×÷'G2(	Bfö–G2â–×÷'B7–6ÆRæB¶VW2¦ö'2ç’Æ÷rÖ6÷WÆ–æràÐ¢æWfW"&—6W2–çFòF†R6ÆÆW"àÐ¢"" Ð¢G'“ Ð¢g&öÒ7&öâç66†VGVÆW%÷&÷f–FW"–×÷'B&W6öÇfUö7&öå÷66†VGVÆW Ð¢&W6öÇfUö7&öå÷66†VGVÆW"‚’æöåö¦ö'5ö6†ævVB‚Ð¢W†6WBW†6WF–öâ2S Ð¢ÆövvW"æFV'Vr‚&öåö¦ö'5ö6†ævVBæ÷F–g’f–ÆVC¢W2"ÂRÐ Ð Ð¦6Æ727&öå66†VGVÆW%&Vv—7G&F–öäW'&÷"…'VçF–ÖTW'&÷"“ Ð¢""$¦ö"v2W'6—7FVB'WB—G2f—'7BW‡FW&æÂG&–vvW"v2æ÷B&Vv—7FW&VBâ"" Ð Ð¢FVbõö–æ—Eõò‡6VÆbÂ¦ö#¢F–7BÂ6W6S¢W†6WF–öâ’ÓâæöæS Ð¢6VÆbæ¦ö"Ò¦ö Ð¢6VÆbæ6W6RÒ6W6PÐ¢7WW"‚’åõö–æ—Eõò€Ð¢b$7&öâ¦ö"w¶¦ö%²v–Bu×Òrv26fVBÂ'WB—G2f—'7B66†VGVÆW" Ð¢b'&Vv—7G&F–öâf–ÆVB‡·G—R†6W6R’åõöæÖUõ÷Ò’âFòæ÷B7&VFR Ð¢&GWÆ–6FRâW6R÷&W7VÖR÷"WFFRF†R¦ö"Fò&WG'’&Vv—7G&F–öââ Ð¢Ð Ð¢FVbW6W%öÖW76vR‡6VÆb’Óâ7G# Ð¢""$‡VÖâÖf6–ærf&–çBf÷"6†Bô4Ä’7W&f6W2†æòW†6WF–öâ6Æ72æÖR’â"" Ð¢Æ&VÂÒ6VÆbæ¦ö"ævWB‚&æÖR"’÷"6VÆbæ¦ö%²&–B%ÐÐ¢&WGW&â€Ð¢b%6fVB7&öâ¦ö"w¶Æ&VÇÒrÂ'WB6÷VÆFâwB&Vv—7FW"—Bv—F‚F†R Ð¢&W‡FW&æÂ66†VGVÆW"–WBâF†R¦ö"—2¶WB(	BFöâwB&RÖ7&VFR—C² Ð¢'W6R÷&W7VÖR÷"VF—B—B†Rærâf–ö7&öâ’Fò&WG'’&Vv—7G&F–öââ Ð¢Ð Ð¢FVbFõöF–7B‡6VÆb’ÓâF–7C Ð¢""%&WGW&âF†RV&Æ–2'F–ÂÖf–ÇW&R6öçG&7Bv—F†÷WB&÷f–FW"FWF–Ç2â"" Ð¢&WGW&â°Ð¢&W'&÷"#¢7G"‡6VÆb’ÀÐ¢&¦ö%ö–B#¢6VÆbæ¦ö%²&–B%ÒÀÐ¢&¦ö%÷6fVB#¢G'VRÀÐ¢'66†VGVÆW%÷&Vv—7FW&VB#¢fÇ6RÀÐ¢'&WG'•ö7&VFR#¢fÇ6RÀÐ¢ÐÐ Ð Ð¦FVb7&VFUö¦ö%÷v—F…÷66†VGVÆW%÷&Vv—7G&F–öâ‚¢¦·v&w2’ÓâF–7C Ð¢""%W'6—7BöæR¦ö"æB&Vv—7FW"—G2f—'7BG&–vvW"v—F‚F†R7F—fR&÷f–FW"â"" Ð¢g&öÒ7&öâæ¦ö'2–×÷'B7&VFUö¦ö Ð¢g&öÒ7&öâç66†VGVÆW%÷&÷f–FW"–×÷'B&W6öÇfUö7&öå÷66†VGVÆW Ð Ð¢¦ö"Ò7&VFUö¦ö"‚¢¦·v&w2Ð¢G'“ Ð¢&W6öÇfUö7&öå÷66†VGVÆW"‚’ç&Vv—7FW%ö¦ö"†¦ö"Ð¢W†6WBW†6WF–öâ2W†3 Ð¢&—6R7&öå66†VGVÆW%&Vv—7G&F–öäW'&÷"†¦ö"ÂW†2’g&öÒW†0Ð¢&WGW&â¦ö Ð Ð Ð¦FVbF–6²€Ð¢fW&&÷6S¢&ööÂÒG'VRÀÐ¢FFW'3ÔæöæRÀÐ¢Æö÷ÔæöæRÀÐ¢7–æ3¢&ööÂÒG'VRÀÐ¢¢ÀÐ¢6åöF—7F6ƒÔæöæRÀÐ¢“ Ð¢"" Ð¢6†V6²æB'VâÆÂGVR¦ö'2àÐ¢ Ð¢W6W2f–ÆRÆö6²6òöæÇ’öæRF–6²'Vç2BF–ÖRÂWfVâ–bF†RvFWv’w0Ð¢–â×&ö6W72F–6¶W"æB7FæFÆöæRFVÖöâ÷"ÖçVÂF–6²÷fW&ÆàÐ¢ Ð¢&w3 Ð¢fW&&÷6S¢v†WF†W"Fò&–çB7FGW2ÖW76vW0Ð¢FFW'3¢÷F–öæÂF–7BÖ–ærÆFf÷&Ò(i"Æ—fRFFW"†g&öÒvFWv’Ð¢Æö÷¢÷F–öæÂ7–æ6–òWfVçBÆö÷†g&öÒvFWv’’f÷"Æ—fRFFW"6VæG0Ð¢6åöF—7F6ƒ¢÷F–öæÂ7–æ6‡&öæ÷W2vFS²fÇ6RÆVfW2GVR¦ö'2VçF÷V6†V@Ð¢f÷"F†RæW‡BÆÆ÷vVBF–6°Ð Ð¢&WGW&ç3 Ð¢çVÖ&W"öb¦ö'2W†V7WFVBƒ–bæ÷F†W"F–6²—2Ç&VG’'Vææ–ærÐ¢"" Ð¢Æö6µöF—"ÂÆö6µöf–ÆRÒövWEöÆö6µ÷F‡2‚Ð¢Æö6µöF—"æÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VRÐ Ð¢27&÷72×ÆFf÷&Òf–ÆRÆö6¶–æs¢f6çFÂöâVæ—‚Â×7f7'Böâv–æF÷w0Ð¢Æö6µöfBÒæöæPÐ¢G'“ Ð¢Æö6µöfBÒ÷Vâ†Æö6µöf–ÆRÂ'r"ÂVæ6öF–æsÒ'WFbÓ‚"Ð¢–bf6çFÃ Ð¢f6çFÂæfÆö6²†Æö6µöfBÂf6çFÂäÄô4µôU‚Âf6çFÂäÄô4µôä"Ð¢VÆ–b×7f7'C Ð¢×7f7'BæÆö6¶–ær†Æö6µöfBæf–ÆVæò‚’Â×7f7'BäÄµôä$Ä4²ÂÐ¢W†6WB„õ4W'&÷"Â”ôW'&÷"“ Ð¢ÆövvW"æFV'Vr‚%F–6²6¶—VB(	Bæ÷F†W"–ç7Fæ6R†öÆG2F†RÆö6²"Ð¢–bÆö6µöfB—2æ÷BæöæS Ð¢Æö6µöfBæ6Æ÷6R‚Ð¢&WGW&â Ð Ð¢G'“ Ð¢2vÆö&ÂVÖW&vVæ7’7F÷††W&ÖW2W6V“¢6¶—F—7F6‚VçF—&VÇ’v†–ÆPÐ¢2F†RU5Dõ6VçF–æVÂW†—7G2âæWfW"F÷V6†W2–âÖfÆ–v‡B'Vç2(	BGVR¦ö'0Ð¢26–×Ç’v—Bf÷"F†RæW‡BF–6²gFW"†W&ÖW2&W7VÖVâÆövvVBöæ6RW Ð¢2VævvVÖVçB†æ÷BWfW'’F–6²’'’6†V6µ÷W6VBàÐ¢G'“ Ð¢g&öÒvVçBæW7F÷–×÷'B6†V6µ÷W6VB2öW7F÷ö6†V6µ÷W6V@Ð¢–böW7F÷ö6†V6µ÷W6VB‚&7&öâ"ÂÆövvW"“ Ð¢&WGW&â Ð¢W†6WB–×÷'DW'&÷# Ð¢70Ð Ð¢–b6åöF—7F6‚—2æ÷BæöæRæBæ÷B6åöF—7F6‚‚“ Ð¢ÆövvW"æFV'Vr‚$7&öâF—7F6‚W6VBv†–ÆRvFWv’G&–ç2W†—7F–ærv÷&²"Ð¢&WGW&â Ð Ð¢GVUö¦ö'2ÒvWEöGVUö¦ö'2‚Ð Ð¢–bæ÷BGVUö¦ö'3 Ð¢2–FÆRF–6³¢6¶—6öæf–rÆöB²ööÂ'F—F–öæ–ærVçF—&VÇÐ¢2‚333c"(	BF†RvFWv’F–6¶W"6ÆÇ2F–6²‡fW&&÷6SÔfÇ6R’WfW'Ð¢2c2Â6ò–FÆRF–6·2&Wf–÷W6Ç’fVÆÂF‡&÷Vv‚FòÆöEö6öæf–r‚’’àÐ¢27F–ÆÂ'VâF†R÷7B×F–6²Ô5÷'†â7vVW¢Ö–â–çFVçF–öæÆÇÐ¢27vVW2öâ–FÆRF–6·26ò÷'†æVB7FF–ò6†–ÆG&Vâg&öÒ7&6†V@Ð¢2¦ö'2&R&VVBWfVâv†Vâæ÷F†–ær—2GVRàÐ¢–bfW&&÷6S Ð¢ÆövvW"æ–æfò‚"W2Òæò¦ö'2GVR"Âö†W&ÖW5öæ÷r‚’ç7G&gF–ÖR‚rTƒ¢TÓ¢U2r’Ð¢G'“ Ð¢g&öÒFööÇ2æÖ7÷FööÂ–×÷'Bö¶–ÆÅö÷'†æVEöÖ7ö6†–ÆG&VàÐ¢ö¶–ÆÅö÷'†æVEöÖ7ö6†–ÆG&Vâ‚Ð¢W†6WBW†6WF–öâ2öS Ð¢ÆövvW"æFV'Vr‚%÷7B×F–6²Ô5÷'†â6ÆVçWf–ÆVC¢W2"ÂöRÐ¢&WGW&â Ð Ð¢–bfW&&÷6S Ð¢ÆövvW"æ–æfò‚"W2ÒW2¦ö"‡2’GVR"Âö†W&ÖW5öæ÷r‚’ç7G&gF–ÖR‚rTƒ¢TÓ¢U2r’ÂÆVâ†GVUö¦ö'2’Ð Ð¢2Gfæ6RæW‡E÷'VåöBf÷"ÆÂ&V7W'&–ær¦ö'2d•%5BÂVæFW"F†Rf–ÆRÆö6²ÀÐ¢2&Vf÷&Rç’W†V7WF–öâ&Vv–ç2âF†—2&W6W'fW2BÖÖ÷7BÖöæ6R6VÖçF–72àÐ¢2f÷"&ÆÆVÂ¦ö'2F†B&RÇ&VG’'Vææ–ærÂF†RGfæ6R¶VW0Ð¢2'V×–æræW‡E÷'VåöBf÷'v&B6òF†Rw&6Rv–æF÷ræWfW"W‡—&W2àÐ¢2Ö&µö¦ö%÷'Vâ‚’÷fW'w&—FW2æW‡E÷'VåöBöâ6ö×ÆWF–öâàÐ¢2&F6†VC¢öæRÆöB²öæR6fRf÷"F†Rv†öÆRGVR6WBÂæ÷BöæRW"¦ö"àÐ¢Gfæ6UöæW‡E÷'Vç2…¶¦ö%²&–B%Òf÷"¦ö"–âGVUö¦ö'5ÒÐ Ð¢2&W6öÇfRÖ‚&ÆÆVÂv÷&¶W'3¢Vçbf"â6öæf–rç–ÖÂâVæ&÷VæFVBàÐ¢26WB„U$ÔU5ô5$ôåôÔ…õ$ÄÄTÃÓFò&W7F÷&RöÆB6W&–Â&V†f–÷W"àÐ¢öÖ…÷v÷&¶W'3¢÷F–öæÅ¶–çEÒÒæöæPÐ¢G'“ Ð¢öVçe÷"Ò÷2ævWFVçb‚$„U$ÔU5ô5$ôåôÔ…õ$ÄÄTÂ"Â""’ç7G&—‚Ð¢–böVçe÷# Ð¢öÖ…÷v÷&¶W'2Ò–çB…öVçe÷"’÷"æöæPÐ¢W†6WB…fÇVTW'&÷"ÂG—TW'&÷"“ Ð¢ÆövvW"çv&æ–ær‚$–çfÆ–B„U$ÔU5ô5$ôåôÔ…õ$ÄÄTÂfÇVS²FVfVÇF–ærFòVæ&÷VæFVB"Ð¢–böÖ…÷v÷&¶W'2—2æöæS Ð¢G'“ Ð¢÷V6frÒÆöEö6öæf–r‚’÷"·ÐÐ¢ö6fu÷"Ò€Ð¢÷V6frævWB‚&7&öâ"Â·Ò’–b—6–ç7Fæ6R…÷V6frÂF–7B’VÇ6R·ÐÐ¢’ævWB‚&Ö…÷&ÆÆVÅö¦ö'2"Ð¢–bö6fu÷"—2æ÷BæöæS Ð¢öÖ…÷v÷&¶W'2Ò–çB…ö6fu÷"’÷"æöæPÐ¢W†6WBW†6WF–öã Ð¢70Ð Ð¢–bfW&&÷6S Ð¢ÆövvW"æ–æfò€Ð¢%'Vææ–ærVB¦ö"‡2’–â&ÆÆVÂ†Ö…÷v÷&¶W'3ÒW2’"ÀÐ¢ÆVâ†GVUö¦ö'2’ÀÐ¢öÖ…÷v÷&¶W'2–böÖ…÷v÷&¶W'2VÇ6R'Væ&÷VæFVB"ÀÐ¢Ð Ð¢FVb÷&ö6W75ö¦ö"†¦ö#¢F–7B’Óâ&ööÃ Ð¢""%'VâöæRGVR¦ö"VæB×FòÖVæBâF†–âw&W"&÷VæBF†R6†&V@Ð¢ÖöGVÆRÖÆWfVÂ'VåööæUö¦ö&6òF–6¶æBW‡FW&æÂ&÷f–FW'0Ð¢„6‡&öæ÷2f—&UöGVV’W6RF†R–FVçF–6ÂW†V7WF^(i'6f^(i&FVÆ—fW.(i&Ö&°Ð¢&öG’â"" Ð¢&WGW&â'VåööæUö¦ö"†¦ö"ÂFFW'3ÖFFW'2ÂÆö÷ÖÆö÷ÂfW&&÷6S×fW&&÷6RÐ Ð¢2'F—F–öâGVR¦ö'3¢F†÷6Rv—F‚W"Ö¦ö"v÷&¶F—"×WFFPÐ¢2÷2æVçf—&öå²%DU$Ô”äÅô5tB%Ò–ç6–FR'Våö¦ö"Âv†–6‚—2&ö6W72ÖvÆö&ÂÂ6ðÐ¢2F†W’VWVRöâF†R6–ævÆR×F‡&VB6WVVçF–ÂööÂFò'VâöæRBF–ÖRàÐ¢2F†BÆöæRöæÇ’¶VW2v÷&¶F—"¦ö'2g&öÒ÷fW&Æ–ærT4‚õD„U#°Ð¢2'Våö¦ö"w2÷FW&Ö–æÅö7vEöÆö6²—2v†BFF—F–öæÆÇ’7F÷26öæ7W'&VçFÇÐ¢2f—&–ærv÷&¶F—"ÖÆW72&ÆÆVÂ×ööÂ¦ö"g&öÒö'6W'f–ærF†R÷fW'&–FRàÐ¢6WVVçF–Åö¦ö'2Ò¶¢f÷"¢–âGVUö¦ö'2–b†¢ævWB‚'v÷&¶F—""’÷"""’ç7G&—‚•ÐÐ¢&ÆÆVÅö¦ö'2Ò¶¢f÷"¢–âGVUö¦ö'2–bæ÷B†¢ævWB‚'v÷&¶F—""’÷"""’ç7G&—‚•ÐÐ Ð¢÷&W7VÇG3¢Æ—7BÒµÐÐ¢öÆÅögWGW&W3¢Æ—7BÒµÐÐ Ð¢FVb÷7V&Ö—E÷v—F…öwV&B†¦ö#¢F–7BÂööÃ¢6öæ7W'&VçBægWGW&W2åF‡&VEööÄW†V7WF÷"“ Ð¢""%7V&Ö—B¦ö"f—&RÖæBÖf÷&vWBv—F‚F†R–âÖfÆ–v‡BFVGWwV&BàÐ Ð¢&WGW&ç2F†RgWGW&RÂ÷"æöæR–bF†R¦ö"v26¶—VB&V6W6R&–÷ Ð¢F–6²w2'VâöbF†R6ÖR¦ö"—27F–ÆÂ–âfÆ–v‡BâF†R'Vææ–ær×6W@Ð¢ÖVÖ&W'6†——2&VÆV6VB–âF†Rv÷&¶W"w2f–æÆÇ’&Æö6²àÐ¢"" Ð¢¦ö%ö–BÒ¦ö%²&–B%ÐÐ¢2F–6²6â&6RvFWv’FV&F÷vã¢öæ6RF†R–çFW'&WFW"—0Ð¢2f–æÆ—¦–ærÂööÂç7V&Ö—F&—6W2&6ææ÷B66†VGVÆRæWrgWGW&W0Ð¢2gFW"–çFW'&WFW"6‡WFF÷vâ"æB7&6†W2F†RF–6²â6¶—6ÆVæÇ’(	@Ð¢2F†R¦ö"7F—2GVRæBv–ÆÂf—&RöâF†RæW‡B†VÇF‡’F–6°Ð¢2‚3Sƒs#Â3SS“#B’àÐ¢–bö–çFW'&WFW%÷6‡WGF–æuöF÷vâ‚“ Ð¢ÆövvW"çv&æ–ær€Ð¢$¦ö"rW2ræ÷BF—7F6†VB(	B–çFW'&WFW"—26‡WGF–ærF÷vâ"ÀÐ¢¦ö"ævWB‚&æÖR"Â¦ö%ö–B’ÀÐ¢Ð¢&WGW&âæöæPÐ¢–bæ÷BG'•÷&Vv—7FW%÷'Vææ–æuö¦ö"†¦ö%ö–B“ Ð¢ÆövvW"æ–æfò‚$¦ö"rW2rÇ&VG’'Vææ–ær(	B6¶—–ær"Â¦ö"ævWB‚&æÖR"Â¦ö%ö–B’Ð¢&WGW&âæöæPÐ¢2&V6÷&BF†RGFV×B&Vf÷&RW†V7WF÷"F—7F6‚â&V6÷fW'’6Æ76–f–W0Ð¢2&æFöæVB&V6÷&G22Væ¶æ÷vã²—BæWfW"WFöÖF–6ÆÇ’&WG&–W2F†VÒàÐ¢W†V7WF–öâÒ7&VFUöW†V7WF–öâ†¦ö%ö–BÂ6÷W&6SÒ&'V–ÇF–â"Ð¢F—7F6†VEö¦ö"ÒF–7B†¦ö"ÂW†V7WF–öåö–CÖW†V7WF–öå²&–B%ÒÐ¢ö7G‚Ò6öçFW‡Gf'2æ6÷•ö6öçFW‡B‚Ð Ð¢FVb÷'VåöæE÷&VÆV6R†£ÖF—7F6†VEö¦ö"Â7GƒÕö7G‚“ Ð¢G'“ Ð¢&WGW&â7G‚ç'Vâ…÷&ö6W75ö¦ö"Â¢Ð¢f–æÆÇ“ Ð¢&VÆV6U÷'Vææ–æuö¦ö"†¥²&–B%ÒÐ Ð¢G'“ Ð¢&WGW&âööÂç7V&Ö—B…÷'VåöæE÷&VÆV6RÐ¢W†6WBW†6WF–öâ27V&Ö—EöW'# Ð¢&VÆV6U÷'Vææ–æuö¦ö"†¦ö%ö–BÐ¢f–æ—6…öW†V7WF–öâ€Ð¢W†V7WF–öå²&–B%ÒÀÐ¢7V66W73ÔfÇ6RÀÐ¢W'&÷#Öb$W†V7WF÷"F—7F6‚f–ÆVC¢·7V&Ö—EöW''Ò"ÀÐ¢Ð¢2–çFW'&WFW"&Vvâf–æÆ—¦–ær&WGvVVâF†RwV&B&÷fRæBF†PÐ¢27V&Ö—B(	B&VÆV6RF†R–âÖfÆ–v‡B6Æ–ÒvR§W7BFöö²æB6¶—àÐ¢–b—6–ç7Fæ6R‡7V&Ö—EöW'"Â'VçF–ÖTW'&÷"’æBö–çFW'&WFW%÷6‡WGF–æuöF÷vâ‡7V&Ö—EöW'"“ Ð¢ÆövvW"çv&æ–ær€Ð¢$¦ö"rW2ræ÷BF—7F6†VB(	B–çFW'&WFW"—26‡WGF–ærF÷vâ"ÀÐ¢¦ö"ævWB‚&æÖR"Â¦ö%ö–B’ÀÐ¢Ð¢&WGW&âæöæPÐ¢ÆövvW"æW'&÷"€Ð¢$¦ö"rW2ræ÷BF—7F6†VC¢W2"ÀÐ¢¦ö"ævWB‚&æÖR"Â¦ö%ö–B’ÀÐ¢7V&Ö—EöW'"ÀÐ¢Ð¢&WGW&âæöæPÐ Ð¢26WVVçF–Â72f÷"VçbÖ×WFF–ær‡v÷&¶F—"’¦ö'2àÐ¢2VWVVBFòW'6—7FVçB6–ævÆR×F‡&VBööÂ6òF†W’'VâöæRBF–ÖPÐ¢2t•D„õUB&Æö6¶–ærF†RF–6¶W"F‡&VB(	BÆöærv÷&¶F—"¦ö"æðÐ¢2ÆöævW"7F'fW2F†R&W7BöbF†R66†VGVÆR‡6ÖRf—‚2F†R&ÆÆVÀÐ¢272Â§W7B6W&–Æ—¦VB’âF†R–âÖfÆ–v‡BwV&B&WfVçG27F–ÆÂ×'Vææ–æpÐ¢2¦ö"g&öÒ&V–ær&R×VWVVBöâF†RæW‡BF–6²àÐ¢–b6WVVçF–Åö¦ö'3 Ð¢6W÷ööÂÒövWE÷6WVVçF–Å÷ööÂ‚Ð¢f÷"¦ö"–â6WVVçF–Åö¦ö'3 Ð¢gWBÒ÷7V&Ö—E÷v—F…öwV&B†¦ö"Â6W÷ööÂÐ¢–bgWB—2æöæS Ð¢6öçF–çVPÐ¢öÆÅögWGW&W2æVæB†gWBÐ¢–bæ÷B7–æ3 Ð¢÷&W7VÇG2æVæB…G'VR’2÷F–Ö—7F–6ÆÇ’6÷VçFV@Ð Ð¢2&ÆÆVÂ72(	BW'6—7FVçBööÂÂæöâÖ&Æö6¶–ærF—7F6‚àÐ¢2¦ö'2F†B&RÇ&VG’'Vææ–ær†g&öÒ&Wf–÷W2F–6²’&R6¶—VBàÐ¢2Ö&µö¦ö%÷'Vâ‚’WFFW2æW‡E÷'VåöBöâ6ö×ÆWF–öâÂ6òF†RæW‡BF–6°Ð¢2gFW"6ö×ÆWF–öâf–æG2F†R¦ö"GVRv–âæGW&ÆÇ’âæò6F6‚×W Ð¢2VWVRæVVFVBàÐ¢–b&ÆÆVÅö¦ö'3 Ð¢ööÂÒövWE÷&ÆÆVÅ÷ööÂ…öÖ…÷v÷&¶W'2Ð¢f÷"¦ö"–â&ÆÆVÅö¦ö'3 Ð¢gWBÒ÷7V&Ö—E÷v—F…öwV&B†¦ö"ÂööÂÐ¢–bgWB—2æöæS Ð¢6öçF–çVPÐ¢öÆÅögWGW&W2æVæB†gWBÐ¢–bæ÷B7–æ3 Ð¢÷&W7VÇG2æVæB…G'VR’2÷F–Ö—7F–6ÆÇ’6÷VçFV@Ð Ð¢2&W7BÖVff÷'B7vVWöbÔ57FF–ò7V'&ö6W76W2F†B7W'f—fVBF†V— Ð¢26W76–öâFV&F÷vââ×W7B'VâeDU"¦ö'2f–æ—6‚6ò7F—fR6W76–öç0Ð¢2†–æ6ÇVF–ærÆ—fRW6W"6†G2’&RæWfW"F÷V6†VB(	BöæÇ’”G2W‡Æ–6—FÇÐ¢2FWFV7FVB2÷'†ç2–âFööÇ2æÖ7÷FööÂå÷'Vå÷7FF–òw2f–æÆÇ’&Æö6²&PÐ¢2&VVBàÐ¢FVb÷7vVWöÖ7ö÷'†ç2‚’ÓâæöæS Ð¢G'“ Ð¢g&öÒFööÇ2æÖ7÷FööÂ–×÷'Bö¶–ÆÅö÷'†æVEöÖ7ö6†–ÆG&VàÐ¢ö¶–ÆÅö÷'†æVEöÖ7ö6†–ÆG&Vâ‚Ð¢W†6WBW†6WF–öâ2öS Ð¢ÆövvW"æFV'Vr‚%÷7B×F–6²Ô5÷'†â6ÆVçWf–ÆVC¢W2"ÂöRÐ Ð¢–b7–æ3 Ð¢27–æ2ÖöFR‡FW7G2òÖçVÂF–6·2“¢v—Bf÷"ÆÂF—7F6†VB¦ö'2ÀÐ¢26öÆÆV7B&W7VÇG2ÂF†Vâ7vVWöæ6RàÐ¢f÷"b–â6öæ7W'&VçBægWGW&W2æ5ö6ö×ÆWFVB…öÆÅögWGW&W2“ Ð¢G'“ Ð¢÷&W7VÇG2æVæB†bç&W7VÇB‚’Ð¢W†6WBW†6WF–öâ2W†3 Ð¢ÆövvW"æW'&÷"‚$7&öâ¦ö"gWGW&Rf–ÆVC¢W2"ÂW†2Ð¢÷&W7VÇG2æVæB„fÇ6RÐ¢÷7vVWöÖ7ö÷'†ç2‚Ð¢&WGW&â7VÒ…÷&W7VÇG2Ð Ð¢27–æ2†vFWv’F–6¶W"’ÖöFS¢FöâwB&Æö6²â7vVW÷'†ç2f–Ð¢2FöæRÖ6ÆÆ&6²f—&VBgFW"F†RÄ5BF—7F6†VB¦ö"6ö×ÆWFW2Â6òF†PÐ¢27vVW7F–ÆÂ†Vç2gFW"¦ö'2f–æ—6‚v—F†÷WB7FÆÆ–ærF†RF–6²àÐ¢–böÆÅögWGW&W3 Ð¢÷&VÖ–æ–ærÒ¶ÆVâ…öÆÅögWGW&W2•ÐÐ Ð¢FVbööåöFöæR…öc¢6öæ7W'&VçBægWGW&W2ägWGW&R’ÓâæöæS Ð¢÷&VÖ–æ–æu³ÒÓÒÐ¢G'“ Ð¢öW†2ÒöbæW†6WF–öâ‚Ð¢–böW†2—2æ÷BæöæS Ð¢ÆövvW"æW'&÷"‚$7&öâ¦ö"gWGW&Rf–ÆVB–â7–æ2ÖöFS¢W2"ÂöW†2ÂW†5ö–æfóÒ‡G—R…öW†2’ÂöW†2ÂöW†2åõ÷G&6V&6µõò’Ð¢W†6WBW†6WF–öã Ð¢70Ð¢–b÷&VÖ–æ–æu³ÒÃÒ Ð¢÷7vVWöÖ7ö÷'†ç2‚Ð Ð¢f÷"öb–âöÆÅögWGW&W3 Ð¢öbæFEöFöæUö6ÆÆ&6²…ööåöFöæRÐ¢VÇ6S Ð¢2æ÷F†–ærF—7F6†VB†ÆÂ6¶—VBòæòGVR¦ö'2’(	B7vVW–æÆ–æRàÐ¢÷7vVWöÖ7ö÷'†ç2‚Ð Ð¢&WGW&â7VÒ…÷&W7VÇG2Ð¢f–æÆÇ“ Ð¢–bf6çFÃ Ð¢G'“ Ð¢f6çFÂæfÆö6²†Æö6µöfBÂf6çFÂäÄô4µõTâÐ¢W†6WB„õ4W'&÷"Â”ôW'&÷"“ Ð¢70Ð¢VÆ–b×7f7'C Ð¢G'“ Ð¢×7f7'BæÆö6¶–ær†Æö6µöfBæf–ÆVæò‚’Â×7f7'BäÄµõTäÄ4²ÂÐ¢W†6WB„õ4W'&÷"Â”ôW'&÷"“ Ð¢70Ð¢Æö6µöfBæ6Æ÷6R‚Ð Ð Ð¦–bõöæÖUõòÓÒ%õöÖ–åõò# Ð¢F–6²‡fW&&÷6SÕG'VRÐ 