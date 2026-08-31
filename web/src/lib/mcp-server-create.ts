import type { McpHttpAuth, McpServerCreate, McpServiceAccountConfig } from "@/lib/api";

export type McpTransport = "http" | "stdio";

export interface McpServerDraft {
  name: string;
  transport: McpTransport;
  url: string;
  httpAuth: McpHttpAuth;
  bearerToken: string;
  command: string;
  args: string;
  env: string;
  // Service account (M2M OAuth) — non-secret fields only.
  saTokenUrl: string;
  saClientId: string;
  saUsername: string;
  saPasswordEnv: string;
  saScope: string;
  saClientSecretEnv: string;
}

export function emptyMcpServerDraft(): McpServerDraft {
  return {
    name: "",
    transport: "http",
    url: "",
    httpAuth: "none",
    bearerToken: "",
    command: "",
    args: "",
    env: "",
    saTokenUrl: "",
    saClientId: "",
    saUsername: "",
    saPasswordEnv: "",
    saScope: "",
    saClientSecretEnv: "",
  };
}

function parseArgs(raw: string): string[] {
  return raw
    .split(/[\s,]+/)
    .map((value) => value.trim())
    .filter(Boolean);
}

function parseEnv(raw: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const rawLine of raw.split("\n")) {
    const line = rawLine.trim();
    if (!line) continue;
    const separator = line.indexOf("=");
    if (separator === -1) continue;
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    if (key) env[key] = value;
  }
  return env;
}

export function buildMcpServerCreate(draft: McpServerDraft): McpServerCreate {
  const name = draft.name.trim();
  if (!name) throw new Error("Name required");

  if (draft.transport === "http") {
    const url = draft.url.trim();
    if (!url) throw new Error("URL required");
    if (draft.httpAuth === "header" && !draft.bearerToken.trim()) {
      throw new Error("Bearer token required");
    }
    if (draft.httpAuth === "service_account") {
      const tokenUrl = draft.saTokenUrl.trim();
      if (!tokenUrl) throw new Error("Token URL required");
      // The token request carries the service-account password, so refuse a
      // plaintext endpoint here rather than letting the backend reject it later.
      if (!tokenUrl.startsWith("https://")) {
        throw new Error("Token URL must be an https:// URL");
      }
      if (!draft.saClientId.trim()) throw new Error("Client ID required");
      if (!draft.saUsername.trim()) throw new Error("Username required");
      if (!draft.saPasswordEnv.trim()) throw new Error("Password env-var name required");
    }

    const server: McpServerCreate = { name, url };
    if (draft.httpAuth !== "none") server.auth = draft.httpAuth;
    if (draft.httpAuth === "header") {
      server.bearer_token = draft.bearerToken;
    }
    if (draft.httpAuth === "service_account") {
      const sa: McpServiceAccountConfig = {
        // The form collects Authentik service-account fields, so it states
        // that strategy outright instead of leaving it to be inferred.
        grant_type: "authentik_app_password",
        token_url: draft.saTokenUrl.trim(),
        client_id: draft.saClientId.trim(),
        username: draft.saUsername.trim(),
        password_env: draft.saPasswordEnv.trim(),
      };
      if (draft.saScope.trim()) sa.scope = draft.saScope.trim();
      if (draft.saClientSecretEnv.trim()) sa.client_secret_env = draft.saClientSecretEnv.trim();
      server.service_account = sa;
    }
    return server;
  }

  const command = draft.command.trim();
  if (!command) throw new Error("Command required");

  const server: McpServerCreate = { name, command };
  const args = parseArgs(draft.args);
  if (args.length) server.args = args;
  const env = parseEnv(draft.env);
  if (Object.keys(env).length) server.env = env;
  return server;
}
