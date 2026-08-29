---
name: codebase-design
description: Orient bounded code areas before designing changes.
version: 0.1.0
author: Lucas Veber (vegapunkpa-hue), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [codebase, architecture, design, modules, interfaces, orientation]
    related_skills: [plan, systematic-debugging]
---

# Codebase Design Skill

Use this skill to understand one bounded area of an existing codebase before
proposing a design change. It produces a concise orientation and design note;
it does not edit code, manage work, or choose a project-wide architecture.

## When to Use

- A requested change touches unfamiliar code and needs a local design first.
- You need to assess module boundaries, contracts, adapters, or ownership.
- A refactor or feature has several plausible local shapes.
- You need to explain why a change belongs at a particular seam.

Don't use for:

- Broad repository tours, architecture inventories, or dependency audits.
- Implementation, edits, commits, reviews, task tracking, or external updates.
- Debugging a concrete failure; establish the failure and root cause first.

## Prerequisites

State a bounded selected area before inspecting code. It must name:

1. one primary module, directory, feature slice, or entry point;
2. the change question or behavior under design; and
3. at most the directly connected callers, consumers, or adapters needed as
   evidence for that question.

If the request is broader, narrow it to a representative path or ask the user
to choose the area. Do not expand the boundary merely because more code exists.

Use only native Hermes read-only tools: `search_files` to locate the selected
area and its direct relationships, and `read_file` to inspect the relevant
sources. Do not use `write_file`, `patch`, `delegate_task`, `cronjob`, task
trackers, or tools that write to external systems.

## How to Run

Begin with a one-sentence scope statement, then make only read-only
`search_files` and `read_file` calls within that boundary. Read the primary
module and the smallest set of direct interfaces, callers, consumers, and
adapters needed to answer the design question before offering a design.

## Quick Reference

- Scope: primary area + design question + direct evidence boundary.
- Orient: responsibilities, callers, consumers, data/control flow, and tests.
- Measure interfaces by what a caller must know, not by file length.
- Find seams where policy can vary without spreading conditional knowledge.
- Prefer a deep module: small interface, substantial hidden complexity.
- Identify adapters that translate between an external contract and local terms.
- Compare options only when the user explicitly asks for alternatives.
- Deliver a local design note; do not make edits or external updates.

## Procedure

### 1. Set the design boundary

Write the selected-area statement before inspecting code: “Design question:
[question]. Primary area: [path or symbol]. Evidence boundary: [direct
callers, consumers, or adapters].” Keep it small enough that each included
file can explain the question rather than merely provide context.

Done when the primary area, decision question, and direct evidence boundary
are all explicit; otherwise stop and narrow the scope.

### 2. Orient the existing path read-only

Use `search_files` to locate the primary symbol, its direct callers or
consumers, adjacent tests, and any translation boundary. Use `read_file` to
read the primary code and the minimal related code completely. Record observed
responsibilities and flow; distinguish evidence from inference.

Done when you can name what enters the selected area, what leaves it, who owns
each responsibility, and the smallest observed path relevant to the question.

### 3. Map interface size and locality

For every direct collaborator, list the knowledge the collaborator needs:
methods, data shapes, ordering rules, lifecycle details, error conventions, or
configuration. This is interface size. A short API with many hidden rules is
still large; a longer API can be smaller when each operation is self-contained.

Check locality: keep knowledge needed to change together near one module or
boundary. Treat repeated interpretation of the same rules across callers as a
signal that responsibility is leaking.

Done when the note identifies each direct interface's required knowledge and
where duplicated or distant knowledge currently lives.

### 4. Find seams, adapters, and leverage

A seam is a boundary where a policy or implementation can vary without forcing
unrelated code to understand the variation. An adapter belongs at a boundary
when it translates an external or legacy contract into the local model; it
should not export foreign terminology through the core.

Identify leverage by asking which small boundary change removes the most
knowledge from callers or prevents the most future branching. Prefer moving
complexity behind the module that can own it, rather than distributing it
across call sites.

Done when the design note names the candidate seam, any needed adapter, and
why the proposed owner has more leverage and better locality than its callers.

### 5. Propose one default local design

Describe the smallest change that improves the selected path. State the module
responsibility, the caller-facing interface, hidden complexity it absorbs,
seam placement, adapter boundary if any, and how the current direct callers
would become simpler. Include non-goals that keep the change inside the
selected area.

Do not automatically present multiple options. Produce alternatives only when
the user explicitly asks to compare designs or a decision genuinely cannot be
made from the observed constraints. In that case, compare at most the requested
options against interface size, locality, seam clarity, leverage, migration
risk, and the evidence gathered.

Done when one default design is tied to observed code and every claimed benefit
is stated in terms of fewer caller obligations, stronger locality, or a clearer
seam.

### 6. Return an orientation and design note

Return this compact structure:

- **Scope:** selected area, design question, and evidence boundary.
- **Observed shape:** responsibilities, direct flow, and relevant contracts.
- **Design pressures:** interface size, leaked knowledge, locality, and seams.
- **Recommendation:** default module boundary, interface, hidden complexity,
  adapters, and leverage.
- **Evidence and limits:** files inspected, assumptions, unanswered questions,
  and non-goals.

Done when the note accounts for every inspected file's relevance and makes no
claim that requires uninspected broad-repository knowledge.

## Pitfalls

- Do not mistake a directory tree for an orientation; follow one selected path.
- Do not call a module deep because it is large. Depth comes from hiding
  complexity behind a small, stable caller obligation.
- Do not count public method names alone. Hidden ordering, configuration, and
  error rules enlarge an interface too.
- Do not introduce a seam just to create an abstraction. It must isolate a
  real variation or translation boundary in the selected area.
- Do not let an adapter become a second domain model. Translate at the edge and
  keep the local core in local terms.
- Do not claim leverage from a speculative future. Ground it in caller
  knowledge or branching observed in the bounded area.
- Do not turn this into a router, delegation workflow, tracker, edit plan, or
  broad exploration process.
- Do not offer option matrices by default; comparison is opt-in.

## Verification

Before responding, confirm all of the following:

- [ ] The selected area and design question were stated before design work.
- [ ] Orientation used only `search_files` and `read_file` and stayed read-only.
- [ ] The primary code and only necessary direct relationships were inspected.
- [ ] The note distinguishes evidence, inference, assumptions, and limits.
- [ ] The recommendation explains deep-module responsibility, interface size,
  seams, adapters, leverage, and locality where relevant.
- [ ] No code, task tracker, external system, or repository state was changed.
- [ ] Alternatives appear only when the user explicitly requested comparison.

## Provenance

This original Hermes-native workflow is informed by Matt Pocock's
MIT-licensed `codebase-design` material in
[Matt Pocock's skills repository](https://github.com/mattpocock/skills/blob/main/docs/engineering/codebase-design.md).
It is independently written for Hermes's native read-only tools and does not
copy the upstream material.
