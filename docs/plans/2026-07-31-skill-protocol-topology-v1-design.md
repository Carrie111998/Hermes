# Skill Protocol Topology V1 Design

Hermes will treat skill topology as optional advisory metadata, parsed from
`metadata.hermes.topology`. A new import-light `agent.skill_topology` module
will own normalization, validation, graph auditing, deterministic scoring,
dependency expansion, budget enforcement, and privacy-safe route artifacts.
It will accept plain skill metadata dictionaries rather than reading profiles
or registering tools, keeping the planner testable and reusable by both CLI and
model-tool adapters. Existing platform, environment, disabled-skill, and tool
permission gates remain upstream of the planner; topology never executes a
skill or grants permission.

The existing skill scanner will gain an opt-in rich-metadata mode. Default
calls keep their current minimal dictionaries and serialized `skills_list()`
shape. Rich mode reads the complete SKILL.md so `cost_chars` and `cost_bytes`
come from the real content, and exposes tags plus normalized topology to the
planner. `skills_list(query=...)` will use this mode and return a route artifact;
without a query it will follow the existing code path unchanged.

Two read-only CLI actions and the `skills_list(query=...)` model-tool path
share one rich installed-inventory builder. `skills route` plans against
currently eligible local, external, and registered plugin skills, while
`skills topology` audits that same installed inventory with disabled and
runtime-ineligible records included for diagnosis. Plugin costs come from the
complete registered `SKILL.md` with the same UTF-8-sig decoding and byte/character
accounting as local rich scanning; an unreadable registered file blocks a
matching route without exposing its path or contents. Ordinary no-query
`skills_list()` keeps its historical local/external-only listing and does not
trigger plugin discovery.

JSON is deterministically serialized but contains no query fingerprint: an
unsalted digest is dictionary-guessable for low-entropy queries and has no
required V1 reader. Route artifacts contain neither raw queries, local paths,
nor skill bodies. Selected skill metadata may naturally repeat query terms.
Human output shows route order, reasons, costs, and diagnostics. No route event
is persisted because V1 has no concrete reader for a separate event log.

Topology has an intentionally narrow authority boundary. Hermes routes only
skills installed into the active local tree, configured external directories,
or registered plugins. A central private MCP skill library is represented by
one installed thin adapter skill; the adapter owns discovery, routing, and
loading inside that library. Hermes must not crawl or ingest the library's
catalog as a second installed inventory, and this design adds no dependency on
the private library.

Ranking uses explicit weighted field matches: exact name first, then exact
tags/domains, then inputs/outputs/category, with loose name and description
tokens last. Ties resolve by canonical name and category. A route greedily
considers ranked roots, expands transitive `requires` depth-first so
prerequisites precede dependents, and omits candidates that exceed count or
character budgets. Missing requirements, dependency cycles, self references,
invalid lifecycle values, and active conflicts produce explicit diagnostics;
when every match is unusable the result is `blocked`, and when nothing matches
it is `no_match`.
