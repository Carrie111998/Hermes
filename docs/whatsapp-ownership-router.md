# WhatsApp inbound ownership router — Hermes side

## Status

Local prototype only. No production configuration, secret, bridge process, or WhatsApp message has been changed or sent.

## Configuration

The adapter accepts an optional `inbound_ownership_router` object in the WhatsApp platform configuration:

```yaml
inbound_ownership_router:
  url: https://<authorized-host>/functions/v1/classify-whatsapp-reply
  token_env: AUTOCRIA_OWNERSHIP_ROUTER_TOKEN
  timeout_ms: 1500
  agent_prefixes:
    - jeffersom
```

Do not place the secret itself in configuration. `token_env` names the environment variable that contains it.

`jeffersom` is always reserved internally. Values in `agent_prefixes` are additive and cannot remove that priority path.

When the object or URL is absent, existing WhatsApp behavior is preserved. When enabled, the adapter passes a non-secret configuration fingerprint into the bridge and verifies the same fingerprint through `/health`; a bridge with stale URL, token, timeout, or prefix configuration is not silently reused.

## Dispatch behavior

1. Existing Hermes echo suppression runs first.
2. Groups are not routed through this AutoCria ownership filter.
3. A word-bounded, case-insensitive `Jeffersom` prefix passes locally without a network request.
4. Other direct events send only bounded metadata to the classifier.
5. `jeffersom` passes to the Python adapter queue.
6. `autocria`, `ambiguous`, invalid responses, authentication/configuration errors, redirects, HTTP failures, network errors, and timeouts are dropped before an agent turn or transcript is created.

The metadata signal contains aliases, current/quoted message IDs, reply category, `hasText`, and `fromOwner`. It excludes the message body and text.

## Transport and secret handling

- Remote endpoints must use HTTPS.
- HTTP is allowed only for `127.0.0.1`, `localhost`, or IPv6 loopback during local development.
- Redirects are rejected.
- A missing token fails closed without transmitting metadata.
- The bearer token is not logged and is not returned by `/health`; only a fingerprint is exposed.
- Timeout is bounded to 1–10000 ms.

## Activation

Every production action requires a new specific authorization.

1. Deploy and verify the AutoCria migration and classifier first.
2. Set `AUTOCRIA_OWNERSHIP_ROUTER_TOKEN` in both authorized runtimes.
3. Add the configuration above using the verified HTTPS endpoint.
4. Restart only the affected WhatsApp bridge.
5. Verify bridge health, configuration fingerprint, and synthetic classification.
6. Run controlled real-message tests only after separate authorization.

Because the router fails closed, an unavailable classifier blocks non-prefixed direct messages. Explicit `Jeffersom` messages remain available as the local priority path.

## Rollback

1. Remove the `inbound_ownership_router` block or clear its URL. The adapter also clears inherited ownership-router environment variables before launching the bridge.
2. Restart only the affected bridge.
3. Verify `/health` reports an empty ownership-router fingerprint and ordinary direct messages queue normally.
4. Do not delete sessions, migration data, or logs during rollback.
