# ADR 0002 — Skill contract is the product brain

## Status

Accepted

## Context

A Python packaging CLI could drift into a second implementation of product
intelligence, competing with `SKILL.md`.

## Decision

- **Hermes + `SKILL.md`** own product/opportunity judgment.
- **Python CLI** owns deterministic packaging: route, validate, recipes, tests,
  envelopes, distribution.

## Consequences

- `do run` prepares workspaces; it does not invent opportunities.
- New domain intelligence goes in profiles/`SKILL.md`, not new Python brains.
