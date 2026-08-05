---
title: "Graph Gated Engineering"
sidebar_label: "Graph Gated Engineering"
description: "Graph-gated engineering: model the whole system, agree on the graph, gate with real tests, slice at seams, verify with receipts"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Graph Gated Engineering

Graph-gated engineering: model the whole system, agree on the graph, gate with real tests, slice at seams, verify with receipts. Resolves the memory-system question structurally.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/software-development/graph-gated-engineering` |
| Version | `1.0.0` |
| Author | Axl Ibiza, MBA |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `graph`, `refactoring`, `conformance`, `verification`, `methodology`, `god-file`, `documentation`, `memory` |
| Related skills | [`systematic-debugging`](/docs/user-guide/skills/bundled/software-development/software-development-systematic-debugging), [`test-driven-development`](/docs/user-guide/skills/bundled/software-development/software-development-test-driven-development), [`plan`](/docs/user-guide/skills/bundled/software-development/software-development-plan), `worktree-hive`, `god-file-decomposition`, [`codebase-inspection`](/docs/user-guide/skills/bundled/github/github-codebase-inspection) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Graph-Gated Engineering

## The doctrine

A system is understood when it is modeled as a graph. Work is correct when
every claim it makes — a code symbol, a doc link, a config key, a memory —
resolves to a real node in that graph. A claim that does not resolve is a
dangling edge, and dangling edges are how knowledge rots.

This is why no one needs to ask which memory system is correct: the answer
is structural, not a choice between systems. A memory is correct iff it
resolves against the source graph it claims to describe. Obsidian, Notion,
an in-memory store, a file — the container is irrelevant; resolution is the
test. Adjudication happens against the graph, never inside the silo.

## The loop (model → agree → gate → slice → verify)

1. **Model the whole system first.** Never seed clusters into the model.
   Every tree models the entire system under inspection independently
   (double-blind). Slices fall out of the model's seams — they are never
   eyeball guesses layered on top of it.

2. **Agree on the graph before cutting.** A typed graph artifact merges the
   independent models. Provenance rule: ≥4-of-5 trees must agree on a node
   or edge before it enters the merged graph. Disputed items resolve by
   inspection of the code itself, never by vote weight alone.

3. **Gate with real execution.** The graph is a query surface — serve it
   (GraphQL works well) and make the quality gates Python programs that
   query it. Green light = all gates pass on real, full-cycle test
   execution. Poison fixtures prove each gate fires: a fixture that
   intentionally breaks one invariant must flip exactly its gate.

4. **Slice at the seams.** Extraction is execution against the agreed graph.
   Each slice carries a partition contract: the exact set of nodes it moves,
   byte-verbatim bodies, module-attribute re-exports that keep the source
   green. Extras are documented helpers, never smuggled behavior.

5. **Verify with receipts.** Every slice ships with its verification
   evidence: import/MRO checks, byte-verbatim diffs against the base,
   targeted tests, `git diff --check` clean. Ad-hoc verification is named
   as ad-hoc; suite-green is named as suite-green. Never blur the two.

## Documentation conformance

The same graph adjudicates documentation. Every doc claim is an edge —
internal link, code symbol, config key, file path — that must resolve to a
real node in the codebase graph. The conformance test
(`tests/conformance/test_docs_graph_conformance.py`) walks every doc,
builds the graph from AST + reference pages, emits all four edge types
(`LINKS_TO`, `REFERENCES`, `NAMES`, `POINTS_TO`), and asserts zero dangling
edges. Spec: `tests/conformance/docs-conformance-graph-spec.md` — node
types, edge types, resolution rules, closure criterion.

This closes the documentation issue class as a mechanism, not as manual
edits: wrong commands, broken links, stale config keys, and doc/code drift
are dangling edges, and the suite refuses to certify the doc set until they
resolve. Any maintainer can adopt the spec + test for their own repo.

## Receipts and honest boundaries

- Report real results, not theater: exact outputs, unverified boundaries,
  real blockers.
- Ad-hoc verification is legitimate but labeled; never present it as a
  full-suite green run.
- Verification artifacts that document a campaign's state are kept as the
  permanent record; throwaway checks are deleted after a passing run.
- A correction lands in the producing layer (skill, memory, rule), not just
  the conversation.

## The philosophy of information

- Information is only real if it resolves. A claim that cannot be checked
  against its source graph is noise, however confident its phrasing.
- Knowledge rots by drift, and the only defense is structural: the gate,
  not the audit. Audits find rot; gates prevent it.
- Provenance is load-bearing: every node carries who claimed it and how
  many independent models agreed.
- There are no borders on knowledge. The graph is shared, the mechanism is
  open, and any maintainer can run the same adjudication on their own
  system — "AI knows no borders" is a property of the method, not a slogan.
