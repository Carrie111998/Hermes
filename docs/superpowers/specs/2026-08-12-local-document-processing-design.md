# Local Document Processing Design

**Date:** 2026-08-12
**Status:** Approved for implementation planning

## Summary

Interfaze will process every supported uploaded document into an agent-readable
Markdown artifact. Processing stays fully local. The original upload is always
retained, and the existing local document-processing fallback handles scanned
or image-only documents when the primary processor cannot extract meaningful
content.

Both the original and processed forms are stored in the database and mirrored
to tenant-isolated local storage. Customers continue to see the document they
uploaded. The processed `.md` form is an internal sidecar for agents, while
administrators can inspect and manage both artifacts and the associated agent
results.

Customer-facing surfaces use product language only: **Uploaded**,
**Processing**, **Ready**, **Needs attention**, and **Failed**. They do not
mention Anydoc, conversion, Markdown generation, or OCR.

## Goals

- Make supported uploaded documents reliably readable by agents without model
  intervention.
- Apply the same processing behavior to onboarding, API uploads, chat
  attachments, and messaging attachments.
- Keep all processing local and make no hosted parsing request.
- Retain the complete original and processed content in both the database and
  local storage.
- Keep the processed Markdown invisible on customer document screens while
  making both forms manageable by administrators.
- Preserve provenance from processed content and extracted records back to the
  original document.
- Let administrators inspect fetched evidence, extracted results, rejects, and
  final structured agent output.
- Preserve prompt-cache stability and avoid adding a model tool.

## Non-goals

- A hosted Firecrawl Parse integration or any other network processing
  fallback.
- Exposing processor implementation names to customers.
- Replacing the original document with Markdown.
- Requiring an agent to decide whether or how to process a document.
- Automatically installing the multi-gigabyte advanced processing stack in an
  upload request.
- Rebuilding image, audio, or video attachment behavior.

## Upstream Dependency

Use the Python distribution `firecrawl-anydoc`, imported as `anydoc`. The
dependency must follow the repository's supply-chain policy and carry both a
floor and an upper bound. The initial supported range will be
`firecrawl-anydoc>=0.1.8,<0.3` and the lockfile will be regenerated.

Anydoc is a local Rust-backed parser with Python bindings. It supports Word,
PowerPoint, Excel, OpenDocument, RTF, EPUB, CSV, and text-based PDF files. It
uses content-based format detection for formats with signatures and requires a
filename or explicit format for signature-less CSV input. It does not perform
OCR for scanned or image-only PDFs. Upstream reference:
<https://github.com/firecrawl/anydoc>.

## Architecture

### Shared processing boundary

Add `agent/document_processing.py` as the single format-processing boundary.
It accepts either a local path or bytes plus a filename and returns a typed
result:

- `converted`: meaningful Markdown was produced by the primary local parser.
- `passthrough`: already-readable textual content was normalized as Markdown.
- `needs_fallback`: the primary parser could not read content that the existing
  local advanced document workflow may handle.
- `failed`: no safe processed artifact was produced.

The module:

- performs no network calls;
- never deletes or mutates the original;
- contains format detection and processing policy;
- returns content and structured diagnostics but does not choose persistence
  paths;
- enforces input, output, time, and resource limits;
- treats empty or whitespace-only output as failure rather than success; and
- translates dependency-specific exceptions into stable internal reason codes.

Existing ingestion adapters call this boundary instead of invoking Anydoc
directly. This prevents onboarding, desktop, CLI, TUI, and messaging behavior
from drifting apart.

### Processing coordinator

Add a bounded background coordinator that owns the lifecycle from a durable
original artifact to a durable processed artifact. It:

1. loads the original from the artifact repository;
2. changes the document's public status to `processing`;
3. runs the shared processor with a timeout;
4. invokes the existing local fallback when the result requests it;
5. validates and stores the processed artifact;
6. atomically changes the public status to `ready`; and
7. records safe public errors separately from internal diagnostics.

Primary processing and fallback processing use separate concurrency limits.
The fallback limit is lower because advanced document processing is more
resource-intensive.

### Persistence boundary

Use a small artifact repository interface with two concrete contexts:

- The Interfaze server repository stores artifacts in the tenant database and
  mirrors them under the configured upload root. These records are available
  to the admin API and UI.
- Standalone Hermes sessions store artifacts in their profile-scoped database
  and local attachment cache. This preserves the same durability contract when
  no Interfaze admin server exists.

The shared processor depends only on the repository contract, not FastAPI,
the desktop gateway, or a particular database implementation.

## Data Model

### Documents

The existing `documents` record remains the logical upload. Add or normalize
fields for:

- public processing status;
- safe public status detail;
- current processing attempt;
- original checksum;
- active processed artifact ID;
- created, updated, processing-started, and ready timestamps; and
- upload origin (`onboarding`, `api`, `desktop`, `tui`, `cli`, or messaging
  platform).

The customer document serializer omits internal artifact IDs, local paths,
processor details, and diagnostics.

### Document artifacts

Add a `document_artifacts` table. Each row belongs to one document and tenant
and has:

- artifact role: `original` or `processed`;
- filename and media type;
- complete content as binary data;
- SHA-256 checksum and byte size;
- local mirror path;
- creation timestamp and processing attempt ID; and
- internal metadata needed for provenance and recovery.

There is exactly one retained original artifact per logical upload. A
successful processing attempt creates a processed artifact, then atomically
makes it active. Failed attempts never replace the active processed artifact.
Retry history remains available through processing attempts and agent-run
events instead of accumulating ambiguous active files.

The database is the authoritative inventory and recovery source. Local mirrors
provide fast file access to existing agent tools. If a local mirror is missing
or its checksum differs, it is reconstructed from database content before use.

### Processing attempts and evidence

Every attempt records:

- document and tenant IDs;
- public status and safe public message;
- internal stage and reason code;
- start and completion timestamps;
- input and output checksums;
- associated semantic agent-run ID, when applicable; and
- sanitized internal diagnostics.

Agent-run evidence remains associated with its run and originating entity.
Evidence records contain source type, source URL or file reference, retrieval
time, title/label, safe metadata, and the extracted result or reference to its
stored payload. Secrets, credentials, authorization headers, system prompts,
and sensitive raw tool arguments are never persisted in admin-visible
evidence.

## Local Storage Layout

Interfaze-managed documents use:

```text
<upload-root>/<company-id>/<document-id>/
├── original/<safe-filename>
└── derived/content.md
```

Standalone desktop/TUI workspace uploads use a private derived directory such
as:

```text
<workspace>/.hermes/desktop-attachments/
├── <safe-original-name>
└── .derived/<sha256>.md
```

Messaging uploads use the profile-scoped document cache with an equivalent
private `.derived` directory. Temporary files are tenant/profile scoped and
atomically promoted only after checksum verification and the database write
succeed.

## End-to-End Data Flow

### Upload and automatic processing

1. Validate the request, filename, size limit, and tenant/session scope.
2. Stream the original once while computing its SHA-256 checksum.
3. Store the complete original in the database and local mirror.
4. Commit the logical document with public status `uploaded`.
5. Enqueue background processing and return the upload response.
6. Normalize or locally parse the original.
7. If necessary and applicable, invoke the existing local advanced document
   fallback.
8. Validate that the result is meaningful UTF-8 Markdown within limits.
9. Store the complete processed artifact in the database and local mirror.
10. Atomically set the active processed artifact and public status `ready`.

The content checksum makes the work idempotent. Repeated processing of the same
unchanged original reuses a verified processed artifact unless an administrator
explicitly requests a fresh attempt.

### Onboarding and semantic extraction

Onboarding upload automatically performs the technical processing above. The
existing document-processing agent run is a separate semantic step: it turns
the ready Markdown into validated domain records.

When `/documents/{id}/process` starts semantic extraction, it resolves the
active processed artifact, reconstructs its local mirror if needed, and gives
that Markdown path to the agent. If technical processing is still running, the
semantic run waits or remains queued. If the document needs attention or
failed, the API returns a safe non-technical state instead of passing the raw
binary to the agent.

Extracted records retain source pointers containing the document ID and, where
available, page, slide, sheet, row, or section information.

### Chat and messaging attachments

Desktop/TUI `file.attach`, CLI file-drop/reference handling, and messaging
document caching all stage the original and enqueue the same processing
lifecycle.

When ready, the prompt/context path uses the processed Markdown while retaining
the original reference as provenance. An attachment-only turn can wait for the
bounded processing result. If user text accompanies the attachment, the client
may display `Processing` until the artifact is ready, then submit the turn.

If processing cannot produce a usable artifact, no empty Markdown is inserted
and no technical exception is placed into customer-visible agent output. The
original remains available for preview, download, retry, and administrator
inspection.

Already-readable Markdown, text, JSON, YAML, and source files bypass Anydoc and
are normalized into the internal processed artifact.

## Status and Language Contract

Customer-visible document states are limited to:

- **Uploaded**
- **Processing**
- **Ready**
- **Needs attention**
- **Failed**

Customer UI, API messages, and agent-authored customer responses must not use
the terms `Anydoc`, `conversion`, `converter`, `Markdown generation`, or `OCR`.
Safe error examples include:

- “We couldn’t process this file. Please upload an unlocked copy or try
  another format.”
- “This file needs attention before it can be used.”

Implementation details remain in restricted diagnostics and server logs. The
admin UI uses the same product-oriented statuses. It may label the stored
sidecar as the “Processed (.md) artifact” because administrators explicitly
need to preview and manage that form, but it does not expose processor names or
fallback implementation details.

## Failure Handling

- Upload success depends only on durably storing the original in the database
  and local mirror. Background processing failure never loses the upload.
- Text-based supported input produces the processed artifact through the
  primary local parser.
- A scanned or image-only document automatically enters the existing local
  fallback path.
- If the advanced dependency is unavailable or the machine lacks required
  resources, set `needs_attention`, preserve the original, and expose an admin
  retry action. Do not install heavyweight dependencies inside the request or
  worker automatically.
- Encrypted or password-protected input sets `failed` with a request for an
  unlocked copy.
- Malformed, unsupported, timed-out, or resource-limited input remains stored,
  gets a sanitized public failure, and retains restricted diagnostics.
- A retry creates a new processing attempt. It never destroys the original or
  a previously valid processed artifact.
- Document deletion removes the logical document, artifact rows, evidence
  references governed by retention rules, and both local mirrors as one
  authorization-checked operation. Partial local cleanup is reported for
  repair without resurrecting database records.

## Admin API and UI

### Admin document endpoints

Add admin-only, tenant-aware endpoints to:

- list and filter documents across companies;
- inspect a document and its processing attempts;
- stream the original artifact;
- preview or stream the processed `.md` artifact;
- view extracted records and rejected records with reasons;
- view associated agent-run results and evidence;
- retry processing; and
- delete the document and both artifact forms.

Artifact bytes are never embedded in ordinary JSON list/detail responses.
Download and preview routes stream data only after administrator authorization.

### Admin Documents screen

Add an Admin Documents destination with filters for company, public status,
origin, and date. A document detail view shows:

- original metadata and download/preview action;
- processed `.md` availability and preview/download action;
- product-facing processing status and attempt timeline;
- extracted records and rejected records with reasons;
- final structured agent output;
- fetched source/evidence metadata and retrieval timestamps;
- related company, run, lead, contact, or other entity;
- safe failure information; and
- retry and delete actions.

The page links to the existing Admin Agent Runs area. The run detail provides
the same final structured output, safe activity timeline, fetched evidence,
related entities, retry history, and failures for every information-fetching
agent run, not only document processing.

All views enforce administrator authorization and explicit tenant filters.
Cross-tenant lookups return not found/forbidden without disclosing existence.

## Security and Privacy

- All processing is local; no uploaded content is sent to Firecrawl or another
  hosted parser.
- Database rows and local paths are tenant/profile scoped.
- Filenames are sanitized and final paths are checked against their storage
  root.
- Artifact previews use correct media types, attachment headers, and existing
  web security headers.
- Internal paths are omitted from customer responses.
- Secrets, credentials, authorization headers, system prompts, and unsafe tool
  arguments are redacted before evidence is stored or shown.
- Checksums are verified when reconstructing or serving local mirrors.
- Existing upload-size limits remain authoritative; processed-output and
  decompression limits prevent expansion attacks.

## Testing Strategy

### Unit tests

- Format classification, readable-text passthrough, and stable result codes.
- Successful Anydoc processing and meaningful-output validation.
- Primary failure to local fallback routing.
- Encrypted, malformed, unsupported, empty, timed-out, and resource-limited
  handling.
- Public status/message mapping with forbidden terminology checks.
- SHA-256 idempotency and active-artifact selection.
- Evidence redaction.

### Repository and storage contract tests

- Original and processed bytes stored completely in the database.
- Identical verified local mirrors for both artifacts.
- Reconstruction from database bytes when a local mirror is missing or corrupt.
- Atomic promotion of a successful processed artifact.
- Retry preserving the original and prior valid processed artifact.
- Deletion removing both database artifacts and local mirrors.
- Equivalent behavior for SQLite and PostgreSQL-backed server paths where the
  repository already supports both.

### API and authorization tests

- Onboarding/API uploads return after durable original storage and enter
  `processing`.
- Customer list/detail responses expose only safe fields and statuses.
- Admin list/detail, artifact streaming, retry, and deletion behavior.
- Agent output and fetched evidence reach admin endpoints.
- Admin artifact/evidence access is tenant-filterable and cannot cross tenant
  boundaries.
- Non-admin users cannot access admin artifacts or run results.

### Integration and end-to-end tests

- Real Anydoc imports against representative committed fixtures for supported
  format families.
- A real temporary database, upload root, and isolated `HERMES_HOME`.
- Onboarding semantic extraction consumes the processed `.md`, not the binary
  original.
- Desktop/TUI and messaging attachment paths supply processed content to agent
  context while retaining original provenance.
- Scanned-document fallback and unavailable-dependency behavior.
- Admin UI renders artifact metadata, processed preview, agent results,
  evidence, and actions.
- Customer UI/API output contains none of the forbidden implementation terms.

All Python tests run through `scripts/run_tests.sh`, as required by the
repository. Frontend tests use the existing server WebUI test path.

## Compatibility and Rollout

- Existing document records without artifact rows remain readable during
  migration. A backfill creates original artifact rows from resolvable storage
  paths and queues processing without changing customer-facing ownership.
- Historical records whose original bytes are unavailable remain visible with
  `needs_attention`; they are not presented as ready.
- Existing document IDs, tenant ownership, and customer API routes remain
  stable.
- The existing semantic `document_processing` run type and deterministic
  record persistence remain in place; only its input changes to the processed
  artifact.
- No model tool, toolset entry, or mutable system-prompt content is added.
  Prompt caching and model-tool schema footprint remain unchanged.

## Acceptance Criteria

The feature is complete when:

1. Every supported upload path stores the complete original in its database
   and local mirror and initiates local processing automatically.
2. Successful processing stores complete Markdown in the database and local
   mirror and makes it the agent's content source.
3. The original remains available for preview, download, audit, retry, and
   provenance.
4. Scanned/image-only documents use the existing local fallback without any
   hosted request.
5. Customer surfaces reveal only approved status language and never expose
   implementation terminology.
6. Administrators can manage both artifacts and inspect extracted records,
   rejects, final agent output, fetched evidence, and run history.
7. Missing local artifacts are recoverable from database content with checksum
   verification.
8. Authorization and tests prove tenant isolation and secret redaction.
9. No model tool or prompt-cache-invalidating behavior is introduced.
