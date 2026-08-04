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
| Preview flexible key-value fields without storing them | `ai:extract` |
| Extract metadata and store it on the file | `ai:extract-structured` |
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

## Extract and persist file metadata

Treat a request to extract metadata as a write workflow, not a request to merely show an AI response. Unless the user asks to preview the result only, use a compatible existing Box metadata template, then write the returned values onto each source file.

1. Resolve the template named by the user, or list templates and select the compatible existing one. Read its fields and inspect any metadata already attached to the file.
   ```bash
   box metadata-templates --json --fields templateKey,displayName,scope
   box metadata-templates:get <TEMPLATE_KEY> --scope enterprise --json
   box files:metadata:get <FILE_ID> --scope enterprise --template-key <TEMPLATE_KEY> --json
   ```
2. Extract against that template. Request only the target file IDs and keep the extraction output in the terminal result rather than downloading the source file.
   ```bash
   box ai:extract-structured --items=id=<FILE_ID>,type=file \
     --metadata-template="type=metadata_template,scope=enterprise,template_key=<TEMPLATE_KEY>" \
     --json
   ```
3. Convert the returned structured values to the template's field keys and types. Add a metadata instance when the file has none; otherwise replace the extracted fields. Do not write fields that are absent, null, or incompatible with the template.
   ```bash
   box files:metadata:add <FILE_ID> --scope enterprise --template-key <TEMPLATE_KEY> \
     --data "invoice_number=INV-001" --data "total=#1250.00" --json

   box files:metadata:update <FILE_ID> --scope enterprise --template-key <TEMPLATE_KEY> \
     --replace "invoice_number=INV-001" --replace "total=#1250.00" --json
   ```
4. Read the attached metadata back and report the file link, template, and fields written.
   ```bash
   box files:metadata:get <FILE_ID> --scope enterprise --template-key <TEMPLATE_KEY> --json
   ```

The extraction request authorizes the matching per-file metadata writes, so do not ask again before attaching or updating those values. Do ask before creating or changing an enterprise metadata template, cascading it across a folder, or applying extraction to a material batch. If no compatible template exists, explain that Box metadata requires a schema and ask whether the user wants to select or create one; do not store arbitrary extraction JSON in an unrelated field.

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
