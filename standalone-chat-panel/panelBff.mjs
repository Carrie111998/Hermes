import { createHash, randomUUID } from "node:crypto";
import { Buffer } from "node:buffer";
import { readBearerToken } from "./panelAuth.mjs";

const MAX_BODY_BYTES = 10_000_000;
const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";

export function registerPanelBff(server, options) {
  const bff = new PanelBff(options);
  server.middlewares.use((req, res, next) => {
    bff.handleHttp(req, res).then((handled) => {
      if (!handled) next();
    }).catch((error) => {
      sendJson(res, 500, { error: { message: error instanceof Error ? error.message : "panel_bff_failed" } });
    });
  });
  server.httpServer?.on("upgrade", (req, socket, head) => {
    if (!String(req.url || "").startsWith("/hermes/ws")) return;
    bff.handleWebSocket(req, socket, head);
  });
}

class PanelBff {
  constructor(options) {
    this.apiServerUrl = String(options.apiServerUrl || "http://127.0.0.1:9120").replace(/\/+$/, "");
    this.apiServerKey = String(options.apiServerKey || "");
    this.authStore = options.authStore;
    this.connections = new Set();
  }

  async handleHttp(req, res) {
    const url = new URL(req.url || "/", "http://panel.local");
    if (url.pathname === "/panel-auth/status") {
      return this.handleAuthStatus(res);
    }
    if (url.pathname === "/panel-auth/bootstrap" && req.method === "POST") {
      return this.handleBootstrap(req, res);
    }
    if (url.pathname === "/panel-auth/login" && req.method === "POST") {
      return this.handleLogin(req, res);
    }
    if (url.pathname === "/panel-auth/me") {
      return this.handleMe(req, res);
    }
    if (url.pathname === "/panel-auth/logout" && req.method === "POST") {
      return this.handleLogout(req, res);
    }
    if (url.pathname === "/hermes/upload" && req.method === "POST") {
      return this.handleUpload(req, res);
    }
    if (url.pathname.startsWith("/panel-api/") || url.pathname === "/panel-api") {
      return this.proxyPanelApi(req, res, "/panel-api");
    }
    return false;
  }

  handleAuthStatus(res) {
    sendJson(res, 200, {
      configured: Boolean(this.apiServerKey),
      api_server_url: this.apiServerUrl,
      auth_driver: "sqlite",
      user_count: this.authStore.userCount(),
      needs_bootstrap: this.authStore.userCount() === 0,
      signup_enabled: process.env.HERMES_PANEL_ALLOW_SIGNUP === "1",
    });
    return true;
  }

  async handleBootstrap(req, res) {
    if (this.authStore.userCount() > 0) {
      sendJson(res, 409, { error: { message: "bootstrap_already_completed" } });
      return true;
    }
    const body = await readJsonBody(req);
    try {
      const user = this.authStore.createUser(body);
      const login = this.authStore.login(body.username, body.password);
      logDecision("auth.bootstrap", "allowed", { username: user.username, workspace_id: user.workspace_id });
      sendJson(res, 201, { user: login.user, token: login.token });
    } catch (error) {
      logDecision("auth.bootstrap", "denied", { reason: error instanceof Error ? error.message : "bootstrap_failed" });
      sendJson(res, 400, { error: { message: error instanceof Error ? error.message : "bootstrap_failed" } });
    }
    return true;
  }

  async handleLogin(req, res) {
    const body = await readJsonBody(req);
    const login = this.authStore.login(body.username, body.password);
    if (!login) {
      logDecision("auth.login", "denied", { username: String(body.username || "") });
      sendJson(res, 401, { error: { message: "invalid_credentials" } });
      return true;
    }
    logDecision("auth.login", "allowed", { username: login.user.username, workspace_id: login.user.workspace_id });
    sendJson(res, 200, login);
    return true;
  }

  handleMe(req, res) {
    const auth = this.authenticateRequest(req);
    if (!auth) {
      sendJson(res, 401, { error: { message: "not_authenticated" } });
      return true;
    }
    sendJson(res, 200, { user: this.authStore.publicUser(auth) });
    return true;
  }

  handleLogout(req, res) {
    this.authStore.revokeToken(readBearerToken(req));
    sendJson(res, 200, { ok: true });
    return true;
  }

  async handleUpload(req, res) {
    const auth = this.authenticateRequest(req);
    if (!auth) {
      sendJson(res, 401, { error: { message: "not_authenticated" } });
      return true;
    }
    const contentType = String(req.headers["content-type"] || "application/octet-stream");
    if (!contentType.startsWith("image/")) {
      sendJson(res, 400, { error: { message: "Only image uploads are supported" } });
      return true;
    }
    const raw = await readRawBody(req);
    const filename = cleanHeader(req.headers["x-hermes-filename"]) || "upload";
    const dataUrl = `data:${contentType};base64,${raw.toString("base64")}`;
    sendJson(res, 200, {
      path: dataUrl,
      url: dataUrl,
      name: filename,
      media_type: "image",
      size: raw.length,
    });
    return true;
  }

  async proxyPanelApi(req, res, prefix) {
    const auth = this.authenticateRequest(req);
    if (!auth) {
      sendJson(res, 401, { error: { message: "not_authenticated", stage: "panel_auth" } });
      return true;
    }
    const path = String(req.url || "/").replace(prefix, "") || "/";
    try {
      const upstream = await this.fetchApi(auth, path, {
        method: req.method || "GET",
        body: ["GET", "HEAD"].includes(String(req.method || "GET").toUpperCase()) ? undefined : await readRawBody(req),
        contentType: req.headers["content-type"],
      });
      res.statusCode = upstream.status;
      copyHeaders(upstream, res);
      if (upstream.body) {
        const reader = upstream.body.getReader();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          res.write(Buffer.from(value));
        }
        res.end();
      } else {
        res.end();
      }
    } catch (error) {
      sendJson(res, 502, { error: { message: error instanceof Error ? error.message : "panel_proxy_failed" } });
    }
    return true;
  }

  handleWebSocket(req, socket) {
    const url = new URL(req.url || "/", "http://panel.local");
    const token = url.searchParams.get("token") || "";
    const auth = this.authStore.verifyToken(token);
    if (!auth) {
      socket.write("HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n");
      socket.destroy();
      return;
    }
    const key = String(req.headers["sec-websocket-key"] || "");
    if (!key) {
      socket.write("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
      socket.destroy();
      return;
    }
    const accept = createHash("sha1").update(key + WS_GUID).digest("base64");
    socket.write([
      "HTTP/1.1 101 Switching Protocols",
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Accept: ${accept}`,
      "\r\n",
    ].join("\r\n"));
    const connection = new PanelWsConnection(this, socket, auth);
    this.connections.add(connection);
    socket.on("close", () => this.connections.delete(connection));
    socket.on("error", () => this.connections.delete(connection));
  }

  authenticateRequest(req) {
    return this.authStore.verifyToken(readBearerToken(req));
  }

  principalHeaders(auth) {
    if (!this.apiServerKey) throw new Error("missing_api_server_key");
    return this.authStore.principalHeaders(auth, this.apiServerKey);
  }

  async fetchApi(auth, path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const headers = {
      ...this.principalHeaders(auth),
      "X-Hermes-Request-Id": requestId(),
      "User-Agent": "ultra-studio-panel-bff",
    };
    if (options.contentType) headers["Content-Type"] = String(options.contentType);
    else if (options.json !== undefined) headers["Content-Type"] = "application/json";
    const body = options.json !== undefined ? JSON.stringify(options.json) : options.body;
    const upstream = await fetch(`${this.apiServerUrl}${path}`, {
      method,
      headers,
      body: ["GET", "HEAD"].includes(method) ? undefined : body,
      signal: options.signal,
    });
    return upstream;
  }

  async apiJson(auth, path, options = {}) {
    const upstream = await this.fetchApi(auth, path, options);
    const text = await upstream.text();
    const payload = text ? safeJson(text) : {};
    if (!upstream.ok) {
      const message = extractErrorMessage(payload) || `HTTP ${upstream.status}`;
      const error = new Error(message);
      error.status = upstream.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }
}

class PanelWsConnection {
  constructor(bff, socket, auth) {
    this.bff = bff;
    this.socket = socket;
    this.auth = auth;
    this.buffer = Buffer.alloc(0);
    this.activeRuns = new Map();
    this.toolIds = new Map();
    socket.on("data", (chunk) => this.onData(chunk));
  }

  onData(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    while (true) {
      const parsed = readWsFrame(this.buffer);
      if (!parsed) return;
      this.buffer = parsed.rest;
      if (parsed.opcode === 8) return this.close();
      if (parsed.opcode === 9) {
        this.sendFrame(parsed.payload, 10);
        continue;
      }
      if (parsed.opcode !== 1) continue;
      let packet;
      try {
        packet = JSON.parse(parsed.payload.toString("utf8"));
      } catch {
        continue;
      }
      void this.handleRpc(packet);
    }
  }

  async handleRpc(packet) {
    const id = typeof packet?.id === "string" ? packet.id : "";
    const method = typeof packet?.method === "string" ? packet.method : "";
    const params = isRecord(packet?.params) ? packet.params : {};
    if (!id || !method) return;
    try {
      const result = await this.dispatch(method, params);
      this.sendJson({ jsonrpc: "2.0", id, result });
    } catch (error) {
      this.sendJson({
        jsonrpc: "2.0",
        id,
        error: {
          code: Number(error?.status || -32000),
          message: error instanceof Error ? error.message : "request_failed",
        },
      });
    }
  }

  async dispatch(method, params) {
    switch (method) {
      case "session.create":
        return this.createSession(params);
      case "session.list":
        return this.listSessions(params);
      case "session.resume":
        return this.resumeSession(params);
      case "prompt.submit":
        return this.submitPrompt(params);
      case "session.interrupt":
        return this.interruptSession(params);
      case "approval.respond":
        return this.respondApproval(params);
      case "clarify.respond":
      case "sudo.respond":
      case "secret.respond":
        return this.respondPrompt(params);
      case "model.options":
        return this.modelOptions();
      case "config.set":
        return { value: String(params.value || ""), warning: "Model changes apply to new API-server sessions." };
      case "input.detect_drop":
      case "image.attach":
        return this.attachImage(params);
      default:
        throw new Error(`unsupported panel method: ${method}`);
    }
  }

  async createSession(params) {
    const body = {
      title: typeof params.title === "string" ? params.title : `Creative Session ${new Date().toLocaleTimeString()}`,
    };
    if (typeof params.model === "string" && params.model) body.model = params.model;
    const payload = await this.bff.apiJson(this.auth, "/api/sessions", { method: "POST", json: body });
    const session = payload.session || {};
    return { session_id: session.id, info: { model: session.model || "", provider: "api_server" } };
  }

  async listSessions(params) {
    const limit = Number.isFinite(Number(params.limit)) ? Math.max(1, Math.min(200, Number(params.limit))) : 40;
    const payload = await this.bff.apiJson(this.auth, `/api/sessions?limit=${limit}`);
    return { sessions: payload.data || [] };
  }

  async resumeSession(params) {
    const sessionId = String(params.session_id || "");
    if (!sessionId) throw new Error("session_id_required");
    const payload = await this.bff.apiJson(this.auth, `/api/sessions/${encodeURIComponent(sessionId)}/messages`);
    return {
      session_id: sessionId,
      resumed: sessionId,
      info: { provider: "api_server" },
      messages: (payload.data || []).map((msg) => ({
        id: String(msg.id || randomUUID()),
        role: msg.role,
        text: msg.content,
      })),
    };
  }

  async submitPrompt(params) {
    const sessionId = String(params.session_id || "");
    if (!sessionId) throw new Error("session_id_required");
    const text = String(params.text || "");
    const attachments = Array.isArray(params.attachments) ? params.attachments : [];
    const message = buildUserMessage(text, attachments);
    const aborter = new AbortController();
    this.activeRuns.set(sessionId, aborter);
    try {
      const upstream = await this.bff.fetchApi(
        this.auth,
        `/api/sessions/${encodeURIComponent(sessionId)}/chat/stream`,
        {
          method: "POST",
          json: { message },
          contentType: "application/json",
          signal: aborter.signal,
        },
      );
      if (!upstream.ok) {
        const textBody = await upstream.text();
        throw new Error(extractErrorMessage(safeJson(textBody)) || `HTTP ${upstream.status}`);
      }
      await readSse(upstream, (event, payload) => this.forwardSseEvent(sessionId, event, payload));
      return { ok: true };
    } finally {
      this.activeRuns.delete(sessionId);
    }
  }

  async interruptSession(params) {
    const sessionId = String(params.session_id || "");
    const aborter = this.activeRuns.get(sessionId);
    if (aborter) aborter.abort();
    if (sessionId) {
      try {
        await this.bff.apiJson(this.auth, `/api/sessions/${encodeURIComponent(sessionId)}/chat/stop`, {
          method: "POST",
          json: {},
        });
      } catch {
        return { ok: true };
      }
    }
    return { ok: true };
  }

  async respondApproval(params) {
    const sessionId = String(params.session_id || "");
    if (!sessionId) throw new Error("session_id_required");
    return this.bff.apiJson(this.auth, `/api/sessions/${encodeURIComponent(sessionId)}/chat/approval`, {
      method: "POST",
      json: { choice: params.choice, all: Boolean(params.all) },
    });
  }

  async respondPrompt(params) {
    const sessionId = String(params.session_id || params.sessionId || "");
    if (!sessionId) throw new Error("session_id_required");
    return this.bff.apiJson(this.auth, `/api/sessions/${encodeURIComponent(sessionId)}/chat/prompt`, {
      method: "POST",
      json: params,
    });
  }

  async modelOptions() {
    const payload = await this.bff.apiJson(this.auth, "/v1/models");
    const models = Array.isArray(payload.data)
      ? payload.data.map((entry) => entry?.id).filter((value) => typeof value === "string" && value)
      : [];
    return {
      provider: "api_server",
      model: models[0] || "hermes-agent",
      providers: [{ name: "Hermes API Server", slug: "api_server", models: models.length ? models : ["hermes-agent"], warning: "" }],
    };
  }

  attachImage(params) {
    const path = String(params.path || params.text || "");
    return {
      matched: Boolean(path),
      path,
      name: "uploaded image",
      text: "[Attached image]",
    };
  }

  forwardSseEvent(sessionId, event, payload) {
    if (event === "run.started") {
      this.sendEvent(sessionId, "status.update", { text: "Thinking" });
      return;
    }
    if (event === "message.started") {
      this.sendEvent(sessionId, "message.start", {});
      return;
    }
    if (event === "assistant.delta") {
      this.sendEvent(sessionId, "message.delta", { text: String(payload.delta || "") });
      return;
    }
    if (event === "assistant.completed") {
      this.sendEvent(sessionId, "message.complete", { text: String(payload.content || "") });
      return;
    }
    if (event === "tool.started") {
      const name = String(payload.tool_name || "tool");
      const toolId = this.toolId(sessionId, payload, name);
      this.sendEvent(sessionId, "tool.start", {
        tool_id: toolId,
        name,
        context: String(payload.preview || ""),
      });
      return;
    }
    if (event === "tool.progress") {
      const name = String(payload.tool_name || "tool");
      if (name === "_thinking") {
        this.sendEvent(sessionId, "status.update", { text: String(payload.delta || payload.preview || "Thinking") });
        return;
      }
      this.sendEvent(sessionId, "tool.progress", {
        tool_id: this.toolId(sessionId, payload, name),
        name,
        preview: String(payload.delta || payload.preview || ""),
      });
      return;
    }
    if (event === "tool.completed" || event === "tool.failed") {
      const name = String(payload.tool_name || "tool");
      const error = event === "tool.failed" ? toolErrorFromPayload(payload) || String(payload.preview || "tool failed") : toolErrorFromPayload(payload);
      this.sendEvent(sessionId, "tool.complete", {
        tool_id: this.toolId(sessionId, payload, name),
        name,
        summary: String(payload.preview || ""),
        error,
      });
      return;
    }
    if (event === "clarify.request" || event === "approval.request" || event === "sudo.request" || event === "secret.request") {
      this.sendEvent(sessionId, event, payload);
      return;
    }
    if (event === "error") {
      this.sendEvent(sessionId, "error", { message: String(payload.message || "stream error") });
    }
  }

  toolId(sessionId, payload, name) {
    const key = `${sessionId}:${payload.run_id || ""}:${name}`;
    if (!this.toolIds.has(key)) this.toolIds.set(key, `tool-${randomUUID()}`);
    return this.toolIds.get(key);
  }

  sendEvent(sessionId, type, payload) {
    this.sendJson({ jsonrpc: "2.0", method: "event", params: { type, session_id: sessionId, payload } });
  }

  sendJson(payload) {
    this.sendFrame(Buffer.from(JSON.stringify(payload), "utf8"), 1);
  }

  sendFrame(payload, opcode = 1) {
    if (this.socket.destroyed) return;
    this.socket.write(writeWsFrame(payload, opcode));
  }

  close() {
    for (const aborter of this.activeRuns.values()) aborter.abort();
    this.activeRuns.clear();
    try {
      this.sendFrame(Buffer.alloc(0), 8);
    } catch {
      return;
    }
    this.socket.end();
  }
}

function buildUserMessage(text, attachments) {
  const imageParts = attachments
    .map((item) => (isRecord(item) ? String(item.url || item.path || "") : ""))
    .filter(Boolean)
    .map((url) => ({ type: "image_url", image_url: { url } }));
  if (!imageParts.length) return text;
  return [{ type: "text", text: text || "Please inspect the attached image." }, ...imageParts];
}

async function readSse(response, onEvent) {
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index;
    while ((index = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const parsed = parseSseBlock(block);
      if (parsed) onEvent(parsed.event, parsed.data);
    }
  }
}

function parseSseBlock(block) {
  let event = "message";
  const data = [];
  for (const line of block.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  return { event, data: safeJson(data.join("\n")) || {} };
}

function readWsFrame(buffer) {
  if (buffer.length < 2) return null;
  const first = buffer[0];
  const second = buffer[1];
  const opcode = first & 0x0f;
  const masked = Boolean(second & 0x80);
  let length = second & 0x7f;
  let offset = 2;
  if (length === 126) {
    if (buffer.length < offset + 2) return null;
    length = buffer.readUInt16BE(offset);
    offset += 2;
  } else if (length === 127) {
    if (buffer.length < offset + 8) return null;
    const big = buffer.readBigUInt64BE(offset);
    if (big > BigInt(MAX_BODY_BYTES)) throw new Error("websocket_frame_too_large");
    length = Number(big);
    offset += 8;
  }
  let mask;
  if (masked) {
    if (buffer.length < offset + 4) return null;
    mask = buffer.subarray(offset, offset + 4);
    offset += 4;
  }
  if (buffer.length < offset + length) return null;
  const payload = Buffer.from(buffer.subarray(offset, offset + length));
  if (mask) {
    for (let i = 0; i < payload.length; i += 1) payload[i] ^= mask[i % 4];
  }
  return { opcode, payload, rest: buffer.subarray(offset + length) };
}

function writeWsFrame(payload, opcode) {
  const length = payload.length;
  let header;
  if (length < 126) {
    header = Buffer.from([0x80 | opcode, length]);
  } else if (length <= 0xffff) {
    header = Buffer.alloc(4);
    header[0] = 0x80 | opcode;
    header[1] = 126;
    header.writeUInt16BE(length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x80 | opcode;
    header[1] = 127;
    header.writeBigUInt64BE(BigInt(length), 2);
  }
  return Buffer.concat([header, payload]);
}

async function readJsonBody(req) {
  const raw = await readRawBody(req);
  if (!raw.length) return {};
  return JSON.parse(raw.toString("utf8"));
}

async function readRawBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    const buf = Buffer.from(chunk);
    size += buf.length;
    if (size > MAX_BODY_BYTES) throw new Error("request_too_large");
    chunks.push(buf);
  }
  return Buffer.concat(chunks);
}

function copyHeaders(upstream, res) {
  for (const header of ["content-type", "cache-control", "x-accel-buffering", "x-hermes-session-id", "x-hermes-session-key", "x-hermes-request-id"]) {
    const value = upstream.headers.get(header);
    if (value) res.setHeader(header, value);
  }
}

function sendJson(res, status, payload) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(payload));
}

function requestId() {
  return `panel_${randomUUID().replaceAll("-", "")}`;
}

function logDecision(action, result, fields = {}) {
  console.log("panel_auth_decision", JSON.stringify({ action, result, ...fields, ts: Date.now() }));
}

function cleanHeader(value) {
  return String(value || "").replace(/[\r\n\x00]/g, " ").trim().slice(0, 160);
}

function safeJson(text) {
  try {
    return JSON.parse(String(text || ""));
  } catch {
    return text;
  }
}

function extractErrorMessage(payload) {
  if (typeof payload === "string") return payload;
  if (!isRecord(payload)) return "";
  const error = payload.error;
  if (typeof error === "string") return error;
  if (isRecord(error)) return String(error.message || error.code || "");
  return "";
}

function toolErrorFromPayload(payload) {
  if (!isRecord(payload)) return "";
  if (payload.is_error === true) {
    const preview = String(payload.preview || "");
    const parsed = safeJson(preview);
    const extracted = extractErrorMessage(parsed);
    return extracted || preview || "tool failed";
  }
  const result = typeof payload.result === "string" ? payload.result : "";
  if (!result) return "";
  const parsed = safeJson(result);
  if (isRecord(parsed) && parsed.success === false) {
    return extractErrorMessage(parsed) || result.slice(0, 500);
  }
  return "";
}

function isRecord(value) {
  return typeof value === "object" && value !== null;
}
