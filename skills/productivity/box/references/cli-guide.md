# Box CLI guide

Run Box commands through Hermes' `terminal` tool. Prefer the documented command in this skill over exploratory help calls. Use help only when a required option is absent here or the installed CLI rejects the syntax.

## Use one command runner

If `box` is on `PATH`, run the examples in this guide as written. If it is missing, install the CLI once in Hermes' user-local directory:

```bash
npm install --prefix "$HOME/.local/share/hermes-box-cli" @box/cli
```

Then run every example by replacing its leading `box` with:

```bash
npm exec --prefix "$HOME/.local/share/hermes-box-cli" -- box
```

For example:

```bash
npm exec --prefix "$HOME/.local/share/hermes-box-cli" -- \
  box users:get me --json --fields id,name,login
```

Do not attempt a global npm install, use `sudo`, change npm's global prefix, or change `PATH`. Keep the same runner for the whole task.

## Check identity and control output

```bash
command -v box
box --version
box users:get me --json --fields id,name,login
box folders:items 0 --json --max-items 20 --fields id,name,type
```

Use `--json` for machine-readable output and `--fields` to return only needed fields. Folder `0` is the current actor's root.

## Environments and actors

```bash
box configure:environments:list
box configure:environments:set-current <ENVIRONMENT_NAME>
box users:get me --json --fields id,name,login
box folders:items <FOLDER_ID> --as-user <USER_ID> --json --fields id,name,type
```

The CLI has one current environment. Confirm before switching it, then verify the actor. Use `--as-user` only when the configured app supports it and the user has asked for that actor.

## Pagination and search

```bash
box folders:items <FOLDER_ID> --json --max-items 100 --fields id,name,type
box search "quarterly review" --json --limit 20 --fields id,name,type,parent
box metadata-query enterprise_12345.contractTemplate <ANCESTOR_FOLDER_ID> \
  --query "status = :status" --query-param status=active --json
```

Paginate inventories fully before bulk work. Metadata queries require the template scope/key and an ancestor folder ID.

## REST escape hatch

When the CLI has no dedicated command, preserve its configured auth with `box request` and perform the ordinary requested operation. Do not stop to ask simply because this uses REST; read [REST API fallback](rest-api.md) for endpoint-specific bodies and headers.

```bash
box request /files/<FILE_ID> --json
box request /files/<FILE_ID> -X PUT --body '{"name":"renamed.pdf"}' --json
box request /folders -X POST --body '{"name":"New folder","parent":{"id":"0"}}' --json
```

Use direct REST only when the CLI is unavailable or application code genuinely needs direct REST.

## Batch inputs and mutations

Many Box CLI commands accept `--bulk-file-path` for CSV or JSON input. Use it only after inventorying the target set and confirming material writes. For ordered moves, version updates, and other recoverable mutations, keep an operation log and process serially. Use bounded concurrency in application SDK code only when its retry and rate-limit behavior is explicit.

## Confirmation rules

- Confirm before deletes, access changes, identity changes, broad moves, or an ambiguous target.
- Confirm the scope before an AI-unit-consuming bulk request.
- Do not pass `--yes` unless the user has already approved the exact operation.
