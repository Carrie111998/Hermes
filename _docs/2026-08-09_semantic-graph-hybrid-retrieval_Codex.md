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

## 設計固定 — llama-server専用Embedding backend

- Production backend: `LlamaCppEmbeddingBackend` only.
- Server: operator-managed dedicated `llama-server` process with `--embedding`.
- Endpoint: `POST /v1/embeddings` only; `/embedding` and `/embeddings` are not used.
- Model: `Qwen/Qwen3-Embedding-0.6B-GGUF`; F16 is the primary baseline and Q8_0 is the later A/B comparison.
- Dimensions: 1024 initially. Any 512-dimensional truncation is a later `truncate -> L2 normalize -> benchmark` decision, not an initial assumption.
- Pooling: `last`; server-side normalization is expected with `--embd-normalize 2`; client-side defensive normalization belongs to Commit 5.
- Query serialization uses the Qwen instruction form; document serialization uses only canonical node fields and no instruction.
- Default deployment is loopback-only at `127.0.0.1:8082`, with `allow_remote: false`; no API key is persisted.
- The plugin connects to the operator-managed server but does not auto-start it. Failure fails open to lexical retrieval with cooldown.
- Generic `LocalHttpEmbeddingBackend`, external Embeddings APIs, and external vector databases are not part of the production design.

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

## Commit 2 — embedding backend protocol

Commit 2 adds only the backend contract and deterministic fake adapter. Production retrieval, configuration, runtime hooks, SQLite schema, HTTP communication, CLI, and dependencies remain unchanged.

Implemented files:
- `plugins/semantic_graph/embedding/base.py`
- `plugins/semantic_graph/embedding/fake.py`
- `plugins/semantic_graph/embedding/__init__.py`
- `tests/plugins/test_semantic_graph_embedding_backend.py`

Contract decisions:
- `EmbeddingModelIdentity.namespace` is stable and maps an empty revision to `unversioned`.
- Identity rejects empty provider/model, non-positive dimensions, and non-positive serializer versions.
- The fake backend validates dimensions and finite numeric values at construction, but does not normalize or pack vectors.
- Returned vectors are defensive copies.
- Unavailable, unknown-input, and injected-failure paths raise `EmbeddingBackendError`.
- No network, database, config, or production recall path is exercised by this commit.

Verification evidence:
- RED: canonical runner failed during collection before implementation with `ModuleNotFoundError: No module named 'plugins.semantic_graph.embedding'` (exit 1).
- Backend contract: canonical runner, 16 tests passed, exit 0.
- Baseline + hardening: canonical runner, 10 tests passed, exit 0.
- Ruff: PASS.
- Python compile check: PASS.
- `git diff --check`: PASS.
- Secret/local-path scan: no findings.
- No live embedding server call performed.

## Commit 3 — canonical embedding serializer and source hash

Commit 3 adds deterministic Qwen3 query/document serialization and SHA-256 source hashing only. It does not connect the serializer to production retrieval, SQLite, runtime, config, HTTP, or CLI paths.

Implemented files:
- `plugins/semantic_graph/embedding/serializer.py`
- `plugins/semantic_graph/embedding/__init__.py` exports
- `tests/plugins/test_semantic_graph_embedding_serializer.py`

Contract decisions:
- Query text uses the Qwen3 instruction form: `Instruct: {instruction}` followed by `Query:{query}`.
- Documents use exactly five fixed lines: `Type`, `Subtype`, `Label`, `Summary`, and `Identity`.
- Only semantic node fields are read; status, authority, confidence, salience, run/session identifiers, timestamps, metadata, evidence, and extra keys are excluded.
- Shared `sanitize_text` is reused; values are normalized deterministically and bounded per field.
- `node_type` and `label` are required; optional fields remain as empty values so the five-line shape is stable.
- `source_text_hash` is SHA-256 over the final canonical UTF-8 text. No Python `hash()` or random identifier is used.
- Query/document serialization is separate; vector normalization and packing remain deferred to Commit 5.

Verification evidence:
- RED: canonical runner failed during collection before implementation with `ModuleNotFoundError: No module named 'plugins.semantic_graph.embedding.serializer'` (exit 1).
- GREEN: canonical runner, 4 files / 43 tests passed, exit 0.
- Ruff: PASS.
- Python compile check: PASS.
- `git diff --check`: PASS.
- Secret/local-path scan: no findings.
- Production files `retrieval.py`, `store.py`, `runtime.py`, `config.py`, and `pyproject.toml` are unchanged.
- No live embedding server call performed.

## 現在の変更範囲

The current branch contains the lexical benchmark baseline, the Phase 2 start log, the Commit 2 backend contract, and the Commit 3 canonical serializer/source hash. No SQLite migration, dependency change, production retrieval change, HTTP adapter, or CLI has been made.


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

Implement the transactional SQLite embedding migration as the next isolated commit, without connecting embeddings to production retrieval yet.

## 変更履歴

- 2026-08-09: Phase 2 start record created from merged `origin/main`.
- 2026-08-09: llama-server-only production backend and 1024-dimensional Qwen3 policy locked.
- 2026-08-09: Commit 3 serializer and source-hash verification recorded.
