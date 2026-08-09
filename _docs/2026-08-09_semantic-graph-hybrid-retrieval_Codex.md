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
- Model candidate: `Etherll/Qwen3-VL-Embedding-2B-Q8_0-GGUF:Q8_0`, served only through a dedicated llama-server; HF is distribution/reference only.
- The model is treated as a separate 2B VL Q8_0 family; do not inherit the earlier Qwen3-Embedding-0.6B/1024-dimension assumption.
- Dimensions are model/runtime metadata and must be confirmed from the live `/v1/embeddings` response or model metadata; vector code must not hard-code 1024.
- The namespace must include model family, quantization, GGUF revision/hash, dimensions, and serializer version; F16 and Q8_0 live fixtures must never share a namespace.
- Candidate server shape for a later live-validation commit: `./llama-server -hf Etherll/Qwen3-VL-Embedding-2B-Q8_0-GGUF:Q8_0 --embedding --pooling last --host 127.0.0.1 --port 8082`. Commit 5 does not start it.
- Client-side finite/dimension/normalization validation belongs to Commit 5; Q8_0 quality comparison is a separate live fixture/namespace from any F16 baseline.
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
- 2026-08-09: llama-server-only production backend locked; model identity and dimensions remain parameterized after switching to Etherll Qwen3-VL Embedding 2B Q8_0.
- 2026-08-09: Commit 3 serializer and source-hash verification recorded.

## Commit 5 — exact embedding vector operations

Commit 5 adds model-agnostic float32-le vector validation, stable packing/unpacking, L2 normalization, cosine/dot similarity, and low-level embedding persistence/exact scan APIs. The active model candidate is `Etherll/Qwen3-VL-Embedding-2B-Q8_0-GGUF:Q8_0`; no model download, llama-server startup, HF SDK runtime call, HTTP adapter, or retrieval integration is included.

Changed files:
- `plugins/semantic_graph/embedding/vectors.py`
- `plugins/semantic_graph/embedding/__init__.py`
- `plugins/semantic_graph/store.py`
- `tests/plugins/test_semantic_graph_embedding_vectors.py`
- `tests/plugins/test_semantic_graph_embedding_exact_search.py`

Verification evidence:
- Vector and exact-search tests: 65 passed, exit 0.
- Ruff: PASS.
- Python compile check: PASS.
- `git diff --check`: PASS.
- Retrieval/runtime/config/serializer/dependencies remain outside the production change boundary.
- No network calls or live embedding server calls performed.

Residual risk:
- Live model dimensions, pooling behavior, and Q8_0 quality remain unverified until the later llama-server live-validation commit.

## Commit 4 — transactional embedding store migration

Commit 4 upgrades the SQLite database schema from v1 to v2 while preserving existing graph data. It adds only the `node_embeddings` table, migration validation/rollback, schema-version separation, and stale-embedding invalidation during node updates.

Changed files:
- `plugins/semantic_graph/store.py`
- `tests/plugins/test_semantic_graph_embedding_store.py`

Design decisions:
- `DB_SCHEMA_VERSION = 2`, `GRAPH_SCHEMA_VERSION = 1`, and compatibility alias `SCHEMA_VERSION = DB_SCHEMA_VERSION`.
- `graph_runs.schema_version` stores the graph format version (`1`), not the SQLite database version.
- `get_status_counts()` reports the live `PRAGMA user_version` and `graph_schema_version` separately.
- v2 DDL is executed one statement at a time inside explicit `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`; `executescript()` is not used for v2.
- The schema is validated before `PRAGMA user_version` advances: exact column set and `nodes(node_id)` foreign key with `ON DELETE CASCADE`.
- `node_embeddings` is keyed by `(node_id, namespace)` and fixes the initial dtype to `float32-le`; vector packing/normalization remain Commit 5 responsibilities.
- Existing v1 nodes, runs, artifacts, and graph data are preserved. Future database versions fail closed.
- `upsert_node()` now participates in the existing thread-local transaction model. Semantic changes to `node_type`, `subtype`, `label`, final merged `summary`, or `identity_key` delete all stale embeddings for that node. Status/authority/confidence/salience/metadata-only changes retain embeddings.
- No embedding save API, vector math, retrieval integration, config, HTTP, CLI, or dependency was added.

Verification evidence:
- RED: canonical runner collection failed before implementation because `DB_SCHEMA_VERSION` and v2 migration symbols did not exist (exit 1).
- Migration tests: 14 passed, exit 0.
- Combined regression: 6 files / 61 tests passed, exit 0.
- Ruff: PASS.
- Python compile check: PASS.
- `git diff --check`: PASS.
- Production boundary: only `store.py`, the new migration test, and this implementation log changed.
- No network calls or live embedding server calls performed.

Residual risk:
- Float32 packing, finite-value validation, normalization, cosine search, dense retrieval, RRF, and the llama-server adapter remain unimplemented. The selected Etherll Qwen3-VL model is not started in this commit.
- Production readiness is not claimed.

Next action:
- Add Commit 5 vector packing/unpacking, finite validation, L2 normalization, and exact cosine tests without changing retrieval integration.

## 変更履歴

- 2026-08-09: Commit 4 transactional migration and invalidation verification recorded.

## Commit 6 — model-agnostic lexical/dense RRF candidate fusion

Commit 6 adds deterministic, read-only hybrid candidate retrieval. It uses the existing lexical ranking path, `DeterministicFakeEmbeddingBackend`, Commit 5 exact cosine search, and a pure reciprocal-rank-fusion function. It does not download or invoke the Etherll model, start llama-server, make HTTP/network calls, add dependencies, change configuration, add graph expansion, or measure model quality.

Selected SOPs and skills:
- `milspec-codex-standard`
- `test-driven-development`
- `windows-hermes-code-verification`
- `SOP-Implementation-Start-Gate`, `SOP-Skill-Selection`, `SOP-Application-Development`, and `SOP-Python`

Changed files:
- `plugins/semantic_graph/fusion.py`
- `plugins/semantic_graph/retrieval.py`
- `tests/plugins/test_semantic_graph_retrieval_rrf.py`
- this implementation log

RRF contract:
- Ranks are one-based and use `score += 1 / (k + rank)`, with `k=60` by default.
- Duplicate IDs retain the first rank within each source and become one candidate.
- Ordering is `rrf_score DESC`, `source_count DESC`, `best_rank ASC`, then `node_id ASC`; `best_rank` is the minimum available source rank.
- `dense_similarity` is ranking metadata attached by the retrieval caller; it is never copied to confidence or authority.
- `top_k` is enforced by the retrieval layer and is separate from the RRF smoothing constant.

Failure and safety contract:
- Disabled, unavailable, empty, stale, dimension-incompatible, or backend-failure dense paths return the existing lexical result unchanged.
- Dense candidates are namespace-scoped and filtered against the current canonical node source hash.
- Hybrid retrieval performs no SQLite writes, status/authority/confidence changes, merges, `same_as` creation, or graph expansion.
- Commit 6 remains model-agnostic; the fake backend dimension is test data and is not the Qwen3-VL native dimension.

Verification evidence:
- RED: focused collection failed before `fusion.py` existed with `ModuleNotFoundError` (exit 2).
- Focused RRF/integration tests: 10 passed, exit 0.
- Related Semantic Graph tests: 122 passed, exit 0.
- Ruff: PASS.
- Python compile check: PASS.
- `git diff --check`: PASS before commit.
- No live model, llama-server, HTTP, or network call performed.
- Production config, runtime, serializer profile, and dependencies remain outside this commit.

Deferred live-model work:
- Add a separate `qwen3_vl` serializer profile for conversation/system instruction handling; do not overwrite the existing `qwen3_text` profile.
- Validate candidate `--pooling last` behavior against Qwen3-VL chat-template/EOS handling.
- First live response dimension expectation is 2048. MRL dimensions such as 1024 require live confirmation or explicit client truncation plus renormalization and a distinct namespace.
- Record GGUF filename/hash, llama.cpp build, server flags, response dimension, finite values, norm, positive-vs-negative semantic ordering, and resource/latency measurements.
- Compare lexical-only, Qwen3-VL 2B + RRF, and Qwen3 text 0.6B + RRF on English/Japanese text-only benchmarks before production selection.

Residual risk:
- Real adapter serialization, pooling, response dimensions, Q8_0 quality, latency, RAM/VRAM use, and multimodal retrieval remain unverified.
- Commit 6 proves fusion and fail-open behavior only; it does not establish Recall@8 improvement or production readiness.

Next action:
- Commit the deterministic fusion implementation, then perform the isolated llama-server live-validation phase.

## Commit 7 — llama.cpp embedding capability probe

Commit 7 adds an independent, stdlib-only capability probe. It is diagnostic infrastructure only: Production adapter code and all existing `plugins/semantic_graph/*` production paths remain unchanged.

Changed files:
- `scripts/semantic_graph_llama_embedding_probe.py`
- `tests/plugins/test_semantic_graph_llama_cpp_probe.py`
- `tests/plugins/test_semantic_graph_llama_cpp_live.py`
- this implementation log

Control and candidate profiles:
- Control: `Qwen/Qwen3-Embedding-0.6B-GGUF`, alias `qwen3-embedding-0.6b-q8_0`, expected native dimension `1024`, port `8083`.
- Candidate: `Etherll/Qwen3-VL-Embedding-2B-Q8_0-GGUF`, alias `qwen3-vl-embedding-2b-q8_0`, expected native dimension `2048`, port `8082`.
- The probe accepts either profile through configuration but does not infer or auto-accept MRL dimensions.

Probe contract:
- Loopback HTTP only when explicitly invoked; stdlib `urllib.request` is used and no production HTTP client is introduced.
- Checks `/health`, `/v1/models`, `/props` best-effort, `/v1/embeddings`, flat-vector shape, count/index coverage, exact dimension, finite/nonzero/unit norm, repeat stability, batch sizes `1,2,4,8,16,32`, and English semantic ordering.
- Japanese/cross-lingual retrieval and instruction-profile comparison are recorded under `observations`; they are not required verdict gates at this stage.
- Soak is opt-in: `soak_requests=0` records `checks.soak=null`, `soak.requested=false`, and `soak.passed=null` without affecting the verdict. When `soak_requests>0`, the soak check is required and reports completed requests and failures.
- Token matrices, nulls, NaN/Inf, zero vectors, dimension drift, missing/duplicate indices, and semantic-ordering failures are rejected.
- Summary JSON contains no raw vectors, raw response, server logs, credentials, or absolute GGUF path. A SHA-256 is computed only when an explicit local `--gguf-path` is supplied.
- Live pytest execution requires `HERMES_RUN_LLAMA_EMBEDDING_LIVE=1` plus `HERMES_TEST_LLAMA_EMBEDDING_URL`, `HERMES_TEST_LLAMA_EMBEDDING_MODEL`, `HERMES_TEST_LLAMA_EMBEDDING_REPO`, and `HERMES_TEST_LLAMA_EMBEDDING_DIMENSIONS`. Soak additionally requires `HERMES_LLAMA_EMBEDDING_SOAK=1` and at least 500 requests; normal CI never performs network calls.
- `pass_text_only` is not a multimodal compatibility claim. Vision/image/video/mixed-input support remains untested.

Verification evidence:
- RED: the initial focused command failed with `file or directory not found` because the probe unit test had not yet been created (exit 4); the fix regression tests then failed at collection because `determine_verdict` and `required_check_names` were absent.
- Fix regression tests: 23 passed, 2 explicitly skipped, exit 0.
- The fix makes soak opt-in, preserves a three-value not-run/passed/failed representation, and keeps Japanese/profile results observational.
- Ruff: PASS.
- Python compile check: PASS.
- `git diff --check`: PASS.
- Production boundary check: no changes in `plugins/semantic_graph/retrieval.py`, `runtime.py`, `config.py`, `store.py`, `embedding/base.py`, or `pyproject.toml`.
- `llama-server --version`: version `10264`, build commit `81b08be15`, MSVC `19.44.35224.0`, Windows AMD64.
- `llama-server --list-devices`: CUDA0 `NVIDIA GeForce RTX 5060 Ti`, `15173 MiB` free.
- No llama-server process was running and no local Qwen Embedding GGUF was found in the checked cache/search locations.

Live-validation status:
- Control live verdict: `pending_live_validation` — control GGUF was not locally available and was not downloaded automatically.
- Candidate live verdict: `pending_live_validation` — candidate GGUF was not locally available and was not downloaded automatically.
- Therefore no model compatibility, dimension, pooling, semantic-quality, or soak PASS/FAIL claim is made by Commit 7.
- The next safe live step is to obtain/pin both GGUFs, record their SHA-256 values, start control first on `127.0.0.1:8083`, then candidate on `127.0.0.1:8082`, and run the explicit live test separately.

Residual risk:
- `/v1/embeddings` behavior, Qwen3-VL text preprocessing/chat-template equivalence, `--pooling last`, actual dimension, Q8_0 quality, resource usage, and long-run stability remain unverified.
- Production adapter creation remains correctly blocked on control/candidate live evidence.

## Commit 7 follow-up — opt-in soak verdict fix

The independent follow-up keeps Commit 7 unchanged and fixes the probe judgment contract without touching production Semantic Graph code.

Changed files:
- `scripts/semantic_graph_llama_embedding_probe.py`
- `tests/plugins/test_semantic_graph_llama_cpp_probe.py`
- this implementation log

Implementation:
- `BASE_REQUIRED_CHECKS` contains only base compatibility checks.
- `required_check_names(soak_requests=...)` adds `soak` only when requests are explicitly requested and rejects negative counts.
- `determine_verdict()` uses identity checks (`is True`) so `None` remains an explicit not-run state.
- `soak_requests=0` does not issue soak requests, records `checks.soak=null`, and emits `soak.requested=false`, `request_count=0`, `passed=null`.
- Requested soak records `passed`, `completed_requests`, and `failures`; a failed requested soak fails the verdict.
- Japanese/cross-lingual and instruction-profile measurements are emitted under `observations` and do not gate the text-only verdict.

Fresh verification:
- Canonical Windows runner: 4 existing target files, 58 tests passed, 0 failed; the requested `test_semantic_graph_hybrid_retrieval.py` file does not exist in this checkout and therefore could not be run.
- Probe/live focused result: 23 passed, 2 skipped.
- Ruff: PASS.
- Python compile check: PASS.
- `git diff --check`: PASS.
- No live server or network call was used; control/candidate validation remains pending.

Residual risk:
- The probe still requires live control and candidate GGUF validation before any production embedding backend or Associative Adapter work.
- The soak-duration option only qualifies an explicitly requested soak; duration alone does not trigger soak execution.

Next action:
- Create the independent fix commit, then obtain pinned control/candidate GGUFs and run the explicit live probes.
