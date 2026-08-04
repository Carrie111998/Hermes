# Test Engineering Guide

Root [`AGENTS.md`](../AGENTS.md) still applies. This file owns Python test
placement, isolation, and proof standards.

## Canonical runner

Use `scripts/run_tests.sh`, not direct pytest, for normal verification. It
clears credential variables, fixes timezone/locale, isolates `HOME`, and runs
each file in a fresh subprocess. Pass-on-retry is reported as flaky and remains
a defect to fix.

## Isolation

Tests never write to the real `~/.hermes`. The autouse fixture supplies a
temporary `HERMES_HOME`. Profile tests that patch `Path.home()` also set
`HERMES_HOME` so both profile roots and active state remain isolated.

Module globals and context variables do not carry between test files because
each file has its own process. Do not write tests that accidentally depend on
cross-file state.

## Test the contract, not the source

- Assert relationships and behavior, not current model names, schema-version
  literals, enumeration counts, or other expected-to-change snapshots.
- Never read production source text and regex for an implementation shape.
  Extract behavior behind a callable boundary and execute it.
- Resolution, config, security, network, and filesystem claims need the real
  import/call chain with external boundaries isolated at the edge.
- Timing tests use event synchronization or generous bounds; negative
  wall-clock races are not proof.

## Language placement

Tests that assert about JavaScript/TypeScript artifacts belong in the JS test
suite selected by the same CI change classifier. A Python test that reads
`package.json` or `.ts` source can miss the PR that changes it.

Skill tests follow [`skills/AGENTS.md`](../skills/AGENTS.md). Long-running
Kanban stress tests have additional instructions in
[`tests/stress/README.md`](stress/README.md).
