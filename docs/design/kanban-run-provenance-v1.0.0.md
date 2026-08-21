# Contract: Structured Run Provenance for Hermes Kanban

- **Version:** 1.0.0-draft
- **Status:** Proposed — awaiting `factory-director` routing, then security review
- **Author:** `solution-architect`, card `t_8c86298c`
- **Date:** 2026-08-20
- **Supersedes:** free-text subject/final SHA in `task_runs.summary` / `task_runs.metadata` prose

---

## §0 — Problem

The provenance broker (research `t_5247914a`) needs, per gated candidate:

```
{run_id, task_id, authenticated profile, outcome, implementation subject SHA,
 final candidate SHA, artifact paths + sha256, completed_at}
```

Today subject and final SHA commonly live in comments, `summary`, or `metadata` prose. **Parsing
prose is a tamper and ambiguity surface.** A worker writes the text; a worker can therefore choose
what the exporter reads. The canonical authority is the local Kanban SQLite database, so the fix is
to make these fields *structured columns the kernel owns*, not text the worker composes.

This document decides the schema, who may write each field, mutability, and the trust seam to the
DSSE attestation design. It is a contract decision, not an implementation.

---

## §1 — Source anchors (verified, not recalled)

Read-only inspection of the live board DB (`immutable=1` open, no writes):

```
db: ~/.hermes/kanban/boards/hermes-agent/kanban.db

task_runs (16 cols)
  id PK, task_id NOT NULL, profile, step_key, status NOT NULL, claim_lock,
  claim_expires, worker_pid, max_runtime_seconds, last_heartbeat_at,
  started_at NOT NULL, ended_at, outcome, summary, metadata, error
  indexes: idx_runs_status, idx_runs_task

tasks (37 cols)
  ... branch_name, project_id, current_run_id, idempotency_key, session_id ...

task_events (6 cols)
  id PK, task_id NOT NULL, run_id, kind NOT NULL, payload, created_at
  indexes: idx_events_run, idx_events_task
```

**Two facts from the schema shape the decision:**

1. `task_runs.profile` and `task_runs.outcome` **already exist as first-class columns.** The
   additive-columns-vs-envelope question is therefore not symmetric — half the required fields are
   already structured. Only the SHAs and artifact hashes live in prose.
2. `task_runs.metadata` is a TEXT blob. It is the surface being eliminated, so the contract must not
   solve the problem by putting a new JSON schema *inside* it.

---

## §2 — Decision

**Additive typed columns on `task_runs`, plus one child table for artifacts. No envelope blob.**

### §2.1 `task_runs` — new columns

| Column | Type | Null | Writer | Notes |
|---|---|---|---|---|
| `subject_sha` | TEXT | yes until claim | **kernel only** | Full 40-char lowercase hex. Captured at claim from the typed task target. Never writable via the worker path. |
| `role` | TEXT | no | **kernel only** | Enum: `implementation`, `code_review`, `qa`, `security`. Derived from dispatch, not caller-supplied. |
| `provenance_version` | INTEGER | no | kernel | Starts at `1`. Lets a future schema change be detected rather than silently reinterpreted. |
| `corrects_run_id` | INTEGER | yes | kernel | FK → `task_runs(id)`. Non-null only on correction rows (§5). |

`profile`, `outcome`, `ended_at` are **existing** columns and are reused as-is. `completed_at` for
export purposes is `ended_at`; no new column.

**CHECK constraints (fail-closed at write time):**
```sql
CHECK (subject_sha IS NULL OR (length(subject_sha) = 40 AND subject_sha GLOB '[0-9a-f]*'))
CHECK (role IN ('implementation','code_review','qa','security'))
CHECK (provenance_version >= 1)
```

### §2.2 New table `run_artifacts`

```sql
CREATE TABLE run_artifacts (
  id            INTEGER PRIMARY KEY,
  run_id        INTEGER NOT NULL REFERENCES task_runs(id),
  artifact_path TEXT    NOT NULL,
  sha256        TEXT    NOT NULL,
  created_at    INTEGER NOT NULL,
  UNIQUE(run_id, artifact_path),
  CHECK (length(sha256) = 64 AND sha256 GLOB '[0-9a-f]*')
);
CREATE INDEX idx_run_artifacts_run ON run_artifacts(run_id);
```

One-to-many, per `platform-engineer`'s constraint. `UNIQUE(run_id, artifact_path)` is deliberate:
double-recording an artifact becomes an error rather than a silent duplicate, closing one of the
ambiguity surfaces this card exists to eliminate.

---

## §3 — Trust semantics

### §3.1 Who may write what

| Field | Worker | Kernel | Broker |
|---|---|---|---|
| `artifact_path`, `sha256` | **supplies** (pre-terminal) | validates + stamps `created_at` | reads |
| `subject_sha` | never | **stamps at claim** | reads |
| `role`, `profile` | never | **stamps at dispatch** | reads |
| `outcome` | never | **derives from transition** | reads |
| `ended_at` | never | stamps at terminalization | reads |
| final candidate SHA | never | never | **derives** (§4) |

**Invariant:** a caller-supplied role, outcome, or identity is a *claim*, never authentication. The
worker's only provenance input is `{artifact_path, sha256}` pairs, and even those are hashes over
content the broker can re-verify independently.

### §3.2 Role independence is per-RUN, not per-commit

Corrected from an earlier draft after `platform-engineer` review. **Commit count is transport; run
identity is provenance.** One run can emit three commits; three commits can be assembled by one
actor. Neither demonstrates that review, QA and security were performed by distinct principals.

The evaluator asserts, over **terminal `task_runs` rows**:

1. For each required role in `{code_review, qa, security}` (+ `implementation` when represented),
   at least one terminal run exists with that `role`.
2. The `profile` values across those required roles are **pairwise distinct**.

> The same profile satisfying two required roles is a **FAIL**, not a warning. Self-review is the
> exact failure this table exists to prevent.

This is enforceable *only* because `profile` is kernel-stamped: a worker cannot write another
profile's name into its own run row.

### §3.3 Repository and PR identity

- The run stores **locators**, not authority.
- The run does **not** carry a PR number. A PR may not exist when the run ends; requiring it would
  force either a nullable field everyone treats as optional, or a write after terminalization —
  which §5 prohibits.
- The broker independently **resolves and validates** `owner/repo` and PR number to immutable
  numeric GitHub IDs at attestation time, and binds those.
- **Branch name is never an authority.** It is worker-influenced and mutable; trusting it is the
  same defect class as trusting a role string in candidate JSON. It may serve as a resolution
  *hint* only.

`repo_numeric_id` is deliberately **not** added to `task_runs` in v1.0.0. Whether it must be
persisted belongs to the DSSE trust design (§7 seam), not to this schema.

---

## §4 — Subject SHA vs final candidate SHA

| | Subject SHA | Final candidate SHA |
|---|---|---|
| Meaning | the commit that was reviewed | the assembled PR head |
| Known at | claim time | after multi-commit assembly |
| Stored | `task_runs.subject_sha` | **not stored on the run** |
| Written by | kernel, at claim | broker, at attestation |

**Why subject SHA must come from a kernel-captured typed task target** (not worker JSON, not the
evidence branch HEAD): if it is read from branch HEAD, the worker chooses *what was reviewed* after
review happened. Capturing at claim makes the subject an **input** to the work rather than an
**output** of it.

**Why final candidate SHA is not a run field:** it is only knowable after assembly — i.e. after the
run is terminal — and §5 makes terminal runs immutable. Storing it on the run would require exactly
the mutation this contract prohibits.

---

## §5 — Mutability and corrections

**Terminal runs are immutable.** Once `status` is terminal and `ended_at` is set, no field on that
row may be UPDATEd.

Corrections are **append-only**: a new `task_runs` row with `corrects_run_id` pointing at the row
being corrected.

Broker obligations — all **fail-closed**:

1. A correction **may not** alter provenance already consumed by an emitted attestation.
2. The broker **MUST reject ambiguous or unresolved correction chains**: two corrections of the same
   row, a cycle, or a dangling `corrects_run_id`. This is a hard fail, **not** a
   last-write-wins tiebreak.
3. The exporter reads the resolved head of a chain. An unresolvable chain yields **no export**,
   never a guess.

Commands and results stay **inside the hashed artifact**, not in DB columns. The DB holds identity
and hashes; the artifact holds the narrative. This keeps the tamper surface small and row size
bounded.

---

## §6 — Export

- The exporter selects terminal runs with a non-null `subject_sha` and complete `run_artifacts`.
  **Prose is never parsed.**
- **Detection without polling prose:** a `provenance_ready` row in `task_events` (kind is already a
  first-class column, `idx_events_run` already exists) emitted by the kernel at terminalization.
- **Watermark/idempotency:** the exporter tracks the highest exported `task_events.id`. Re-export of
  an already-watermarked run is a no-op, not a duplicate.
- **Exporter credentials must be read-only against the canonical SQLite.** An exporter that can
  rewrite the source of truth is not an exporter.
- **Minimal export:** `{run_id, task_id, profile, role, outcome, subject_sha, artifacts[], ended_at}`.
  No `summary`, no `error`, no `body` — those may carry paths, tokens, or user content.
- Missing or ambiguous fields **fail closed**: no attestation.

---

## §7 — Seam to the DSSE design (deliberately deferred)

This contract does **not** decide:

- whether the attestation must bind numeric repo ID and PR number;
- the DSSE envelope format or signing primitive.

Those belong to the security trust design. This document names the seam so the two cannot silently
disagree: `repo_numeric_id` is **absent in v1.0.0**, and adding it is a `provenance_version` bump.

**Authoritative DSSE design: `t_8513bc6e`** — *"Security design: authenticate PR #36 reviewer roles
without candidate-controlled trust"*, `status=done`, `assignee=security-reviewer`, created by
`factory-director`. Its first `security-reviewer` comment (`created_at=1787270558`, 12,139 chars) is
the binding recommendation.

> **Correction to an earlier revision of this document.** A prior draft recorded `t_8513bc6e` as
> non-existent. That was **my error, not a dangling reference.** The card is present in
> `~/.hermes/kanban/boards/account-gen/kanban.db` (156 tasks). The CLI board resolver misrouted the
> lookup — the known defect carded as `t_9b4f8ded` (*explicit `--board` ignored when
> `HERMES_KANBAN_DB` is set*). Verified by opening the DB directly, read-only:
> `t_8513bc6e: PRESENT`. Absence of a CLI result is not evidence of absence when the resolver
> itself is under repair.

### §7.1 — Binding constraint from `t_8513bc6e` (changes this contract)

The security design answers **YES** to the question of whether the shared GitHub identity defeats
API-derived role provenance:

> *"the shared `SiWarlock` GitHub identity makes the GitHub review API insufficient by itself. The
> API authenticates one GitHub account, association and review `commit_id`; it cannot distinguish
> `code-reviewer`, `qa-verifier`, and `security-reviewer`."*

Independently verified against the live repository:

```
gh pr view 36 --json reviews   ->  [{"author": "SiWarlock", "state": "COMMENTED"}]
git log --format='%an' -8 origin/main   ->  8x "Cody Clayton"
```

Every factory role acts through **one** GitHub account. Therefore GitHub review-API identity is
**necessary but not sufficient**: it authenticates *repository, PR, and `commit_id`*, and cannot
authenticate *which factory role performed the review*.

**This is precisely why §3.2's kernel-stamped `task_runs.profile` is the trust anchor.** The Kanban
run record is the only place where role is established by the dispatcher rather than asserted by
the actor. The security design states the same conclusion independently:

> *"Kanban run profile/claim records, not artifact fields or comments, establish role."*

Division of authority, now settled:

| Fact | Authenticated by |
|---|---|
| repository identity, PR number, head SHA | GitHub API (numeric IDs) |
| **factory role** (`code_review` / `qa` / `security`) | **kernel-stamped `task_runs.profile`** |
| artifact content | SHA-256 recomputed from git objects by the broker |
| the binding of all of the above | DSSE/in-toto attestation, asymmetric KMS/HSM key |

`repo_numeric_id` remains **absent from `task_runs` in v1.0.0**: the broker resolves it from GitHub
at attestation time (§3.3) and binds it in the attestation. Adding it to the run row would be a
`provenance_version` bump, and `t_8513bc6e` does not require run-level persistence.

---

## §8 — Rejected alternatives

| Alternative | Why rejected |
|---|---|
| **JSON envelope in `task_runs.metadata`** | Re-creates the parsing surface being eliminated, one layer deeper. No CHECK constraints, no FK, no uniqueness. |
| **Fixed `artifact_1..artifact_n` columns** | Cannot express variable artifact counts; forces either truncation or a schema change per new artifact. |
| **Final candidate SHA as a run column** | Only knowable post-assembly, i.e. post-terminal. Would require mutating an immutable row (§5). |
| **Commit-count-based role independence** | Commit count is transport, not identity. Three commits by one actor prove nothing about independence. |
| **Branch name as identity anchor** | Worker-influenced and mutable — same class as trusting caller-supplied role JSON. |
| **UPDATE-in-place corrections** | Destroys the audit trail and lets consumed provenance change under an emitted attestation. |

---

## §9 — Migration and compatibility

1. All new columns are **additive and nullable-on-existing-rows**. Existing rows read as
   `provenance_version = NULL` → treated as *legacy, unattestable*.
2. **No backfill.** A backfilled subject SHA would be inferred, not captured — inferred provenance
   is exactly what this contract removes. Legacy runs are excluded from export rather than
   retroactively blessed.
3. `run_artifacts` is a new table; absence is indistinguishable from "no artifacts recorded", so the
   evaluator requires `provenance_version >= 1` before asserting anything about artifacts.
4. Rollback: drop `run_artifacts`, drop the four added columns. No existing column changes type or
   nullability, so rollback cannot corrupt pre-existing rows.

**Blast radius:** schema change to the kernel DB used by every board and every bot. Requires a
migration path and a backup verified by checksum before application. This is a
`factory-director` apply decision, not an implementer's.

---

## §10 — Acceptance tests (executable)

| # | Test | Expected |
|---|---|---|
| A1 | Worker attempts to write `role` or `profile` on its own run | rejected; kernel value unchanged |
| A2 | Worker supplies `{path, sha256}`; kernel stamps `created_at` | row present, hash matches recomputed digest |
| A3 | Same profile produces `code_review` and `security` terminal runs | evaluator **FAIL** (§3.2) |
| A4 | Three distinct profiles across the three required roles | evaluator **PASS** |
| A5 | `subject_sha` of 39 chars, uppercase, or non-hex | CHECK rejects |
| A6 | UPDATE any field on a terminal run | rejected |
| A7 | Two correction rows target the same `corrects_run_id` | broker **fails closed**, no export |
| A8 | `corrects_run_id` points at a non-existent row | broker **fails closed** |
| A9 | Duplicate `(run_id, artifact_path)` | UNIQUE violation |
| A10 | Re-export an already-watermarked run | no-op, no duplicate attestation |
| A11 | Run with `provenance_version = NULL` (legacy) | excluded from export, not an error |
| A12 | Export payload inspected for `summary` / `error` / `body` | absent (§6 minimal export) |

---

## §11 — Provenance of this document

Design constraints in §3.2, §3.3 and §4 were supplied by `platform-engineer` (card `t_8c86298c`
consultation) and **corrected two errors** in the author's earlier model: commit-level rather than
run-level independence, and final candidate SHA held as a run column. Both are recorded in §8 as
rejected alternatives so the reasoning survives the decision.

Schema facts in §1 were read from the live database, not recalled.

**Security review is a required next gate and has not occurred.** At time of writing,
`security-reviewer` returns HTTP 402 provider billing errors rather than opinions (observed on
`t_bde9863c`, escalated to `factory-director`). This contract must not be treated as
security-approved.
