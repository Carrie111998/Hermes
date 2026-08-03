# SDK development

Use this reference for shipped Box applications. For a one-off Hermes task, use the CLI references instead.

## Start with the application

Inspect the repository for existing Box clients, `BOX_` configuration, token storage, webhook handlers, retry policy, and language conventions. Extend the existing integration instead of mixing SDK and raw REST without a reason.

## Choose an identity

| Identity | Use when |
| --- | --- |
| OAuth | each end user connects their own Box account |
| CCG | a server-side app needs its own service-account identity |
| `as_user` / managed user | an authorized enterprise app must act as a specified user |

OAuth follows the user's permissions and app scopes. CCG is a separate identity and needs folder collaboration unless enterprise capabilities explicitly provide another model.

## Use an official SDK

- [Python SDK Gen](https://github.com/box/box-python-sdk-gen)
- [Node SDK](https://github.com/box/box-node-sdk)
- [Other Box SDKs](https://developer.box.com/guides/tooling/sdks/)

Use the SDK matching the project language. Store credentials in the project's approved secret mechanism, not source control.

## Python CCG client

```python
import os

from box_sdk_gen import BoxCCGAuth, BoxClient, CCGConfig

auth = BoxCCGAuth(
    CCGConfig(
        client_id=os.environ["BOX_CLIENT_ID"],
        client_secret=os.environ["BOX_CLIENT_SECRET"],
        enterprise_id=os.environ["BOX_ENTERPRISE_ID"],
    )
)
client = BoxClient(auth)
me = client.users.get_user_me()
```

Use the generated SDK's file, folder, search, metadata, and webhook APIs rather than rebuilding HTTP and token refresh logic. Follow the installed SDK's current method names when implementing a concrete call.

## Build document-aware apps with Box AI

When an application must understand Box documents, call Box AI rather than downloading document bodies to an unrelated model service:

- ask for Q&A and summaries;
- structured extract for repeatable fields or a metadata template;
- extract for variable fields;
- text generation for output grounded in one Box file.

Expose plan/AI-unit implications in the product flow. Do not silently switch to external processing when Box AI is unavailable. Treat Box AI responses as potentially confidential application data.

## Webhooks and reliability

Verify webhook signatures, persist idempotency keys, fetch authoritative state after events, and keep retry/backoff policy explicit. Bound concurrent API calls and make retries safe before increasing throughput. See [Webhooks and events](webhooks-and-events.md).
