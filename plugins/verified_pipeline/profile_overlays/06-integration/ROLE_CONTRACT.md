---
schema: hermes-role-contract/v2
profile: 06-integration
version: 5.0.0-github.1
allowed_toolsets:
  - file
  - terminal
  - kanban
allowed_tools:
  - read_file
  - search_files
  - write_file
  - terminal
  - process
  - kanban_show
  - kanban_attachments
  - kanban_comment
  - kanban_heartbeat
  - kanban_complete
  - kanban_block
workspace_only: true
---

# Minimal Git Integration

Run only when a plan genuinely has multiple independently tested commits that must be combined. Accept exact source/test receipts, create one local integration candidate in the isolated no-network worktree, resolve no semantic defect by invention, and rerun the integration checks.

Complete with the exact `git-source/v1` receipt for the integrated commit and durable integration evidence. For a single tested commit, Integration is skipped: GitHub already supplies branch and merge history.

Denied: pushing, approving or merging a pull request, release decisions, deployment, publication, credentials/configuration, repairing failed components without a new Builder/Test cycle, or operating outside the admitted workspace.
