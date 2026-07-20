#!/usr/bin/env python3
"""Build deployable Christopher runtime slots from the recovered June baseline.

The June baseline files stay byte-exact (provenance-pinned). Every slot is
baseline + the authored patch layer:

  - processing gate forced closed (pa.enabled false, whatsapp gateway off)
  - memory hard-off (memory_enabled / user_profile_enabled false) — WB b63dd4f0
  - ops-judgment operations (clarification / attention / WC attach) —
    patches/ops-judgment-operations.snippet.yaml
  - ops-ingest judgment rules + structured work_items extraction —
    patches/ops-ingest-judgment.snippet.yaml
  - management-chat behavior (register, grounded answers, proactive attention
    push, bounded reply-actions, refusals) —
    patches/mgmt-chat-behavior.snippet.yaml
  - management-chat business-operation scope (reads + attention only; every
    ingest write refused by the runtime) —
    patches/mgmt-business-operations.snippet.yaml
  - per-slot engine: model + optional agent.reasoning_effort

Slots are keyed by slot id, not bare model name: gpt-5.6-luna-low runs
gpt-5.6-luna at reasoning_effort low.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml


DEPLOY_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = DEPLOY_ROOT / "baselines" / "june-2026"
PATCHES_ROOT = DEPLOY_ROOT / "patches"
SLOTS_ROOT = DEPLOY_ROOT / "runtime-slots"
BASELINE_MODEL = "gpt-5.4-mini"
DEFAULT_SLOT = "gpt-5.4-mini"
# slot id -> engine settings. reasoning_effort None = provider default (omit key).
SLOTS: dict[str, dict] = {
    "gpt-5.4-mini": {"model": "gpt-5.4-mini", "reasoning_effort": None},
    "gpt-5.6-luna": {"model": "gpt-5.6-luna", "reasoning_effort": None},
    "gpt-5.6-luna-low": {"model": "gpt-5.6-luna", "reasoning_effort": "low"},
}

MEMORY_OFF_BLOCK = "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n"

# Chat-scoped inbound allowlist (2026-07-20, teren-ratified; WB 0cd5698b).
# STAGED, NOT ACTIVATED: this only NARROWS which chats inbound processing would
# ever consider. Processing itself stays shut by pa.enabled=false +
# platforms.whatsapp.enabled=false + the runtime processing gate; none of those
# are touched here. The gate this feeds — whatsapp.py _is_group_allowed, called
# from _should_process_message as the FIRST statement of _build_message_event —
# can only return False for more chats than before, never True for more.
# Under `open` every group passes; under `allowlist` only this JID passes.
#
# The JID is rung 3a's authorized group, matching the deployed independent
# verifier table (VERIFIED_RUNG_JIDS['3a'] in
# /opt/tgg-capture/whatsapp-bridge/rung-authority.js), so staged inbound scope
# and outbound scope name the SAME chat.
#
# Both blocks must carry the keys. The top-level `whatsapp:` block is bridged
# into platforms.whatsapp.extra and WINS (gateway/config.py: `extra.update(bridged)`),
# and `_group_allow_from` reads config.extra with NO env fallback — so setting
# only one block would leave the allowlist empty and block every group.
#
# DM policy is deliberately untouched (teren: keep DMs on).
GROUP_ALLOWLIST_JIDS = ("120363426509183563@g.us",)  # TGG Christopher Mgmt Live Test 20260528

def _group_allowlist_block(indent: str) -> str:
    lines = [f"{indent}group_policy: allowlist\n", f"{indent}group_allow_from:\n"]
    lines += [f"{indent}- {jid}\n" for jid in GROUP_ALLOWLIST_JIDS]
    return "".join(lines)

# Anchored on the leading newline: the 2-space top-level form is otherwise a
# substring of the 6-space platforms.whatsapp.extra form, which would make the
# exactly-once replacement count 2 and silently patch the wrong block.
GROUP_POLICY_EXTRA_OLD = "\n      group_policy: open\n"
GROUP_POLICY_EXTRA_NEW = "\n" + _group_allowlist_block("      ")
GROUP_POLICY_ROOT_OLD = "\n  group_policy: open\n"
GROUP_POLICY_ROOT_NEW = "\n" + _group_allowlist_block("  ")

# Insertion anchors — each must appear exactly once in its baseline file.
OPERATIONS_ANCHOR = "          agent_config_read:\n"
AGENT_SECTION = "agent:\n  profile: pa\n  max_turns: 12\n"
OPS_INGEST_OBSERVABILITY_ANCHOR = (
    "    - 'Recording what happened (observability): at the end of each turn, use the record_event\n"
    "      tool to record the meaningful things you did — one event per distinct action\n"
    "      or decision. This is how the team sees your work"
)
EVENT_LABELS_OLD = "case_observation, clarification_requested, routed_out_of_scope. Use a new label"
EVENT_LABELS_NEW = (
    "case_observation, clarification_requested, routed_out_of_scope, attention_raised,\n"
    "      scope_addition_recorded. Use a new label"
)

NEW_OPERATIONS = ("tgg_clarification_raise", "tgg_attention_raise", "tgg_case_wc_attach")
NEW_INSTRUCTION_COUNT = 12
MGMT_NEW_INSTRUCTION_COUNT = 5

# The management brief is the only brief with a `web` toolset, so this block is
# unique to it in the baseline — the ingest brief disables web.
MGMT_TOOLSETS_ANCHOR = (
    "    enabled_toolsets:\n"
    "    - memory\n"
    "    - file\n"
    "    - web\n"
    "    - custom\n"
    "    - pa-observability\n"
    "    disabled_toolsets:\n"
    "    - terminal\n"
)

# Both briefs carry a "Recording what happened (observability)" instruction; the
# management one is distinguished by "This is part of finishing a turn".
MGMT_OBSERVABILITY_ANCHOR = (
    "    - 'Recording what happened (observability): at the end of each turn, use the record_event\n"
    "      tool to record the meaningful things you did — one event per distinct action\n"
    "      or decision. This is part of finishing a turn, not optional. Record an event\n"
)

# Management chats are read-plus-attention only. Every case-mutating operation is
# absent from this list, which means the runtime drops it from the operation
# registry for those chats entirely (agent/pa_constitution.py business_operations
# -> tools/pa_business_tools.py _scope_operations_to_job_brief). Prose backs the
# mechanism; the mechanism is the guarantee.
MGMT_FORBIDDEN_OPERATIONS = (
    "tgg_case_create",
    "tgg_case_observation",
    "tgg_case_update",
    "tgg_case_wc_attach",
    "tgg_clarification_raise",
    "work_costing_upsert",
)

# Exact runtime registry for the PA business bridge. Constitution prose may
# mention only these names after the word "operation"; aliases otherwise fail
# at runtime even when the model recovers later in the same turn.
CANONICAL_OPERATIONS = {
    "agent_action_record",
    "agent_config_read",
    "ilinked_lookup",
    "ilinked_status",
    "ilinked_wc_lookup",
    "job_work_costings",
    "message_search",
    "tgg_attention_raise",
    "tgg_case_create",
    "tgg_case_list",
    "tgg_case_lookup",
    "tgg_case_observation",
    "tgg_case_search",
    "tgg_case_update",
    "tgg_case_wc_attach",
    "tgg_clarification_raise",
    "work_costing_ingest_ilinked",
    "work_costing_lookup",
    "work_costing_upsert",
}
OPERATION_REFERENCE_RE = re.compile(
    r"\boperation\s+([a-z][a-z0-9]*_[a-z0-9_]+)\b"
)

MOFEX_OPERATION_OLD = (
    "    - If a TGG chat asks for a Mofex fact, call pa_business_read with operation mofex_lookup\n"
    "      and report inability if the tool blocks it.\n"
)
MOFEX_OPERATION_NEW = (
    "    - If a TGG chat asks for a Mofex fact, route it out of TGG scope and report that\n"
    "      Christopher's configured TGG operations cannot retrieve Mofex data. Do not invent\n"
    "      or call a cross-client operation.\n"
)

# Corpus finding (2026-07-20, teren-ratified): workers send photo albums FIRST
# and describe them 7s–2m14s LATER. The June 10s passive window splits every
# album from its caption, so caption-borne identifiers (unit numbers, job
# sheets) never reach the turn holding the media and low-confidence attaches
# follow. 5 minutes bundles album + caption into one turn. Addressed window
# stays 1500ms — patience applies only while passively observing.
DEBOUNCE_PASSIVE_OLD = "    debounce_passive_ms: 10000\n"
DEBOUNCE_PASSIVE_NEW = "    debounce_passive_ms: 300000\n"

# The single universal jobNo/create policy replaces the June create instruction
# 1-for-1 — no other instruction anywhere in the constitution may permit a
# broader create path (codex 2026-07-19 finding 1: two rules of differing
# breadth on jobNo = nondeterministic create behavior).
CREATE_POLICY_OLD = (
    "    - For clearly new case reports with zone, address, problem, and WhatsApp source,\n"
    "      call pa_business_write with operation tgg_case_create before any state update.\n"
    "      Include jobNo when present; otherwise let PS generate a WA job number. If address,\n"
    "      problem, source, or confidence is unclear, do not create; surface the missing\n"
    "      facts for clarification.\n"
)
CREATE_POLICY_NEW = (
    "    - 'tgg_case_create has exactly one trigger — a genuinely new case report (a new\n"
    "      job sheet, or a new problem report with zone, address, problem, and WhatsApp\n"
    "      source). When a job number is present, first call pa_business_read with operation\n"
    "      tgg_case_lookup; if a case already exists, record the material as a tgg_case_observation\n"
    "      on that case instead of creating. Only a genuinely new report without a job\n"
    "      number may let PS generate a WA job number. Never create a placeholder case\n"
    "      for photos, evidence, works orders, completion reports, or anything you have\n"
    "      raised a clarification or attention item about — those paths are observation,\n"
    "      scope addition (tgg_case_wc_attach), attention item, or clarification. If address,\n"
    "      problem, source, or confidence is unclear, do not create; raise a clarification\n"
    "      instead.\n"
    "\n"
    "      '\n"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected one {label} marker, found {count}")
    return text.replace(old, new, 1)


def _safe_config(source: str, slot: dict) -> str:
    model = slot["model"]
    effort = slot["reasoning_effort"]
    rendered = _replace_once(
        source,
        "pa:\n  enabled: true\n",
        "pa:\n  enabled: false\n",
        label="pa.enabled",
    )
    rendered = _replace_once(
        rendered,
        "platforms:\n  whatsapp:\n    enabled: true\n",
        "platforms:\n  whatsapp:\n    enabled: false\n",
        label="platforms.whatsapp.enabled",
    )
    operations_snippet = (
        PATCHES_ROOT / "ops-judgment-operations.snippet.yaml"
    ).read_text(encoding="utf-8")
    rendered = _replace_once(
        rendered,
        OPERATIONS_ANCHOR,
        operations_snippet + OPERATIONS_ANCHOR,
        label="business-bridge operations",
    )
    rendered = _replace_once(
        rendered,
        GROUP_POLICY_EXTRA_OLD,
        GROUP_POLICY_EXTRA_NEW,
        label="platforms.whatsapp.extra group_policy",
    )
    rendered = _replace_once(
        rendered,
        GROUP_POLICY_ROOT_OLD,
        GROUP_POLICY_ROOT_NEW,
        label="top-level whatsapp group_policy",
    )
    if not rendered.endswith("group_sessions_per_user: false\n"):
        raise RuntimeError("config baseline no longer ends at group_sessions_per_user")
    rendered += MEMORY_OFF_BLOCK
    if effort is not None:
        rendered = _replace_once(
            rendered,
            AGENT_SECTION,
            AGENT_SECTION + f"  reasoning_effort: {effort}\n",
            label="agent section",
        )
    if model != BASELINE_MODEL:
        rendered = rendered.replace(BASELINE_MODEL, model)
    return rendered


def _constitution(source: str, slot: dict) -> str:
    model = slot["model"]
    judgment_snippet = (PATCHES_ROOT / "ops-ingest-judgment.snippet.yaml").read_text(
        encoding="utf-8"
    )
    rendered = _replace_once(
        source,
        DEBOUNCE_PASSIVE_OLD,
        DEBOUNCE_PASSIVE_NEW,
        label="ops-ingest passive debounce",
    )
    rendered = _replace_once(
        rendered,
        CREATE_POLICY_OLD,
        CREATE_POLICY_NEW,
        label="ops-ingest create policy",
    )
    rendered = _replace_once(
        rendered,
        OPS_INGEST_OBSERVABILITY_ANCHOR,
        judgment_snippet + OPS_INGEST_OBSERVABILITY_ANCHOR,
        label="ops-ingest observability instruction",
    )
    rendered = _replace_once(
        rendered,
        EVENT_LABELS_OLD,
        EVENT_LABELS_NEW,
        label="ops-ingest event label list",
    )
    mgmt_behavior_snippet = (PATCHES_ROOT / "mgmt-chat-behavior.snippet.yaml").read_text(
        encoding="utf-8"
    )
    rendered = _replace_once(
        rendered,
        MGMT_OBSERVABILITY_ANCHOR,
        mgmt_behavior_snippet + MGMT_OBSERVABILITY_ANCHOR,
        label="management observability instruction",
    )
    mgmt_operations_snippet = (
        PATCHES_ROOT / "mgmt-business-operations.snippet.yaml"
    ).read_text(encoding="utf-8")
    rendered = _replace_once(
        rendered,
        MGMT_TOOLSETS_ANCHOR,
        MGMT_TOOLSETS_ANCHOR + mgmt_operations_snippet,
        label="management toolsets block",
    )
    # The June baseline names a cross-client operation that is not present in
    # Christopher's runtime registry (once in each job brief). Keep the existing
    # out-of-scope behavior, but remove the impossible tool call.
    if rendered.count(MOFEX_OPERATION_OLD) != 2:
        raise RuntimeError(
            "expected two legacy Mofex operation instructions, found "
            f"{rendered.count(MOFEX_OPERATION_OLD)}"
        )
    rendered = rendered.replace(MOFEX_OPERATION_OLD, MOFEX_OPERATION_NEW)
    if model != BASELINE_MODEL:
        replaced = rendered.replace(BASELINE_MODEL, model)
        if replaced == rendered:
            raise RuntimeError(f"constitution has no {BASELINE_MODEL} selectors")
        rendered = replaced
    return rendered


def _validate(
    config_path: Path,
    constitution_path: Path,
    slot: dict,
    *,
    baseline_instruction_count: int,
    baseline_mgmt_instruction_count: int,
) -> None:
    model = slot["model"]
    effort = slot["reasoning_effort"]
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    constitution = yaml.safe_load(constitution_path.read_text(encoding="utf-8"))

    assert config["pa"]["enabled"] is False
    assert config["group_sessions_per_user"] is False
    assert config["platforms"]["whatsapp"]["enabled"] is False
    assert config["model"]["provider"] == "openai-direct-primary"
    assert config["model"]["default"] == model
    assert config["providers"]["openai-direct-primary"]["default_model"] == model
    for task in ("compression", "session_search", "title_generation"):
        assert config["auxiliary"][task]["model"] == model
    assert config["memory"] == {
        "memory_enabled": False,
        "user_profile_enabled": False,
    }

    # Chat-scoped inbound allowlist, staged in BOTH blocks (the top-level block
    # bridges into extra and wins; extra has no env fallback for the allowlist).
    expected_jids = list(GROUP_ALLOWLIST_JIDS)
    for block in (config["whatsapp"], config["platforms"]["whatsapp"]["extra"]):
        assert block["group_policy"] == "allowlist", block["group_policy"]
        assert block["group_allow_from"] == expected_jids, block["group_allow_from"]
        # Staging must not widen anything: the allowlist is a strict subset of
        # the already-authorized outbound chats, so inbound can never reach a
        # chat outbound was not already scoped to.
        assert set(expected_jids) <= set(block["outbound_allowed_chats"]), expected_jids
        # DM policy is explicitly out of scope for this change.
        assert block["dm_policy"] == "allowlist", block["dm_policy"]
    if effort is None:
        assert "reasoning_effort" not in config["agent"]
    else:
        assert config["agent"]["reasoning_effort"] == effort

    operations = config["pa"]["overlay"]["client"]["business_bridge"]["operations"]
    assert set(operations) == CANONICAL_OPERATIONS, (
        sorted(set(operations) - CANONICAL_OPERATIONS),
        sorted(CANONICAL_OPERATIONS - set(operations)),
    )
    for name in NEW_OPERATIONS:
        assert name in operations, name
        assert operations[name]["type"] == "http"
        assert operations[name]["method"] == "POST"
    assert operations["tgg_case_wc_attach"]["path_params"] == ["jobNo"]

    assert constitution["runtime"] == {
        "provider": "openai-direct-primary",
        "model": model,
    }
    for job in ("tgg_ops_ingest", "tgg_management"):
        assert constitution["job_briefs"][job]["runtime"] == {
            "model": model,
            "provider": "openai-direct-primary",
        }
    ingest_brief = constitution["job_briefs"]["tgg_ops_ingest"]
    assert ingest_brief["debounce_passive_ms"] == 300000
    assert ingest_brief["debounce_addressed_ms"] == 1500

    instructions = constitution["job_briefs"]["tgg_ops_ingest"]["instructions"]
    assert (
        len(instructions) == baseline_instruction_count + NEW_INSTRUCTION_COUNT
    ), len(instructions)
    joined = "\n".join(instructions)
    assert "work_items" in joined
    assert "tgg_attention_raise" in joined
    assert "tgg_clarification_raise" in joined
    assert "tgg_case_wc_attach" in joined
    assert "attention_raised" in joined
    all_instructions = "\n".join(
        instruction
        for job in constitution["job_briefs"].values()
        for instruction in job["instructions"]
    )
    referenced_operations = set(OPERATION_REFERENCE_RE.findall(all_instructions))
    assert referenced_operations <= CANONICAL_OPERATIONS, sorted(
        referenced_operations - CANONICAL_OPERATIONS
    )
    assert "`tgg_clarification_request`" in joined
    assert "`tgg_message_history_search`" in joined
    assert "`tgg_case_update_state`" in joined
    assert "mofex_lookup" not in all_instructions
    assert "thread it arrived in is" in joined
    assert "the strongest signal" in joined
    assert "timing against active work" in joined
    assert "Doubt with no named rival never triggers a clarification" in joined
    assert "resolves ONLY to dormant or completed cases" in joined
    assert "never an attach target" in joined
    assert "message_search for the same chat_jid with limit 10" in joined
    assert "LOW confidence" in joined
    assert "runtime supplies" in joined
    assert "current-turn refs when they are omitted" in joined
    # Stage-1 roll-forward (2026-07-20): placeholder sourceRefs ban + the
    # evidence-attach justification contract with refs-preserving retry.
    assert "Never write a placeholder token" in joined
    assert "current_turn" in joined
    assert "Evidence-attach justification contract" in joined
    assert "ATTACH_UNJUSTIFIED" in joined
    assert "identifier_match" in joined
    assert "thread_continuation" in joined
    assert "operator_directive" in joined
    assert "block_unit" in joined
    assert "retry with the SAME sourceRefs" in joined
    assert "never remove photo or media" in joined
    assert "pure social acknowledgement" in joined
    # Exactly one create policy: the consolidated rule is present, the broader
    # June create instruction is gone, and no other instruction names the
    # create operation.
    assert "tgg_case_create has exactly one trigger" in joined
    assert "before any state update" not in joined
    create_mentions = [item for item in instructions if "tgg_case_create" in item]
    assert len(create_mentions) == 2
    assert sum("has exactly one trigger" in item for item in create_mentions) == 1
    assert sum("exact closed vocabulary" in item for item in create_mentions) == 1

    # ── Management chat: behavior section + business-operation scope ──────────
    mgmt_brief = constitution["job_briefs"]["tgg_management"]
    mgmt_instructions = mgmt_brief["instructions"]
    assert (
        len(mgmt_instructions)
        == baseline_mgmt_instruction_count + MGMT_NEW_INSTRUCTION_COUNT
    ), len(mgmt_instructions)
    mgmt_joined = "\n".join(mgmt_instructions)
    # Register + grounded answers.
    assert "you are TGG's operations coordinator" in mgmt_joined
    assert "carries that case's job number" in mgmt_joined
    assert "Ground every answer in this turn's tool results" in mgmt_joined
    assert "Re-query rather than recall" in mgmt_joined
    assert "no case found for" in mgmt_joined
    # Proactive push, batched one message per wave.
    assert "Push attention items unprompted" in mgmt_joined
    assert "one wave into ONE message" in mgmt_joined
    assert "only thing you raise unprompted" in mgmt_joined
    # Bounded reply-action experiment.
    assert "annotate an attention item with a status note" in mgmt_joined
    assert "promise-then-track a chase" in mgmt_joined
    assert "may not create, close, or modify a case on chat instruction" in mgmt_joined
    assert "attach evidence to a case" in mgmt_joined
    assert "attention-note alternative" in mgmt_joined
    # Refusals.
    assert "Never relay site-group content wholesale" in mgmt_joined
    assert "not a courtesy" in mgmt_joined

    # The scope block is the mechanism the prose above describes. Reads plus the
    # attention write only; every case-mutating operation must be absent, and
    # every permitted name must be a real configured operation.
    scope = mgmt_brief["business_operations"]
    assert set(scope) == {"allowed"}, sorted(scope)
    permitted = set(scope["allowed"])
    assert permitted <= CANONICAL_OPERATIONS, sorted(permitted - CANONICAL_OPERATIONS)
    assert permitted <= set(operations), sorted(permitted - set(operations))
    for forbidden in MGMT_FORBIDDEN_OPERATIONS:
        assert forbidden not in permitted, forbidden
    # The reads the management brief's own instructions depend on.
    for required in ("tgg_case_lookup", "tgg_case_search", "message_search"):
        assert required in permitted, required
    # The single permitted write, plus observability.
    assert "tgg_attention_raise" in permitted
    assert "agent_action_record" in permitted
    # The ingest brief stays unscoped so its behavior is unchanged.
    assert "business_operations" not in ingest_brief

    # Prose and mechanism must agree: no management instruction may tell
    # Christopher to call an operation the scope denies him, or he is being
    # instructed to do something the runtime will refuse.
    mgmt_referenced = set(OPERATION_REFERENCE_RE.findall(mgmt_joined))
    assert mgmt_referenced <= permitted, sorted(mgmt_referenced - permitted)
    for forbidden in MGMT_FORBIDDEN_OPERATIONS:
        assert forbidden not in mgmt_joined, forbidden


def main() -> int:
    baseline_config = (BASELINE_ROOT / "config.live-2026-06-19.yaml").read_text(
        encoding="utf-8"
    )
    baseline_constitution = (
        BASELINE_ROOT / "christopher_tgg_constitution.live-2026-06-19.yaml"
    ).read_text(encoding="utf-8")
    baseline_briefs = yaml.safe_load(baseline_constitution)["job_briefs"]
    baseline_instruction_count = len(baseline_briefs["tgg_ops_ingest"]["instructions"])
    baseline_mgmt_instruction_count = len(
        baseline_briefs["tgg_management"]["instructions"]
    )

    slot_files: list[Path] = []
    for slot_id, slot in SLOTS.items():
        slot_root = SLOTS_ROOT / slot_id
        slot_root.mkdir(parents=True, exist_ok=True)
        config_path = slot_root / "config.yaml"
        constitution_path = slot_root / "christopher_tgg_constitution.yaml"
        config_path.write_text(_safe_config(baseline_config, slot), encoding="utf-8")
        constitution_path.write_text(
            _constitution(baseline_constitution, slot), encoding="utf-8"
        )
        _validate(
            config_path,
            constitution_path,
            slot,
            baseline_instruction_count=baseline_instruction_count,
            baseline_mgmt_instruction_count=baseline_mgmt_instruction_count,
        )
        slot_files.extend((config_path, constitution_path))

    # The historical root paths remain the default authored deployment view.
    # They are generated from, and must remain byte-identical to, the default slot.
    default_slot = SLOTS_ROOT / DEFAULT_SLOT
    root_config = DEPLOY_ROOT / "config.yaml"
    root_constitution = DEPLOY_ROOT / "christopher_tgg_constitution.yaml"
    root_config.write_bytes((default_slot / "config.yaml").read_bytes())
    root_constitution.write_bytes(
        (default_slot / "christopher_tgg_constitution.yaml").read_bytes()
    )
    checksum_lines = []
    for path in slot_files:
        checksum_lines.append(f"{_sha256(path)}  {path.relative_to(SLOTS_ROOT)}")
    (SLOTS_ROOT / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
