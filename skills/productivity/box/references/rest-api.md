# REST API fallback

Use `box request` to extend the CLI when it has no dedicated subcommand. It reuses the configured Box identity, so continue ordinary requested work without asking the user to choose a REST fallback. Confirm only for deletes, access or identity changes, broad or costly batches, or an ambiguous target or scope. Use direct REST only when the CLI is unavailable or application code needs a raw endpoint that an SDK cannot cover.

Using REST does not bypass Box metadata safety rules: inspect metadata instances and schemas first, require approval for enterprise-wide template changes, and retrieve and compare the metadata instance after every write. Never use a file description as an implicit metadata fallback.

## CLI request escape hatch

```bash
box request /files/<FILE_ID> --json
box request /files/<FILE_ID> -X PUT --body '{"name":"renamed.pdf"}' --json
box request /folders -X POST --body '{"name":"New folder","parent":{"id":"0"}}' --json
```

## Create a native Box Note

When asked to create a Box Note, create the native note with the Box Notes API; do not upload plain text with a `.boxnote` suffix. Use the intended parent folder (use `0` only when the user's target is unambiguously their root), then fetch the returned file to verify it:

```bash
box request /notes/convert -X POST \
  --header "box-version: 2026.0" \
  --body '{"content":"# Hello world\n\nhello world","content_format":"markdown","parent":{"id":"0"},"name":"hello-world"}' \
  --json
box files:get <RETURNED_FILE_ID> --json --fields id,name,type,parent
```

`content` is Markdown and is limited to 1 MB. Report the returned file ID and its normal Box file link.

## Direct REST with CCG

For normal CCG work, do not mint a Service Account token with `box_subject_type=enterprise`: that bypasses Hermes's dedicated App User permission boundary. Prefer `box request`, which uses the selected `hermes-agent` environment. If the CLI is unavailable, use an SDK client configured with the App User ID as shown in [SDK development](sdk-development.md). Reserve Service Account tokens for approved provisioning actions only. Never echo, log, or commit credentials or tokens.

## Sources

- [Box API reference](https://developer.box.com/reference/)
- [Box Notes API: create a note from Markdown](https://developer.box.com/guides/box-notes/convert-markdown/)
- [Client credentials](https://developer.box.com/guides/authentication/client-credentials/)
