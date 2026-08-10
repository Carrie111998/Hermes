# Semantic Graph Abstention-Gate Measurement

## Overview

A benchmark-only abstention-gate measurement harness was added without changing the production retrieval path, embedding model, endpoint, serializer, or default configuration. The benchmark now captures full pre-truncation candidate metadata and query-level score summaries; the measurement consumes the existing four-candidate, 90-query quantisation result package and reports whether dense-floor, lexical+dense-agreement, and top-score-margin gates are measurable from the available fields.

## Background / Requirements

The authoritative control state remains fixed:

- Model: `nsfw-bge-m3-v5-q6_k.gguf`
- Endpoint: `127.0.0.1:8084`
- Health: `PASS`
- Embedding: 1024-dimensional, finite, norm approximately 1.0
- Quantisation: `Q6_K`
- Hybrid Recall@8: `0.6667`
- Japanese-to-English Recall@8: `1.0000`
- Safety invariants: rejected/superseded leak 0, cross-run leak 0, secret leak 0, state mutation 0

The negative behaviour is an always-return baseline: `negative_false_recall_rate = 1.0` and `negative_no_result_precision = 0.0`. The work therefore measures retrieval-side abstention candidates only. It does not promote a gate to production.

## Assumptions / Decisions

- `proc_1bd0bfd6830c` was ignored as a delayed watch notification.
- No process restart, model switch, live endpoint request, or live revalidation was performed.
- The existing raw query-level result file is the sole benchmark result input.
- The raw result rows contain returned IDs and baseline labels, but no candidate-level dense similarity, lexical rank, dense rank, or RRF score fields. The harness reports the proposed gates as `not_measurable`; it does not invent scores.
- The fixture is used only to restore query text that is absent from the raw result rows. The negative set contains 10 queries.
- Production adapter, default retrieval path, serializer, PR merge, and Phase 3 remain HOLD / unchanged / NOT STARTED.

## Changed Files

- `scripts/measure_abstention_gates.py`
- `tests/plugins/test_measure_abstention_gates.py`
- `scripts/semantic_graph_embedding_ab_benchmark.py`
- `tests/plugins/test_semantic_graph_embedding_ab_benchmark.py`
- `artifacts/nsfw-bge-m3-v5-abstention/measurement.json`
- This implementation log

No production retrieval file was changed.

## Implementation Details

The benchmark retains the existing top-8 returned result contract while adding, before top-8 truncation, one observation row per fused candidate with `node_id`, lexical/dense ranks, dense similarity, RRF score, source count, best rank, final rank, and top-8 selection. `source_count` is the number of retrieval channels contributing a candidate to the fusion input. The query observation records top-one/top-two dense and RRF values, margins, lexical/dense top-one agreement, and expected-node overlap.

The measurement script validates candidate alignment against the 90-query reference order, preserves baseline metrics for all four candidates, reads the new nested candidate observations when present, and records explicit non-invasive execution flags:

- `live_endpoint_called: false`
- `sqlite_write_performed: false`
- `production_path_changed: false`

The checked-in legacy artifact remains `baseline_only_missing_score_fields` because it was generated from the prior raw result package. The new benchmark schema is tested and ready for a separate benchmark-only collection; no threshold sweep or production adoption is performed by this change.

## Commands Run

```text
uv run pytest tests/plugins/test_measure_abstention_gates.py -q
uv run pytest tests/plugins/test_measure_abstention_gates.py tests/plugins/test_semantic_graph_embedding_ab_benchmark.py tests/plugins/test_semantic_graph_retrieval_rrf.py -q
uv run ruff check scripts/measure_abstention_gates.py tests/plugins/test_measure_abstention_gates.py
git diff --check
uv run python scripts/measure_abstention_gates.py --input C:/Users/downl/AppData/Local/Temp/nsfw-bge-m3-v5-quantisation-query-results.json --output artifacts/nsfw-bge-m3-v5-abstention/measurement.json
```

## Test / Verification Results

- Targeted abstention tests before schema extension: `3 passed`.
- Benchmark and measurement tests after schema extension: `23 passed`.
- Ruff: passed.
- `git diff --check`: passed.
- Measurement output: generated successfully.
- Measurement status: `baseline_only_missing_score_fields`.
- Candidate count: 4.
- Query count: 90.
- Negative query count: 10.
- Q6_K baseline Recall@8: `0.6666666666666666`.
- Q6_K negative false recall rate: `1.0`.
- Q6_K negative no-result precision: `0.0`.
- Q6_K state mutation count: `0`.
- Production retrieval file hashes match the checked-out pre-measurement state; no production retrieval/config/runtime/fusion file appears in the diff.
- Secret scan over the new script, test, and measurement artifact found no findings.

## Residual Risks

The existing raw result artifact remains insufficient for a real dense-floor, lexical+dense-agreement, or top-score-margin threshold sweep. The new benchmark schema addresses the observation requirement, but a fresh Q6_K collection is still required before any gate quality claim can be made.

The negative sample has 10 queries, so confidence intervals will be wide. Any future threshold recommendation must report query-level confusion matrices and uncertainty, and must remain separate from production adoption.

## Recommended Next Actions

1. Run the benchmark-only collection with the new schema into a separate artifact; do not overwrite the legacy raw result.
2. Re-run the measurement on the new artifact with no production path or model change.
3. Sweep each gate independently before evaluating combinations.
4. Keep production adapter, default retrieval path, serializer, PR merge, and Phase 3 on HOLD until positive/negative trade-offs are demonstrated.
5. Keep HF upload pending until authentication and repository ownership are independently verified.

## Selected SOPs and Skills

- `SOP-Implementation-Start-Gate.md`
- `SOP-MLOps.md`
- `SOP-Python.md`
- `SOP-Security.md`
- `milspec-codex-standard`
- `software-development/test-driven-development`

This document records a measurement result only; it does not claim formal MILSPEC compliance.

## Traceability

| Requirement | Implementation | Evidence | Status |
|---|---|---|---|
| Preserve Q6_K authoritative state | No model/endpoint/process operation | Measurement flags and command history | Satisfied |
| Measure abstention candidates separately | New benchmark-only script | `measurement.json` | Partially measurable; score fields absent |
| Avoid production mutation | No production files or DB writes | `git diff`, hashes, script flags | Satisfied |
| Preserve safety invariants | Baseline values carried through | Q6_K report fields | Satisfied for supplied baseline |
| Do not promote a gate | No retrieval/config edit | Git status and implementation scope | Satisfied |

## Status

`Q6_K: selected Control`; `Production adapter: HOLD`; `default retrieval path: unchanged`; `serializer: unchanged`; `PR merge: HOLD`; `Phase 3: NOT STARTED`.
