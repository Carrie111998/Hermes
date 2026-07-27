# cleanup-operator — Destructive DB Operations Workflow

Issue: https://github.com/queeph/trading-bot/issues/139 (Acceptance Criterion B)
Pipeline position: `builder -> tester -> cleanup-operator -> release-ci`

## Purpose

Destructive operations on the live trading database (bulk UPDATE/DELETE,
schema rewrites, `--apply`, `--apply-confirm`) are gated behind a
mandatory operator-confirmation step. The gate is enforced at TWO levels
so a single bypass is impossible:

1. **Dispatcher-level gate** — `hermes_cli/kanban_db.py::dispatch_once`
   refuses to spawn a worker for any `cleanup-operator` task whose body
   lacks an `apply_confirm_token_prefix:` field. The check runs before
   the profile-exists filter and before any worker subprocess is launched,
   so a missing/short token is the first signal the operator sees. The
   rejected task is auto-blocked with a `dispatch_rejected` event whose
   payload names the missing-field reason.
2. **Worker-level gate** — the `kanban-cleanup-operator` skill
   (`skills/devops/kanban-cleanup-operator/SKILL.md`) enforces a
   three-step sequence (dry-run -> operator Telegram reply -> --apply-confirm)
   inside the worker. The skill never runs `--apply` without an
   operator-supplied confirmation in the previous 5 minutes.

Together: even if a builder/tester task is somehow assigned to
`cleanup-operator` without the body field, it never spawns. Even if a
worker is somehow spawned without the operator's Telegram reply, the
worker's own skill refuses to run the destructive step.

## Task body contract

A `cleanup-operator` task body MUST contain the
`apply_confirm_token_prefix:` field on its own line, with at least 8
non-whitespace characters. Example:

```markdown
## Goal

Run scripts/cleanup_999010_999011.py against the live trading DB to
mark run_ids 999010 (BTC) and 999011 (SOL) as phantom_paper.

apply_confirm_token_prefix: deadbeef
```

The dispatcher will reject anything shorter than 8 chars or missing
entirely. A full example body:

```markdown
## Goal

Mark two phantom-paper positions in the live DB.

## Recon

scripts/cleanup_999010_999011.py exists, dry-run is green, parent
task t_eb666528 already merged via PR #101.

## Apply target

host: prod-trading-db
db: trading.db

apply_confirm_token_prefix: deadbeef
```

## Creating a cleanup-operator task

```python
kanban_create(
    title="Mark 999010 + 999011 as phantom_paper (live apply)",
    assignee="cleanup-operator",
    body="""
## Goal

Run scripts/cleanup_999010_999011.py against the live trading DB.

apply_confirm_token_prefix: deadbeef

""",
    workspace="worktree:/home/srv/trading-bot",
    priority=5,
)
```

The token is the full token the operator generated with the cleanup
script's token-issuing command (`scripts/cleanup_*.py issue-token` or
similar). The body holds the **prefix** (first 8 chars) so the
dispatcher can validate it; the worker reads the full token from a
file the operator provides via `APPLY_CONFIRM_TOKEN_FILE`.

## Telegram confirmation pattern

The cleanup-operator skill sends the dry-run summary to the operator's
Telegram chat (the chat id is configured in
`~/.hermes/profiles/cleanup-operator/config.yaml` under
`gateway.notifier.routes`). The operator replies with one of:

- `<prefix> confirm` — authorise the apply. The worker proceeds.
- `<prefix> cancel` — abort. The worker exits 130, the task lands in
  `blocked` with reason "operator cancelled apply".
- anything else — the worker logs the message and keeps waiting.
  Free-form chat is NEVER interpreted as a confirmation.

Default timeout: 5 minutes. Override with
`APPLY_CONFIRM_TIMEOUT_SECONDS` env var on the worker host. On
timeout the worker exits 77 and the task lands in `blocked` with
reason "no operator reply within timeout window".

## Why a separate profile (and not just a flag)?

The `cleanup-operator` profile exists so the workflow gate cannot be
bypassed by a builder/tester that "just runs --apply directly". With a
profile, the dispatcher can refuse to spawn the worker for tasks that
lack the body field — a builder cannot accidentally produce a task
that bypasses the gate, because the gate is the dispatch step itself.

Issue #139-C (a follow-up) will harden builder/tester profiles to
**refuse** the `--apply` flag in their own skills, so even if a
builder task somehow has a cleanup script, the builder skill will not
run it. Until that lands, the dispatcher gate is the only enforcement
point, and the cleanup-operator skill is the only place the
operator-confirm sequence lives.

## Testing the gate

```bash
# Should auto-block: missing token prefix
hermes kanban create "dry-run test (no token)" \
  --assignee cleanup-operator \
  --body "## Goal
Just a test.

" \
  --workspace scratch

# Should spawn: valid token prefix
hermes kanban create "dry-run test (with token)" \
  --assignee cleanup-operator \
  --body "## Goal
Just a test.

apply_confirm_token_prefix: deadbeef

" \
  --workspace scratch
```

The first task lands in `blocked` immediately (next dispatch tick).
The second task spawns a worker normally. Both are observable in
`hermes kanban show <task-id>` — the first has a `dispatch_rejected`
event in its timeline; the second transitions to `running`.

The dispatcher-side test suite
(`tests/hermes_cli/test_kanban_cleanup_operator.py`) covers all
seven canonical paths:

- `test_dispatch_refuses_missing_token_prefix` — no field → auto-blocked + dispatch_rejected event.
- `test_dispatch_accepts_with_token_prefix` — valid field → normal spawn.
- `test_worker_dry_run_emits_token` — the worker's dry-run step produces a fresh 32-hex-char token; the prefix embedded in the body satisfies the gate.
- `test_worker_confirms_via_telegram` (mocked) — operator reply `<prefix> confirm` triggers the apply step.
- `test_worker_aborts_on_token_mismatch` (exit 77) — the worker refuses to apply when the on-disk token doesn't match the body prefix.
- `test_worker_aborts_on_operator_cancel` (exit 130) — operator reply `<prefix> cancel` aborts.
- `test_token_format_validation` (8 hex chars prefix) — boundary cases for the dispatcher-side field check (empty body, missing field, empty value, 7-char prefix, 8-char prefix, 32-char prefix, = separator, embedded in long body, comment lines).

## What the operator does (concrete recipe)

```bash
# 1. Generate a token (the script's CLI; varies per script)
python3 scripts/cleanup_999010_999011.py issue-token > /tmp/cleanup.token
PREFIX=$(cut -c1-8 /tmp/cleanup.token)
echo "Token prefix: $PREFIX"

# 2. Create the kanban task with the prefix in the body
hermes kanban create "Apply 999010+999011 phantom_paper" \
  --assignee cleanup-operator \
  --body "## Goal

Apply phantom_paper.

apply_confirm_token_prefix: $PREFIX

" \
  --workspace "worktree:/home/srv/trading-bot"

# 3. Watch for the Telegram message from the worker. Reply:
#     "<prefix> confirm"   -> authorise the apply
#     "<prefix> cancel"    -> abort
```

## Profile setup (live config — separate from this repo)

The `cleanup-operator` profile is **not** versioned in the
hermes-agent repo. It is live configuration under
`~/.hermes/profiles/cleanup-operator/`. The full setup recipe
(YAML, dirs, curator command) lives in
`skills/devops/kanban-cleanup-operator/SKILL.md` under "Profile setup
(live config — not a repo change)".

## References

- Issue: https://github.com/queeph/trading-bot/issues/139
- Issue #139-A PR #140 (merged): --apply-confirm=<token> mechanism
- Skill: `skills/devops/kanban-cleanup-operator/SKILL.md`
- Dispatcher gate: `hermes_cli/kanban_db.py::_validate_cleanup_operator_task`
- Test: `tests/hermes_cli/test_kanban_cleanup_operator.py`
- Iron-Law #14: DB-Write-vor-Review verboten
