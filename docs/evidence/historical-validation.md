# Historical validation evidence

These records preserve the provenance of older validation claims. They are not
the `0.19.0-agentic-foundation` release gate; that gate is pinned separately in
[READINESS.md](../../READINESS.md). No raw test-output artifact was committed
for these historical runs, so claims without a retained command are explicitly
not independently reconstructable.

| Claim | Recording commit | Exact command retained? | Evidence status |
| --- | --- | --- | --- |
| 281 focused tests (exact-grant increment) | `43808baf1e6e0744eb73521c480a117c4868a04b` | Yes, in that commit's README: `source .venv/bin/activate` followed by the seven focused `pytest -q tests/hermes_cli/...` paths listed there | Reported result; raw output not retained |
| 361 focused Python tests plus desktop/TUI/web typechecks | `37d093960d03c088d9fd88e59d323647a80884ec` | No | Historical claim only; command and raw output are not independently reconstructable |
| 161 tests across 20 governed-runtime files | `302041c11c09c97fd2aaa7dee533fd83690ff011` | No | Historical claim only; file list, command, environment, and raw output are not independently reconstructable |

Future release evidence must commit the exact command, environment summary, and
raw or machine-readable output artifact alongside the evidence SHA.
