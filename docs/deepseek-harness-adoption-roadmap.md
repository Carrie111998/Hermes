# DeepSeek Harness adoption roadmap

This roadmap tracks narrowly scoped DeepSeek Harness (DSH) mechanisms evaluated for independent adoption in Hermes. Each candidate requires an Issue, an isolated worktree, strict RED→GREEN tests, measurable before/after evidence, Hermes gates, an exact staged digest, and two independent reviews. Architectural similarity alone is not a reason to port code.

## Source baseline

- Repository: <https://github.com/deepseek-ai/deepseek-harness>
- Pinned analysis commit: `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`
- License: MIT
- Adoption policy: independently implement behavior through existing Hermes seams; do not port Cordis, plugin registries, or storage abstractions unless a separate measured Issue proves they are necessary.

## Ranked candidates

| Rank | Candidate | Hermes seam | Status | Decision evidence | Next boundary |
| --- | --- | --- | --- | --- | --- |
| 1 | Bounded head/tail previews for persisted tool results | `tools/tool_result_storage.py::generate_preview` | Locally verified in Issue [#95717](https://github.com/NousResearch/hermes-agent/issues/95717); exact-digest review in progress | Synthetic benchmark retained the tail sentinel in 8,000/8,000 oversized samples versus 0/8,000 for the prefix baseline, respected the 1,500-character cap, and measured a 0.819214 median runtime ratio | Require a detached post-stage digest attestation and 2/2 PASS; no publication without explicit authorization |
| 2 | Defer additional low-frequency, high-schema-cost tools | Existing `tool_search` / `tool_describe` / `tool_call` progressive-disclosure seam | Prototype next | Prior source-first inventory found 30 visible tools, roughly 16,861 schema tokens, including six specialized BFL tools; Hermes already has the required registry/disclosure mechanism, so only catalog tuning is justified | Separate Issue; measure schema tokens, discovery success, task success, and latency before changing defaults |
| 3 | Post-middleware request snapshot and deterministic provider replay | Existing transport and middleware abstractions | Deferred until candidate 2 completes | DSH has a scriptable mock adapter and replay testkit; Hermes lacks one general artifact for the effective post-middleware request and normalized response replay through the loop | Separate Issue; define a redacted deterministic format and prove a real regression it catches |
| 4 | Cordis/plugin architecture port | Core runtime | Rejected | No measured gap requires a framework port; Hermes already has narrower extension seams | Reconsider only if two independent adopted candidates demonstrate a shared missing contract |

## Issue #95717 — head/tail preview slice

- Hermes base: `9aa7530f7b53699e2c6d648ded8f6300503b3dc7`
- Worktree: `C:/ha-dsh-head-tail-latest`
- DSH source mechanism: `packages/spill/spill-policy/src/index.ts:94-101`, which uses a head/tail `TextRetainer`
- Hermes implementation: independent character-budget split in the existing pure preview function; full spill storage, paths, cleanup, and recovery guidance remain unchanged
- RED→GREEN coverage:
  - decisive tail sentinel retention;
  - one-character budget;
  - zero and negative budgets;
  - honest persisted-output head/tail label.
- Local verification:
  - focused storage suite: 39 passed;
  - adjacent canonical batch: 122 passed across five files after excluding one Windows symlink-privilege test that fails identically on clean `upstream/main`;
  - Ruff, production `ty`, compile, real-path smoke, and `git diff --check`: passed;
  - synthetic benchmark: passed all predeclared thresholds.
- Known baseline limitations, not hidden:
  - `TestPathCanonicalization::test_symlink_aliases_are_not_parallelized` fails on this host and clean base with `WinError 1314`;
  - `ty` reports two unresolved-import diagnostics in the pre-existing test module on clean base, while production-only `ty` passes.
- Evidence: `docs/evidence/dsh-head-tail-preview.json`
- Review state: the first exact-digest round returned 0/2 PASS because this roadmap and the staged evidence still described staging as pending. That round is preserved in evidence and invalidated by the evidence-only correction. The immutable payload cannot contain its own SHA-256 without changing that SHA-256, so each final digest and Auto Research run are detached post-stage attestations supplied to both reviewers.
- Publication state: payload staged for review; not committed, not pushed, no PR, and no CI result.

## Rollback

Revert the Issue-specific preview function, message label, tests, roadmap entry, and evidence. The on-disk spill format and full persisted content are unchanged, so rollback requires no data migration.
