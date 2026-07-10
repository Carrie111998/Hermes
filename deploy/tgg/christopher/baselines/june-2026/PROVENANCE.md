# Christopher June 2026 Live Baseline

These two YAML files are byte-exact recovery copies of Christopher's live
Hermes home on the former production host on 2026-06-19. They are immutable
source evidence for rebuilds and evaluations. They are not an active deployment
configuration: the live source config records `pa.enabled: true` because it was
captured after the June 16 go-live. Every new deployment must derive a safe
active config through `client-agent-deployment.yaml`, which keeps processing
disabled until the principal rung-gate is explicitly opened.

## Source chain

- Deployed code snapshot: `bd57c948a7b3d2bf80242ef4e373b987ca9d21d3`
  (`snapshot: TGG prod deployed state — capture 43-file uncommitted drift + b826
  before constant-download work (2026-06-19)`). This is the code baseline, not
  the constitution/config source.
- Config source: read-only SCP from
  `tgg-prod-sg:/home/pclaw/.hermes-christopher-tgg/config.yaml`.
- Constitution source: read-only SCP from
  `tgg-prod-sg:/home/pclaw/.hermes-christopher-tgg/christopher_tgg_constitution.yaml`.
- Runtime-adapter trail:
  `~/.codex/sessions/2026/06/19/rollout-2026-06-19T00-25-01-019edb8c-d779-70a3-87c4-204c16b74880.jsonl`.
  Function call `call_vkoFqJ1r7axsJbZ51k4kiu63` ran
  `specs/2026-06-19-tgg-amk-decision-surface/run_full_amk_eval.sh`; its first
  output was `live config pulled from tgg-prod-sg`. The committed runner's SCP
  block names the two remote paths above and only falls back after a failed SCP.
- Independent equality check: the 2026-06-18 portion replay and the 2026-06-19
  full replay have identical SHA-256 values for both live-config files.

## Recovered engine

- provider: `openai-direct-primary`
- model: `gpt-5.4-mini`
- base URL: `https://api.openai.com/v1`
- secret reference: `OPENAI_API_KEY` (the key value is not present here)
- `group_sessions_per_user: false`

The constitution declares the same provider/model at its runtime root and in
both TGG job briefs. Do not substitute the stale registry health block or a
rescue-host copy for this baseline.
