import { createHmac, timingSafeEqual } from "node:crypto";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(fileURLToPath(import.meta.url));
const DEFAULT_DASHBOARD_URL = "http://127.0.0.1:9119";
const DEFAULT_API_SERVER_URL = "http://127.0.0.1:9120";
const DEFAULT_API_SERVER_KEY = "";

const PANEL_USERS = {
  alice: {
    password: "alice123",
    label: "Alice / Brand Studio",
    workspace: "workspace-brand",
    principal: {
      tenant_id: "tenant-demo",
      workspace_id: "workspace-brand",
      project_id: "project-ultra",
      user_id: "user-alice",
      roles: "creator",
    },
  },
  bob: {
    password: "bob123",
    label: "Bob / Video Studio",
    workspace: "workspace-video",
    principal: {
      tenant_id: "tenant-demo",
      workspace_id: "workspace-video",
      project_id: "project-ultra",
      user_id: "user-bob",
      roles: "creator",
    },
  },
};

function dashboardUrl() {
  const raw = process.env.HERMES_DASHBOARD_URL || DEFAULT_DASHBOARD_URL;
  return raw.replace(/\/+$/, "");
}

function apiServerUrl() {
  const raw = process.env.HERMES_API_SERVER_URL || DEFAULT_API_SERVER_URL;
  return raw.replace(/\/+$/, "");
}

function apiServerKey() {
  return process.env.HERMES_API_SERVER_KEY || process.env.API_SERVER_KEY || DEFAULT_API_SERVER_KEY;
}

function authSecret() {
  return process.env.HERMES_PANEL_AUTH_SECRET || apiServerKey();
}

function base64url(input) {
  return Buffer.from(input).toString("base64url");
}

function publicUser(username, user) {
  return { username, label: user.label, workspace: user.workspace };
}

function tokenSignature(payload) {
  return createHmac("sha256", authSecret()).update(payload).digest("base64url");
}

function issuePanelToken(username) {
  const payload = base64url(JSON.stringify({ username, exp: Date.now() + 12 * 60 * 60 * 1000 }));
  return `${payload}.${tokenSignature(payload)}`;
}

function verifyPanelToken(raw) {
  const token = String(raw || "").replace(/^Bearer\s+/i, "").trim();
  const [payload, signature] = token.split(".");
  if (!payload || !signature) return null;
  const expected = tokenSignature(payload);
  const actualBuffer = Buffer.from(signature);
  const expectedBuffer = Buffer.from(expected);
  if (actualBuffer.length !== expectedBuffer.length || !timingSafeEqual(actualBuffer, expectedBuffer)) return null;
  try {
    const data = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    const username = String(data.username || "").toLowerCase();
    const user = PANEL_USERS[username];
    if (!user || Number(data.exp || 0) < Date.now()) return null;
    return { username, user };
  } catch {
    return null;
  }
}

function sendJson(res, status, payload) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(payload));
}

function readRequestJson(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 16_384) reject(new Error("request too large"));
    });
    req.on("error", reject);
    req.on("end", () => {
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch (error) {
        reject(error);
      }
    });
  });
}

function panelAuthPlugin() {
  return {
    name: "ultra-studio-panel-auth",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const path = String(req.url || "").split("?")[0];
        if (path === "/hermes-auth/login" && req.method === "POST") {
          try {
            const body = await readRequestJson(req);
            const username = String(body.username || "").trim().toLowerCase();
            const password = String(body.password || "");
            const user = PANEL_USERS[username];
            if (!user || user.password !== password) {
              return sendJson(res, 401, { error: "invalid_login" });
            }
            return sendJson(res, 200, {
              token: issuePanelToken(username),
              user: publicUser(username, user),
            });
          } catch (error) {
            return sendJson(res, 400, { error: error instanceof Error ? error.message : "invalid_request" });
          }
        }
        if (path === "/hermes-auth/me" && req.method === "GET") {
          const auth = verifyPanelToken(req.headers.authorization || req.headers["x-ultra-auth"]);
          if (!auth) return sendJson(res, 401, { error: "not_authenticated" });
          return sendJson(res, 200, { user: publicUser(auth.username, auth.user) });
        }
        if (path.startsWith("/hermes-api")) {
          if (!apiServerKey()) return sendJson(res, 500, { error: "missing_api_server_key" });
          const auth = verifyPanelToken(req.headers.authorization || req.headers["x-ultra-auth"]);
          if (!auth) return sendJson(res, 401, { error: "not_authenticated" });
          req.headers["x-ultra-user"] = auth.username;
        }
        next();
      });
    },
  };
}

function applyPrincipalHeaders(proxyReq, req) {
  const userKey = String(req.headers["x-ultra-user"] || "").trim().toLowerCase();
  const user = PANEL_USERS[userKey]?.principal;
  proxyReq.setHeader("Authorization", `Bearer ${apiServerKey()}`);
  if (!user) return;
  proxyReq.setHeader("X-Hermes-Tenant-Id", user.tenant_id);
  proxyReq.setHeader("X-Hermes-Workspace-Id", user.workspace_id);
  proxyReq.setHeader("X-Hermes-Project-Id", user.project_id);
  proxyReq.setHeader("X-Hermes-User-Id", user.user_id);
  proxyReq.setHeader("X-Hermes-Roles", user.roles);
}

async function fetchDashboardToken(url) {
  const response = await fetch(`${url}/chat`);
  if (!response.ok) {
    throw new Error(`Hermes dashboard ${url}/chat returned HTTP ${response.status}`);
  }

  const html = await response.text();
  const match = html.match(/window\.__HERMES_SESSION_TOKEN__="([^"]+)"/);
  if (!match) {
    throw new Error(
      "Hermes session token was not found. Start the dashboard on loopback with embedded chat enabled.",
    );
  }
  return match[1];
}

export default async function config({ command }) {
  const target = dashboardUrl();
  const apiTarget = apiServerUrl();
  const token = command === "serve" ? await fetchDashboardToken(target) : "";

  return {
    root,
    plugins: [panelAuthPlugin()],
    build: {
      outDir: "dist",
      emptyOutDir: true,
    },
    server: {
      host: "127.0.0.1",
      port: Number(process.env.HERMES_PANEL_PORT || "9131"),
      strictPort: false,
      proxy:
        command === "serve"
          ? {
              "/hermes/ws": {
                target,
                ws: true,
                changeOrigin: true,
                rewrite: () => `/api/ws?token=${encodeURIComponent(token)}`,
              },
              "/hermes/upload": {
                target,
                changeOrigin: true,
                headers: {
                  "X-Hermes-Session-Token": token,
                },
                rewrite: () => "/api/chat/uploads",
              },
              "/hermes/status": {
                target,
                changeOrigin: true,
                rewrite: () => "/api/status",
              },
              "/hermes-api": {
                target: apiTarget,
                changeOrigin: true,
                rewrite: (path) => path.replace(/^\/hermes-api/, ""),
                configure(proxy) {
                  proxy.on("proxyReq", applyPrincipalHeaders);
                },
              },
            }
          : undefined,
    },
  };
}
