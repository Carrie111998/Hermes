# Engineering Review Tool Permissions

Engineering Review is read-only by default.

## Allowed Without Additional Approval

| Tool/action | Purpose |
|---|---|
| `read_file` | Inspect source, docs, configs, templates, and skills |
| `search_files` | Locate subsystems, patterns, docs, tests, and references |
| `terminal` read-only commands | `git status`, `git rev-parse`, `python3 --version`, tests, linters, collectors |
| `web_extract` | Check official Hermes docs when local docs are ambiguous |
| `mcp_codebase_memory_*` read tools | Architecture and graph analysis when repo is indexed |
| Evidence collector without `--output` | Collect a JSON snapshot on stdout without writing an artifact |

## Conditionally Allowed

| Action | Condition |
|---|---|
| Run tests/builds | Allowed if they do not publish, deploy, delete, or modify external systems |
| Create report or evidence files | Allowed only when the user asks for an artifact; save under `.hermes/reviews/` or the requested path |
| Read collector-suggested commands | Allowed; suggested commands are recommendations, not automatically executed |
| Read local runtime logs | Allowed if secrets are redacted and paths are relevant |

## Requires Explicit User Authorization

| Action | Reason |
|---|---|
| Modify Hermes source code | Review is not implementation |
| Change config, profiles, skills, cron jobs, or MCP settings | Could affect active runtime |
| Restart gateway/scheduler/services | Operational side effect |
| Commit, push, publish, deploy, or run migrations | External or durable side effect |
| Delete, move, or rewrite AI-Vault notes | Repository governance requires a separate write pipeline |

## Secret Safety

- Do not print `.env`, token files, OAuth files, or credential pools.
- If a config file may contain secrets, inspect only key names with a redacting command or read code that defines the schema.
- Prefer `hermes config` summaries and source schemas over raw secret-bearing files.
- If secret exposure is necessary to diagnose a finding, stop and ask for explicit direction.

## Safe Command Examples

```bash
git status --short --branch
git rev-parse --show-toplevel HEAD
python3 skills/software-development/engineering-review/scripts/collect_hermes_review_evidence.py --repo . --output .hermes/reviews/evidence.json
python3 -m pytest tests/agent tests/tools -q -o 'addopts='
```

## Unsafe Command Examples

```bash
hermes gateway restart
hermes cron remove <id>
git reset --hard
rm -rf ~/.hermes
cat ~/.hermes/.env
```
