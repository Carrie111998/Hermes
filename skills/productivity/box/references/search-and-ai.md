# Search, metadata, and Box AI

Use Box search and metadata before AI when they answer the request deterministically. For semantic understanding of Box-hosted files, use Box AI before downloading source bytes.

## Search and metadata queries

```bash
box search "invoice ACME" --json --limit 25 --fields id,name,type,parent
box metadata-query enterprise_12345.contractTemplate <ANCESTOR_FOLDER_ID> \
  --query "status = :status" --query-param status=active --json
```

Search only returns content visible to the current actor. Resolve IDs and confirm the actor before treating empty results as missing files.

## Select a Box AI operation

| Need | Command |
| --- | --- |
| Answer, summarize, or compare content | `ai:ask` |
| Extract variable key-value fields | `ai:extract` |
| Extract known fields or a metadata template | `ai:extract-structured` |
| Write or rewrite text grounded in one file | `ai:text-gen` |

```bash
box ai:ask --items=id=<FILE_ID>,type=file \
  --prompt "Summarize the renewal obligations and dates." --json

box ai:extract --items=id=<FILE_ID>,type=file \
  --prompt "invoice_number, vendor, total, due_date" --json

box ai:extract-structured --items=id=<FILE_ID>,type=file \
  --fields "key=invoice_number,type=string,description=Invoice number" \
  --fields "key=total,type=float,description=Invoice total" --json

box ai:text-gen --items=id=<FILE_ID>,type=file \
  --prompt "Draft a concise customer update based on this file." --json
```

`ai:text-gen` supports exactly one item. Use structured extraction when the schema is known and must be repeatable. Use a metadata template with `--metadata-template` when the Box template is the source of truth.

## Confidentiality and AI units

Box AI processes source files through Box's governed AI integration instead of downloading source bodies into Hermes' coding-model context. Box AI responses returned to Hermes can still contain confidential information. Do not claim that no third-party model provider is involved or that content can never be used for training; follow Box's current trust and plan documentation.

Before the first Box AI request, explain that the API must be enabled and calls consume AI units. For a material batch, state the file count and ask for confirmation. Do not promise a unit balance or per-call cost unless Box exposes it for the current account.

If Box AI is unavailable or out of units, offer existing metadata/search, a smaller sample, enabling units, or explicit approval for local/external analysis. Never silently fall back to downloading files for an external model.

## Scale

Use `--bulk-file-path` where the command supports it. For hundreds of files, inventory first, sample the schema, confirm unit-consuming scope, and use [Bulk operations](bulk-operations.md). For recurring, high-throughput extraction, evaluate Box Extract rather than simulating a folder-wide workflow through repeated downloads.

## Sources

- [Box AI API](https://developer.box.com/ai/box-ai-api/)
- [Structured metadata extraction](https://developer.box.com/guides/box-ai/ai-tutorials/extract-metadata-structured/)
- [Box AI trust](https://www.box.com/ai/trust/)
- [AI units and plan access](https://support.box.com/hc/en-us/articles/45612941554835-Expanded-AI-API-Access-and-AI-Units-for-Business-Business-Plus-and-Enterprise-Plans)
