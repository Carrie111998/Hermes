# TODO

## `server/config.yaml.example` carries one deployment's values

The image seeds `$HERMES_HOME/config.yaml` from this file when the volume has
none, so a fresh volume now boots correctly. But the seed is the same template
the repo ships, which names `agent-rota.fly.dev` in `cors_origins` — a second
deployment gets that origin until an operator edits the file. Fine while there
is one deployment; split the template into generic defaults plus a per-deploy
overlay before there are two.

## Admin password recovery is a manual operator task

`auth_mode: local` has no system-mail sender, so `POST
/api/v1/auth/password-reset/request` cannot deliver its token, and the endpoint
is unauthenticated — returning the token in the response would hand account
takeover to anyone who knows an admin email. In production the token is
therefore issued and withheld (`server/auth.py:243`).

Keep `INTERFAZE_BOOTSTRAP_ADMIN_PASSWORD` in the password manager: it is the
only way in. If it is lost, recovery means computing a new hash and updating the
row by hand:

```bash
uv run python -c "from server.auth import hash_password; print(hash_password('<new password>'))"
```

then, in the Supabase SQL editor:

```sql
update users set password_hash = '<hash>', updated_at = extract(epoch from now())
where role = 'admin';
```

Worth replacing with real reset emails once a transactional sender (Resend,
Postmark, SES) exists — that is the actual fix, not a bigger workaround.

## Migrate the public base URL to `agent.tugrap.dev`

Production currently runs on the Fly-issued domain:

```
INTERFAZE_PUBLIC_BASE_URL=https://agent-rota.fly.dev
```

Target: `https://agent.tugrap.dev`.

**Do this before the first outbound email goes out.** `INTERFAZE_PUBLIC_BASE_URL`
is embedded as an absolute URL into every unsubscribe link
(`server/compliance.py:36`). Links already delivered to recipients keep pointing
at the old host forever — if `agent-rota.fly.dev` ever stops answering, those
opt-out links 404, which is a KVKK/CAN-SPAM violation and not something a later
fix can reach. A `fly.dev` link also reads as throwaway infrastructure to spam
filters, so deliverability improves on the branded domain.

Steps:

1. `fly certs add agent.tugrap.dev` and add the A/AAAA records it prints.
2. `fly secrets set INTERFAZE_PUBLIC_BASE_URL="https://agent.tugrap.dev"`.
3. Add the new callback URLs to the Google and Microsoft OAuth apps — the
   `redirect_uri` is derived from this same value (`server/routes/oauth.py:116`),
   and a mismatch fails the authorization with `redirect_uri_mismatch`:
   - `https://agent.tugrap.dev/api/v1/integrations/email/oauth/google/callback`
   - `https://agent.tugrap.dev/api/v1/integrations/email/oauth/microsoft/callback`
4. Keep the `agent-rota.fly.dev` domain resolving afterwards, so unsubscribe
   links mailed before the cutover still work.

To make step 3 a no-op later, register **both** hosts' callback URLs when first
creating the OAuth apps — Google and Microsoft both accept multiple redirect
URIs, and the unused ones are harmless.
