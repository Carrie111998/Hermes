export interface PanelUser {
  username: string;
  label: string;
  tenant_id: string;
  workspace_id: string;
  workspace?: string;
  project_id: string;
  user_id: string;
  roles?: string[];
}

export interface PanelAuthState {
  token: string;
  user: PanelUser;
}

export interface PanelAuthStatus {
  configured?: boolean;
  needs_bootstrap?: boolean;
  api_server_url?: string;
  user_count?: number;
}

export type PanelConn = "idle" | "connecting" | "open" | "closed" | "error";

export interface PanelEvent {
  type: string;
  session_id?: string;
  payload?: unknown;
}

interface PanelPending<T> {
  resolve: (value: T) => void;
  reject: (error: Error) => void;
  timer: number;
}

const AUTH_KEY = "ultra-studio-agent.auth.v3";

export function loadAuth(): PanelAuthState | null {
  try {
    const raw = JSON.parse(localStorage.getItem(AUTH_KEY) || "null");
    if (!isRecord(raw) || !isRecord(raw.user) || typeof raw.token !== "string") return null;
    return {
      token: raw.token,
      user: normalizeUser(raw.user),
    };
  } catch {
    return null;
  }
}

export function saveAuth(next: PanelAuthState | null): void {
  if (next) localStorage.setItem(AUTH_KEY, JSON.stringify(next));
  else localStorage.removeItem(AUTH_KEY);
}

export function authScope(auth: PanelAuthState | null): string {
  if (!auth?.user) return "anonymous";
  const user = auth.user;
  return [user.tenant_id, user.workspace_id, user.project_id, user.user_id].filter(Boolean).join(":") || user.username;
}

export async function authStatus(): Promise<PanelAuthStatus> {
  return authFetch<PanelAuthStatus>("/panel-auth/status");
}

export async function authMe(auth: PanelAuthState): Promise<PanelAuthState> {
  const body = await authFetch<{ user: PanelUser }>("/panel-auth/me", {}, auth);
  return { token: auth.token, user: normalizeUser(body.user) };
}

export async function authLogin(username: string, password: string): Promise<PanelAuthState> {
  const body = await authFetch<{ token: string; user: PanelUser }>("/panel-auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  return { token: body.token, user: normalizeUser(body.user) };
}

export async function authBootstrap(input: {
  username: string;
  password: string;
  label?: string;
  workspace?: string;
}): Promise<PanelAuthState> {
  const body = await authFetch<{ token: string; user: PanelUser }>("/panel-auth/bootstrap", {
    method: "POST",
    body: JSON.stringify(input),
  });
  return { token: body.token, user: normalizeUser(body.user) };
}

export async function authLogout(auth: PanelAuthState | null): Promise<void> {
  if (!auth) return;
  try {
    await authFetch("/panel-auth/logout", { method: "POST" }, auth);
  } catch {
    return;
  }
}

export async function authFetch<T>(path: string, init: RequestInit = {}, auth?: PanelAuthState | null): Promise<T> {
  const headers = new Headers(init.headers);
  if (auth?.token) headers.set("Authorization", `Bearer ${auth.token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers });
  const text = await res.text();
  const body = text ? safeJson(text) : {};
  if (!res.ok) {
    const message = extractError(body) || `HTTP ${res.status}`;
    const error = new Error(message);
    (error as Error & { status?: number }).status = res.status;
    throw error;
  }
  return body as T;
}

export class PanelGatewayClient {
  private ws: WebSocket | null = null;
  private seq = 0;
  private pending = new Map<string, PanelPending<unknown>>();

  constructor(
    private onEvent: (event: PanelEvent) => void,
    private onState: (state: PanelConn) => void,
    private onError: (message: string) => void,
  ) {}

  connect(token: string): Promise<void> {
    this.close();
    this.onState("connecting");
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${scheme}//${location.host}/hermes/ws?token=${encodeURIComponent(token)}`);
    this.ws = ws;
    ws.addEventListener("message", (event) => this.dispatch(event.data));
    ws.addEventListener("close", () => {
      this.rejectAll(new Error("WebSocket closed"));
      this.onState("closed");
    });
    return new Promise((resolve, reject) => {
      const onOpen = () => {
        ws.removeEventListener("error", onErr);
        this.onState("open");
        resolve();
      };
      const onErr = () => {
        ws.removeEventListener("open", onOpen);
        this.onState("error");
        reject(new Error("WebSocket connection failed"));
      };
      ws.addEventListener("open", onOpen, { once: true });
      ws.addEventListener("error", onErr, { once: true });
    });
  }

  request<T = unknown>(method: string, params: Record<string, unknown> = {}, timeoutMs = 120_000): Promise<T> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("gateway not connected"));
    }
    const id = `p${++this.seq}`;
    return new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error(`request timed out: ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve: (value) => resolve(value as T), reject, timer });
      this.ws?.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    });
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
    this.rejectAll(new Error("WebSocket closed"));
  }

  private dispatch(data: unknown): void {
    try {
      const raw: unknown = JSON.parse(String(data));
      if (!isRecord(raw)) return;
      const id = typeof raw.id === "string" ? raw.id : "";
      if (id && this.pending.has(id)) {
        const waiting = this.pending.get(id);
        if (!waiting) return;
        this.pending.delete(id);
        window.clearTimeout(waiting.timer);
        const msg = isRecord(raw.error) ? text(raw.error.message) : "";
        if (msg) waiting.reject(new Error(msg));
        else waiting.resolve(raw.result);
        return;
      }
      if (raw.method === "event" && isRecord(raw.params) && typeof raw.params.type === "string") {
        this.onEvent(raw.params as unknown as PanelEvent);
      }
    } catch (err) {
      this.onError(err instanceof Error ? err.message : String(err));
    }
  }

  private rejectAll(err: Error): void {
    for (const waiting of this.pending.values()) {
      window.clearTimeout(waiting.timer);
      waiting.reject(err);
    }
    this.pending.clear();
  }
}

function normalizeUser(raw: unknown): PanelUser {
  const user = isRecord(raw) ? raw : {};
  return {
    username: text(user.username),
    label: text(user.label) || text(user.username),
    tenant_id: text(user.tenant_id),
    workspace_id: text(user.workspace_id) || text(user.workspace),
    workspace: text(user.workspace),
    project_id: text(user.project_id),
    user_id: text(user.user_id),
    roles: Array.isArray(user.roles) ? user.roles.filter((role): role is string => typeof role === "string") : [],
  };
}

function safeJson(textValue: string): unknown {
  try {
    return JSON.parse(textValue);
  } catch {
    return textValue;
  }
}

function extractError(body: unknown): string {
  if (typeof body === "string") return body;
  if (!isRecord(body)) return "";
  if (typeof body.error === "string") return body.error;
  if (isRecord(body.error)) return text(body.error.message) || text(body.error.code);
  return "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}
