type Role = "user" | "assistant" | "system" | "tool";
type Status = "idle" | "thinking" | "creating" | "streaming" | "error";

interface UserProfile {
  username: string;
  label: string;
  workspace: string;
  token: string;
}
interface Message { id: string; role: Role; text: string; streaming?: boolean }
interface SessionRow { id: string; title?: string; preview?: string; message_count?: number; source?: string; started_at?: number }
interface Attachment { id: string; name: string; dataUrl: string; previewUrl: string }

class PanelHttpError extends Error {
  constructor(public status: number, public path: string, public body: unknown) {
    super(formatHttpError(status, path, body));
  }
}

const AUTH_KEY = "ultra-studio-agent.auth.v2";
const MODEL_OPTIONS = [
  { value: "grok-4.3", label: "grok-4.3 / xai-oauth" },
  { value: "claude-sonnet-4.5", label: "claude-sonnet-4.5" },
  { value: "gpt-5.1", label: "gpt-5.1" },
];

const refs = {
  messages: el("messages"),
  statusPill: el("status-pill"),
  statusLabel: el("status-label"),
  input: el("prompt-input") as HTMLTextAreaElement,
  form: el("composer") as HTMLFormElement,
  send: el("send-button") as HTMLButtonElement,
  attach: el("attach-button") as HTMLButtonElement,
  file: el("image-input") as HTMLInputElement,
  shelf: el("attachment-shelf"),
  error: el("error-banner"),
  fresh: el("new-session") as HTMLButtonElement,
  history: el("history-list"),
  historyRefresh: el("refresh-history") as HTMLButtonElement,
  reconnect: el("reconnect-button") as HTMLButtonElement,
  stop: el("interrupt-button") as HTMLButtonElement,
  logout: el("logout-button") as HTMLButtonElement,
  modelSelect: el("model-select") as HTMLSelectElement,
  modelStatus: el("model-switch-status"),
};

let me: UserProfile | null = loadAuth();
let sessions: SessionRow[] = [];
let sid = "";
let messages: Message[] = [];
let attachments: Attachment[] = [];
let runState: Status = "idle";
let running = false;
let seq = 0;
let aborter: AbortController | null = null;

function el(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Missing element #${id}`);
  return node;
}
function id(prefix: string): string {
  seq += 1;
  return `${prefix}-${Date.now().toString(36)}-${seq}`;
}
function loadAuth(): UserProfile | null {
  try {
    const raw = JSON.parse(localStorage.getItem(AUTH_KEY) || "null");
    if (!raw?.token || !raw?.username) return null;
    return {
      username: String(raw.username),
      label: String(raw.label || raw.username),
      workspace: String(raw.workspace || "workspace"),
      token: String(raw.token),
    };
  } catch {
    return null;
  }
}
function saveAuth(user: UserProfile | null): void {
  if (user) localStorage.setItem(AUTH_KEY, JSON.stringify(user));
  else localStorage.removeItem(AUTH_KEY);
}

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  if (!me) throw new Error("not logged in");
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  headers.set("Authorization", `Bearer ${me.token}`);
  const res = await fetch(`/hermes-api${path}`, { ...init, headers });
  const text = await res.text();
  const body = text ? safeJson(text) : {};
  if (!res.ok) throw new PanelHttpError(res.status, path, body || text);
  return body as T;
}
function safeJson(text: string): unknown {
  try { return JSON.parse(text); } catch { return text; }
}
function formatHttpError(status: number, path: string, body: unknown): string {
  let detail = "";
  if (typeof body === "string") detail = body;
  else if (body && typeof body === "object") {
    const error = (body as Record<string, unknown>).error;
    if (typeof error === "string") detail = error;
    else if (error && typeof error === "object") {
      detail = String((error as Record<string, unknown>).message || (error as Record<string, unknown>).code || "");
    }
  }
  return detail ? `${path} -> HTTP ${status}: ${detail}` : `${path} -> HTTP ${status}`;
}
function isAuthFailure(err: unknown): boolean {
  return err instanceof PanelHttpError && (err.status === 401 || err.status === 403);
}

function showLogin(message = ""): void {
  document.body.append(loginOverlay(message));
  renderShell();
}
function loginOverlay(message: string): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "auth-overlay";
  wrap.innerHTML = `
    <form class="auth-card">
      <img src="/atlas-avatar.png" alt="">
      <h2><span>Bringing</span> it to life</h2>
      <p>登录后进入隔离的 Hermes API Server workspace。</p>
      <label>Username</label>
      <input name="username" autocomplete="username" value="alice">
      <label>Password</label>
      <input name="password" type="password" autocomplete="current-password">
      <button type="submit">Login</button>
      <small>${message || "本地测试账号：alice / alice123，bob / bob123"}</small>
    </form>`;
  wrap.querySelector("form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget as HTMLFormElement);
    const username = String(form.get("username") || "").trim().toLowerCase();
    const password = String(form.get("password") || "");
    void login(username, password).catch((err) => {
      wrap.remove();
      showLogin(err instanceof Error ? err.message : "登录失败");
    });
  });
  return wrap;
}

async function login(username: string, password: string): Promise<void> {
  const res = await fetch("/hermes-auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok || !body?.token || !body?.user) throw new Error("用户名或密码不正确");
  me = { ...body.user, token: String(body.token) };
  saveAuth(me);
  document.querySelector(".auth-overlay")?.remove();
  await boot();
}

async function boot(): Promise<void> {
  if (!me) return showLogin();
  runState = "idle";
  renderShell();
  try {
    await refreshSessions();
    await newSession();
  } catch (err) {
    if (isAuthFailure(err)) {
      me = null;
      saveAuth(null);
      clearError();
      showLogin(err instanceof Error ? err.message : "登录状态失效");
      return;
    }
    showError(err);
  }
}
function renderShell(): void {
  refs.statusPill.dataset.state = runState === "error" ? "error" : "open";
  refs.statusLabel.textContent = running ? labelForStatus(runState) : me ? me.username : "login";
  refs.send.disabled = !me || running || (!refs.input.value.trim() && !attachments.length);
  refs.stop.disabled = !running;
  refs.logout.hidden = !me;
  refs.modelStatus.textContent = me ? refs.modelSelect.selectedOptions[0]?.textContent || refs.modelSelect.value : "Login required";
  document.querySelector(".brand small")!.textContent = me ? `${me.label} · ${me.workspace}` : "Login required";
  renderMessages();
  renderHistory();
  renderAttachments();
}
function labelForStatus(value: Status): string {
  if (value === "thinking") return "Thinking";
  if (value === "creating") return "Creating";
  if (value === "streaming") return "Streaming";
  if (value === "error") return "Error";
  return "Ready";
}
function showError(err: unknown): void {
  runState = "error";
  refs.error.hidden = false;
  refs.error.textContent = err instanceof Error ? err.message : String(err);
  renderShell();
}
function clearError(): void {
  refs.error.hidden = true;
  refs.error.textContent = "";
}

async function refreshSessions(): Promise<void> {
  const payload = await api<{ data?: SessionRow[] }>("/api/sessions?limit=80");
  sessions = payload.data || [];
  renderHistory();
}
async function newSession(): Promise<void> {
  const payload = await api<{ session: SessionRow }>("/api/sessions", {
    method: "POST",
    body: JSON.stringify({
      title: `Creative Session ${new Date().toLocaleTimeString()}`,
      model: refs.modelSelect.value,
    }),
  });
  sid = payload.session.id;
  messages = [];
  await refreshSessions();
  renderShell();
}
async function openSession(nextSid: string): Promise<void> {
  clearError();
  const payload = await api<{ data?: Array<{ id?: string; role?: string; content?: string }> }>(
    `/api/sessions/${encodeURIComponent(nextSid)}/messages`,
  );
  sid = nextSid;
  messages = (payload.data || []).map((msg) => ({
    id: String(msg.id || id("msg")),
    role: normalizeRole(msg.role),
    text: String(msg.content || ""),
  })).filter((msg) => msg.text);
  renderShell();
}
function normalizeRole(role: unknown): Role {
  return role === "user" || role === "assistant" || role === "system" || role === "tool" ? role : "assistant";
}

async function submit(): Promise<void> {
  if (!me || running) return;
  if (!sid) await newSession();
  const text = refs.input.value.trim();
  const outbound = [...attachments];
  if (!text && !outbound.length) return;
  refs.input.value = "";
  attachments = [];
  resizeInput();
  clearError();
  messages.push({ id: id("user"), role: "user", text: text || `[${outbound.length} image attachment]` });
  const assistantId = id("assistant");
  messages.push({ id: assistantId, role: "assistant", text: "", streaming: true });
  running = true;
  runState = "thinking";
  renderShell();
  try {
    await streamChat(assistantId, buildChatMessage(text, outbound));
    await refreshSessions();
  } catch (err) {
    showError(err);
  } finally {
    running = false;
    runState = "idle";
    aborter = null;
    renderShell();
  }
}
function buildChatMessage(text: string, outbound: Attachment[]): unknown {
  if (!outbound.length) return text;
  const parts: Array<Record<string, unknown>> = [];
  if (text) parts.push({ type: "input_text", text });
  for (const item of outbound) parts.push({ type: "input_image", image_url: item.dataUrl });
  return parts;
}
async function streamChat(assistantId: string, message: unknown): Promise<void> {
  if (!me) throw new Error("not logged in");
  aborter = new AbortController();
  const res = await fetch(`/hermes-api/api/sessions/${encodeURIComponent(sid)}/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Authorization": `Bearer ${me.token}` },
    body: JSON.stringify({ message, model: refs.modelSelect.value }),
    signal: aborter.signal,
  });
  if (!res.ok || !res.body) {
    const text = await res.text();
    throw new PanelHttpError(
      res.status,
      `/api/sessions/${sid}/chat/stream`,
      text ? safeJson(text) : "stream unavailable",
    );
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() || "";
    for (const chunk of chunks) handleSseChunk(assistantId, chunk);
  }
}
function handleSseChunk(assistantId: string, chunk: string): void {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of chunk.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return;
  const payload = safeJson(dataLines.join("\n"));
  if (!payload || typeof payload !== "object") return;
  const data = payload as Record<string, unknown>;
  if (event === "assistant.delta") {
    appendAssistant(assistantId, String(data.delta || ""));
  } else if (event === "assistant.completed") {
    finishAssistant(assistantId, String(data.content || ""));
  } else if (event === "tool.started") {
    runState = String(data.tool_name || "").includes("image") ? "creating" : "thinking";
  } else if (event === "tool.failed" || event === "error") {
    throw new Error(String(data.message || data.preview || "stream error"));
  }
  renderShell();
}
function appendAssistant(messageId: string, delta: string): void {
  const msg = messages.find((entry) => entry.id === messageId);
  if (msg) msg.text += delta;
}
function finishAssistant(messageId: string, content: string): void {
  const msg = messages.find((entry) => entry.id === messageId);
  if (!msg) return;
  if (content.trim()) msg.text = content;
  msg.streaming = false;
}

function renderHistory(): void {
  refs.history.replaceChildren();
  if (!me) {
    refs.history.append(div("history-empty", "Login required."));
    return;
  }
  if (!sessions.length) {
    refs.history.append(div("history-empty", "No saved chats yet."));
    return;
  }
  for (const row of sessions) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "history-item";
    if (row.id === sid) button.dataset.active = "true";
    button.append(
      div("history-title", row.title || row.id),
      div("history-preview", row.preview || "No preview"),
      div("history-meta", `${row.message_count || 0} msgs / ${row.source || "api_server"}`),
    );
    button.addEventListener("click", () => void openSession(row.id));
    refs.history.append(button);
  }
}
function renderMessages(): void {
  refs.messages.replaceChildren();
  if (!messages.length) {
    refs.messages.append(div("empty-state", me ? "Session ready." : "Login required."));
    return;
  }
  for (const msg of messages) {
    const card = document.createElement("article");
    card.className = `message ${msg.role}`;
    if (msg.streaming) card.dataset.streaming = "true";
    card.append(div("message-label", msg.role));
    const body = div("message-body", msg.text || labelForStatus(runState));
    card.append(body);
    const media = mediaPreview(msg.text);
    if (media) card.append(media);
    refs.messages.append(card);
  }
  refs.messages.scrollTop = refs.messages.scrollHeight;
}
function mediaPreview(text: string): HTMLElement | null {
  const urls = [...new Set((text.match(/https?:\/\/[^\s)]+/g) || []).map((url) => url.replace(/[),.;!?]+$/, "")))];
  const media = urls.filter((url) => /\.(png|jpe?g|webp|gif|mp4|webm|mov)(\?|$)/i.test(url)).slice(0, 4);
  if (!media.length) return null;
  const wrap = div("media-preview", "");
  for (const url of media) {
    if (/\.(mp4|webm|mov)(\?|$)/i.test(url)) {
      const video = document.createElement("video");
      video.src = url;
      video.controls = true;
      video.playsInline = true;
      wrap.append(video);
    } else {
      const img = document.createElement("img");
      img.src = url;
      img.alt = "Generated media";
      img.loading = "lazy";
      wrap.append(img);
    }
  }
  return wrap;
}
function renderAttachments(): void {
  refs.shelf.replaceChildren();
  refs.shelf.hidden = !attachments.length;
  for (const item of attachments) {
    const chip = div("attachment-chip", "");
    const image = document.createElement("img");
    image.src = item.previewUrl;
    image.alt = "";
    chip.append(image, div("", item.name, "span"));
    refs.shelf.append(chip);
  }
}
function div(className: string, text: string, tag = "div"): HTMLElement {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}
function resizeInput(): void {
  refs.input.style.height = "0px";
  refs.input.style.height = `${Math.min(180, refs.input.scrollHeight)}px`;
}
async function attachFile(file: File): Promise<void> {
  if (!file.type.startsWith("image/")) return showError("Only image files are accepted.");
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Could not read image"));
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsDataURL(file);
  });
  attachments.push({ id: id("attach"), name: file.name || "image", dataUrl, previewUrl: URL.createObjectURL(file) });
  renderShell();
}

refs.form.addEventListener("submit", (event) => { event.preventDefault(); void submit(); });
refs.input.addEventListener("input", () => { resizeInput(); renderShell(); });
refs.input.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); } });
refs.attach.addEventListener("click", () => refs.file.click());
refs.file.addEventListener("change", () => { const file = refs.file.files?.[0]; refs.file.value = ""; if (file) void attachFile(file); });
refs.fresh.addEventListener("click", () => void newSession());
refs.historyRefresh.addEventListener("click", () => void refreshSessions().catch(showError));
refs.reconnect.addEventListener("click", () => void refreshSessions().catch(showError));
refs.stop.addEventListener("click", () => aborter?.abort());
refs.logout.addEventListener("click", () => {
  me = null;
  saveAuth(null);
  sid = "";
  sessions = [];
  messages = [];
  showLogin("已退出，可以切换账号。");
});
window.addEventListener("beforeunload", () => { for (const item of attachments) URL.revokeObjectURL(item.previewUrl); });

for (const option of MODEL_OPTIONS) {
  const node = document.createElement("option");
  node.value = option.value;
  node.textContent = option.label;
  refs.modelSelect.append(node);
}
refs.modelSelect.addEventListener("change", renderShell);

void boot();
