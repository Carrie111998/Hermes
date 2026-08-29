import { describe, expect, it } from "vitest";

import { buildMcpServerCreate, emptyMcpServerDraft } from "./mcp-server-create";

describe("buildMcpServerCreate", () => {
  it("builds an HTTP Bearer request without stdio fields", () => {
    const server = buildMcpServerCreate({
      ...emptyMcpServerDraft(),
      name: " Linear ",
      url: " https://mcp.linear.app/mcp ",
      httpAuth: "header",
      bearerToken: "Bearer secret-token",
      command: "ignored",
      args: "--ignored",
      env: "IGNORED=value",
    });

    expect(server).toEqual({
      name: "Linear",
      url: "https://mcp.linear.app/mcp",
      auth: "header",
      bearer_token: "Bearer secret-token",
    });
  });

  it("builds OAuth and unauthenticated HTTP requests without a token", () => {
    expect(
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "oauth",
        url: "https://example.com/mcp",
        httpAuth: "oauth",
      }),
    ).toEqual({
      name: "oauth",
      url: "https://example.com/mcp",
      auth: "oauth",
    });

    expect(
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "public",
        url: "https://example.com/mcp",
      }),
    ).toEqual({
      name: "public",
      url: "https://example.com/mcp",
    });
  });

  it("parses stdio arguments and environment assignments", () => {
    const server = buildMcpServerCreate({
      ...emptyMcpServerDraft(),
      name: "local",
      transport: "stdio",
      command: " uvx ",
      args: "mcp-server, --debug",
      env: "API_KEY=secret\nURL=https://example.com?a=b\nINVALID",
    });

    expect(server).toEqual({
      name: "local",
      command: "uvx",
      args: ["mcp-server", "--debug"],
      env: {
        API_KEY: "secret",
        URL: "https://example.com?a=b",
      },
    });
  });

  it("builds a service_account request with non-secret fields only", () => {
    const server = buildMcpServerCreate({
      ...emptyMcpServerDraft(),
      name: "toolhive",
      url: "https://mcp.example/mcp",
      httpAuth: "service_account",
      saTokenUrl: " https://idp.example/o/toolhive/token/ ",
      saClientId: " toolhive ",
      saUsername: " svc-user ",
      saPasswordEnv: "MY_SERVICE_PASSWORD",
      saScope: "openid profile",
      saClientSecretEnv: "MY_CLIENT_SECRET",
    });

    expect(server).toEqual({
      name: "toolhive",
      url: "https://mcp.example/mcp",
      auth: "service_account",
      service_account: {
        grant_type: "authentik_app_password",
        token_url: "https://idp.example/o/toolhive/token/",
        client_id: "toolhive",
        username: "svc-user",
        password_env: "MY_SERVICE_PASSWORD",
        scope: "openid profile",
        client_secret_env: "MY_CLIENT_SECRET",
      },
    });
    // bearer_token must never appear in a service_account request
    expect("bearer_token" in server).toBe(false);
  });

  it("omits optional service_account fields when blank", () => {
    const server = buildMcpServerCreate({
      ...emptyMcpServerDraft(),
      name: "minimal",
      url: "https://mcp.example/mcp",
      httpAuth: "service_account",
      saTokenUrl: "https://idp.example/token/",
      saClientId: "client",
      saUsername: "user",
      saPasswordEnv: "PWD_ENV",
    });

    expect(server.service_account).toEqual({
      // Always emitted: the strategy is stated, never left to be inferred
      // from which fields the form happened to fill in.
      grant_type: "authentik_app_password",
      token_url: "https://idp.example/token/",
      client_id: "client",
      username: "user",
      password_env: "PWD_ENV",
    });
    expect(server.service_account && "scope" in server.service_account).toBe(false);
    expect(server.service_account && "client_secret_env" in server.service_account).toBe(false);
  });

  it("rejects a plaintext http:// token URL", () => {
    // The token request carries the service-account password, so a plaintext
    // endpoint must be refused before the request is ever built.
    expect(() =>
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "sa",
        url: "https://mcp.example/mcp",
        httpAuth: "service_account",
        saTokenUrl: "http://idp.example/token/",
        saClientId: "client",
        saUsername: "user",
        saPasswordEnv: "PWD_ENV",
      }),
    ).toThrow("https://");
  });

  it("rejects service_account with missing required fields", () => {
    const base = {
      ...emptyMcpServerDraft(),
      name: "sa",
      url: "https://mcp.example/mcp",
      httpAuth: "service_account" as const,
    };
    expect(() => buildMcpServerCreate(base)).toThrow("Token URL required");
    expect(() =>
      buildMcpServerCreate({ ...base, saTokenUrl: "https://idp.example/token/" }),
    ).toThrow("Client ID required");
    expect(() =>
      buildMcpServerCreate({
        ...base,
        saTokenUrl: "https://idp.example/token/",
        saClientId: "c",
      }),
    ).toThrow("Username required");
    expect(() =>
      buildMcpServerCreate({
        ...base,
        saTokenUrl: "https://idp.example/token/",
        saClientId: "c",
        saUsername: "u",
      }),
    ).toThrow("Password env-var name required");
  });

  it("rejects missing transport fields and Bearer tokens", () => {
    expect(() => buildMcpServerCreate(emptyMcpServerDraft())).toThrow(
      "Name required",
    );
    expect(() =>
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "remote",
      }),
    ).toThrow("URL required");
    expect(() =>
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "remote",
        url: "https://example.com/mcp",
        httpAuth: "header",
      }),
    ).toThrow("Bearer token required");
    expect(() =>
      buildMcpServerCreate({
        ...emptyMcpServerDraft(),
        name: "local",
        transport: "stdio",
      }),
    ).toThrow("Command required");
  });
});
