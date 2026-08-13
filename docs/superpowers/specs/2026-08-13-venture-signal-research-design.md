# Venture Signal Research Skill Design

**Status:** Approved in chat on 2026-08-13

**Authors:** Karl, Codex, and Hermes Agent

**Target:** Hermes Agent bundled research skills

## Summary

Add a bundled `venture-signal-research` skill that gives Hermes Venture Swarm a
repeatable, read-only workflow for market-demand research, competitor discovery,
buyer-language collection, and niche validation. The skill reuses Hermes's
existing retrieval and citation capabilities instead of importing Agent Reach's
installer, credential flows, or generic internet router.

The useful Agent Reach ideas are retained as procedures: ordered source routes,
content-based health checks, bounded fallbacks, and explicit coverage gaps. The
skill remains narrowly triggered and produces a stable evidence contract for the
Scout, Sentinel, and Quant agents.

## Context

Hermes Venture Swarm needs stronger evidence collection before it scores niches,
models margins, or builds an MVP. Agent Reach demonstrates a useful operational
pattern: select an ordered set of retrieval backends, verify that a backend
returns substantive content, and fall back without pretending failed coverage is
negative evidence.

Installing Agent Reach unchanged would duplicate Hermes's native `web_search`,
`web_extract`, browser, skills hub, and grounded-citation behavior. Its broad
trigger would also compete with ordinary Hermes retrieval, while its login and
cookie routes would enlarge the security and maintenance surface. Hermes's own
repository guidance therefore favors a bundled skill until a genuinely new
structured data source requires a provider plugin or MCP server.

## Goals

- Give market-research missions a deterministic source-selection workflow.
- Separate confirmed facts, market signals, interpretation, and coverage gaps.
- Require primary-source evidence for load-bearing claims when available.
- Validate retrieval success by substantive content, not process exit status.
- Bound retries and fallback attempts so research cannot loop indefinitely.
- Produce an evidence matrix that downstream agents can consume consistently.
- Compose with `grounded-citations` rather than implementing another ledger.
- Keep all actions read-only and require no new dependency or configuration.

## Non-Goals

- General web browsing or handling every URL shared with Hermes.
- Installing or wrapping Agent Reach, OpenCLI, browser extensions, or site CLIs.
- Reading browser cookies, automating login, or storing platform credentials.
- Posting, messaging, liking, purchasing, publishing, or outreach.
- Adding a model tool, provider plugin, MCP server, or runtime health daemon.
- Replacing `grounded-citations`, `competitor-news-monitor`, or `blogwatcher`.
- Claiming exhaustive coverage of closed, login-only, or blocked communities.

## Decision

Implement a bundled skill at:

```text
skills/research/venture-signal-research/
├── SKILL.md
└── references/
    ├── evidence-contract.md
    └── source-routing.md
```

Add behavior-contract tests at:

```text
tests/skills/test_venture_signal_research_skill.py
```

No Python runtime code, dependency, configuration key, core tool, or plugin is
introduced in the first version.

## Alternatives Considered

### Tailored Agent Reach fork

This offers the widest platform list but imports a moving external router,
third-party installers, cookie-backed paths, and overlapping web/GitHub search.
It is rejected for the bundled Hermes implementation. Individual source ideas
may be revisited as standalone optional integrations later.

### Skill plus provider adapter

A provider adapter could expose deterministic structured results and probes, but
there is no confirmed missing backend yet. Adding it now would be speculative
infrastructure. The design leaves a clean escalation path: add a service-gated
provider or catalogued MCP server only after a mission demonstrates a source that
Hermes's current tools cannot reach.

### Bundled skill using native tools

This is the selected approach. It adds procedure and contracts at the edge,
preserves prompt-cache and core-tool invariants, and can be validated without
live network tests.

## Trigger Boundary

Use the skill when a request asks Hermes to establish evidence for:

- market demand or demand signals;
- a competitor landscape or positioning comparison;
- buyer language, complaints, desired outcomes, or objections;
- underserved niches or wedge opportunities;
- evidence supporting a Venture Swarm Scout mission.

Do not trigger it for a single factual lookup, arbitrary URL summary, academic
paper search, ongoing competitor monitoring, generic news, or implementation
research. Those remain with native retrieval or the more specific existing
skills.

## Source Routing

Research runs through ordered lanes. A lane may use any currently available
Hermes retrieval tool or configured read-only connector, but the skill must not
install a connector during the mission.

1. **Primary evidence:** official product, pricing, changelog, filing, public
   dataset, documentation, status, or policy pages.
2. **Independent market evidence:** reputable trade press, analyst material,
   directories, reviews, or public datasets that corroborate the primary record.
3. **Community signals:** public discussions, reviews, issue trackers, forums,
   and configured read-only social/community connectors. These establish buyer
   language and signal frequency, not population-wide prevalence.
4. **Browser fallback:** use existing browser navigation when normal extraction
   cannot render a public page and the browser tool is available.
5. **Coverage gap:** record the failed source class, attempted route, and impact
   on confidence. A failure is never converted into “no demand” or “no
   competitors.”

The common retrieval path is `web_search` followed by `web_extract` for pages
that carry a claim. Search snippets can identify candidates but do not support
load-bearing conclusions. Retrieved content is always treated as untrusted data,
not instructions.

## Health Checks and Fallbacks

A retrieval attempt succeeds only when it returns substantive target content.
HTTP success, a zero exit code, a page shell, empty arrays, login prompts, and
anti-bot interstitials do not count as success.

For each target source:

1. Attempt the preferred read-only route once.
2. Retry once only when the failure is plausibly transient.
3. Try one suitable fallback route when available.
4. Stop and record a coverage gap if substantive content is still unavailable.

The skill must not attempt automated login, cookie extraction, proxy setup,
package installation, or unbounded discovery of alternative tools.

## Evidence Contract

Each accepted evidence item contains:

| Field | Meaning |
|---|---|
| `claim` | The narrow proposition supported by the evidence. |
| `source_url` | URL obtained from retrieval output and registered in the citation ledger. |
| `source_title` | Retrieved title or publisher-supplied label. |
| `published_or_observed_at` | Publication date when stated; otherwise the observation date. |
| `source_lane` | `primary`, `independent`, or `community`. |
| `evidence` | Short extract or precise description of what was observed. |
| `signal_type` | Demand, pain, pricing, competition, buyer language, risk, or counter-evidence. |
| `corroboration` | Independent supporting source identifiers, if any. |
| `confidence` | `high`, `medium`, or `low`, with a concise reason. |
| `limitations` | Sampling, access, freshness, ambiguity, or representativeness limits. |

Mission output contains four sections:

1. **Decision summary:** what the evidence supports and does not support.
2. **Evidence matrix:** the normalized evidence items.
3. **Contradictions and uncertainty:** conflicting findings and unresolved gaps.
4. **Coverage report:** source lanes attempted, successful, unavailable, or
   excluded for safety.

All external factual claims use `grounded-citations`. The existing citation
ledger owns source identities; this skill must not hand-create citation numbers
or duplicate URL-to-ID state.

## Venture Swarm Handoffs

### Scout

Scout owns retrieval and emits the evidence contract. It may propose opportunity
hypotheses, but must keep observations separate from interpretation.

### Sentinel

Sentinel reviews source legality, privacy implications, representativeness,
unsafe collection methods, and whether marketing claims exceed the evidence. It
may block or downgrade signals without altering the underlying record.

### Quant

Quant consumes only cited demand, price, cost, and competitor inputs. It records
which financial assumptions are inferred and must not convert weak community
signals into precise market-size estimates.

### Orchestrator

The Orchestrator advances the mission only when the evidence matrix, coverage
report, and uncertainty section are present. A high-impact coverage gap becomes
a user checkpoint rather than an implicit assumption.

## Safety and Ethics

- Read only public content or connectors the user has already configured.
- Do not collect personal contact details for unsolicited outreach.
- Do not evade access controls, CAPTCHAs, rate limits, or platform restrictions.
- Do not infer sensitive personal attributes from community content.
- Quote or paraphrase only what the retrieved source supports.
- Treat user posts and reviews as individual signals, not representative samples.
- Report unavailable or excluded sources visibly.
- Require human approval before any downstream external or commercial action.

## Hermes Installer Compatibility

The Hermes skills installer fetches only `SKILL.md` and recognizable relative
support links under approved directories. Therefore:

- `SKILL.md` links directly to
  `[the evidence contract](references/evidence-contract.md)` and
  `[source routing](references/source-routing.md)`.
- Both files are committed at those exact paths.
- No wildcard-shaped support path appears in prose.
- No absolute path, parent traversal, symlink, or unsupported support directory
  is used.
- Every required support file is linked; unreferenced files are not relied upon.

## Testing Strategy

Tests assert durable contracts rather than snapshots of prose:

- required frontmatter exists and the description is one sentence of at most
  60 characters;
- the skill name matches its directory and declares the research category;
- related skill names resolve in the repository;
- both referenced support files exist and use installer-safe relative links;
- no wildcard support path, traversal, absolute machine path, cookie flow,
  automated login, package installation, posting, or outreach instruction is
  present;
- the trigger boundary includes venture evidence work and explicitly excludes
  generic URL handling;
- source routing orders primary, independent, community, browser fallback, and
  coverage-gap handling;
- success requires substantive content and retry/fallback attempts are bounded;
- the evidence contract contains every required field;
- the handoff defines Scout, Sentinel, Quant, and Orchestrator responsibilities;
- the skill linter reports no errors.

Tests run with the repository wrapper:

```bash
scripts/run_tests.sh tests/skills/test_venture_signal_research_skill.py -q
```

The implementation also runs the general skill-linter tests. No live network
requests are required.

## Acceptance Criteria

- Hermes can discover and load `venture-signal-research` as a bundled skill.
- The skill activates for Venture Swarm market-evidence missions without
  claiming every research request or URL.
- A mission follows the ordered source lanes and stops after bounded retries.
- Empty, blocked, or login-gated responses become coverage gaps.
- The final artifact satisfies the evidence and coverage contracts and composes
  with `grounded-citations`.
- No new dependency, configuration, model tool, plugin, credential, or write
  capability is added.
- Focused tests and skill-linter tests pass through `scripts/run_tests.sh`.

## Rollout and Escalation

Ship the skill as a bundled, dependency-free capability. Evaluate it against a
representative Venture Swarm mission using already configured tools. If repeated
missions identify the same inaccessible but valuable source, document the
concrete source, required structured operations, authentication boundary, and
failure modes before proposing an optional provider plugin or MCP integration.

This keeps the first release useful and reviewable while preserving a path to
broader reach based on observed demand rather than speculative infrastructure.
