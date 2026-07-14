# MTU draft-first regression — staged production-stack replay

Date: 2026-07-14 SGT
Environment: temporary `HERMES_HOME`, source revision under test, real Hermes `GatewayRunner._handle_message` path, `gpt-5.4-mini`
Data: synthetic only
Live Telegram bot: unchanged

## Behaviour under test (Amelia-locked 2026-07-14)

Draft-first-with-placeholders. Reverses the prior "do not draft partial" gate (WB 58e5cca4),
which contradicted Melody's own P0 spec ("produce a draft + note which fields were missing").

- The agent produces the full copy-pasteable BOR in the same turn.
- Any missing case fact becomes a specific `[[MISSING: ...]]` placeholder — never a pre-draft question, never a fabricated value.
- ROP-vs-not is the ONLY permitted pre-draft question, and only when it cannot be inferred.

## Acceptance result

- **Case A (ROP, most facts missing)** — drafted a full plain-text BOR immediately: ROP replacement-disadvantages disclosure + standard alternatives declaration inserted as Amelia-approved template defaults; `[[MISSING]]` placeholders for before/after coverage, premiums, alternatives considered, the 50%-income boolean, and reference number; "Check before use" footer. No ask-round.
- **Case B (non-ROP new purchase)** — drafted immediately; all ROP wording correctly excluded; used the supplied SA / term / premium; placeholders for alternatives + 50% boolean + reference number.
- **Case C (ROP signal absent)** — asked exactly one conversational question, "Quick check: is this replacing an existing policy? (yes/no)", and nothing else.

Guardrail verified: no fabricated figures, rationale, or disclosure values — every unknown rendered as a placeholder. Plain-text output (no Markdown markers). ROP/non-ROP wording gated correctly.

Harness: session scratchpad `mtu_draftfirst_replay.py` (temp `HERMES_HOME` = copy of `~/.hermes-mtu` with the new constitution + repointed `constitution_path`); synthetic cases only; live bot untouched.
