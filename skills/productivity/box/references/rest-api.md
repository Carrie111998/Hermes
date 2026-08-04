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

Keep secrets out of shell history and command output. Prefer the CLI or SDK for token refresh; this is a fallback pattern only.

The following `curl` example uses POSIX shell syntax. On Windows PowerShell, prefer the Box CLI or SDK instead of translating this secret-bearing request manually.

```bash
curl -sS -X POST https://api.box.com/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials" \
  -d "client_id=${BOX_CLIENT_ID}" \
  -d "client_secret=${BOX_CLIENT_SECRET}" \
  -d "box_subject_type=enterprise" \
  -d "box_subject_id=${BOX_ENTERPRISE_ID}"
```

Pass the returned access token only through a protected process boundary. Never echo, log, or commit it.

## Sources

- [Box API reference](https://developer.box.com/reference/)
- [Box Notes API: create a note from Markdown](https://developer.box.com/guides/box-notes/convert-markdown/)
- [Client credentials](https://developer.box.com/guides/authentication/client-credentials/)
