# Supabase deployment

1. Create a Supabase project and apply the SQL files in `migrations/` order.
   Existing Phase 2 databases apply `002_chat_sessions.sql` when enabling chat.
2. Configure the API process with `SUPABASE_DB_URL`, `SUPABASE_URL`,
   `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, and a Fernet
   `INTERFAZE_CREDENTIAL_KEY`.
3. Set `interfaze_server.auth_mode: supabase` in `~/.hermes/config.yaml`.
4. Provision customer users through `/api/v1/admin/users`; their first valid
   Supabase token binds its subject to the pre-provisioned email.
5. Start the service with `interfaze-api`.

The API remains the write boundary. RLS provides defense in depth for direct
Supabase reads and ensures customer tokens cannot cross company IDs.
