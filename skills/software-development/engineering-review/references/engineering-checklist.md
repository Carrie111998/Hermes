# Hermes Engineering Checklist

Use this checklist to audit the full Hermes project. Each category should produce
positive findings, risks, unknowns, and a score. Start with the evidence
collector output, then add targeted file reads and commands only where they
materially improve confidence.

## 1. Architecture

Evidence targets:

- `run_agent.py`
- `model_tools.py`
- `toolsets.py`
- `agent/`
- `tools/registry.py`
- `hermes_cli/`
- Architecture docs under `website/docs/`

Checks:

- Clear subsystem boundaries
- Agent loop responsibilities are not overloaded
- Tool registry and toolsets are coherent
- CLI, gateway, cron, and tools share conventions without hidden coupling
- Extension points are documented

## 2. Source Code

Evidence targets:

- Core Python packages
- Tests under `tests/`
- Static analysis/build scripts

Checks:

- Tests cover core runtime and regression-prone subsystems
- Error handling is explicit around I/O, network, subprocesses, and model calls
- Code avoids duplicate implementations of the same concept
- Platform-specific behavior is isolated and tested
- Security-sensitive code has targeted tests

## 3. Configuration

Evidence targets:

- `hermes_cli/config.py`
- Config docs
- Environment variable docs
- Setup/auth code

Checks:

- Defaults are safe
- Secrets are separated from config
- Config schema is discoverable
- Profile-aware paths use `get_hermes_home()` or equivalent
- Config changes document restart/session implications

## 4. Runtime

Evidence targets:

- CLI health/status commands
- Runtime logs (redacted)
- Agent loop source
- Error handling and retry code

Checks:

- Runtime state is queryable
- Health checks distinguish degraded vs healthy states
- Long-running tasks have cancellation/timeouts
- Background process lifecycle is visible
- Failures produce actionable diagnostics

## 5. Prompt System

Evidence targets:

- `agent/prompt_builder.py`
- Project context loading code
- Skills loading code
- Tests for prompt construction

Checks:

- Prompt responsibilities are modular
- Context injection has clear precedence
- Prompt caching is not broken unnecessarily
- Tool-use and safety rules are not duplicated inconsistently
- Project/profile/session context boundaries are respected

## 6. Profiles

Evidence targets:

- Profile CLI code
- Profile docs
- Gateway/profile configuration handling

Checks:

- Profiles isolate config, skills, plugins, cron, sessions, and memory
- Gateway platform ownership conflicts are handled or documented
- Profile create/clone/import/export behavior is tested
- Cross-profile writes are guarded

## 7. Skills

Evidence targets:

- `skills/`
- `tools/skill_manager_tool.py`
- `agent/skill_utils.py`
- Skills docs and catalog generator

Checks:

- Skill frontmatter validation is enforced
- Skills are discoverable and versioned
- Supporting files follow progressive disclosure
- Bundled vs local skill behavior is documented
- Skill mutation has safety guards

## 8. Memory

Evidence targets:

- Memory provider code
- Session search code
- Honcho/Mnemosyne integration if present
- Privacy/redaction controls

Checks:

- Memory writes are intentional and scoped
- User/profile/session memory boundaries are clear
- Retrieval has provenance or confidence when needed
- Sensitive data is not stored accidentally
- Memory failure degrades gracefully

## 9. AI-Vault / Knowledge Pipeline

Evidence targets:

- Note-taking skills
- AI-Vault operations skills
- Import/extraction scripts
- Repository governance docs if present

Checks:

- Markdown/frontmatter/link validation exists where writes happen
- Import pipeline favors knowledge extraction over raw archiving
- AI-Vault path assumptions are configurable
- Quartz/build compatibility is checked when relevant
- Write operations produce reports

## 10. Gateway

Evidence targets:

- `gateway/`
- `plugins/platforms/`
- Messaging docs
- Gateway tests

Checks:

- Platform adapters share a safe base contract
- Delivery failures are observable
- Authorization and pairing are documented/tested
- Message role alternation is preserved
- Platform-specific capabilities do not leak across profiles

## 11. Scheduler / Cron

Evidence targets:

- `cron/`
- Cron CLI and docs
- Scheduler tests

Checks:

- Jobs have durable state and locking
- Duplicate ticks are prevented
- Delivery targets are explicit
- Failures, retries, and timeouts are documented
- No-agent scripts are isolated from agent runs

## 12. MCP

Evidence targets:

- MCP CLI/subcommands
- MCP client/server code
- MCP docs

Checks:

- Server configuration is explicit and testable
- Tool discovery handles failures safely
- Per-server tool selection is understandable
- Remote tool trust boundaries are documented
- MCP reload/test flows are reliable

## 13. Docker / Deployment

Evidence targets:

- Dockerfile / compose files
- Install scripts
- Deployment docs
- CI configuration

Checks:

- Images avoid embedding secrets
- Runtime volumes and paths are documented
- Upgrade path preserves user data
- Health checks exist when services are containerized
- Platform differences are documented

## 14. Documentation

Evidence targets:

- `website/docs/`
- README / developer docs
- Generated skill docs
- CLI help text

Checks:

- Docs match current CLI/source behavior
- Generated docs can be regenerated
- User-facing docs separate concepts from commands
- Developer docs identify extension points
- Known pitfalls and platform quirks are documented

## Category Output Mini-Template

```markdown
### <Category>

Score: <0-100 or Unknown>
Status: PASS / REVIEW / BLOCK / UNKNOWN
Evidence checked:
- <path/command/test>

Positive controls:
- <positive finding>

Findings:
- <finding IDs>

Unknowns:
- <manual-review gaps>
```
