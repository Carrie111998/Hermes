# Collective Wisdom Agent requirements ledger

This ledger implements the Agent-owned portion of the canonical design in
[Gateway PR #215](https://github.com/NousResearch/gateway-gateway/pull/215) at
`ee7b6123a08ef2379ef1641ed8e6defa10329eaa`. The wire contract is Gateway PR
#213 at `f1c1e8418bbd1efdae67d7ac9e0d40f47fe35b42`; `hermes_wisdom/contract.py`
pins its checked-in OpenAPI artifact, package-manifest schema, and
canonical-vector digests.

Local usage, refinement, qualification, ranking, stability, and dismissal
signals are device-private. They are never part of a Gateway request, log,
stored row, audit event, Portal response, or publication consent artifact.

| Requirement area | Delivery slice | Implementation | Verification | Status / dependency |
| --- | --- | --- | --- | --- |
| Profile setup, disclosure, SQLite migrations, installation identity | local foundation | `hermes_wisdom/store.py`, `service.py` | `tests/wisdom/test_store.py`, `test_service.py` | complete in this PR |
| Instruction-only overlay and declarative System Specification | contribution | `hermes_wisdom/package.py`, `contract.py` | `tests/wisdom/test_package.py` | complete; no dependency execution |
| Canonical content, author-copy, and manifest hashes | contribution | `contract.py`, pinned fixtures and OpenAPI | `scripts/verify_wisdom_contract.py`, `test_contract.py` | complete; three-hash consent |
| Built-in guard plus NVIDIA SkillEvaluator advisory | contribution | `service.py`, existing `tools/skillevaluator_scan.py` | `test_service.py` | complete; advisory is not compatibility or ranking |
| Owner-private submission, exact server review, receipt, publish recovery | contribution | `client.py`, `service.py` | `test_client.py`, `test_service.py` | complete; Gateway remains authority |
| Explicit managed install and four deterministic local outcomes | consumption | `compatibility.py`, `service.py` | `test_compatibility.py`, `test_service.py` | complete; compatibility is re-evaluated at apply and inventory is never uploaded |
| CLI and natural-language plan/apply install | interfaces | `hermes_cli/subcommands/wisdom.py`, built-in skill | `tests/hermes_cli/test_wisdom_parser.py`, `tests/skills` | complete in this PR |
| Continuous local qualification and durable candidate event | contribution | `hermes_wisdom/qualification.py`, `store.py`, `tools/skill_usage.py` | `tests/wisdom/test_qualification.py`, legacy skill-usage suites | complete in this PR; all reasons, excerpts, and counters stay on-device |
| In-chat promotion card and dashboard/native candidate UI | interfaces | `hermes_cli/web_server.py`, dashboard `CollectiveWisdomPanel`, desktop `CollectiveTab` and `WisdomCandidateCard` | focused web/desktop component, scope, XSS, consent, and hydration tests | complete in this PR; live cross-repo visual E2E remains a rollout gate |
| Update modes, required forks, feed, cadence, Telegram, uninstall | consumption | `hermes_wisdom/consumption.py`, `store.py`, Wisdom CLI and background checker | `tests/wisdom/test_consumption.py`, client/store/CLI suites | complete in this PR; Telegram resolves only the configured home identity and delivery failure is non-transactional |
| Completed discovery/detail/install/update dashboard/native UX | interfaces | profile-scoped BFF, dashboard `CollectiveWisdomPanel`, desktop `CollectiveTab` and transcript notice card | focused BFF/web/desktop UI suites plus production builds | complete in this PR; live visual E2E remains a rollout gate |
| Two-identity same-org and negative cross-org live E2E | cross-repo rollout | Agent + Gateway + Portal | live dogfood harness | pending local/live stack evidence and product sign-off; rollout stays disabled |

The Gateway verifies both the exact `wisdom:*` permission and the signed NAS
admin containment claim during dogfood. Token claims decoded by Hermes are
display hints only and never local authorization authority.
