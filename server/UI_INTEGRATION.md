# Dashboard integration contract

Base URL: `http://localhost:8000/api/v1` in local development.

## Authentication

1. `POST /auth/login` with `{email,password}`.
2. Send `Authorization: Bearer <access_token>` on every protected request.
3. Customer users are automatically scoped to their assigned company.
4. Admin calls to tenant resources must add `X-Company-ID: <company_id>`.
5. Refresh with `POST /auth/refresh`; clear local tokens after `/auth/logout`.

The dashboard should never send a company ID for a customer user or rely on a
client-side role gate as authorization. The API enforces both.

## Agent runs

```text
POST /agent-runs                    create queued run
POST /agent-runs/{id}/start         start background execution
GET  /agent-runs/{id}               poll status
GET  /agent-runs/{id}/events        poll structured progress
GET  /agent-runs/{id}/logs          poll model/tool logs
POST /agent-runs/{id}/cancel        request real process termination
POST /agent-runs/{id}/retry         create a new queued attempt
```

Statuses: `queued`, `running`, `succeeded`, `failed`, `cancelled`.
Most domain actions such as Company Brain build and lead-scan start create and
start runs automatically; use the returned run ID to drive progress UI.

## Approval boundary

Generated outreach is `pending_approval`. The dashboard may edit it with
`PATCH /outreach/messages/{id}`. Every edit increments `revision` and clears
approval. Only after `POST .../{id}/approve` may the dashboard call
`create-draft` or `send`. A repeated delivery request for the same approved
revision returns the existing provider result instead of sending twice.

Draft should be the default dashboard action. Approved direct send should be a
separate, explicit confirmation.

## Uploads and exports

- Upload documents as multipart fields `document_type` and `file`.
- `POST /documents/{id}/process` returns an agent run.
- Export creation is synchronous for MVP and returns an export ID.
- Download with authenticated `GET /exports/{id}/download`.

## Error handling

- `401`: log in or refresh.
- `403`: role or tenant violation; never retry with a different company ID.
- `409`: state-machine or safety gate (approval, send window, do-not-contact).
- `422`: invalid request or deterministic QA failure.
- `429`: daily sending cap.
- `502/503`: provider or deployment configuration unavailable.

Use `/openapi.json` as the source for generated TypeScript API types.

