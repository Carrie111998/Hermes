# REST API fallback

Prefer `box request` when the CLI is installed because it reuses the configured Box identity. Use direct REST only when the CLI is unavailable or application code needs a raw endpoint that an SDK cannot cover.

## CLI request escape hatch

```bash
box request /files/<FILE_ID> --json
box request /files/<FILE_ID> -X PUT --body '{"name":"renamed.pdf"}' --json
box request /folders -X POST --body '{"name":"New folder","parent":{"id":"0"}}' --json
```

## Direct REST with CCG

Keep secrets out of shell history and command output. Prefer the CLI or SDK for token refresh; this is a fallback pattern only.

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
- [Client credentials](https://developer.box.com/guides/authentication/client-credentials/)
