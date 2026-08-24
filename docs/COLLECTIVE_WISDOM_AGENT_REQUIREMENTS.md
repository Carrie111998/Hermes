# Collective Wisdom Agent requirements ledger

This ledger implements the Agent-owned portion of the canonical design in
[Gateway PR #215](https://github.com/NousResearch/gateway-gateway/pull/215) at
`ee7b6123a08ef2379ef1641ed8e6defa10329eaa`. The wire contract is Gateway PR
#213 at `391bf7077c0dee5ca3b818ceff61c31cf14f3efd`; `hermes_wisdom/contract.py`
pins its OpenAPI, package-manifest schema, and canonical-vector digests.

Local usage, refinement, qualification, ranking, stability, and dismissal
signals are device-private. They are never part of a Gateway request, log,
stored row, audit event, Portal response, or publication consent artifact.

| Requirement area | Owning PR | Implementation | Verification | Status / dependency |
| --- | --- | --- | --- | --- |
| Profile setup, disclosure, SQLite migrations, installation identity | foundation | `hermes_wisdom/store.py`, `service.py` | `tests/wisdom/test_store.py`, `test_service.py` | complete in foundation |
| Instruction-only overlay and declarative System Specification | foundation | `hermes_wisdom/package.py`, `contract.py` | `tests/wisdom/test_package.py` | complete; no dependency execution |
| Canonical content, author-copy, and manifest hashes | foundation | `contract.py`, pinned fixtures | `scripts/verify_wisdom_contract.py`, `test_contract.py` | complete; three-hash consent |
| Built-in guard plus NVIDIA SkillEvaluator advisory | foundation | `service.py`, existing `tools/skillevaluator_scan.py` | `test_service.py` | complete; advisory is not compatibility or ranking |
| Owner-private submission, exact server review, receipt, publish recovery | foundation | `client.py`, `service.py` | `test_client.py`, `test_service.py` | complete; Gateway remains authority |
| Explicit managed install and four deterministic local outcomes | foundation | `compatibility.py`, `service.py` | `test_compatibility.py`, `test_service.py` | complete; local inventory is never uploaded |
| CLI and natural-language plan/apply install | foundation | `hermes_cli/subcommands/wisdom.py`, built-in skill | `test_cli.py`, `tests/skills` | complete in foundation |
| Continuous local qualification and durable candidate event | contribution loop | `hermes_wisdom/qualification.py`, `store.py`, `tools/skill_usage.py` | `tests/wisdom/test_qualification.py`, legacy skill-usage suites | complete in stacked PR 2; all reasons and counters stay on-device |
| In-chat promotion card and dashboard/native candidate UI | contribution loop | `hermes_cli/web_server.py`, dashboard `CollectiveWisdomPanel`, desktop `CollectiveTab` and `WisdomCandidateCard` | focused web/desktop component, scope, XSS, consent, and hydration tests | complete in stacked PR 2; live cross-repo visual E2E remains a rollout gate |
| Update modes, required forks, feed, cadence, Telegram, uninstall | consumption | update/feed modules | failure-injection and feed suites | partial until stacked PR 3 |
| Completed discovery/detail/install/update dashboard/native UX | consumption | web and desktop capability surfaces | accessibility and visual E2E | partial until stacked PR 3 |
| Two-identity same-org and negative cross-org live E2E | cross-repo rollout | Agent + Gateway + Portal | live dogfood harness | blocked on deployed pinned stack and sign-off |

The Gateway verifies both the exact `wisdom:*` permission and the signed NAS
admin containment claim during dogfood. Token claims decoded by Hermes are
display hints only and never local authorization authority.
