# Troubleshooting

Capture the actor, object ID and type, exact command, status code, and safe error body before changing approach.

## First checks

```bash
box users:get me --json --fields id,name,login
box configure:environments:list
box files:get <FILE_ID> --json --fields id,name,parent
```

Confirm the current actor, object type, ID, collaboration, app scopes, and selected environment.

## Common failures

| Signal | Likely cause | Next action |
| --- | --- | --- |
| 401 or 403 | expired auth, missing scope, insufficient role | verify identity, reauthorize the app, and check folder role |
| 404 or empty search | wrong actor or unshared CCG folder | verify `users:get me`, then share the parent folder or select the correct environment |
| 409 | duplicate name, existing collaboration, metadata conflict | list the parent/template and reuse or rename deliberately |
| 429 | rate limit | honor `Retry-After`, retry the same request, and reduce batch rate |
| Box AI access error | feature disabled, plan/unit restriction, unsupported content | explain the limitation and offer metadata/search, a sample, units, or approved fallback |

Do not diagnose missing content until identity and access are verified. Do not silently change actors, broaden sharing, or download confidential source files as a workaround.
