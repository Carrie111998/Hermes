# Grover pinned runtime integration

This package is private to the Grizzly pinned Hermes release. It adds one narrow
Telegram callback route: opaque `od:` tokens are resolved by the owner-only
loopback action service, and the server-issued SHADOW receipt is edited into the
same bot-owned card. The callback contains no action, decision ID, destination,
or authority. `act:` text is never treated as an executable callback.

## Mac profile preparation

On the Mac mini, from the reviewed pinned checkout:

```bash
python -m grover_runtime.profile_preparation prepare --dry-run
python -m grover_runtime.profile_preparation prepare
```

Preparation creates clean `grover-prod` and `grover-shadow` profile homes
without cloning `.env`, tokens, sessions, or state. Both Telegram adapters are
explicitly disabled. The production Telegram credential and the action-service
bridge token must be provisioned separately through the owner-only credential
lane; do not put either value in Git, command arguments, or this package.

Hermes profiles are runtime/configuration grouping, not a security boundary.
The two Mac runtimes therefore also require separate runtime directories,
least-privilege filesystem ownership, and the typed action-service capability
boundary; profile names alone provide no isolation.

`grover-shadow` is mechanically external-effect-free even if credentials are
accidentally injected: its Telegram adapter denies connect, polling, webhook,
send, callback answer/edit, and standalone-send paths, while the enabled
`grover-shadow-guard` plugin blocks LLM and tool execution. Missing credentials
and disabled Telegram configuration are defense-in-depth, not the boundary.
The callback client itself can only call the allowlisted SHADOW
callback/receipt routes; it cannot call the Control Plane execute API or
provider APIs.

## Cutover gate

Before cutover, verify all of the following:

1. the pinned checkout SHA and clean worktree;
2. focused callback and fail-closed authorization tests pass;
3. action service is loopback-only and its bridge token is an owner-only regular
   file under the `grover-prod` profile home;
4. the old Telegram consumer is stopped and polling is quiescent;
5. `grover-shadow` still resolves `gateway.platforms.telegram.enabled=false`.

Profile preparation intentionally exposes no cutover command. Cutover must use
`grover_runtime.operations.execute_cutover` with reviewed fixed-argv commands.
That gate proves exactly one known old consumer, transitions through zero
consumers, starts exactly one pinned new consumer, verifies release-bound
health and a provider-native Telegram delivery receipt, and invokes the
fail-closed rollback sequence on any post-stop failure. Do not start both
runtimes against the same bot token.

## Rollback

1. Disable `grover-prod` Telegram config and stop its gateway.
2. Keep both action-service ledger and receipt state for audit; never delete or
   rewrite receipts.
3. Restore the previous pinned release and previous production profile backup.
4. Start exactly one previous Telegram consumer.
5. Verify one inbound update, one ordinary outbound reply, and absence of
   duplicate polling before declaring rollback complete.

A failed action-service resolution is consumed and reports that nothing was
executed. If the SHADOW decision commits but Telegram receipt editing fails,
the buttons are removed and the durable receipt remains pending for the
supervised mirror retry; it does not replay the action.
