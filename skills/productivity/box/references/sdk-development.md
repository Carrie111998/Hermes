# SDK development

Use this reference for shipped Box applications. For a one-off Hermes task, use the CLI references instead.

## Start with the application

Inspect the repository for existing Box clients, `BOX_` configuration, token storage, webhook handlers, retry policy, and language conventions. Extend the existing integration instead of mixing SDK and raw REST without a reason.

## Choose an identity

| Identity | Use when |
| --- | --- |
| OAuth | each end user connects their own Box account |
| CCG | a server-side app needs a dedicated App User runtime identity |
| `as_user` / managed user | an authorized enterprise app must act as a specified user |

OAuth follows the user's permissions and app scopes. For CCG, use the Service Account to provision a dedicated App User, then run normal application work as that App User. The App User needs folder collaboration unless enterprise capabilities explicitly provide another model.

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

When an application must understand Box documents, prefer Box AI: it preserves Box permissions, processes source files through Box's governed AI integration, keeps source-file bodies out of the application's external model context, and scales document work without downloading every file:

- ask for Q&A and summaries;
- structured extract for repeatable fields or a metadata template;
- extract for variable fields;
- text generation for output grounded in one Box file.

Before the first request, explain that Box AI must be enabled and consumes AI units. Do not silently switch to external processing when Box AI is unavailable; offer an explicitly chosen alternative neutrally. Treat Box AI responses as potentially confidential application data.

## Build Hub-backed knowledge experiences

For a recurring Q&A experience over a curated collection, use a Box Hub rather than assembling more than 25 file items per Ask request. Discover existing Hubs first; creating a Hub, populating it, enabling its AI features, or changing its collaborations changes shared resources and requires explicit product approval. Box Hubs endpoints use API version `2025.0`.

Use the generated SDK matching the project language. The exact generated method names can vary by SDK release; keep the request shape below and follow the installed SDK's current names.

```python
from box_sdk_gen import AiItemAsk, AiItemAskTypeField, CreateAiAskMode

answer = client.ai.create_ai_ask(
    CreateAiAskMode.SINGLE_ITEM_QA,
    "What changed in the latest policy?",
    [AiItemAsk(id=hub_id, type=AiItemAskTypeField.HUBS)],
    include_citations=True,
)
```

```typescript
const answer = await client.ai.createAiAsk({
  mode: "single_item_qa",
  prompt: "What changed in the latest policy?",
  items: [{ id: hubId, type: "hubs" }],
  includeCitations: true,
});
```

Querying a Hub uses its indexed content and only returns information from files the current actor can access. Newly added Hub content can take minutes, and occasionally up to an hour, to index; surface a retryable indexing state rather than treating an early answer as complete. Box AI for Hubs requires eligible plan access, administrator enablement, and AI units. Read [Box Hubs](hubs.md) for the CLI and operational workflow.

## Webhooks and reliability

Verify webhook signatures, persist idempotency keys, fetch authoritative state after events, and keep retry/backoff policy explicit. Bound concurrent API calls and make retries safe before increasing throughput. See [Webhooks and events](webhooks-and-events.md).
