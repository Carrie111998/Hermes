# Webhooks and events

Use webhooks for push notifications about a file or folder. Use Events API polling for catch-up, backfill, or a durable cursor.

## Create and inspect a webhook

```bash
box webhooks:list --json
box webhooks:create folder <FOLDER_ID> \
  --triggers FILE.UPLOADED,FILE.VERSION_UPLOADED \
  --address https://example.com/box/webhook --json
box events --json --limit 100
```

The current actor needs access to the target and the app needs appropriate scopes. Confirm the destination URL and event triggers before creating a webhook.

## Application handler contract

When implementing a shipped application:

1. Verify the Box signature before parsing or acting on the body.
2. Persist idempotency keys because deliveries can repeat.
3. Acknowledge quickly and process work asynchronously.
4. Fetch the current file or folder from Box; do not trust an event payload as the final state.
5. Persist the Events API cursor when polling.

Test a valid event, duplicate event, invalid signature, and restart/catch-up path.

## Sources

- [Box webhooks](https://developer.box.com/guides/webhooks/)
- [Events resource](https://developer.box.com/reference/resources/event/)
