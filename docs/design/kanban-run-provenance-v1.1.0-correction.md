# Contract correction: Structured Run Provenance for Hermes Kanban — v1.1.0

- **Version:** 1.1.0 (corrects v1.0.0-draft, attachment id 4,
  sha256 `38d66a972800f501b6a6e776ea3076b0d1d12e51804a64c4171e9426ced741bd`)
- **Status:** Proposed — security review is still a required, un-run gate
- **Author:** `solution-architect`, card `t_8c86298c`, run 8
- **Scope:** v1.0.0 remains in force **except** for §A and §B below.

This is a correction, not a replacement. v1.0.0 §0–§4, §6, §8–§11 stand
as written. Two changes are made, each for a stated cause: one is a
binding director ruling that closes a seam v1.0.0 deliberately left
open; the other is an enforceability defect I found in the live source
after v1.0.0 was frozen.

**Contract source for the security design:** attachment id 3,
`security-design-t_8513bc6e-comment-573.md` (12,212 bytes). Verified
this run: byte-identical to live account-gen comment 573 after
whitespace normalization, differing only by a 67-character provenance
header. All "comment 573 §N" references resolve to that attachment, not
to a cross-board CLI lookup.

**Executable evidence:** attachment `verify_adr0007_mechanisms.py`,
36/36 checks passing. See §C.

---

## §A — Repository binding: seam closed (supersedes v1.0.0 §3.3, §7, §7.1)

v1.0.0 §7 stated that whether the attestation must bind numeric repo ID
and PR number "belongs to the security trust design", left
`repo_numeric_id` absent, and specified that adding it "is a
`provenance_version` bump". **factory-director has now ruled the
requirement binding.** This document is that version bump; v1.0.0's own
mechanism is being followed, not overridden.

The ruling, applied:

- The DSSE attestation MUST bind the immutable numeric GitHub repository
  ID and the PR number, alongside trust-root commit, subject SHA, final
  candidate SHA, policy version, artifact hashes, run/task IDs, and
  authenticated profiles/outcomes.
- These MUST NOT be parsed from comments, summary, or worker metadata.

### §A.1 The nullable-vs-required rule

v1.0.0 §3.3 rejected persisting a PR number because "a PR may not exist
when the run ends; requiring it would force either a nullable field
everyone treats as optional, or a write after terminalization". That
objection (originally platform-engineer's) is correct and is **not**
discarded. It is resolved by making the requirement *conditional on
export finalization* rather than on run terminalization.

Two columns and one flag:

```sql
repo_github_id   INTEGER,  -- immutable numeric GitHub repository id
event_locator    TEXT,     -- 'pr:<number>' for PR gates; NULL otherwise
export_finalized INTEGER NOT NULL DEFAULT 0
```

- **General local Kanban runs** (`export_finalized = 0`):
  `repo_github_id` and `event_locator` MAY be NULL. Most factory work is
  not a PR gate — research spikes, docs cards, scratch analysis — and
  must not be forced to invent repository identity it does not have.
  Such a run is simply never exported. This is the common case.
- **Runs finalized for broker export** (`export_finalized = 1`): ALL of
  `repo_github_id`, `event_locator` (the PR number for PR gates),
  `subject_sha`, `verified_head_sha`, and every artifact `sha256` are
  mandatory and non-NULL. Any missing, malformed, or ambiguous value
  means the record is **not finalized and not exported**. Fail closed,
  never fail open.

Finalization is a **distinct, later step** than terminalization, run by
the operator/exporter once the PR actually exists. This is what
dissolves the original objection: nothing is written to a terminal run,
and no field is nullable-but-secretly-required. A run that terminates
before its PR exists is simply not yet finalized.

Because v1.0.0 §5 makes terminal rows immutable, finalization is
implemented as an **insert** of a finalization record keyed to the run,
not an UPDATE of the run row. A record that never finalizes is inert,
which is the safe default.

### §A.2 Provenance of the two new fields

Neither is worker-writable, and neither is parsed from prose.

- `repo_github_id` — resolved by the kernel from the workspace `origin`
  remote via an authenticated GitHub API lookup, cached per repository.
  If the lookup fails or is ambiguous the field stays NULL and the run
  is not finalizable. It is **never** derived from the remote URL
  string, because a remote URL is renameable and therefore not immutable
  identity; comment 573 requires the immutable numeric id specifically.
  This preserves v1.0.0 §3.3's "branch name is never an authority"
  principle and extends it to remote strings.
- `event_locator` — supplied at finalization. It remains a **locator,
  not authority**, exactly as v1.0.0 §3.3 framed repository identity.
  The broker still independently resolves and validates it against
  GitHub and still requires exact PR-head equality (comment 573 §4.1).

### §A.3 What did NOT change

`final_candidate_sha` is still **not stored** on the run. v1.0.0 §4's
reasoning is untouched and correct: it is knowable only after assembly,
i.e. after the run is terminal, and storing it would require the
mutation §5 prohibits. The director's ruling lists it among the
attestation's mandatory fields — that is a requirement on the **DSSE
attestation the broker assembles**, not on the Kanban record. Kanban
supplies the two SHAs it can honestly witness (`subject_sha` at claim,
and the head it actually verified); the broker derives the third.

A Kanban column named `final_sha` would invite exactly the false
equivalence this contract exists to prevent. Acceptance test A17 below
guards against a future implementer helpfully adding one.

---

## §B — Immutability needs an enforcement mechanism (corrects v1.0.0 §5, A6)

**This is a defect in v1.0.0, found by reading the live source after the
document was frozen.** v1.0.0 §5 asserts "once `status` is terminal and
`ended_at` is set, no field on that row may be UPDATEd", and test A6
expects "UPDATE any field on a terminal run → rejected". v1.0.0 names no
mechanism that would reject it, and in the current kernel nothing does.

Source anchors, inspected at commit `fbb4454ed` in
`/Users/dreddy/.hermes/hermes-agent`:

- `edit_completed_task_result` (`hermes_cli/kanban_db.py:6181-6243`)
  UPDATEs `task_runs.summary` at `:6220-6223` and `task_runs.metadata`
  at `:6224-6228` on a run whose `outcome` is already `'completed'`.
  A6 fails today.
- `_end_run` (`:4350-4374`) UPDATEs the run row at terminalization, and
  the reclaim path in `claim_task` (`:4662-4677`) UPDATEs it again.

So `task_runs` is a legitimately-mutable table with at least three live
UPDATE paths, one of which fires *after* completion. Placing an
immutability guarantee on that table is unenforceable by assertion.

**Correction.** The immutable terminal provenance record MUST live in
its own append-only table protected by SQLite triggers, rather than
relying on a documented prohibition against updating `task_runs`:

```sql
CREATE TRIGGER trg_run_provenance_no_update BEFORE UPDATE ON run_provenance
BEGIN SELECT RAISE(ABORT, 'run_provenance is append-only'); END;
CREATE TRIGGER trg_run_provenance_no_delete BEFORE DELETE ON run_provenance
BEGIN SELECT RAISE(ABORT, 'run_provenance is append-only'); END;
```

Artifact rows carry a `sealed` flag flipped by the kernel at
terminalization, with a `BEFORE UPDATE ... WHEN OLD.sealed = 1` trigger.
Rows stay editable while the run is live (so a worker may re-declare
before terminalizing) and become immutable the moment the run closes.

v1.0.0's `corrects_run_id` append-only correction model (§5) is
**unchanged and still correct**; this only adds the mechanism that makes
"no UPDATE" true rather than merely stated.

**Residual risk, stated plainly:** a local actor with write access to
`kanban.db` can drop a trigger. SQLite has no in-database privilege
separation. Triggers raise the cost of tampering and make casual or
programmatic mutation fail loudly; they are not a defense against root
on the box. Comment 573 already places canonical dispatcher records
inside the trust boundary, so this does not widen it — and the broker's
independent recomputation of every artifact digest from the git object
(573 §4) is what actually catches a doctored record. **This specific
tradeoff needs security-reviewer sign-off** (§D).

### §B.1 Export cursor

A consequence worth stating, because it is easy to get wrong: the export
watermark must be monotonic in **terminalization order**, which
`task_runs.id` is not — runs terminate out of creation order. The
append-only provenance table's own AUTOINCREMENT `seq` provides this
correctly. Verified empirically (§C): runs 99 and 50 terminating in that
order receive ascending `seq` 5 and 6.

This refines v1.0.0 §6's "highest exported `task_events.id`", which is
directionally right but keys off a table that also receives unrelated
events. Per research-scout, the cursor is **local-exporter-only** and is
never exposed as a broker-facing pull surface: canonical Kanban is
local-only and the broker must never reach into it.

---

## §C — Executable verification performed

`verify_adr0007_mechanisms.py` (attached) exercises these mechanisms
against real SQLite and real git, so the claims above are demonstrated
rather than asserted:

```
python3 verify_adr0007_mechanisms.py     ->  36/36 checks passed
```

Proven, in order of the assertions in this document:

- A row is written for **every** terminal outcome (completed, blocked,
  crashed) — absence of a record can never itself be read as evidence;
  only completed+SHAs+artifacts is `attestable=1`.
- UPDATE and DELETE on the provenance table abort with
  `sqlite3.IntegrityError: run_provenance is append-only`, and the
  record survives the tamper attempt byte-identical (§B).
- Sealed artifact rows reject writes; unsealed rows remain editable.
- Duplicate `run_id` rejected; duplicate `(run_id, path)` rejected.
- `seq` strictly ascending and in a different order from `run_id`,
  proving the cursor tracks terminalization (§B.1).
- Re-export from a watermark yields nothing; export digests unique.
- A `mode=ro` connection raises `attempt to write a readonly database`,
  confirming v1.0.0 §6's read-only exporter requirement is achievable.
- Abbreviated (`a1b2c3d`) and uppercase SHAs rejected; 40-hex required.
- Artifact digest is stable under input ordering and changes when any
  hash changes.
- A real file digest differs from a worker-claimed one — i.e. the kernel
  must compute, never accept, the hash.
- `git rev-parse` yields full 40-hex for HEAD and for tracked blobs; a
  non-git scratch dir yields nothing, so scratch runs are structurally
  non-attestable.
- **§A specifically:** a complete gated run finalizes; a non-gated run
  with NULL repo fields is legal but never finalizes; finalization fails
  closed *naming the offending field* for each of the five mandatory
  fields individually; a remote-string repo id, a malformed event
  locator, an abbreviated SHA, and an unresolved correction chain are
  each refused; and no `final_candidate_sha`/`final_sha` column exists.

This is a mechanism probe against a model of the schema. It is **not** a
substitute for the acceptance tests, which must run against the real
kernel and belong to the implementer.

### §C.1 Additional acceptance tests (extend v1.0.0 §10)

v1.0.0 A1–A12 stand. Add:

| # | Test | Expected |
|---|---|---|
| A13 | Non-gated run with NULL `repo_github_id`/`event_locator` | completes normally, `export_finalized=0`, never exported |
| A14 | Finalization attempted with any one mandatory field missing | refused, fails closed, offending field named |
| A15 | `origin` remote renamed | `repo_github_id` unchanged; remote string never accepted as identity |
| A16 | Finalization performed | original provenance row byte-identical, digest unchanged |
| A17 | Schema inspected for `final_candidate_sha` / `final_sha` | absent (guards §A.3) |
| A18 | `edit_completed_task_result` called on a completed run | `summary`/`metadata` may change; provenance record digest **unchanged** |
| A19 | Direct UPDATE/DELETE on provenance table | raises `sqlite3.IntegrityError` |
| A20 | Runs terminating out of `run_id` order | export `seq` still ascending |

A18 is the regression test for the §B defect specifically: the existing
post-completion mutation path must remain functional for its own purpose
while being provably unable to touch provenance.

---

## §D — Open items owned by others

- **security-reviewer** (required next gate, has NOT occurred): sign-off
  on §B's trigger-based immutability and its stated residual risk;
  whether board slug is acceptable in the export payload or must be an
  opaque id; sign-off on §A.1's nullable-vs-required split.
- **platform-engineer**: implementation.
- **broker / research-scout**: `final_candidate_sha` derivation stays
  broker-side, out of scope here.

Resolved, no longer open: repository id and PR number (§A, director
ruling); DSSE source reference (attachment id 3, verified above).

## §E — Limitations of this document

1. **Not security-approved.** v1.0.0 §11's warning stands unchanged.
2. §C is a probe against a schema model, not the live kernel. The
   §C.1/§10 tests must be run against the real kernel before
   implementation is considered verified.
3. `repo_github_id` resolution assumes an authenticated GitHub API path
   exists in the kernel for that lookup. I did not verify one exists;
   if it does not, that is implementation work platform-engineer must
   scope, and until then no run is finalizable.
4. The §B residual risk (trigger-droppable by a local root actor) is
   accepted-by-default here and explicitly routed to security-reviewer.
5. I did not modify the Hermes repository, the live Kanban DB, or any
   profile, per card scope.
