# Local Document Processing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically process every supported uploaded document into a durable agent-readable Markdown artifact while retaining the original, keeping processing local, and exposing artifacts plus agent evidence to administrators.

**Architecture:** A focused `agent.document_processing` module owns local format handling and stable result codes. Interfaze uses a tenant-scoped artifact repository and background coordinator backed by the product database and local mirrors; standalone Hermes surfaces use a profile-scoped artifact database and the same processor. Existing upload adapters consume processed artifact paths, while admin-only APIs aggregate artifacts, attempts, agent outputs, events, and redacted evidence.

**Tech Stack:** Python 3.11–3.13, `firecrawl-anydoc` Python bindings, SQLite/PostgreSQL, FastAPI, existing vanilla WebUI JavaScript, pytest through `scripts/run_tests.sh`, Vitest for desktop renderer tests.

## Global Constraints

- Processing is fully local. No uploaded content may be sent to Firecrawl Parse or another hosted parser.
- Use `firecrawl-anydoc>=0.1.8,<0.3`, imported as `anydoc`, and regenerate `uv.lock`.
- Retain complete original and processed content in both the database and tenant/profile-scoped local storage.
- The database is the authoritative recovery source; local mirrors must be checksum-verified and reconstructible.
- Customer-visible states are exactly `uploaded`, `processing`, `ready`, `needs_attention`, and `failed`, rendered as Uploaded, Processing, Ready, Needs attention, and Failed.
- Customer surfaces and agent-authored customer responses must not mention Anydoc, conversion, converter, Markdown generation, or OCR.
- The admin UI may call the internal sidecar “Processed (.md) artifact” but must not expose processor or fallback implementation names.
- Never mutate or delete the original during processing. A failed retry must not replace a previously valid processed artifact.
- Do not automatically install the heavyweight advanced-processing dependency during an upload or worker run.
- Do not add a model tool, toolset entry, or mutable system-prompt content.
- Preserve tenant/profile isolation, prompt caching, strict message alternation, and existing attachment behavior for images, audio, video, and unsupported arbitrary binaries.
- Run Python tests only through `scripts/run_tests.sh`.
- Preserve all unrelated working-tree changes.

---

## File Structure

### New focused modules

- `agent/document_processing.py` — local format policy, Anydoc adapter, readable-text passthrough, optional advanced fallback, stable result types.
- `agent/document_artifacts.py` — profile-scoped standalone SQLite artifact inventory, local mirrors, checksum recovery, and synchronous processing facade used by CLI/TUI/gateway attachments.
- `server/document_artifacts.py` — Interfaze database/local-mirror repository for original and processed artifacts plus attempts.
- `server/document_processing_service.py` — bounded background lifecycle and product-safe status transitions.
- `server/agent_evidence.py` — redaction and deterministic evidence extraction from run logs/output.
- `server/routes/admin_documents.py` — admin-only document, artifact, attempt, result, and evidence endpoints.
- `server/supabase/migrations/007_document_artifacts.sql` — PostgreSQL tables, columns, indexes, RLS, and migration ledger entry.
- `server/webui/js/pages/admin-documents.js` — admin Documents list/detail/preview actions.

### Modified integration points

- `pyproject.toml`, `uv.lock` — local processor dependency.
- `server/db.py`, `server/postgres.py` — SQLite schema/additive columns and required PostgreSQL migration.
- `server/app.py` — construct/shutdown repository and processing service; register admin router.
- `server/routes/knowledge.py` — durable upload, automatic processing, processed-artifact semantic input, safe statuses.
- `server/agent_service.py`, `server/run_types.py` — keep technical and semantic processing distinct; persist evidence and safe run detail.
- `tui_gateway/server.py` — stage both forms and return a processed reference when ready.
- `gateway/platforms/base.py`, `gateway/run.py` — persist messaging originals and use processed context paths.
- `agent/context_references.py` — resolve known binary originals to verified processed sidecars before emitting a binary guidance block.
- `apps/desktop/src/store/composer.ts`, `apps/desktop/src/app/session/hooks/use-prompt-actions/index.ts` — product-safe processing state while an attached file is prepared.
- `server/webui/js/api.js`, `server/webui/js/main.js`, `server/webui/js/pages/admin.js`, `server/webui/js/pages/agent-runs.js` — admin routes, navigation, results, evidence, and artifact management.

---

### Task 1: Shared Local Document Processor

**Files:**
- Create: `agent/document_processing.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/agent/test_document_processing.py`
- Test fixtures: `tests/fixtures/documents/sample.csv`, `tests/fixtures/documents/sample.pdf`, `tests/fixtures/documents/sample.docx`

**Interfaces:**
- Produces: `ProcessingDisposition`, `DocumentProcessingResult`, `process_document(path: Path | None = None, *, data: bytes | None = None, filename: str = "", use_fallback: bool = True) -> DocumentProcessingResult`.
- Produces: `SUPPORTED_DOCUMENT_EXTENSIONS`, `READABLE_TEXT_EXTENSIONS`, and `is_processable_document(filename: str, content_type: str = "") -> bool`.
- Consumers: server coordinator, profile artifact store, context references.

- [ ] **Step 1: Write failing processor contract tests**

```python
def test_csv_uses_filename_hint(monkeypatch):
    monkeypatch.setattr("agent.document_processing._anydoc_to_markdown", lambda data, fmt: "| a |\n|---|\n| 1 |")
    result = process_document(data=b"a\n1\n", filename="sample.csv")
    assert result.disposition is ProcessingDisposition.CONVERTED
    assert result.markdown.startswith("| a |")
    assert result.source_format == "csv"


def test_empty_primary_output_requests_fallback(monkeypatch):
    monkeypatch.setattr("agent.document_processing._anydoc_to_markdown", lambda data, fmt: "  ")
    monkeypatch.setattr("agent.document_processing._advanced_pdf_markdown", lambda path: "# Scan\nRecovered")
    result = process_document(data=b"%PDF-1.4", filename="scan.pdf")
    assert result.disposition is ProcessingDisposition.CONVERTED
    assert result.used_fallback is True


def test_missing_advanced_dependency_needs_attention(monkeypatch):
    monkeypatch.setattr("agent.document_processing._anydoc_to_markdown", lambda data, fmt: (_ for _ in ()).throw(RuntimeError("unsupported")))
    monkeypatch.setattr("agent.document_processing._advanced_pdf_markdown", lambda path: (_ for _ in ()).throw(ModuleNotFoundError("marker")))
    result = process_document(data=b"%PDF-1.4", filename="scan.pdf")
    assert result.disposition is ProcessingDisposition.NEEDS_ATTENTION
    assert result.reason_code == "advanced_processing_unavailable"
```

- [ ] **Step 2: Run the focused tests and confirm the module is missing**

Run: `scripts/run_tests.sh tests/agent/test_document_processing.py -q`

Expected: FAIL during import because `agent.document_processing` does not exist.

- [ ] **Step 3: Implement the stable result model and format policy**

```python
class ProcessingDisposition(str, Enum):
    CONVERTED = "converted"
    PASSTHROUGH = "passthrough"
    NEEDS_ATTENTION = "needs_attention"
    FAILED = "failed"


@dataclass(frozen=True)
class DocumentProcessingResult:
    disposition: ProcessingDisposition
    markdown: str | None = None
    source_format: str | None = None
    reason_code: str | None = None
    diagnostic: str | None = None
    used_fallback: bool = False
```

Implement these rules in `process_document`:

1. Require exactly one input source (`path` or `data`).
2. Decode known readable text as UTF-8 with a stable failure result on invalid input.
3. Pass CSV's extension to `anydoc.to_markdown_bytes(data, "csv")`; let signature-bearing formats auto-detect.
4. Treat blank output as primary failure.
5. Attempt the existing Marker-based local path only for PDFs that need it and only when `use_fallback=True`.
6. Catch Anydoc exception classes by stable semantic category: encrypted, resource limit, malformed/missing part, unsupported.
7. Return sanitized diagnostics; never expose raw document content in an error.

- [ ] **Step 4: Add the bounded dependency and regenerate the lockfile**

Add `"firecrawl-anydoc>=0.1.8,<0.3"` to core dependencies because every upload surface calls this code, then run:

`uv lock`

Expected: `uv.lock` resolves `firecrawl-anydoc` and its platform wheel metadata without changing unrelated direct pins.

- [ ] **Step 5: Add representative real-import fixture tests**

Use small committed fixtures and assert behavior, not byte-for-byte snapshots:

```python
@pytest.mark.parametrize("name,needle", [
    ("sample.csv", "Widget"),
    ("sample.docx", "Quarterly catalogue"),
    ("sample.pdf", "Terms and conditions"),
])
def test_real_anydoc_fixture_yields_meaningful_markdown(name, needle):
    result = process_document(path=FIXTURES / name)
    assert result.disposition is ProcessingDisposition.CONVERTED
    assert needle in result.markdown
```

- [ ] **Step 6: Run processor and metadata tests**

Run: `scripts/run_tests.sh tests/agent/test_document_processing.py tests/test_project_metadata.py -q`

Expected: PASS.

- [ ] **Step 7: Commit the processor**

```bash
git add agent/document_processing.py tests/agent/test_document_processing.py tests/fixtures/documents pyproject.toml uv.lock
git commit -m "feat: add local document processor"
```

---

### Task 2: Durable Interfaze Artifact Repository

**Files:**
- Create: `server/document_artifacts.py`
- Create: `server/supabase/migrations/007_document_artifacts.sql`
- Modify: `server/db.py`
- Modify: `server/postgres.py`
- Test: `tests/server/test_document_artifacts.py`
- Test: `tests/server/test_postgres_backend.py`

**Interfaces:**
- Consumes: `DocumentProcessingResult` from Task 1.
- Produces: `ArtifactRecord`, `AttemptRecord`, and `DocumentArtifactRepository`.
- Produces methods: `store_original(...)`, `start_attempt(...)`, `store_processed(...)`, `materialize(...)`, `get_active_processed(...)`, `finish_attempt(...)`, `delete_document(...)`, and `backfill_existing_documents()`.
- Consumers: processing coordinator, knowledge routes, admin routes.

- [ ] **Step 1: Write failing schema and mirror-recovery tests**

```python
def test_original_and_processed_are_database_authoritative(repo, db, tmp_path):
    original = repo.store_original("cmp_1", "doc_1", "report.pdf", "application/pdf", b"%PDF-test")
    attempt = repo.start_attempt("cmp_1", "doc_1")
    processed = repo.store_processed("cmp_1", "doc_1", attempt.id, "# Report\nBody")

    assert db.one("SELECT content FROM document_artifacts WHERE id=?", (original.id,))["content"] == b"%PDF-test"
    assert db.one("SELECT content FROM document_artifacts WHERE id=?", (processed.id,))["content"] == b"# Report\nBody"

    Path(processed.local_path).unlink()
    rebuilt = repo.materialize("cmp_1", processed.id)
    assert rebuilt.read_text() == "# Report\nBody"
    assert sha256(rebuilt.read_bytes()).hexdigest() == processed.checksum
```

Also assert a different company cannot materialize or delete the artifact.

- [ ] **Step 2: Run the repository test and verify missing tables/module**

Run: `scripts/run_tests.sh tests/server/test_document_artifacts.py -q`

Expected: FAIL because `document_artifacts` and `DocumentArtifactRepository` do not exist.

- [ ] **Step 3: Add SQLite tables and additive document columns**

Add:

```sql
CREATE TABLE IF NOT EXISTS document_processing_attempts (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    company_id TEXT NOT NULL REFERENCES companies(id),
    public_status TEXT NOT NULL,
    public_message TEXT,
    internal_stage TEXT NOT NULL,
    reason_code TEXT,
    diagnostic TEXT,
    input_checksum TEXT NOT NULL,
    output_checksum TEXT,
    run_id TEXT,
    started_at REAL NOT NULL,
    completed_at REAL
);
CREATE TABLE IF NOT EXISTS document_artifacts (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    company_id TEXT NOT NULL REFERENCES companies(id),
    role TEXT NOT NULL CHECK(role IN ('original','processed')),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content BLOB NOT NULL,
    checksum TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    local_path TEXT NOT NULL,
    attempt_id TEXT REFERENCES document_processing_attempts(id),
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
```

Add nullable document columns through `COLUMN_MIGRATIONS`: `status_detail`, `original_checksum`, `active_processed_artifact_id`, `current_processing_attempt_id`, `processing_started_at`, `ready_at`, and `origin`.

- [ ] **Step 4: Add PostgreSQL migration parity and require it at startup**

Create `007_document_artifacts.sql` with `bytea` content, JSONB metadata, indexes on `(company_id, document_id, role)` and `(company_id, public_status)`, RLS policies using `interfaze_company_access(company_id)`, and:

```sql
insert into schema_migrations(version) values ('007_document_artifacts')
on conflict (version) do nothing;
```

Update `PostgresDatabase.REQUIRED_MIGRATIONS` to include both the existing `006_message_supersession` and `007_document_artifacts`.

- [ ] **Step 5: Implement repository writes, atomic mirrors, and recovery**

Use explicit column lists and a temp sibling followed by `Path.replace()`:

```python
def _write_verified(path: Path, content: bytes, checksum: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(content)
    if sha256(temporary.read_bytes()).hexdigest() != checksum:
        temporary.unlink(missing_ok=True)
        raise IOError("artifact checksum mismatch")
    temporary.replace(path)
    return path
```

`store_original` uses `<upload_root>/<company>/<document>/original/<safe-name>`; `store_processed` uses `derived/content.md`. `materialize` verifies the tenant and checksum before returning a path and reconstructs missing/corrupt mirrors from `content`.

- [ ] **Step 6: Implement idempotency and deletion contracts**

- Exactly one original row per document/checksum.
- Reuse a verified processed artifact for the same input checksum unless `force=True`.
- Promote `active_processed_artifact_id` only inside the same transaction that completes the successful attempt.
- Delete database rows transactionally, then remove only the explicit document mirror directory; record cleanup diagnostics without restoring deleted rows.

- [ ] **Step 7: Run storage and PostgreSQL contract tests**

Run: `scripts/run_tests.sh tests/server/test_document_artifacts.py tests/server/test_postgres_backend.py -q`

Expected: PASS for SQLite and SQL translation/migration assertions.

- [ ] **Step 8: Commit durable artifacts**

```bash
git add server/document_artifacts.py server/db.py server/postgres.py server/supabase/migrations/007_document_artifacts.sql tests/server/test_document_artifacts.py tests/server/test_postgres_backend.py
git commit -m "feat: persist document artifacts"
```

---

### Task 3: Background Processing Coordinator

**Files:**
- Create: `server/document_processing_service.py`
- Modify: `server/app.py`
- Modify: `server/config.py`
- Test: `tests/server/test_document_processing_service.py`
- Test: `tests/server/test_config.py`

**Interfaces:**
- Consumes: `process_document` and `DocumentArtifactRepository`.
- Produces: `DocumentProcessingService.submit(company_id: str, document_id: str, *, force: bool = False) -> AttemptRecord`, `wait_until_settled(...)`, `retry(...)`, and `shutdown()`.
- Consumers: upload route, semantic processing route, admin retry endpoint.

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_submit_transitions_uploaded_processing_ready(service, repo, db):
    service.submit("cmp_1", "doc_1")
    settled = service.wait_until_settled("cmp_1", "doc_1", timeout=2)
    assert settled.public_status == "ready"
    row = db.one("SELECT status,active_processed_artifact_id FROM documents WHERE id='doc_1'")
    assert row["status"] == "ready"
    assert row["active_processed_artifact_id"]


def test_missing_fallback_dependency_is_needs_attention(service, db):
    service.processor = lambda **_: DocumentProcessingResult(
        ProcessingDisposition.NEEDS_ATTENTION,
        reason_code="advanced_processing_unavailable",
    )
    service.submit("cmp_1", "doc_1")
    service.wait_until_settled("cmp_1", "doc_1", timeout=2)
    row = db.one("SELECT status,status_detail FROM documents WHERE id='doc_1'")
    assert row["status"] == "needs_attention"
    assert "OCR" not in row["status_detail"]
```

- [ ] **Step 2: Run tests and confirm the service is missing**

Run: `scripts/run_tests.sh tests/server/test_document_processing_service.py -q`

Expected: FAIL importing `DocumentProcessingService`.

- [ ] **Step 3: Implement bounded worker lifecycle and status mapping**

Use a `ThreadPoolExecutor` with explicit settings:

```python
PUBLIC_FAILURES = {
    "encrypted": ("failed", "We couldn’t process this file. Please upload an unlocked copy or try another format."),
    "advanced_processing_unavailable": ("needs_attention", "This file needs attention before it can be used."),
}
```

Add config keys under `interfaze_server` with defaults: `document_workers: 2`, `document_processing_timeout_seconds: 180`, and `document_output_max_bytes: 50 * 1024 * 1024`. These are behavioral `config.yaml` settings, not environment variables.

- [ ] **Step 4: Make promotion and retry failure-safe**

The worker must:

1. materialize and verify the original;
2. mark the attempt/document `processing`;
3. run the processor through a future bounded by the configured timeout;
4. reject blank or oversized output;
5. store/promote processed content only on success;
6. preserve any prior active artifact on retry failure; and
7. store technical reason codes only in restricted attempt fields.

- [ ] **Step 5: Wire service lifecycle into FastAPI**

In `create_app`, construct `DocumentArtifactRepository`, then `DocumentProcessingService`; expose them as `app.state.document_artifacts` and `app.state.document_processing`. In lifespan shutdown, call `document_processing.shutdown()` before closing the database.

- [ ] **Step 6: Run lifecycle/config tests**

Run: `scripts/run_tests.sh tests/server/test_document_processing_service.py tests/server/test_config.py -q`

Expected: PASS, including timeout and shutdown cases.

- [ ] **Step 7: Commit the coordinator**

```bash
git add server/document_processing_service.py server/app.py server/config.py tests/server/test_document_processing_service.py tests/server/test_config.py
git commit -m "feat: coordinate document processing"
```

---

### Task 4: Onboarding Upload and Semantic Extraction Integration

**Files:**
- Modify: `server/routes/knowledge.py`
- Modify: `server/agent_service.py`
- Modify: `server/run_types.py`
- Modify: `server/storage.py`
- Test: `tests/server/test_api_mvp.py`
- Test: `tests/server/test_run_harness.py`
- Test: `tests/server/test_webui.py`

**Interfaces:**
- Consumes: server artifact repository and processing service.
- Produces: customer-safe document JSON and semantic runs whose `payload.path` points to a verified processed `.md` mirror.

- [ ] **Step 1: Replace the existing pipeline test with the new two-stage contract**

```python
def test_document_upload_stores_both_forms_and_semantic_run_reads_markdown():
    uploaded = upload_text_document("catalog.txt", b"Widget catalogue")
    ready = wait_for_document(uploaded["id"])
    assert ready["status"] == "ready"
    assert set(ready) >= {"id", "name", "status"}
    assert "active_processed_artifact_id" not in ready

    started = client.post(f"/api/v1/documents/{uploaded['id']}/process", headers=headers)
    run = wait_for_run(client, headers, started.json()["id"])
    assert run["status"] == "succeeded"
    assert run["payload"]["path"].endswith("content.md")
```

- [ ] **Step 2: Run the API test and verify the old `processed` status/path behavior fails**

Run: `scripts/run_tests.sh tests/server/test_api_mvp.py::test_document_upload_stores_both_forms_and_semantic_run_reads_markdown -q`

Expected: FAIL because upload currently stores one local/remote object and semantic extraction receives the original path.

- [ ] **Step 3: Change upload to durable original + automatic processing**

Read at most `max_upload_bytes + 1`, reject oversized input before database commit, insert the logical document, call `store_original`, update its checksum/path metadata, and call `document_processing.submit`. Return the customer serializer immediately with status `uploaded` or `processing`; never return artifact IDs, local paths, diagnostic codes, or processor names.

- [ ] **Step 4: Separate technical readiness from semantic run state**

In `/documents/{id}/process`:

- require document status `ready`;
- call `get_active_processed` and `materialize`;
- pass the `.md` path plus `source_document_id` and document type to the run;
- return a safe 409 body when the document is still processing or needs attention.

Remove `AgentRunService` updates that change the document's technical status to `processed`, `failed`, or `cancelled`. Semantic results continue to update `documents.data` with records/rejects and `processing_run_id`, while the public document remains `ready`.

- [ ] **Step 5: Make semantic processing idempotently replace source records**

Before persisting a successful re-run, delete or replace records whose `source_document_id` matches this document so the existing “re-running replaces, never duplicates” skill rule becomes true. Preserve tenant predicates on every delete/update.

- [ ] **Step 6: Retire the document-specific use of the old storage backend**

Remove `app.state.storage` calls from `knowledge.py`. Keep `server/storage.py` only if another server path still imports it; otherwise delete its construction from `app.py` and update its isolated tests. Do not change unrelated file storage behavior outside the document route.

- [ ] **Step 7: Run API, run-service, and WebUI upload tests**

Run: `scripts/run_tests.sh tests/server/test_api_mvp.py tests/server/test_run_harness.py tests/server/test_webui.py -q`

Expected: PASS with `ready` as the technical terminal state and processed Markdown as semantic input.

- [ ] **Step 8: Commit server ingestion**

```bash
git add server/routes/knowledge.py server/agent_service.py server/run_types.py server/storage.py tests/server/test_api_mvp.py tests/server/test_run_harness.py tests/server/test_webui.py
git commit -m "feat: process onboarding documents locally"
```

---

### Task 5: Profile-Scoped Standalone Artifact Store and Context Resolution

**Files:**
- Create: `agent/document_artifacts.py`
- Modify: `agent/context_references.py`
- Test: `tests/agent/test_document_artifacts.py`
- Test: `tests/agent/test_context_references.py`

**Interfaces:**
- Consumes: shared `process_document`.
- Produces: `ProfileDocumentArtifactStore(db_path: Path | None = None, root: Path | None = None)`.
- Produces methods: `ingest(path: Path, *, session_id: str, origin: str) -> ProfileDocument`, `processed_path_for(original: Path) -> Path | None`, `wait_until_settled(document_id: str, timeout: float)`, and `close()`.
- Consumers: TUI gateway, messaging gateway, CLI/context references.

- [ ] **Step 1: Write failing profile persistence and recovery tests**

```python
def test_profile_store_keeps_original_and_processed_in_db_and_locally(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = tmp_path / "report.csv"
    source.write_text("name\nWidget\n")
    store = ProfileDocumentArtifactStore()
    record = store.ingest(source, session_id="sid", origin="desktop")
    settled = store.wait_until_settled(record.id, timeout=2)
    assert settled.status == "ready"
    assert store._one("SELECT content FROM artifacts WHERE role='original'")["content"] == source.read_bytes()
    assert Path(settled.processed_path).read_text().find("Widget") >= 0
```

- [ ] **Step 2: Run tests and confirm the profile store is missing**

Run: `scripts/run_tests.sh tests/agent/test_document_artifacts.py -q`

Expected: FAIL on import.

- [ ] **Step 3: Implement the profile-scoped database and mirror layout**

Default to `get_hermes_home() / "document_artifacts.db"` and `get_hermes_home() / "cache" / "documents"`. Create focused `documents`, `artifacts`, and `attempts` tables with `session_id`, `origin`, bytes, checksums, paths, statuses, and timestamps. Use a module-level lazy singleton per resolved `HERMES_HOME`, guarded by a lock; never use `Path.home() / ".hermes"`.

- [ ] **Step 4: Implement eager processing and safe reuse**

`ingest` copies/stores the original, computes its checksum, submits local processing, and reuses an existing ready sidecar for the same checksum. `wait_until_settled` returns product-safe status and the verified processed path. Missing/corrupt local files reconstruct from SQLite content.

- [ ] **Step 5: Resolve binary `@file:` references through the store**

Before `_binary_reference_block`, call `processed_path_for(path)`. When a verified sidecar exists, inline it with original provenance:

```python
return None, (
    f"📄 {ref.raw} processed from `{path.name}` "
    f"({estimate_tokens_rough(text)} tokens)\n```markdown\n{text}\n```"
)
```

If no sidecar exists, preserve the current actionable binary block. Do not create a new processing attempt merely by probing a sensitive or out-of-root path; authorization checks run first.

- [ ] **Step 6: Run profile and context tests**

Run: `scripts/run_tests.sh tests/agent/test_document_artifacts.py tests/agent/test_context_references.py -q`

Expected: PASS, including sensitive-path and hard token-budget tests.

- [ ] **Step 7: Commit standalone persistence**

```bash
git add agent/document_artifacts.py agent/context_references.py tests/agent/test_document_artifacts.py tests/agent/test_context_references.py
git commit -m "feat: persist standalone document artifacts"
```

---

### Task 6: Desktop, TUI, CLI, and Messaging Attachment Integration

**Files:**
- Modify: `tui_gateway/server.py`
- Modify: `gateway/platforms/base.py`
- Modify: `gateway/run.py`
- Modify: `apps/desktop/src/store/composer.ts`
- Modify: `apps/desktop/src/app/session/hooks/use-prompt-actions/index.ts`
- Modify: `apps/desktop/src/app/chat/composer/attachments.tsx`
- Test: `tests/test_tui_gateway_server.py`
- Test: `tests/gateway/test_document_cache.py`
- Test: `tests/gateway/test_document_context_note.py`
- Test: `apps/desktop/src/app/session/hooks/use-prompt-actions/index.test.tsx`
- Test: `apps/desktop/src/app/chat/composer/attachments.test.tsx`

**Interfaces:**
- Consumes: `ProfileDocumentArtifactStore`.
- Produces: `file.attach` response fields `processing_status`, `original_path`, `processed_path`, and `ref_text` targeting processed Markdown when ready.
- Produces: `CachedMedia.processed_path` and product-safe status for messaging context.

- [ ] **Step 1: Write failing TUI gateway attachment test**

```python
def test_file_attach_returns_processed_reference(monkeypatch, tmp_path):
    response = attach_bytes("catalog.csv", b"name\nWidget\n")
    assert response["result"]["processing_status"] == "ready"
    assert response["result"]["original_path"].endswith("catalog.csv")
    assert response["result"]["processed_path"].endswith(".md")
    assert response["result"]["ref_text"].endswith(".md")
```

- [ ] **Step 2: Write failing messaging context test**

```python
def test_cached_document_context_uses_processed_sidecar(monkeypatch):
    cached = cache_media_bytes(b"name\nWidget\n", filename="catalog.csv", mime_type="text/csv")
    note = _build_document_context_note(cached.display_name, cached.path, cached.media_type)
    assert "Widget" in preprocess_note_context(note)
    assert "binary" not in note.lower()
```

- [ ] **Step 3: Run focused attachment tests and confirm they fail on original refs**

Run: `scripts/run_tests.sh tests/test_tui_gateway_server.py tests/gateway/test_document_cache.py tests/gateway/test_document_context_note.py -q`

Expected: FAIL because `file.attach` and `CachedMedia` expose only original paths.

- [ ] **Step 4: Integrate `file.attach` with the profile store**

After `_stage_session_file_attachment`, call `ingest(..., session_id=session["id"], origin="desktop")`, wait up to the configured attachment-processing timeout, and return the processed ref on `ready`. On non-ready states return the original ref plus only `processing_status` and a product-safe `message`. Keep image/PDF vision tile RPCs unchanged.

- [ ] **Step 5: Integrate gateway document caching and prompt preparation**

Extend `CachedMedia` with optional `document_id`, `processed_path`, and `processing_status`. `cache_media_bytes` persists arbitrary document originals. Before `_build_document_context_note` and `preprocess_context_references_async`, resolve/wait for the sidecar. Use the processed path for agent context and keep the original display name in the note. Images/audio/video continue through existing caches.

- [ ] **Step 6: Add product-safe desktop state**

Change:

```typescript
uploadState?: 'uploading' | 'processing' | 'error'
```

Map `file.attach.processing_status === 'processing'` to the attachment pill text `Processing`; never display implementation names. When ready, store `refText` from the gateway but keep the original label/preview path so the user continues to see the uploaded file.

- [ ] **Step 7: Run Python and desktop tests**

Run: `scripts/run_tests.sh tests/test_tui_gateway_server.py tests/gateway/test_document_cache.py tests/gateway/test_document_context_note.py -q`

Run: `npm test -- --run apps/desktop/src/app/session/hooks/use-prompt-actions/index.test.tsx apps/desktop/src/app/chat/composer/attachments.test.tsx`

Expected: PASS; original labels remain visible and submitted refs point to `.md` when ready.

- [ ] **Step 8: Commit attachment integration**

```bash
git add tui_gateway/server.py gateway/platforms/base.py gateway/run.py apps/desktop/src/store/composer.ts apps/desktop/src/app/session/hooks/use-prompt-actions/index.ts apps/desktop/src/app/chat/composer/attachments.tsx tests/test_tui_gateway_server.py tests/gateway/test_document_cache.py tests/gateway/test_document_context_note.py apps/desktop/src/app/session/hooks/use-prompt-actions/index.test.tsx apps/desktop/src/app/chat/composer/attachments.test.tsx
git commit -m "feat: process uploaded chat documents"
```

---

### Task 7: Agent Results and Evidence Persistence

**Files:**
- Create: `server/agent_evidence.py`
- Modify: `server/db.py`
- Modify: `server/supabase/migrations/007_document_artifacts.sql`
- Modify: `server/agent_service.py`
- Modify: `server/routes/agent_runs.py`
- Test: `tests/server/test_agent_evidence.py`
- Test: `tests/server/test_run_harness.py`

**Interfaces:**
- Produces: `redact_evidence(value: Any) -> Any`, `evidence_from_output(output: dict) -> list[EvidenceInput]`, and `evidence_from_log(line: str) -> list[EvidenceInput]`.
- Produces: `AgentRunService.record_evidence(...)` and `AgentRunService.detail(company_id, run_id) -> dict`.
- Consumers: admin run detail and admin document detail.

- [ ] **Step 1: Write failing redaction and persistence tests**

```python
def test_evidence_redacts_credentials_and_keeps_source_result():
    raw = {"url": "https://example.test/report", "authorization": "Bearer secret", "result": {"name": "Widget"}}
    safe = redact_evidence(raw)
    assert safe["url"] == "https://example.test/report"
    assert safe["authorization"] == "[REDACTED]"
    assert safe["result"] == {"name": "Widget"}


def test_run_detail_includes_output_events_and_evidence(runs, company_id):
    run = completed_run_with_output_and_source(runs, company_id)
    detail = runs.detail(company_id, run["id"])
    assert detail["output"]
    assert detail["events"]
    assert detail["evidence"][0]["source_url"] == "https://example.test/report"
```

- [ ] **Step 2: Run evidence tests and confirm missing interfaces**

Run: `scripts/run_tests.sh tests/server/test_agent_evidence.py tests/server/test_run_harness.py -q`

Expected: FAIL importing `server.agent_evidence` or calling `runs.detail`.

- [ ] **Step 3: Add `agent_run_evidence` schema**

Add tenant/run/entity-scoped rows with source type, source URL/file reference, title, retrieved timestamp, redacted metadata/result JSON, and created timestamp. Add `(company_id, run_id)` and `(company_id, entity_type, entity_id)` indexes plus PostgreSQL RLS.

- [ ] **Step 4: Implement deterministic redaction and source extraction**

Redact keys matching `authorization`, `cookie`, `token`, `secret`, `password`, `api_key`, `system_prompt`, and raw tool argument containers. Bound strings and nested collection sizes. Recursively collect explicit URL/source/provenance fields from final structured output and URL-bearing safe log lines; deduplicate by `(source_type, source_url, file_reference)`.

- [ ] **Step 5: Persist evidence during and after runs**

- `HermesProcessExecutor` passes sanitized log-derived evidence to `service.record_evidence` as lines arrive.
- `_execute` extracts evidence from the validated final output before persisting success.
- `apply_output` retains existing domain persistence.
- `detail` returns the run, events, final structured output, output reference, related IDs from payload, and evidence rows.

- [ ] **Step 6: Make customer and admin run boundaries explicit**

Keep existing tenant-scoped `/agent-runs` responses compatible. Add `GET /agent-runs/{run_id}/detail` only if needed by existing selected-tenant admin navigation; it must call `company_scope`. The cross-company Admin API added in Task 8 uses `require_admin` and explicit company predicates.

- [ ] **Step 7: Run evidence/run tests**

Run: `scripts/run_tests.sh tests/server/test_agent_evidence.py tests/server/test_run_harness.py tests/server/test_api_mvp.py -q`

Expected: PASS with credentials absent from serialized evidence.

- [ ] **Step 8: Commit evidence persistence**

```bash
git add server/agent_evidence.py server/db.py server/supabase/migrations/007_document_artifacts.sql server/agent_service.py server/routes/agent_runs.py tests/server/test_agent_evidence.py tests/server/test_run_harness.py
git commit -m "feat: retain agent evidence for admins"
```

---

### Task 8: Admin Document and Run Observability API

**Files:**
- Create: `server/routes/admin_documents.py`
- Modify: `server/app.py`
- Modify: `server/routes/admin.py`
- Test: `tests/server/test_admin_documents.py`
- Test: `tests/server/test_api_mvp.py`

**Interfaces:**
- Produces endpoints:
  - `GET /api/v1/admin/documents`
  - `GET /api/v1/admin/documents/{document_id}`
  - `GET /api/v1/admin/documents/{document_id}/artifacts/{role}`
  - `POST /api/v1/admin/documents/{document_id}/retry`
  - `DELETE /api/v1/admin/documents/{document_id}`
  - `GET /api/v1/admin/agent-runs/{run_id}/detail`
- Consumers: Admin Documents and Admin Agent Run pages.

- [ ] **Step 1: Write failing admin authorization and detail tests**

```python
def test_admin_document_detail_contains_artifacts_attempts_results_and_evidence(admin_client):
    detail = admin_client.get(f"/api/v1/admin/documents/{document_id}").json()
    assert {item["role"] for item in detail["artifacts"]} == {"original", "processed"}
    assert detail["attempts"]
    assert detail["agent_run"]["output"] == {"records": records, "rejects": rejects}
    assert detail["agent_run"]["evidence"]


def test_customer_cannot_download_processed_artifact(customer_client):
    response = customer_client.get(f"/api/v1/admin/documents/{document_id}/artifacts/processed")
    assert response.status_code in {401, 403}
```

- [ ] **Step 2: Run admin tests and confirm routes are missing**

Run: `scripts/run_tests.sh tests/server/test_admin_documents.py -q`

Expected: FAIL with 404.

- [ ] **Step 3: Implement admin list/detail with explicit tenant filters**

Require admin on every route. The list accepts `company_id`, `status`, `origin`, `created_from`, and `created_to`. Detail returns metadata only: document, artifact metadata, attempts, semantic records/rejects, and `AgentRunService.detail`; it never embeds artifact bytes in JSON.

- [ ] **Step 4: Implement safe streaming and actions**

Artifact endpoint resolves by document/company/role, materializes with checksum verification, and returns `FileResponse` with `nosniff`, correct media type, and original/processed filename. Retry calls the processing coordinator with `force=True`. Delete verifies the exact document, calls repository deletion, and records an admin activity event.

- [ ] **Step 5: Implement cross-company admin run detail**

Look up the run's company ID, then call `runs.detail(company_id, run_id)`. Do not accept an unverified caller-supplied company ID. Return 404 for unknown runs without exposing another tenant through a guessed ID.

- [ ] **Step 6: Run authorization and API tests**

Run: `scripts/run_tests.sh tests/server/test_admin_documents.py tests/server/test_api_mvp.py -q`

Expected: PASS for admin access, customer denial, cross-tenant isolation, retry, streaming, and complete deletion.

- [ ] **Step 7: Commit admin API**

```bash
git add server/routes/admin_documents.py server/routes/admin.py server/app.py tests/server/test_admin_documents.py tests/server/test_api_mvp.py
git commit -m "feat: add admin document observability api"
```

---

### Task 9: Admin Documents and Agent Result UI

**Files:**
- Create: `server/webui/js/pages/admin-documents.js`
- Modify: `server/webui/js/api.js`
- Modify: `server/webui/js/main.js`
- Modify: `server/webui/js/pages/admin.js`
- Modify: `server/webui/js/pages/agent-runs.js`
- Modify: `server/webui/js/adapters.js`
- Modify: `server/webui/js/real-state.js`
- Test: `tests/server/test_webui.py`

**Interfaces:**
- Consumes: admin endpoints from Task 8.
- Produces: `/admin/documents`, `/admin/documents/:documentId`, and enhanced `/admin/agent-runs/:runId` views.

- [ ] **Step 1: Write failing static-route and product-language tests**

```python
def test_admin_documents_ui_is_wired_and_customer_copy_hides_implementation_terms():
    main = client.get("/js/main.js").text
    admin = client.get("/js/pages/admin.js").text
    documents = client.get("/js/pages/admin-documents.js").text
    assert "'/admin/documents'" in main
    assert "['/admin/documents', 'Documents']" in admin
    assert "Processed (.md) artifact" in documents
    customer_sources = client.get("/js/pages/setup.js").text + client.get("/js/pages/onboarding.js").text
    for forbidden in ("Anydoc", "conversion", "converter", "Markdown generation", "OCR"):
        assert forbidden.lower() not in customer_sources.lower()
```

- [ ] **Step 2: Run WebUI test and confirm module/route is missing**

Run: `scripts/run_tests.sh tests/server/test_webui.py::test_admin_documents_ui_is_wired_and_customer_copy_hides_implementation_terms -q`

Expected: FAIL because `admin-documents.js` and routes do not exist.

- [ ] **Step 3: Add API catalog entries and routes**

Add `admin.documents.list/detail/artifact/retry/delete` and `admin.agentRuns.detail` to `api.js`. Register exact list/detail routes in `main.js` before the static fallback. Add Documents to `ADMIN_TABS`.

- [ ] **Step 4: Implement Admin Documents list and detail**

The list renders company, original filename/type, public status, origin, timestamp, and processed-artifact availability. Detail renders:

- original preview/download;
- Processed (.md) artifact preview/download;
- public status and attempt timeline;
- records and rejects;
- final structured agent output;
- evidence sources/results and retrieval times;
- related entities; and
- retry/delete actions with confirmation.

Render structured JSON with escaped text nodes, never `innerHTML`. Use existing `dataTable`, `card`, `badge`, `modal`, and download helpers.

- [ ] **Step 5: Enhance Admin Agent Run detail**

Fetch `admin.agentRuns.detail`. Add cards for final structured output, related entity links, evidence/source results, and the event timeline. Preserve live polling/cancel/retry behavior and do not show credentials, system prompts, or raw tool arguments.

- [ ] **Step 6: Run WebUI tests**

Run: `scripts/run_tests.sh tests/server/test_webui.py -q`

Expected: PASS with all static imports/routes resolving and forbidden customer language absent.

- [ ] **Step 7: Commit admin UI**

```bash
git add server/webui/js/pages/admin-documents.js server/webui/js/api.js server/webui/js/main.js server/webui/js/pages/admin.js server/webui/js/pages/agent-runs.js server/webui/js/adapters.js server/webui/js/real-state.js tests/server/test_webui.py
git commit -m "feat: show document processing to admins"
```

---

### Task 10: Backfill, Whole-Path Verification, and Documentation

**Files:**
- Modify: `server/document_artifacts.py`
- Modify: `server/app.py`
- Modify: `website/docs/user-guide/configuration.md`
- Modify: `PRODUCT.md`
- Test: `tests/server/test_document_backfill.py`
- Test: `tests/integration/test_document_ingestion.py`
- Test: `tests/test_packaging_metadata.py`

**Interfaces:**
- Consumes all prior tasks.
- Produces idempotent legacy backfill and final supported configuration documentation.

- [ ] **Step 1: Write failing legacy-backfill test**

```python
def test_backfill_creates_original_artifact_and_queues_processing(repo, db, legacy_file):
    seed_legacy_document(storage_path=str(legacy_file), status="uploaded")
    summary = repo.backfill_existing_documents()
    assert summary == {"backfilled": 1, "missing": 0, "already_current": 0}
    assert db.one("SELECT role FROM document_artifacts WHERE document_id='doc_old'")["role"] == "original"
    assert db.one("SELECT status FROM documents WHERE id='doc_old'")["status"] in {"uploaded", "processing"}
```

- [ ] **Step 2: Implement idempotent backfill and startup scheduling**

For legacy rows without original artifacts:

- If `storage_path` resolves to readable local bytes, create the database artifact/mirror and queue processing.
- If it is a Supabase location, use the existing storage resolver only for this migration read, persist the bytes locally/database, then stop depending on the signed URL.
- If bytes are unavailable, set `needs_attention` with product-safe detail.
- Record a schema/data migration marker so normal startup only scans unfinished legacy rows.

- [ ] **Step 3: Add an end-to-end real-import ingestion test**

With a temporary database, upload root, and `HERMES_HOME`:

1. upload a fixture through FastAPI;
2. wait for Ready;
3. verify original and processed bytes in DB and local mirrors;
4. delete the processed local mirror and verify admin preview reconstructs it;
5. start semantic processing and verify `.md` input;
6. inspect admin detail for output/evidence;
7. delete the document and verify both forms are gone.

- [ ] **Step 4: Document product-safe settings and behavior**

Document `interfaze_server.document_workers`, `document_processing_timeout_seconds`, and `document_output_max_bytes`. Update `PRODUCT.md` document/admin route sections to state that uploads enter Processing automatically, agents use an internal processed artifact, and admins can audit both forms and agent evidence. Customer docs use only approved product terminology.

- [ ] **Step 5: Run targeted whole-feature verification**

Run:

```bash
scripts/run_tests.sh \
  tests/agent/test_document_processing.py \
  tests/agent/test_document_artifacts.py \
  tests/agent/test_context_references.py \
  tests/server/test_document_artifacts.py \
  tests/server/test_document_processing_service.py \
  tests/server/test_admin_documents.py \
  tests/server/test_agent_evidence.py \
  tests/server/test_document_backfill.py \
  tests/integration/test_document_ingestion.py \
  tests/test_tui_gateway_server.py \
  tests/gateway/test_document_cache.py \
  tests/gateway/test_document_context_note.py \
  -q
```

Expected: PASS.

- [ ] **Step 6: Run metadata, complete server, and desktop verification**

Run:

```bash
scripts/run_tests.sh tests/test_project_metadata.py tests/test_packaging_metadata.py tests/server/ -q
npm test -- --run apps/desktop/src/app/session/hooks/use-prompt-actions/index.test.tsx apps/desktop/src/app/chat/composer/attachments.test.tsx
npm run typecheck --workspace apps/desktop
```

Expected: PASS.

- [ ] **Step 7: Run diff and terminology checks**

Run:

```bash
git diff --check
rg -n -i "anydoc|conversion|converter|markdown generation|ocr" server/webui/js/pages/setup.js server/webui/js/pages/onboarding.js server/routes/knowledge.py
```

Expected: `git diff --check` is clean. The terminology search returns no customer-visible implementation copy; internal code identifiers/comments may be inspected separately.

- [ ] **Step 8: Commit final rollout work**

```bash
git add server/document_artifacts.py server/app.py website/docs/user-guide/configuration.md PRODUCT.md tests/server/test_document_backfill.py tests/integration/test_document_ingestion.py tests/test_packaging_metadata.py
git commit -m "feat: complete local document ingestion"
```

- [ ] **Step 9: Invoke verification-before-completion**

Use `superpowers:verification-before-completion`, re-run its required commands, inspect `git status --short`, and report any unrelated pre-existing changes separately from this feature.
