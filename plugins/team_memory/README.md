# Team Memory Plugin

Stage 1 is a reviewed, read-only-for-agents shared store. It does not replace
Hermes' active personal memory provider and it does not modify `SOUL.md`,
`MEMORY.md`, or `USER.md`.

## Enable one profile

The plugin is opt-in:

```yaml
plugins:
  enabled:
    - team-memory
team_memory:
  enabled: true
  workspace_id: xinxiang
  project_id: xinxiang-app
  database_path: /Users/xinxin/.hermes/team-memory/xinxiang.db
  agent_variant: enhanced
```

`database_path` is the explicit sharing boundary. Set the same absolute path
and `workspace_id` in the Frontend, Backend, and DevOps profiles to share one
workspace. Omitting it keeps the legacy profile-local path under
`$HERMES_HOME/plugins/shared_memory.db`.

Initialize and seed entries through the operator CLI:

```bash
hermes plugins enable team-memory
hermes team-memory init --workspace xinxiang
hermes team-memory add --workspace xinxiang --category api_contract \
  --title 'Users API' --content 'POST /users requires name and email.' \
  --author operator --tags api,users
```

The agent only receives `team_memory_search`, and only after the feature flag
and explicit workspace are valid. Writes stay behind the CLI so a bad model
turn cannot silently publish a decision.

Entries may have an ISO-8601 `valid_until`; expired entries are excluded from
Agent search. Operators can inspect them with
`hermes team-memory list --workspace xinxiang --include-expired` before
retiring or replacing a contract.

## Rollback

For a running process, start a new Hermes process after disabling the flag;
the system prompt and tool snapshot are intentionally stable for a live
conversation:

```yaml
team_memory:
  enabled: false
```

To remove only the Stage 1 files, use the explicit confirmation gate:

```bash
hermes team-memory uninstall --yes
```

Existing personal memory, session search, profiles, and agent Markdown files
are untouched.
