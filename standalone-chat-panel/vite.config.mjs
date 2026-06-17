import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";
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

function panelRequestId(req) {
  const raw = String(req.headers["x-hermes-request-id"] || req.headers["x-request-id"] || "").trim();
  return /^[A-Za-z0-9_.:-]{1,96}$/.test(raw) ? raw : `panel_${randomUUID().replaceAll("-", "")}`;
}

function logPanelApiDecision(fields) {
  console.log("panel_api_decision", JSON.stringify(fields));
}

function panelApiPrefix(path) {
  if (path.startsWith("/panel-api")) return "/panel-api";
  if (path.startsWith("/hermes-api")) return "/hermes-api";
  return "";
}

function principalHeaders(user) {
  const principal = user.principal;
  return {
    "Authorization": `Bearer ${apiServerKey()}`,
    "X-Hermes-Tenant-Id": principal.tenant_id,
    "X-Hermes-Workspace-Id": principal.workspace_id,
    "X-Hermes-Project-Id": principal.project_id,
    "X-Hermes-User-Id": principal.user_id,
    "X-Hermes-Roles": principal.roles,
  };
}

function readRequestRaw(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > 10_000_000) {
        reject(new Error("request too large"));
        return;
      }
      chunks.push(chunk);
    });
    req.on("error", reject);
    req.on("end", () => resolve(Buffer.concat(chunks)));
  });
}

function panelErrorPayload(status, body, stage) {
  if (body && typeof body === "object" && !Array.isArray(body)) {
    const error = body.error && typeof body.error === "object" ? body.error : null;
    if (error) return { ...body, error: { ...error, stage, upstream_status: status } };
    return { error: { message: JSON.stringify(body), stage, upstream_status: status } };
  }
  return { error: { message: String(body || `HTTP ${status}`), stage, upstream_status: status } };
}

function safeJson(text) {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function writeUpstreamBody(res, upstream) {
  if (!upstream.body) {
    res.end();
    return;
  }
  const reader = upstream.body.getReader();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    res.write(Buffer.from(value));
  }
  res.end();
}

async function forwardPanelApi(req, res, prefix, auth) {
  const requestId = panelRequestId(req);
  const started = Date.now();
  if (!apiServerKey()) {
    logPanelApiDecision({ request_id: requestId, result: "denied", status: 500, stage: "panel_config", reason: "missing_api_server_key" });
    res.setHeader("X-Hermes-Request-Id", requestId);
    return sendJson(res, 500, { error: { message: "missing_api_server_key", stage: "panel_config" } });
  }
  if (!auth) {
    logPanelApiDecision({ request_id: requestId, result: "denied", status: 401, stage: "panel_auth", reason: "not_authenticated" });
    res.setHeader("X-Hermes-Request-Id", requestId);
    return sendJson(res, 401, { error: { message: "not_authenticated", stage: "panel_auth" } });
  }

  const method = String(req.method || "GET").toUpperCase();
  const upstreamPath = (String(req.url || "").replace(prefix, "") || "/");
  const upstreamUrl = `${apiServerUrl()}${upstreamPath}`;
  const headers = {
    ...principalHeaders(auth.user),
    "X-Hermes-Request-Id": requestId,
    "User-Agent": "ultra-studio-panel-bff",
  };
  const contentType = req.headers["content-type"];
  if (contentType) headers["Content-Type"] = String(contentType);

  try {
    const raw = method === "GET" || method === "HEAD" ? undefined : await readRequestRaw(req);
    logPanelApiDecision({
      request_id: requestId,
      result: "started",
      method,
      upstream_path: upstreamPath,
      user: auth.username,
      workspace: auth.user.workspace,
    });
    const upstream = await fetch(upstreamUrl, {
      method,
      headers,
      body: raw && raw.length ? raw : undefined,
    });
    res.statusCode = upstream.status;
    for (const header of ["content-type", "cache-control", "x-accel-buffering", "x-hermes-session-id", "x-hermes-session-key"]) {
      const value = upstream.headers.get(header);
      if (value) res.setHeader(header, value);
    }
    res.setHeader("X-Hermes-Request-Id", upstream.headers.get("x-hermes-request-id") || requestId);
    if (!upstream.ok) {
      const text = await upstream.text();
      const parsed = text ? safeJson(text) : "";
      logPanelApiDecision({
        request_id: requestId,
        result: "denied",
        status: upstream.status,
        method,
        upstream_path: upstreamPath,
        user: auth.username,
        duration_ms: Date.now() - started,
      });
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(panelErrorPayload(upstream.status, parsed, "api_server")));
      return;
    }
    await writeUpstreamBody(res, upstream);
    logPanelApiDecision({
      request_id: requestId,
      result: "completed",
      status: upstream.status,
      method,
      upstream_path: upstreamPath,
      user: auth.username,
      duration_ms: Date.now() - started,
    });
  } catch (error) {
    logPanelApiDecision({
      request_id: requestId,
      result: "failed",
      status: 502,
      method,
      upstream_path: upstreamPath,
      user: auth.username,
      reason: error instanceof Error ? error.message : "proxy_failed",
      duration_ms: Date.now() - started,
    });
    if (!res.headersSent) {
      res.setHeader("X-Hermes-Request-Id", requestId);
      sendJson(res, 502, {
        error: {
          message: error instanceof Error ? error.message : "proxy_failed",
          stage: "panel_proxy",
        },
      });
    } else {
      res.end();
    }
  }
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
        const apiPrefix = panelApiPrefix(path);
        if (apiPrefix) {
          const auth = verifyPanelToken(req.headers.authorization || req.headers["x-ultra-auth"]);
          return forwardPanelApi(req, res, apiPrefix, auth);
        }
        if (path === "/hermes/upload") {
          const auth = verifyPanelToken(req.headers.authorization || req.headers["x-ultra-auth"]);
          if (!auth) return sendJson(res, 401, { error: "not_authenticated" });
        }
        next();
      });
    },
  };
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
            }
          : undefined,
    },
  };
}
