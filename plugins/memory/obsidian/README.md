# Obsidian memory provider

The Obsidian provider treats a markdown vault as the source of truth and keeps
a disposable SQLite FTS5 index under the active `HERMES_HOME`.

Enable it in `config.yaml`:

```yaml
memory:
  provider: obsidian

plugins:
  obsidian:
    vault_path: /srv/dj/obsidian
    top_k: 5
    sync_interval_minutes: 5
    exclude_dirs: [".git", ".obsidian", ".trash"]
    pinned: ["memory/daniel.md"]
```

The provider performs an incremental background re-sync, loads pinned notes
once when the session system prompt is built, and exposes
`obsidian_remember` for explicit write-back under `<vault>/hermes/`. Write-back
redacts recognized secrets, refuses paths ignored by the vault repository, and
updates the search index immediately.

The legacy `/root/.hermes/scripts/obsidian_memory_sync.py` cron workflow is
superseded by this provider. Do not schedule that script alongside the
provider: it mirrors vault content into built-in memory and creates two
competing sources of truth. The old script may remain on disk for rollback,
but its cron job should be removed.
