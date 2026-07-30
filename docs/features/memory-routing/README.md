# Hybrid Open-set Domain Router

This folder is the repo snapshot for the Hermes Memory Routing & Always-in-State proposal inspired by the InMind implicit-association benchmark.

## Documents

- [`PRD.md`](./PRD.md) — product requirements for tiered memory routing, domain-slot recall, safety/profile gates, write tagging, and benchmark expectations.
- [`plan.md`](./plan.md) — phased implementation plan, including baseline stratification, open-set routing tasks, isolated benchmark harness requirements, and cost constraints.
- [`memory-routing-policy.yaml`](./memory-routing-policy.yaml) — initial YAML SSOT for memory tiers, risk levels, domains, inheritance, and `required_if_available` slots.
- [`claude-opus-review.md`](./claude-opus-review.md) — Claude Code Opus review notes incorporated into the PRD and plan.

## Scope

This change is documentation-only. It does not modify Hermes runtime code, tools, or agent behavior. The policy file is intended as a human-readable starting point for a future implementation and benchmark harness.

## Core idea

Hermes should not rely only on lexical or semantic retrieval when user facts are indirectly relevant. A hybrid router combines:

1. deterministic domain/risk policy slots,
2. open-set classifier proposals for unknown domains,
3. always-in-state preservation of critical profile facts, and
4. turn-local triggered memory blocks for relevant but non-global facts.

The implementation plan requires stratifying results by whether the necessary fact is already in the always-visible memory block or in overflow/archive memory before claiming routing improvements.
