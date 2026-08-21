# Hermes Production Workflow Registry

This directory is the Hermes-owned snapshot of the production routing and workflow policy.
It is intentionally separate from the source runtime. Hermes reads this snapshot; it does
not silently follow later edits in the source runtime.

The registry carries behavior, not provider credentials or host-specific installation
details. A change is a release candidate until it has a new registry version, a recorded
diff, a benchmark run, and a review decision.

## Contents

- `route-policy.json` — turn classification and escalation policy.
- `capability-contracts.json` — required capabilities and artifact constraints.
- `workflow-templates.json` — workflow stages and expected outputs.
- `execution-roles.json` — planner, worker, reviewer, and publisher responsibilities.
- `model-policies.json` — abstract role-to-policy and ordered provider candidates.
- `model-profiles.json` — provider/model profile metadata used by the bridge.
- `semantic-router-prompt.md` — classifier contract.
- `manifest.json` — source snapshot, hashes, version, and promotion state.

Specialized local workflows are opt-in. A user can select one with a first-line
`workflow: local_deterministic` or `workflow: local_visual_extraction` directive;
the semantic router may return the same registry keys only for an explicit local
request. They resolve to `ollama/qwen3-vl:2b` and are isolated from the default
cloud multimodal chain.

The portable, runtime-neutral migration contract is kept separately at
`../goose-migration/PORTABLE_CONTRACT.md`. It is the material to export when Goose becomes
the production runtime; this registry is not itself a Goose configuration bundle.

## Promotion states

`DRAFT → READY_FOR_REVIEW → APPROVED → PUBLISHED`

No state transition is implied by a file being present. The active Hermes process must be
restarted after a registry/config change, and the resulting route, review, artifact, and
publish evidence must be checked separately.
