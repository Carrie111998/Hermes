# Controlled Workflow v1 pilot runner

`scripts/kanban_workflow_pilot.py` validates and prepares one disposable
Workflow v1 Phase 1/2 campaign. It is intentionally **not** a generic Kanban
dispatcher: preparation creates isolated Phase 1 worktrees and protected task
rows while both durable launch gates remain off.

```bash
python scripts/kanban_workflow_pilot.py \
  --runner-source-tree <independently-reviewed-clean-HEAD-tree> \
  validate /path/to/pilot.json
python scripts/kanban_workflow_pilot.py \
  --runner-source-tree <independently-reviewed-clean-HEAD-tree> \
  prepare /path/to/pilot.json
```

Both actions fail closed unless the runner repository is clean and its current
`HEAD^{tree}` exactly matches the immutable tree supplied on the command line.
This prevents a reviewed runner from being silently replaced or locally patched
between approval and the live preparation step.

`prepare` is idempotent only for the exact same permit, manifest, pin, branch,
worktree, immutable leaf identities, and safe manifest board slug. The
`HERMES_KANBAN_BOARD` environment must exactly equal that manifest board before
the database is touched. Drift fails closed. It never enables
dispatch, launches a worker, pushes, opens a PR, mutates a GitHub Project,
merges, releases, or deploys.

Preparation is a controller mutation and therefore fails closed in delegated
child contexts. Run it only from the authorized controller or scheduler
context that owns the selected isolated board.

## Manifest contract

```json
{
  "schema": "hermes.workflow-pilot.v1",
  "campaign": {
    "repository": "owner/repository",
    "issue": "257",
    "board": "workflow-v1-pilot"
  },
  "source": {
    "path": "/absolute/path/to/repository",
    "pin_sha": "FULL_40_CHARACTER_COMMIT_SHA",
    "worktree_root": "/absolute/path/to/dedicated-pilot-worktrees"
  },
  "controls": {
    "concurrency": 2,
    "permit": "owner-approved-disposable-pilot-id"
  },
  "leaves": [
    {
      "id": "alpha",
      "version": 1,
      "phase": 1,
      "branch": "pilot/alpha-v1",
      "worktree": "alpha-v1",
      "objective": "Produce the bounded Alpha artifact.",
      "allowed_paths": [".workflow-pilot/alpha.json"],
      "relevant_files": ["AGENTS.md"],
      "symbols": ["Rule 0"],
      "acceptance_checks": ["git diff --check PIN...HEAD"]
    },
    {
      "id": "beta",
      "version": 1,
      "phase": 1,
      "branch": "pilot/beta-v1",
      "worktree": "beta-v1",
      "objective": "Produce the bounded Beta artifact.",
      "allowed_paths": [".workflow-pilot/beta.json"],
      "relevant_files": ["AGENTS.md"],
      "symbols": ["Rule 0"],
      "acceptance_checks": ["git diff --check PIN...HEAD"]
    },
    {
      "id": "dependent",
      "version": 1,
      "phase": 2,
      "dispatchable": false,
      "objective": "Remain blocked until Alpha and Beta are verified.",
      "allowed_paths": [".workflow-pilot/dependent.json"],
      "relevant_files": ["AGENTS.md"],
      "symbols": ["Rule 0"],
      "acceptance_checks": ["git diff --check PIN...HEAD"],
      "depends_on": ["alpha/v1", "beta/v1"]
    }
  ]
}
```

The initial Phase 2 leaf must be non-dispatchable and depend on both—and only—the
two Phase 1 leaves. It is registered before Phase 1 completes so readiness
proves the fan-in dependency block. After both Phase 1 artifacts are independently
verified, the controller
must use the existing supersession API, construct a newly pinned v2 manifest
and capsule, and prepare a fresh worktree; it must never edit v1 in place. The
v2 pin must retain every same-repository Phase 1 candidate as a Git ancestor
(for example, through a controller-owned local integration merge). A stale
base or a content-equivalent cherry-pick does not satisfy that coordinate
fence.

`allowed_paths` is the writable evidence boundary. `relevant_files` is bounded,
pin-verified, read-only context and may be outside that boundary. This permits a
new-file-only leaf without granting write access to the source files used to
construct its capsule.

Pilot write boundaries are exact paths rather than globs, and every disposable
branch must live under `pilot/`. Registration persists that exact manifest branch
and readiness re-reads the worktree's symbolic branch; a detached HEAD or any
clean same-SHA branch switch fails with `branch_mismatch` before reservation or
launch. The worktree root is an explicit absolute
directory distinct from the pinned source worktree; it may be a controlled
sibling directory.
