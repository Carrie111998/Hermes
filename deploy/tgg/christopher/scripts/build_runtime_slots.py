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
NEW_INSTRUCTION_COUNT = 11

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


def main() -> int:
    baseline_config = (BASELINE_ROOT / "config.live-2026-06-19.yaml").read_text(
        encoding="utf-8"
    )
    baseline_constitution = (
        BASELINE_ROOT / "christopher_tgg_constitution.live-2026-06-19.yaml"
    ).read_text(encoding="utf-8")
    baseline_instruction_count = len(
        yaml.safe_load(baseline_constitution)["job_briefs"]["tgg_ops_ingest"][
            "instructions"
        ]
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
