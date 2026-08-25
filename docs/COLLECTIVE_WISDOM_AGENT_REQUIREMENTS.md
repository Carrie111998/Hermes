# Collective Wisdom Agent requirements ledger

This ledger covers the Agent-owned portion of the canonical design in
[Gateway PR #215](https://github.com/NousResearch/gateway-gateway/pull/215) at
`600bbf5f181a89e5e54f8ed494c94aa8f8687b01`. The implementation is one unified
[Hermes Agent PR #94266](https://github.com/NousResearch/hermes-agent/pull/94266).

## Pinned contract and QA catalog

| Artifact | Immutable pin |
| --- | --- |
| Gateway implementation | PR #213 at `fda8bd80fb8ca461966d14a79dda4ed0e07fb4d4` |
| Gateway OpenAPI | SHA-256 `1c38e43df6fdb83705cd9d14d58655bf3eab08b84f41198272821866c8d2f603` |
| Package-manifest schema | SHA-256 `64d0010eada1d79fa16309e9fd715faf77b6186360ea0b095182b2bdaeec5714` |
| Canonical hash vectors | SHA-256 `e2b28c708f69e99b342de1df48498d96efde68867857391590bc964a609a730b` |
| `DOGFOOD_QA_TEST_IDS.json` | schema 3, 369 IDs, SHA-256 `0cccb3502cf168046ed2a6a9f924162249f3fe935919cfc2dd3ed4d6008a6276` |
| `DOGFOOD_QA_VERIFICATION_SPEC.md` | SHA-256 `1d2e9185753d951b93406a1a23a477f4a5327f2c6278badd7fbfdfb4b1d78772` |

`hermes_wisdom/contract.py` and `scripts/verify_wisdom_contract.py` enforce the
wire/schema/vector pins. The QA catalog is cross-repository release evidence:
an Agent unit test is not represented as proof of a Gateway or Portal row.

Local usage, refinement, qualification, ranking, stability, and dismissal
signals are device-private. They are never part of a Gateway request, log,
stored row, audit event, Portal response, or publication consent artifact.

## Agent ownership matrix

| Canonical IDs / source | Agent behavior | Implementation | Verification | Owning PR | Status / external dependency |
| --- | --- | --- | --- | --- | --- |
| PRIV-001, 004, 005, 008–010; SUG-001–010 | Device-local qualification, browse-all/manual selection, durable suppression, and no local evidence or target inventory on the wire | `hermes_wisdom/qualification.py`, `store.py`, `service.py`, `tools/skill_usage.py` | `tests/wisdom/test_qualification.py`, `test_service.py`, request-body tests | Agent #94266 | implemented; live capture across Agent→Gateway remains release evidence |
| PKG-POS-001–005; PKG-NEG-001–073; LOCAL-003–004 | Exact instruction-only overlay; text-only inert `refs/`/`assets/`; hostile paths, active files, special nodes, hard links, unknown types, count/byte/depth bombs, and malformed manifests fail closed | `hermes_wisdom/package.py`, `contract.py` | `tests/wisdom/test_package.py`, `test_contract.py` | Agent #94266 | implementation complete; the catalog still requires one materialized `(id, case_id)` fixture row per hostile case before rollout |
| HASH-002, 003, 006; LOCAL-001, 007 | Agent and Gateway agree on canonical content/description/manifest hashes and downloaded bytes are rehashed before mutation | `contract.py`, `package.py`, pinned fixtures | `scripts/verify_wisdom_contract.py`, `tests/wisdom/test_contract.py`, `test_client.py` | Agent #94266 + Gateway #213 | implemented against the pinned Gateway commit |
| SUG-008–010; CONSENT-001–005, 009–015 | Owner sees exact server reconstruction and three hashes; receipts are revision/hash-bound; noninteractive execution cannot approve | `hermes_wisdom/client.py`, `service.py` | `tests/wisdom/test_client.py`, `test_service.py` | Agent #94266 + Gateway #213 | implemented; server-side consent/state authority remains Gateway-owned |
| SCAN-001–004, 012 (Agent portion) | Built-in guard is local policy; NVIDIA SkillEvaluator is an optional adviser and unavailable never means passed; Gateway scan is separately server-enforced | `hermes_wisdom/service.py`, existing `tools/skillevaluator_scan.py` | `tests/wisdom/test_service.py` | Agent #94266 + Gateway #213 | implemented; authoritative policy/scan cases are Gateway evidence |
| INST-001–009; LOCAL-001–008 | Explicit authenticated install, local compatibility, staging/atomic replacement, final record journal, and no unattended background update mutation | `hermes_wisdom/service.py`, `compatibility.py`, `consumption.py` | `tests/wisdom/test_compatibility.py`, `test_service.py`, `test_consumption.py` | Agent #94266 + Gateway #213 | implemented; macOS/Windows atomic-filesystem execution remains release evidence |
| TAKE-006 plus feed/lifecycle consumer portion | Takedown/archive is surfaced and blocks new server authorization, but an existing local install is preserved; uninstall is explicit and recoverable | `hermes_wisdom/consumption.py`, `store.py` | `tests/wisdom/test_consumption.py`, `test_client.py` | Agent #94266 + Gateway #213 | implemented; Gateway owns lifecycle transition/audit rows |
| CRASH/ATOMIC/RECOV client boundaries | Install, update, required-fork, uninstall, and final Gateway recording resume from a durable operation journal | `hermes_wisdom/consumption.py`, `store.py` | fault-injection cases in `tests/wisdom/test_consumption.py` and `test_service.py` | Agent #94266 | implemented locally; full cross-platform schedule matrix remains release evidence |
| CLI/product specification | Setup is the explicit disclosure/organization activation boundary; status is non-enrolling; scan/suggest/review/decision/discovery/install/check/update/uninstall commands plus natural-language plan/apply install | `hermes_cli/subcommands/wisdom.py`, `hermes_wisdom/service.py`, built-in Collective Wisdom skill | `tests/hermes_cli/test_wisdom_parser.py`, `tests/wisdom/test_service.py`, `test_web_api.py`, `tests/skills/test_collective_wisdom_install_skill.py`, `tests/agent/test_wisdom_skill_namespace.py` | Agent #94266 | implemented |
| Dashboard/native/in-chat product specification | Profile-scoped local BFF, discovery/detail/candidate/review/install/update surfaces, and durable in-chat candidate event | `hermes_cli/web_server.py`, dashboard `CollectiveWisdomPanel`, desktop `CollectiveTab` and `WisdomCandidateCard` | focused BFF/web/desktop component, scope, XSS, consent, and hydration tests | Agent #94266 | implemented; live visual E2E remains a rollout gate |
| E2E-001–007 | Two identities in one org, negative cross-org access, exact consent, crash convergence, takedown preservation, and flag rollback | Agent + Gateway + Portal | live dogfood harness | Agent #94266 + Gateway #213 + Portal #1022 | pending local/live stack evidence and product-owner sign-off; rollout remains disabled |

The Gateway verifies both the exact `wisdom:*` permission and the signed NAS
admin containment claim during dogfood. Token claims decoded by Hermes are
display hints only and never local authorization authority.
