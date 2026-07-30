# ADR 0003 — Hub mirror strategy

## Status

Accepted

## Context

Hermes Hub only installs allowlisted, file-level paths referenced from
`SKILL.md`. Root `schemas/`, `profiles/`, and `evals/` are not allowlisted.

## Decision

- Canonical sources remain at repo root (`schemas/`, `profiles/`, `evals/`).
- Hub mirrors live under allowlisted dirs (`references/schemas`, etc.).
- `distribution.yaml` + `distribution_spine.py` are the single sync/verify path.

## Consequences

- Hand-edited mirrors and SKILL support lists are rejected by CI.
- Directory-style hub path refs are forbidden (break Hub fetch).
