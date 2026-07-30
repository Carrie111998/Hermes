# Feature Slice Review: Bind displayed approvals to exact queued requests

Status: In progress  
Slice: `approval-request-binding`  
Date: `2026-07-30`  
Review log: `reviews/2026-07-30-approval-request-binding.md`

## Slice contract

### Goal

Every interactive approval surface resolves the precise queued request shown to
the user, while typed approval commands and clients that omit a request ID keep
their existing FIFO behavior.

### In scope

- Give each queued gateway approval a validated, stable request ID and include
  it in the notification payload.
- Resolve an exact pending entry by request ID without disturbing sibling
  entries.
- Port request binding and the existing authorization seam through the current
  native messaging adapters.
- Carry request binding through the Relay prompt state and response path.
- Carry request binding through the TUI/Desktop approval event and response
  protocol without changing the rendered controls.
- Expose exact request binding through the Runs API, including stale-ID
  rejection, capability flags, pending-status handling, and a fixed,
  server-owned structured preview.
- Preserve redaction, adapter-local authorization, and legacy FIFO behavior.
- Update the relevant API and TUI protocol documentation.

### Out of scope

- Changing approval choices or approval policy.
- Redesigning approval controls or adding user-facing copy.
- Removing or changing the typed `/approve` and `/deny` FIFO contract.
- Unrelated current-main refactors, host-specific test repairs, or CI changes.
- Closing or modifying PR #6105.

### Acceptance criteria

| ID | Observable criterion | Evidence | Status |
| --- | --- | --- | --- |
| AC-1 | Selecting the second of two pending approvals resolves only the second and leaves the first pending. | Queue-core and live-transport regression tests. | Pass |
| AC-2 | An unknown, invalid, or stale request ID resolves nothing and fails closed at public protocol boundaries. | Core, Relay, TUI gateway, and Runs API negative-path tests. | Pass |
| AC-3 | Exact request selection cannot be combined with `all` or `resolve_all`. | Runs API and TUI gateway validation tests. | Pass |
| AC-4 | Native adapters, Relay, TUI, Desktop, and the Runs API round-trip the displayed request ID. | Adapter, protocol, and client behavior tests. | Pass |
| AC-5 | Clients and typed commands that omit a request ID retain FIFO behavior. | Existing FIFO regression tests plus compatibility tests. | Pass |
| AC-6 | A run remains `waiting_for_approval` while another request is pending. | Mounted Runs API two-request integration test. | Pass |
| AC-7 | Existing authorization and command-redaction behavior does not regress. | Authorization and redaction suites. | Pass |
| AC-8 | Structured Runs API previews contain only fixed Hermes-owned categories and allowlisted labels. | Preview allowlist and secret/path exclusion tests. | Pass |

### Constraints and recovery

- Safety: Resolve only the selected entry; stale identities fail closed;
  authorization remains ahead of resolution; preview fields never echo command,
  plugin, credential, or private-path text.
- Compatibility: No-ID clients and typed commands retain FIFO semantics. New
  clients send a request ID; new backends accept both modes.
- Rendered behavior: Existing approval controls, choices, layout, and copy stay
  unchanged. Only protocol state gains the request ID.
- Rollback or recovery: No data migration or persisted schema is introduced.
  The slice can be reverted as code and documentation commits.
- Documentation targets:
  `website/docs/developer-guide/programmatic-integration.md`,
  `website/docs/user-guide/features/api-server.md`, and `ui-tui/README.md`.
- Version-control strategy: Work from
  `codex/http-approval-request-binding-rewrite` based on
  `upstream/main@8defb9fd6`; preserve mr.Shu's authorship for the
  contributor-derived commit; publish to the existing PR branch only after the
  final publication gate.

### Scope discussion and approval

- Recommendation and rationale: Close the complete displayed-request bug class,
  including the reviewer-requested Relay path and the equivalent current-main
  TUI/Desktop path, while preserving the existing Runs API extension.
- Alternatives considered: Relay-only would be smaller but leave TUI/Desktop
  with the same FIFO substitution risk; dropping the structured Runs API
  preview would discard an already-reviewed safety boundary.
- User decisions: Approved the proposed slice, review-log path, and
  version-control strategy.
- Approved at: 2026-07-30, user response: "yes I approve. lets do it".

## Test strategy

| Acceptance criterion | Pre-implementation gap | Planned test or evidence | What it proves | Limitations |
| --- | --- | --- | --- | --- |
| AC-1 | Core, Relay, and TUI/Desktop currently resolve by session FIFO. | Two-entry queue tests at core, Relay, TUI gateway, and mounted Runs API layers. | The selected second request is the only entry signalled and the first remains pending. | Native service SDKs remain mocked at the network edge. |
| AC-2 | Public handlers accept no exact selector today. | Invalid-format, unknown, expired, and double-response tests. | Bad or stale identities cannot fall back to another pending action. | Does not simulate every real network retry ordering. |
| AC-3 | `all` currently has no request-ID interaction to validate. | Protocol tests for request ID plus `all`/`resolve_all`. | Exact and bulk scopes cannot be mixed ambiguously. | Limited to public protocol boundaries where bulk scope exists. |
| AC-4 | Events and client state omit the approval request ID. | Native adapter payload tests, Relay state test, TUI event/type/response tests, Desktop store/component/notification tests, Runs API SSE test. | The ID survives every displayed approval round trip. | No visual screenshot is needed because layout/copy do not change. |
| AC-5 | Existing FIFO behavior is intentional compatibility. | Existing FIFO tests plus explicit no-ID API/TUI tests. | Legacy callers and typed commands retain their contract. | Does not guarantee behavior of unknown third-party clients beyond the documented protocol. |
| AC-6 | Runs API always marks a successful response `running`. | Mounted API test with two live pending entries. | Status accurately reports that another approval still blocks the run. | Exercises the server boundary without launching a real model provider. |
| AC-7 | The port crosses several authorization and redaction seams. | Existing authorization/redaction suites plus exact-ID assertions after authorization. | Request binding cannot bypass actor checks or leak unredacted commands. | External platform authorization APIs remain represented by established test doubles. |
| AC-8 | Existing API events expose redacted but still free-form fields. | Structured-preview category, allowlist, truncation, credential/path, and plugin-text tests. | The advertised preview is bounded and server-owned. | Legacy free-form event fields remain for compatibility; clients must use the advertised preview capability. |

### Baseline results

| Command or action | Environment | Result | Notes |
| --- | --- | --- | --- |
| Source and call-path inspection | macOS worktree at `upstream/main@1fd7548b4` | Fail (gap reproduced) | Core, Runs API, Relay, and TUI gateway all resolve displayed approvals without a request selector. |
| GitHub review inspection | PR #68080 | Pass | One actionable top-level reviewer cluster; no inline review threads; branch conflicts; no checks reported. |
| `HERMES_PYTHON=/Users/hazeion/projects/hermes-agent/.venv/bin/python HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/gateway/test_approve_deny_commands.py tests/gateway/relay/test_relay_interactive.py tests/gateway/test_api_server_runs.py tests/gateway/test_tui_approval_redaction.py` | macOS, sandboxed workspace | Environment-limited | 27 passed; 12 mounted Runs API tests failed because the sandbox rejected loopback socket binding with `PermissionError: [Errno 1]`. |
| `HERMES_PYTHON=/Users/hazeion/projects/hermes-agent/.venv/bin/python HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh tests/gateway/test_api_server_runs.py` | macOS, host execution | Pass | 16 passed; confirms the sandbox-only socket failures are not a current-main regression. |
| New exact-request core and Relay regression tests | macOS, sandboxed workspace | Expected fail | Three tests failed on pristine current-main behavior: the core rejected the new `request_id` argument/import, and Relay resolved the older FIFO entry instead of the displayed second request. |

### Test discussion and approval

- User questions and decisions: Approved the proposed focused, safety,
  compatibility, full-suite, and documentation checks.
- Accepted coverage gaps:
  - Platform network APIs remain behind the repository's established test
    doubles.
  - No screenshot comparison is required because approval controls, layout,
    and wording do not change.
  - Third-party legacy clients are represented by documented no-ID protocol
    compatibility tests rather than tested individually.
- Approved at: 2026-07-30, user response: "yes".

## Implementation record

### Changes

- The approval queue now assigns a validated request ID to every entry and can
  resolve one exact entry without disturbing its siblings. Typed commands and
  callers that omit the ID retain FIFO behavior.
- Gateway notifications carry the request ID through Relay and the current
  native messaging adapters. Each adapter keeps its existing authorization
  gate ahead of resolution, uses an ID short enough for Telegram callback data,
  and fails closed when a displayed request has expired.
- The Runs API and TUI/Desktop JSON-RPC protocols accept an optional request
  ID, reject ambiguous exact-plus-bulk requests, and return a stale-request
  error without falling back to FIFO.
- Runs API approval events advertise request-binding and structured-preview
  capabilities. The preview is fixed server-owned data with allowlisted risk
  labels; it does not echo commands, paths, plugin text, or credentials.
- TUI and Desktop client state round-trip the request ID without changing
  approval controls, choices, layout, or copy.
- Desktop native notifications capture the immutable request ID in the
  notification action itself. Late Desktop and TUI responses clear client
  state only when the completed response still matches the visible request.
- Relay consumes malformed, unknown, expired, and replayed structured
  responses without falling back to typed FIFO denial. Matrix retains each
  same-session event-to-request mapping until that event is resolved or
  expires.
- Runs API status transitions share one re-entrant lock. An older approval
  response or tool-progress callback cannot overwrite a newer
  `waiting_for_approval` transition.
- Regression coverage now exercises exact second-request selection, stale and
  invalid IDs, legacy no-ID FIFO behavior, authorization order, redaction,
  sibling-pending run state, structured-response replay, late client
  completions, same-session Matrix prompts, and transport-specific request
  propagation.

### Deviations and decisions

- The request-ID grammar is deliberately limited to 48 transport-safe
  characters because Telegram's 64-byte callback-data limit is the tightest
  native transport in scope.
- The existing adapter-local authorization checks were preserved. The stale
  PR's removed global base-adapter authorization seam was not restored because
  current main has explicit platform-specific authorization behavior.
- The branch was refreshed from `upstream/main@1fd7548b4` to
  `upstream/main@8defb9fd6` after implementation. The two intervening upstream
  commits changed only
  `apps/desktop/src/app/chat/composer/status-stack/index.tsx`, outside this
  slice, and the uncommitted rewrite applied without conflict.
- TUI and Desktop still render one approval prompt per session, as they did
  before this slice. Exact binding prevents a visible prompt from resolving
  the wrong queue entry or clearing a newer prompt, but an older sibling is not
  automatically resurfaced. A true multi-prompt UI remains a separate product
  change.

## Verification

### Focused checks

| Command or action | Environment | Exit/result | Pass/fail/skip counts | Notes or artifacts |
| --- | --- | --- | --- | --- |
| Core queue and Relay request-binding suites | macOS worktree | Pass | 25 passed | Includes exact second-entry selection, stale-ID no-op, unique IDs, Relay state binding, and legacy no-ID FIFO. |
| TUI gateway exact-request cases | macOS worktree | Pass | 2 passed | Exact selection, stale failure, exact-plus-bulk rejection, and compatibility behavior were also covered by the later full gateway file. |
| Native adapter approval, authorization, and redaction suite | macOS host execution | Pass | 227 passed | Thirteen QQ, WhatsApp, Slack, Telegram, Feishu, Teams, Discord, and Matrix files. |
| Runs API, queue-command, Relay, and Matrix suites | macOS host execution | Pass | 142 passed | Includes mounted two-pending-request behavior, safe preview, capability flags, stale IDs, legacy FIFO, replay consumption, same-session Matrix bindings, and serialized response/progress status races. |
| Desktop focused post-review suites | Node/Vitest on macOS | Pass | 35 passed | Exact component response, immutable native-notification action binding, late completion, and newer-prompt preservation. |
| TUI focused post-review suites | Node/Vitest on macOS | Pass | 98 passed | Event propagation plus exact guarded clearing for matching, stale, and legacy prompts. |
| TUI and Desktop type checks | Node/TypeScript on macOS | Pass | 2 commands passed | No TypeScript errors. |
| TUI lint | Node/ESLint on macOS | Pass | 0 errors | Clean. |
| Desktop lint | Node/ESLint on macOS | Pass with warnings | 0 errors, 74 warnings | Warnings are pre-existing; no new lint errors. |
| Desktop full UI suite | Node/Vitest on macOS host execution | Pass | 3,994 passed, 2 skipped | 428 files passed and 1 skipped. Sequential host rerun avoided the timing failures seen under concurrent load. |
| TUI full UI suite | Node/Vitest on macOS host execution | Pass | 1,439 passed, 1 skipped | 134 files passed. Sequential host rerun avoided sandbox watcher exhaustion. |
| `git diff --check` | macOS worktree | Pass | 0 whitespace errors | Run repeatedly after implementation. |
| `scripts/run_tests.sh tests/tools/test_approval.py` | macOS worktree | Mixed, unrelated baseline issue | 89 passed, 1 failed | Existing untouched test assumes mocked `/tmp` remains canonical on macOS; implementation diff does not touch dangerous-command detection or this test. |

### Full suite

| Command or action | Environment | Exit/result | Pass/fail/skip counts | Notes |
| --- | --- | --- | --- | --- |
| `HERMES_PYTHON=/Users/hazeion/projects/hermes-agent/.venv/bin/python HERMES_TEST_FILE_RETRIES=0 scripts/run_tests.sh` | macOS host execution, 16 workers | Mixed | 22,813 passed, 24 failed across 16 files | All changed feature suites passed. The failures are in untouched shutdown/systemd, provider, update, monitoring, service, macOS temp-path, computer-use, file/performance, execution-flag, Modal, audio, and transcription tests. Heavy parallelism also triggered platform-specific and timing failures. Exact failing files are retained below for audit. |

Full-run failing files:

- `tests/gateway/test_shutdown_forensics.py`
- `tests/gateway/test_systemd_notify.py`
- `tests/hermes_cli/test_runtime_provider_resolution.py`
- `tests/hermes_cli/test_gateway_service.py`
- `tests/hermes_cli/test_update_eol_churn.py`
- `tests/monitoring/test_otlp_exporter.py`
- `tests/hermes_cli/test_service_manager.py`
- `tests/tools/test_approval.py`
- `tests/tools/test_computer_use.py`
- `tests/tools/test_file_sync_perf.py`
- `tests/tools/test_file_tools.py`
- `tests/tools/test_execution_flag_detection.py`
- `tests/tools/test_modal_snapshot_isolation.py`
- `tests/tools/test_tts_command_providers.py`
- `tests/tools/test_transcription_tools.py`
- `tests/tools/test_voice_mode.py`

No failing test file is modified by this branch. The one failure in the
approval subsystem was rerun separately and remained confined to an untouched
dangerous-`rm` macOS path assertion; all 89 sibling tests passed.

### Rendered or manual behavior

- Not applicable unless implementation changes rendered output.

## Adversarial review

### Round 1

Two independent, read-only reviewers examined the implementation and tests:

- Security/reliability reviewer: changes requested.
  - Desktop native notification actions looked up mutable session state instead
    of carrying the request ID captured when the notification was created.
  - Replayed or expired Relay structured responses could fall through to a
    typed FIFO `/deny`.
  - Late Desktop/TUI exact-response completions could clear a newer prompt.
  - An older Runs API response could race with a newer approval notification
    and overwrite `waiting_for_approval` with `running`.
- Compatibility/integration reviewer: changes requested.
  - Confirmed the Desktop native-action and late-completion issues.
  - Found that a second same-session Matrix prompt removed the first event's
    request mapping, making the still-visible older reaction inert.

All five findings were accepted. The implementation now carries immutable
Desktop action IDs, consumes invalid structured Relay responses without FIFO
fallback, guards client clearing by exact ID, serializes Runs API status
transitions, and preserves independent Matrix event bindings. Dedicated
regressions were added for every finding.

### Round 2

The same two reviewers re-examined the corrected slice on
`upstream/main@8defb9fd6`:

- Compatibility/integration reviewer: approved. All eight acceptance criteria
  passed, all round-one findings were resolved, and no new P0-P2 issue was
  found.
- Security/reliability reviewer: changes requested. The reviewer found one
  additional P2 status race: a tool-progress callback could snapshot
  `running`, then overwrite a newer `waiting_for_approval` transition because
  that read/modify/write path did not share the approval status lock.

The P2 was accepted. All run-status writes now use a shared
`threading.RLock`, and the tool-progress callback holds it across the current
status lookup and update. A deterministic barrier regression reproduces the
old interleaving and verifies that the newer approval wait wins. The complete
affected Python set passes with 142 tests.

### Round 3

Both reviewers approved the final slice on `upstream/main@8defb9fd6`:

- Security/reliability reviewer: approved with no remaining P0-P2 findings.
  The reviewer confirmed the shared `RLock`, queue-sampling critical section,
  tool-progress critical section, lock ordering, and deterministic regression.
- Compatibility/integration reviewer: approved with no P0-P2 findings. The
  reviewer confirmed that re-entrancy is intentional, no deadlock path was
  introduced, and AC-1 through AC-8 still pass.

Independent reviewer reruns included 14 focused Runs race, Relay, and Matrix
tests plus a clean `git diff --check`.

## Documentation updates

- Roadmap: No repository roadmap exists.
- Changelog: No repository changelog exists.
- Architecture/operator docs: Updated Runs API developer and user guides plus
  the TUI protocol README with capability detection, request-ID round trips,
  stale-ID behavior, safe preview fields, and legacy FIFO compatibility.
- Project/session notes: This review log is the slice record.
- Documentation verification: Diff and link/path inspection complete; no
  generated documentation build is configured for these Markdown-only edits.

## Publication gate

- Proposed files: 48 total, split into two commits.
  - Contributor-authored core/native commit:
    `tools/approval.py`, `gateway/run.py`,
    `gateway/platforms/qqbot/adapter.py`,
    `gateway/platforms/whatsapp_cloud.py`,
    `plugins/platforms/discord/adapter.py`,
    `plugins/platforms/feishu/adapter.py`,
    `plugins/platforms/matrix/adapter.py`,
    `plugins/platforms/slack/adapter.py`,
    `plugins/platforms/teams/adapter.py`,
    `plugins/platforms/telegram/adapter.py`,
    `tests/gateway/test_approve_deny_commands.py`,
    `tests/gateway/test_discord_component_auth.py`,
    `tests/gateway/test_feishu_approval_buttons.py`,
    `tests/gateway/test_matrix_exec_approval.py`,
    `tests/gateway/test_qqbot.py`,
    `tests/gateway/test_slack_approval_buttons.py`,
    `tests/gateway/test_teams.py`,
    `tests/gateway/test_telegram_approval_buttons.py`, and
    `tests/gateway/test_whatsapp_cloud.py`.
  - Current-main Relay/API/client/docs commit:
    `gateway/platforms/api_server.py`, `gateway/relay/adapter.py`,
    `tests/gateway/relay/test_relay_interactive.py`,
    `tests/gateway/test_api_server.py`,
    `tests/gateway/test_api_server_runs.py`,
    `tests/test_tui_gateway_server.py`,
    `tui_gateway/methods_prompt.py`,
    `apps/desktop/electron/main.ts`,
    `apps/desktop/src/app/contrib/hooks/use-desktop-integrations.ts`,
    `apps/desktop/src/app/session/hooks/use-message-stream/gateway-event.ts`,
    `apps/desktop/src/components/assistant-ui/tool/approval.test.tsx`,
    `apps/desktop/src/components/assistant-ui/tool/approval.tsx`,
    `apps/desktop/src/global.d.ts`,
    `apps/desktop/src/store/native-notifications.test.ts`,
    `apps/desktop/src/store/native-notifications.ts`,
    `apps/desktop/src/store/prompts.test.ts`,
    `apps/desktop/src/store/prompts.ts`, `ui-tui/README.md`,
    `ui-tui/src/__tests__/createGatewayEventHandler.test.ts`,
    `ui-tui/src/__tests__/overlayStore.test.ts`,
    `ui-tui/src/app/createGatewayEventHandler.ts`,
    `ui-tui/src/app/overlayStore.ts`,
    `ui-tui/src/app/useInputHandlers.ts`,
    `ui-tui/src/app/useMainApp.ts`, `ui-tui/src/gatewayTypes.ts`,
    `ui-tui/src/types.ts`,
    `website/docs/developer-guide/programmatic-integration.md`,
    `website/docs/user-guide/features/api-server.md`, and
    `reviews/2026-07-30-approval-request-binding.md`.
- Branch and base:
  `codex/http-approval-request-binding-rewrite` on
  `upstream/main@8defb9fd6`.
- Commit messages:
  - `fix(approval): bind interactive approvals to exact requests`, authored by
    `mr.Shu <mr@shu.io>` and committed by the current maintainer.
  - `fix(approval): complete request binding across current clients`.
- PR title: `fix(approval): bind interactive responses to exact requests`.
- PR summary: Exact request IDs now round-trip through every current
  interactive transport. Stale IDs fail closed, no-ID callers retain FIFO, and
  Runs clients get a bounded structured preview plus capability flags.
- Unresolved risks:
  - TUI/Desktop remain single-prompt surfaces and do not automatically
    resurface an older sibling.
  - Legacy no-ID overlaps remain intentionally FIFO.
  - Real platform networks are covered through established test doubles.
  - The broad Python run retains 24 failures in 16 untouched files; all changed
    feature suites pass.
- User authorization and scope: Approved the exact 48-file publication packet,
  both commit identities and messages, the humanized PR description and
  reviewer response, and the force-with-lease update on 2026-07-30.
- Commit hash:
  - Contributor-authored commit:
    `611475bcab9c57b9ef594903c52a46eb7f92c557`.
  - Current-client integration commit: this review log is included in that
    commit; its hash is reported in the publication outcome.
- Ready PR URL: Existing PR #68080 will be updated only after approval.

## Outcome review

- Classification: Approved by both independent reviewers; ready for the
  publication approval gate.
- Acceptance criteria summary: AC-1 through AC-8 pass in implementation tests
  and both final reviewer verdicts.
- Potential bugs or untested paths: Real platform networks remain represented
  by established test doubles. TUI/Desktop intentionally remain single-prompt
  surfaces and do not automatically resurface an older sibling.
- Remaining reviewer dissent: None.
- Compatibility/migration/rollback concerns: No migration planned; legacy FIFO
  compatibility is tested and remains an acceptance criterion.
- User decision: Approved publication and the existing PR rewrite.
- Next slice authorized: No.
