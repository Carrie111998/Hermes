# Phase 2 — Semantic Graph Hybrid Retrieval

## 概要

P0 Semantic Graph hardening is closed as an immutable baseline. Phase 2 starts from the merged `origin/main`, not from the historical P0 feature branch. The goal is to extend the existing lexical Graph-aware RAG with optional dense retrieval, deterministic RRF fusion, provenance filtering, and bounded graph expansion.

## 開始点・構成管理

- Branch: `feat/semantic-graph-hybrid-retrieval`
- Base ref: `origin/main`
- Base SHA: `dcd697b84a99b93935df0586d42d386fdffdd964`
- Base commit: `fix(semantic-graph): harden persistence scope, retrieval, and verification (#56)`
- PR #56: merged
- PR #56 mergedAt: `2026-08-09T08:37:01Z`
- PR #56 merge commit: `dcd697b84a99b93935df0586d42d386fdffdd964`
- Verified historical P0 PR head: `8f009bb0e394169e89fa0972ef9ea6cafb4bb0d5`
- The historical P0 head is retained for audit only and is not the Phase 2 branch base.

## 要求・不変条件

- Existing FTS5 / LIKE / conservative Japanese synonym retrieval remains the lexical path.
- Dense embedding is an optional candidate generator only; it must not promote, merge, supersede, alter authority, alter confidence, or create `same_as`.
- Canonical embedding text contains only semantic fields: `node_type`, `subtype`, `label`, `summary`, and `identity_key`.
- Status, authority, confidence, salience, run scope, and timestamps are retrieval filters/metadata, not embedding text.
- `embedding.enabled` defaults to false.
- Backend failure must fail open to the existing lexical result without failing the LLM turn or mutating SQLite.
- Rejected and superseded nodes remain excluded from normal recall; history queries are explicit.
- Run-scoped retrieval and graph expansion must not cross `run_id` scope.
- Existing `pre_llm_call`, bounded `data_only` context rendering, provenance policy, and character limits remain intact.
- No mandatory vector database or new mandatory HTTP dependency is introduced.
- Unit tests do not call a live embedding server.

## 実装順

1. Freeze and record the lexical retrieval benchmark baseline.
2. Add the `EmbeddingBackend` protocol and deterministic fake backend.
3. Add transactional SQLite migration v2 and validated float32-le vector storage.
4. Add exact cosine search and deterministic lexical+dense RRF.
5. Add bounded provenance-aware one-hop expansion.
6. Prove disabled and failed-backend lexical fail-open behavior.
7. Add the local HTTP `/v1/embeddings` adapter.
8. Add embedding status/backfill/rebuild CLI operations.
9. Record A/B benchmark results and residual risks.

## Baseline commit 1 — lexical retrieval

The first Phase 2 implementation commit adds only a machine-readable 90-query fixture and a lexical-only benchmark harness. No production retrieval code, embedding code, schema migration, dependency, or live network call was added.

Fixture composition:
- exact identifiers: 20
- English paraphrases: 20
- Japanese paraphrases: 20
- Japanese-to-English concept queries: 10
- correction/history queries: 10
- irrelevant negative queries: 10

Measured variant: `A_lexical`, current FTS5/LIKE/synonym path, `top_k=8`.

Baseline results:
```json
{
  "query_count": 90,
  "overall_recall_at_8": 0.6444444444444445,
  "groups": {
    "exact_identifier": {"count": 20, "recall_at_8": 0.95, "mrr_at_8": 0.6213095238095239},
    "english_paraphrase": {"count": 20, "recall_at_8": 0.0, "mrr_at_8": 0.0},
    "japanese_paraphrase": {"count": 20, "recall_at_8": 0.95, "mrr_at_8": 0.4546428571428572},
    "japanese_to_english": {"count": 10, "recall_at_8": 1.0, "mrr_at_8": 0.67},
    "correction_history": {"count": 10, "recall_at_8": 1.0, "mrr_at_8": 0.95},
    "negative": {"count": 10, "recall_at_8": 0.0, "mrr_at_8": 0.0}
  },
  "negative_false_recall_rate": 0.0,
  "negative_no_result_precision": 0.0,
  "latency_ms_p50": 5.48,
  "latency_ms_p95": 6.97,
  "context_chars_max": 1477,
  "rejected_or_superseded_leak_rate": 0.0,
  "cross_run_leak_rate": 0.0,
  "secret_recall_count": 0,
  "state_mutation_count": 0
}
```

The negative set intentionally records lexical candidate-generation behavior: the baseline returns candidates for irrelevant queries, while no expected memory is marked as a hit. The `negative_no_result_precision` metric is therefore `0.0`; later hybrid acceptance must not worsen the measured false-recall behavior, and this limitation remains visible rather than being hidden by the hit-only metric.

## 現在の変更範囲

The current branch contains the lexical benchmark baseline and the Phase 2 start log only. No embedding implementation, schema migration, dependency change, or live network call has been made.


## 検証計画

- Use the canonical `scripts/run_tests.sh` runner.
- Run targeted Semantic Graph tests and the relevant Hermes plugin regression tests.
- Run `py_compile`, `git diff --check`, and a secret/local-path scan before each logical commit.
- Record exact exit codes and skipped checks.

## 残留リスク

- Dense retrieval, migration v2, RRF, graph expansion, HTTP adapter, and CLI are not implemented yet.
- Live embedding-server behavior remains intentionally unverified until the adapter exists.
- Production readiness is not claimed.

## 次のアクション

Add the lexical-only benchmark baseline as the first logical commit, without changing production retrieval code. Record its measured metrics here after fresh canonical-runner verification.

## 変更履歴

- 2026-08-09: Phase 2 start record created from merged `origin/main`.
