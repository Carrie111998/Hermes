# Skill Steward: research-backed implementation plan

Date: 2026-08-29 UTC

## Problem

Hermes currently mixes three different concerns:

1. Routing: find the smallest set of relevant skills for a task.
2. Stewardship: learn whether surfaced skills helped and improve weak ones.
3. Curation: merge or archive skills safely.

Usage counters cannot answer whether a skill helped. A skill with no recorded use may be undiscoverable rather than useless. A fixed archive quota rewards deletion rather than quality.

## Evidence reviewed

- Hermes Agent current `main`, contributor doctrine, curator, skill tools, usage sidecar, prompt builder, plugin system, tests, and CI.
- Existing upstream work:
  - PR #93591: deterministic BM25 skill routing, green CI.
  - PR #84420: local outcome telemetry, utility scoring, retrieval and review proposals, but currently conflicting and too broad.
  - PR #97609: curator safety work, currently blocked.
  - Issue #17649: on-demand skill retrieval instead of broadcasting the catalog.
- Industry and research patterns from Anthropic tool-design guidance, OpenAI agent tooling, LangGraph/LangChain, Semantic Kernel/AutoGen, Google ADK, MCP routing discussions, Tool-to-Agent Retrieval, and large-tool-catalog routing research.
- Practitioner reports from GitHub, X, and Reddit about tool overload, hierarchical retrieval, and missed capabilities.

## Findings

1. Progressive disclosure beats broadcasting every capability. Rank locally, return compact cards, then load exact instructions.
2. Hybrid lexical retrieval is the safe first layer. It is deterministic, offline, cheap, and easy to test. Semantic retrieval can be added later behind the same contract.
3. Do not mutate the system prompt or toolset during a conversation. Skill routing must use existing tools and preserve prompt-cache bytes.
4. Separate observation from automation. Local outcome data can guide ranking and proposals, but it must not silently rewrite or delete skills.
5. Unknown is not failure. No usage and unknown outcomes are discovery gaps, not archive evidence.
6. Curator actions need evidence and reversible state. Remove archive quotas. Keep human review for consolidation and low-confidence changes.
7. Telemetry stays local. No third-party analytics, personal identifiers, or raw task text are required.
8. Contributor credit must survive integration. Build on existing commits rather than replacing them without attribution.

## Target design

### Routing lane

Extend `skills_list` with an optional natural-language query. It returns a bounded ranked list of compact skill cards. `skill_view` remains the exact loader. Ranking is deterministic and local. Outcome reporting extends the existing skills toolset rather than adding a new standalone toolset; the tool schema stays fixed for the conversation.

### Stewardship lane

Record a bounded local outcome for a loaded skill: success, failure, or unknown. Keep only the task identifier and coarse error type. Do not store task text. Use a confidence floor before global per-skill utility affects ranking. Low-utility skills produce review proposals, never automatic edits.

This PR establishes routing, local outcomes, review proposals, dashboard visibility, and archive safety. It does not yet ship the full opportunity→surface→follow funnel or automatic service loop from the design; those remain follow-up work after the event contract proves useful.

### Curation lane

Automatic inactivity transitions may mark skills stale. They must not archive a skill without known outcome evidence. Consolidation remains a separate opt-in background pass, has no minimum archive quota, preserves unique content and support files, and remains reversible.

## Phases and dependencies

### Phase 1: baseline and contracts

- Rebase on current `main`.
- Confirm overlapping PR state and preserve authorship.
- Define privacy, cache, compatibility, and rollback invariants.

Dependency: none.

### Phase 2: deterministic retrieval

- Integrate BM25 routing through `skills_list(query=...)`.
- Keep normal catalog output unchanged when no query is supplied.
- Cover zero-score tails, category filters, plugin skills, limits, and malformed input.

Dependency: Phase 1.

### Phase 3: local outcomes

- Add bounded success/failure/unknown records.
- Add confidence-gated utility scoring.
- Feed utility into retrieval only after enough samples.
- Keep failures best-effort so telemetry never breaks task execution.

Dependency: Phase 2 contracts; implementation can run in parallel with Phase 2 after interfaces freeze.

### Phase 4: evidence-gated curator

- Treat no outcomes as insufficient evidence for archive.
- Remove fixed archive-count pressure from the curator prompt.
- Keep archive reversible and consolidation opt-in.
- Test never-tried, unknown-only, successful, failed, pinned, cron-referenced, bundled, and external skills.

Dependency: Phase 3 record shape.

### Phase 5: QA and review

Parallel lanes:

- Routing tests and benchmark replay.
- Outcome storage, concurrency, privacy, and corruption tests.
- Curator lifecycle and archive sabotage tests.
- Prompt-cache and tool-schema stability checks.
- Independent architecture, security/privacy, and contributor-credit review.

Dependency: Phases 2 to 4.

### Phase 6: ship

- Run the focused one-minute gate.
- Run repository lint and affected test suites.
- Push the branch and open one draft PR that clearly credits and supersedes overlapping work.
- Read back base, head SHA, files, body, and live checks.

Dependency: Phase 5 green.

## One-minute local gate

Run the focused Python suites for skill routing, skill outcomes, skill reflection, skill APIs, and curator activity. Run Ruff on changed Python files. Run `git diff --check`. The gate must stay deterministic and use temporary Hermes homes only.

## Full CI

- Python tests and Ruff.
- macOS and Windows path checks.
- E2E test that routes, loads, records an outcome, then runs a curator transition in a temporary home.
- Benchmark replay with fixed fixtures and relationship-based thresholds, not frozen implementation values.
- Security checks proving no raw task text or external network call is made.
- Contributor-attribution check.

## Rollout

1. Merge retrieval first or keep it as the base commits in one draft integration PR.
2. Ship outcomes as local and best-effort.
3. Keep utility re-ranking confidence-gated.
4. Keep reflection proposals and consolidation review-only.
5. Measure missed-route rate, top-k acceptance, outcome coverage, false archive attempts, latency, and index rebuild time.

## Explicitly not in this PR

- Automatic pre-task loading of a ranked skill.
- The full opportunity→surface→load→follow→completed event stream.
- Automatic readiness testing or mutation of underused skills.
- Task-class-specific utility. Outcome storage deliberately omits raw task text, so utility is global per skill.
- Automatic bundle creation.

## Definition of done

- Query routing returns relevant compact candidates without changing the no-query contract.
- Prompt and tool schemas remain stable within a conversation.
- Outcomes are local, bounded, privacy-safe, and cannot break task execution.
- Unknown or absent evidence cannot trigger automatic archive.
- Curator has no archive quota and every destructive transition is recoverable.
- Focused tests, lint, diff checks, and live CI pass.
- One reviewable PR exists with preserved contributor credit, verified remote SHA, exact test evidence, risks, exclusions, and overlap notes.
