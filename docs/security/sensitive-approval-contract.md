# Sensitive Approval Contract

`tools.approval` provides a narrow primitive for high-risk approval flows. It
binds a pending request to a minted `request_id`, a non-empty authenticated
platform `user_id`, and immutable routing context such as platform, chat,
thread, and session.

This primitive is not a complete business-write authorization rail. The MCP
prepare/execute permission rail adds the higher-level binding for configured
MCP servers: proposal token digest, prepare/execute tool relation, preview and
canonical-result digests, short TTL, and exact Telegram user/chat/thread/session
context. Initial protected MCP executes are Telegram-only, because local
TUI/Desktop JSON-RPC has no authenticated human `user_id` and must fail closed
for sensitive approvals.

Logs must not include raw `request_id`, command payloads, proposal payloads, or
context identifiers. Use a short SHA-256 digest or another opaque correlation
value when correlation is needed.

Residual same-UID risk: an OS process with permission to inspect the gateway
process memory, environment, or `/proc` entries may steal the HMAC signer key.
The subprocess env scrubber removes configured attestation `key_env` values
from child process environments, but that is not a same-UID memory isolation
boundary. Production deployments that need to defend against this threat should
run the signer in an isolated process, account, container, or external service
with a narrower access boundary than the gateway worker.
