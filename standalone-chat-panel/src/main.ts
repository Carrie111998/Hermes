import {
  createPanelHistoryController,
  type PanelHistoryActivation,
} from "./panelHistory";

type Conn = "idle" | "connecting" | "open" | "closed" | "error";
type Role = "user" | "assistant" | "system" | "tool";
type ToolState = "running" | "done" | "error";

interface PanelEvent { type: string; session_id?: string; payload?: unknown }
interface PanelInfo { provider?: string; model?: string }
interface PanelCreate { session_id: string; info?: PanelInfo }
interface PanelAttach { id: string; name: string; path: string; text: string; meta?: string; previewUrl?: string }
interface PanelMsg { id: string; role: Role; text: string; status: "streaming" | "complete"; attachments?: PanelAttach[]; toolId?: string; toolName?: string }
interface PanelTool { id: string; toolId?: string; name: string; context?: string; preview?: string; summary?: string; error?: string; status: ToolState }
interface MediaRef { url: string; kind: "image" | "video"; alt?: string }
type PanelPrompt =
  | { kind: "clarify"; requestId: string; question: string; choices?: string[] }
  | { kind: "approval"; command: string; description: string }
  | { kind: "sudo"; requestId: string }
  | { kind: "secret"; requestId: string; prompt: string };
interface PanelDrop { matched?: boolean; path?: string; name?: string; text?: string; width?: number; height?: number; token_estimate?: number; count?: number }
interface PanelPending<T> { resolve: (value: T) => void; reject: (error: Error) => void; timer: number }

const refs = {
  messages: el("messages"),
  tools: el("tools-list"),
  pending: el("pending-panel"),
  error: el("error-banner"),
  statusPill: el("status-pill"),
  statusLabel: el("status-label"),
  connection: el("connection-state"),
  model: el("model-label"),
  session: el("session-label"),
  provider: el("provider-label"),
  run: el("run-state"),
  input: el("prompt-input") as HTMLTextAreaElement,
  form: el("composer") as HTMLFormElement,
  send: el("send-button") as HTMLButtonElement,
  attach: el("attach-button") as HTMLButtonElement,
  file: el("image-input") as HTMLInputElement,
  shelf: el("attachment-shelf"),
  stop: el("interrupt-button") as HTMLButtonElement,
  reconnect: el("reconnect-button") as HTMLButtonElement,
  fresh: el("new-session") as HTMLButtonElement,
  history: el("history-list"),
  historyRefresh: el("refresh-history") as HTMLButtonElement,
};

let conn: Conn = "idle";
let sid: string | null = null;
let info: PanelInfo = {};
let run = false;
let runStatus = "idle";
let errorText = "";
let msgs: PanelMsg[] = [];
let tools: PanelTool[] = [];
let attached: PanelAttach[] = [];
let pendingPrompt: PanelPrompt | null = null;
let activeAssistant: string | null = null;
let seq = 0;
let gw: PanelGatewayClient | null = null;
const history = createPanelHistoryController({
  listElement: refs.history,
  refreshButton: refs.historyRefresh,
  request: (method, params, timeoutMs) => {
    if (!gw) throw new Error("gateway not connected");
    return gw.request(method, params, timeoutMs);
  },
  makeId: id,
  getSessionId: () => sid,
  getMessages: () => msgs,
  isRunning: () => run,
  activate: activateHistory,
  onError: showError,
});

class PanelGatewayClient {
  private ws: WebSocket | null = null;
  private seq = 0;
  private pending = new Map<string, PanelPending<unknown>>();
  constructor(private onEvent: (event: PanelEvent) => void, private onState: (state: Conn) => void, private onError: (message: string) => void) {}
  connect(): Promise<void> {
    this.close();
    this.onState("connecting");
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${scheme}//${location.host}/hermes/ws`);
    this.ws = ws;
    ws.addEventListener("message", (event) => this.dispatch(event.data));
    ws.addEventListener("close", () => {
      this.rejectAll(new Error("WebSocket closed"));
      this.onState("closed");
    });
    return new Promise((resolve, reject) => {
      const onOpen = () => { ws.removeEventListener("error", onErr); this.onState("open"); resolve(); };
      const onErr = () => { ws.removeEventListener("open", onOpen); this.onState("error"); reject(new Error("WebSocket connection failed")); };
      ws.addEventListener("open", onOpen, { once: true });
      ws.addEventListener("error", onErr, { once: true });
    });
  }
  request<T = unknown>(method: string, params: Record<string, unknown> = {}, timeoutMs = 120_000): Promise<T> {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return Promise.reject(new Error(`gateway not connected (${conn})`));
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
      if (!rec(raw)) return;
      const id = typeof raw.id === "string" ? raw.id : "";
      if (id && this.pending.has(id)) {
        const waiting = this.pending.get(id);
        if (!waiting) return;
        this.pending.delete(id);
        window.clearTimeout(waiting.timer);
        const msg = rec(raw.error) ? str(raw.error, "message") : "";
        if (msg) waiting.reject(new Error(msg));
        else waiting.resolve(raw.result);
        return;
      }
      if (raw.method === "event" && rec(raw.params) && typeof raw.params.type === "string") {
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

function el(id: string): HTMLElement {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Missing element #${id}`);
  return node;
}
function rec(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
function str(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === "string" ? value : "";
}
function id(prefix: string): string {
  seq += 1;
  return `${prefix}-${Date.now().toString(36)}-${seq}`;
}
function basename(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || path;
}
const TOOL_STATUS_LABELS: Record<string, string> = {
  image_generate: "Creating image",
  video_generate: "Creating video",
  vision_analyze: "Inspecting image",
  browser_navigate: "Opening page",
  browser_vision: "Inspecting page",
  web_search: "Searching web",
  search_files: "Searching files",
  read_file: "Reading file",
  write_file: "Writing file",
  patch: "Applying patch",
  terminal: "Executing command",
  process: "Running process",
  execute_code: "Executing code",
  skill_view: "Loading skill",
  skills_list: "Loading skills",
  todo: "Updating plan",
};
function phaseStatusLabel(value: string): string {
  const phase = (value || "").trim().toLowerCase();
  if (!phase || phase === "running") return "Thinking";
  if (phase.includes("upload")) return "Uploading image";
  if (phase.includes("tool")) return "Using tools";
  if (phase.includes("reason")) return "Reasoning";
  if (phase.includes("think")) return "Thinking";
  if (phase.includes("generat") || phase.includes("creat")) return "Creating";
  if (phase.includes("final") || phase.includes("complete")) return "Finalizing";
  return value;
}
function toolStatusLabel(name: string): string {
  if (TOOL_STATUS_LABELS[name]) return TOOL_STATUS_LABELS[name];
  if (name.includes("search")) return "Searching";
  if (name.includes("image")) return "Creating image";
  if (name.includes("video")) return "Creating video";
  if (name.includes("file")) return "Working with files";
  return `Running ${name || "tool"}`;
}
function toolMessageText(name: string, context = "", detail = ""): string {
  const label = toolStatusLabel(name);
  return [detail ? `${label}: ${detail}` : label, context].filter(Boolean).join("\n");
}

async function connect(): Promise<void> {
  reset();
  let client: PanelGatewayClient;
  client = new PanelGatewayClient(
    (event) => { if (client === gw) onEvent(event); },
    (state) => { if (client === gw) { conn = state; render(); } },
    (message) => { if (client === gw) showError(message); },
  );
  gw = client;
  try {
    await client.connect();
    const created = await client.request<PanelCreate>("session.create", {});
    sid = created.session_id;
    history.setActive(sid);
    info = created.info ?? {};
    runStatus = "idle";
    render();
    void history.refresh();
  } catch (err) {
    conn = "error";
    showError(err instanceof Error ? err.message : String(err));
  }
}

function reset(): void {
  gw?.close();
  sid = null;
  history.setActive("");
  info = {};
  run = false;
  runStatus = "connecting";
  errorText = "";
  pendingPrompt = null;
  activeAssistant = null;
  tools = [];
  attached = [];
  refs.input.value = "";
  render();
}

function onEvent(event: PanelEvent): void {
  const payload = rec(event.payload) ? event.payload : {};
  switch (event.type) {
    case "session.info":
      if (event.session_id) sid = event.session_id;
      if (event.session_id) history.ensureActive(event.session_id);
      info = { ...info, ...payload };
      runStatus = "idle";
      return render();
    case "message.start":
      run = true;
      activeAssistant = id("assistant");
      msgs.push({ id: activeAssistant, role: "assistant", text: "", status: "streaming" });
      return render();
    case "message.delta":
      return addAssistant(str(payload, "text"));
    case "message.complete":
      return completeAssistant(str(payload, "text"));
    case "status.update":
      runStatus = str(payload, "text") || str(payload, "kind") || "running";
      return render();
    case "tool.start":
      return startTool(payload);
    case "tool.progress":
      return updateTool(payload);
    case "tool.complete":
      return finishTool(payload);
    case "clarify.request":
      pendingPrompt = { kind: "clarify", requestId: str(payload, "request_id"), question: str(payload, "question") || "Clarification needed", choices: choices(payload.choices) };
      run = true;
      return render();
    case "approval.request":
      pendingPrompt = { kind: "approval", command: str(payload, "command"), description: str(payload, "description") || "Approval needed" };
      run = true;
      return render();
    case "sudo.request":
      pendingPrompt = { kind: "sudo", requestId: str(payload, "request_id") };
      run = true;
      return render();
    case "secret.request":
      pendingPrompt = { kind: "secret", requestId: str(payload, "request_id"), prompt: str(payload, "prompt") || "Secret required" };
      run = true;
      return render();
    case "error":
      run = false;
      return showError(str(payload, "message") || "gateway error");
    default:
      return;
  }
}

function choices(value: unknown): string[] | undefined {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : undefined;
}
function addMsg(role: Role, text: string, attachments?: PanelAttach[]): void {
  msgs.push({ id: id(role), role, text, status: "complete", attachments: attachments?.length ? attachments : undefined });
  renderMessages();
}
function addAssistant(text: string): void {
  if (!text) return;
  if (!activeAssistant) {
    activeAssistant = id("assistant");
    msgs.push({ id: activeAssistant, role: "assistant", text: "", status: "streaming" });
  }
  const msg = msgs.find((entry) => entry.id === activeAssistant);
  if (msg) msg.text += text;
  renderMessages();
}
function completeAssistant(text?: string): void {
  const msg = msgs.find((entry) => entry.id === activeAssistant);
  if (msg) {
    if (text?.trim()) msg.text = text;
    msg.status = "complete";
  } else if (text?.trim()) addMsg("assistant", text);
  activeAssistant = null;
  run = false;
  runStatus = "idle";
  history.remember();
  render();
}
function showError(message: string): void {
  errorText = message;
  addMsg("system", `error: ${message}`);
  render();
}

function startTool(payload: Record<string, unknown>): void {
  const toolId = str(payload, "tool_id");
  if (!toolId) return;
  const name = str(payload, "name") || "tool";
  const context = str(payload, "context");
  runStatus = toolStatusLabel(name);
  tools.push({ id: id("tool"), toolId, name, context, status: "running" });
  tools = tools.slice(-24);
  msgs.push({
    id: id("toolmsg"),
    role: "tool",
    text: toolMessageText(name, context),
    status: "streaming",
    toolId,
    toolName: name,
  });
  render();
}
function updateTool(payload: Record<string, unknown>): void {
  const toolId = str(payload, "tool_id");
  const name = str(payload, "name");
  const preview = str(payload, "preview");
  const match = toolId
    ? tools.findLast((tool) => tool.toolId === toolId)
    : tools.findLast((tool) => tool.name === name && tool.status === "running");
  if (match) match.preview = preview || match.preview;
  if (match) runStatus = toolStatusLabel(match.name);
  const msg = toolId
    ? msgs.find((entry) => entry.toolId === toolId)
    : msgs.findLast((entry) => entry.role === "tool" && entry.toolName === name && entry.status === "streaming");
  if (msg && preview) msg.text = toolMessageText(msg.toolName || name || "tool", "", preview);
  render();
}
function finishTool(payload: Record<string, unknown>): void {
  const match = tools.findLast((tool) => tool.toolId === str(payload, "tool_id"));
  if (!match) return;
  match.summary = str(payload, "summary");
  match.error = str(payload, "error");
  match.status = match.error ? "error" : "done";
  runStatus = match.error ? "Tool error" : "Finalizing";
  const msg = msgs.find((entry) => entry.toolId === match.toolId);
  if (msg) {
    msg.status = "complete";
    msg.text = match.error
      ? `${match.name} failed\n${match.error}`
      : match.summary || `${match.name} complete`;
  }
  render();
}

async function upload(file: File): Promise<void> {
  if (!file.type.startsWith("image/")) return showError("Only image files are accepted.");
  if (!gw || !sid) return showError("Session is not ready.");
  refs.attach.disabled = true;
  runStatus = "uploading";
  render();
  try {
    const res = await fetch("/hermes/upload", {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream", "X-Hermes-Filename": file.name || "upload.png" },
      body: file,
    });
    if (!res.ok) throw new Error(await res.text());
    const raw: unknown = await res.json();
    if (!rec(raw) || !str(raw, "path")) throw new Error("Invalid upload response");
    await attachPath(str(raw, "path"), file);
    runStatus = run ? runStatus : "idle";
  } catch (err) {
    showError(err instanceof Error ? err.message : String(err));
  } finally {
    refs.attach.disabled = false;
    render();
  }
}

async function attachPath(path: string, file?: File): Promise<void> {
  if (!gw || !sid) throw new Error("Session is not ready.");
  const detected = await gw.request<PanelDrop>("input.detect_drop", { session_id: sid, text: path });
  const result = detected.matched ? detected : await gw.request<PanelDrop>("image.attach", { session_id: sid, path });
  const realPath = result.path || path;
  const name = result.name || file?.name || basename(realPath);
  const meta = attachMeta(result);
  const label = result.text || `[User attached image: ${name}]`;
  const toolHint = `[Attached image path for tools: ${realPath}]`;
  attached.push({ id: id("attach"), name, path: realPath, text: `${label}\n${toolHint}`, meta, previewUrl: file ? URL.createObjectURL(file) : undefined });
  renderAttachments();
  render();
}
function attachMeta(result: PanelDrop): string | undefined {
  const bits: string[] = [];
  if (result.width && result.height) bits.push(`${result.width}x${result.height}`);
  if (result.token_estimate) bits.push(`~${result.token_estimate} tokens`);
  if (result.count) bits.push(`image #${result.count}`);
  return bits.length ? bits.join(" / ") : undefined;
}

async function submit(): Promise<void> {
  if (!gw || !sid || run) return;
  const text = refs.input.value.trim();
  const outboundAttachments = [...attached];
  if (!text && !outboundAttachments.length) return;
  refs.input.value = "";
  resizeInput();
  attached = [];
  renderAttachments();
  addMsg("user", text || "[attachment]", outboundAttachments);
  history.remember(text || outboundAttachments[0]?.name || "New session");
  errorText = "";
  run = true;
  runStatus = "running";
  render();
  try {
    if (!outboundAttachments.length && text.startsWith("/")) await slash(text);
    else await sendAgent([outboundAttachments.map((item) => item.text).join("\n"), text].filter(Boolean).join("\n\n"));
  } catch (err) {
    run = false;
    showError(err instanceof Error ? err.message : String(err));
  }
}
async function sendAgent(text: string): Promise<void> {
  if (!gw || !sid) throw new Error("Session is not ready.");
  await gw.request("prompt.submit", { session_id: sid, text });
}
async function slash(command: string): Promise<void> {
  if (!gw || !sid) return;
  const name = command.replace(/^\/+/, "").split(/\s+/, 1)[0] || "";
  const arg = command.replace(/^\/+\S+\s*/, "").trim();
  try {
    const res = await gw.request<Record<string, unknown>>("slash.exec", { command: command.replace(/^\/+/, ""), session_id: sid });
    addMsg("system", [str(res, "warning") ? `warning: ${str(res, "warning")}` : "", str(res, "output") || `/${name}: no output`].filter(Boolean).join("\n"));
    run = false;
    return render();
  } catch {
    const res = await gw.request<unknown>("command.dispatch", { name, arg, session_id: sid });
    if (!rec(res) || typeof res.type !== "string") throw new Error("invalid command.dispatch response");
    if (res.type === "send" || res.type === "skill") {
      const msg = str(res, "message");
      if (!msg) throw new Error("empty command message");
      addMsg("user", msg);
      await sendAgent(msg);
      return;
    }
    if (res.type === "alias") return slash(`/${str(res, "target")}${arg ? ` ${arg}` : ""}`);
    addMsg("system", str(res, "output") || "(no output)");
    run = false;
    render();
  }
}

async function stopRun(): Promise<void> {
  if (!gw || !sid) return;
  try {
    await gw.request("session.interrupt", { session_id: sid }, 15_000);
    pendingPrompt = null;
    run = false;
    runStatus = "interrupted";
    render();
  } catch (err) {
    showError(err instanceof Error ? err.message : String(err));
  }
}

async function answerClarify(answer: string): Promise<void> {
  if (!gw || pendingPrompt?.kind !== "clarify") return;
  const requestId = pendingPrompt.requestId;
  pendingPrompt = null;
  await gw.request("clarify.respond", { request_id: requestId, answer });
  addMsg("user", answer);
  runStatus = "running";
  render();
}
async function answerApproval(choice: "once" | "deny"): Promise<void> {
  if (!gw || !sid) return;
  pendingPrompt = null;
  await gw.request("approval.respond", { session_id: sid, choice, all: false });
  runStatus = choice === "deny" ? "denied" : "running";
  render();
}
async function answerSecret(value: string): Promise<void> {
  if (!gw || !pendingPrompt) return;
  const current = pendingPrompt;
  pendingPrompt = null;
  if (current.kind === "sudo") await gw.request("sudo.respond", { request_id: current.requestId, password: value });
  if (current.kind === "secret") await gw.request("secret.respond", { request_id: current.requestId, value });
  runStatus = "running";
  render();
}

function activateHistory(view: PanelHistoryActivation): void {
  sid = view.sessionId;
  history.setActive(view.activeId);
  info = rec(view.info) ? { provider: str(view.info, "provider"), model: str(view.info, "model") } : {};
  msgs = view.messages;
  tools = [];
  pendingPrompt = null;
  activeAssistant = null;
  for (const item of attached) if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
  attached = [];
  run = false;
  runStatus = "idle";
  errorText = "";
  renderAttachments();
  render();
}

function render(): void {
  refs.connection.textContent = conn;
  const displayStatus = run ? phaseStatusLabel(runStatus) : runStatus;
  refs.run.textContent = displayStatus;
  refs.statusLabel.textContent = run ? displayStatus : conn;
  refs.statusPill.dataset.state = conn;
  refs.model.textContent = info.model || "pending";
  refs.provider.textContent = info.provider || "gateway";
  refs.session.textContent = sid ? sid.slice(0, 10) : "pending";
  renderInputState();
  refs.stop.disabled = !run || !sid;
  refs.error.hidden = !errorText;
  refs.error.textContent = errorText;
  history.render();
  renderMessages();
  renderTools();
  renderPrompt();
}
function renderMessages(): void {
  refs.messages.replaceChildren();
  if (!msgs.length) {
    const empty = div("empty-state", conn === "open" ? "Session ready." : "Connecting.");
    refs.messages.append(empty);
    return;
  }
  for (const msg of msgs) {
    const card = document.createElement("article");
    card.className = `message ${msg.role}`;
    if (msg.status === "streaming") card.dataset.streaming = "true";
    card.append(div("message-label", msg.role));
    if (msg.attachments?.length) {
      const list = document.createElement("div");
      list.className = "message-attachments";
      for (const item of msg.attachments) list.append(div("", item.name, "span"));
      card.append(list);
    }
    const body = document.createElement("div");
    body.className = "message-body";
    if (msg.text) {
      appendRichText(body, msg.text);
    } else if (msg.status === "streaming") {
      const placeholder = document.createElement("span");
      placeholder.className = "stream-placeholder";
      placeholder.textContent = phaseStatusLabel(runStatus);
      body.append(placeholder);
    }
    card.append(body);
    const media = mediaPreview(msg.text);
    if (media) card.append(media);
    refs.messages.append(card);
  }
  refs.messages.scrollTop = refs.messages.scrollHeight;
}
function appendRichText(container: HTMLElement, text: string): void {
  const copy = text.replace(/!\[([^\]]*)]\((https?:\/\/[^)\s]+)\)/g, (_all, alt) => String(alt || "").trim());
  if (!copy.trim()) return;
  const block = document.createElement("p");
  block.className = "message-copy";
  appendInlineText(block, copy);
  container.append(block);
}
function appendInlineText(container: HTMLElement, text: string): void {
  const pattern = /\[([^\]]+)]\((https?:\/\/[^)\s]+)\)|(https?:\/\/[^\s<>"']+)/g;
  let index = 0;
  for (const match of text.matchAll(pattern)) {
    const start = match.index ?? 0;
    if (start > index) container.append(document.createTextNode(text.slice(index, start)));
    const markdownUrl = match[2];
    const bareUrl = match[3];
    const rawUrl = markdownUrl || bareUrl || "";
    const cleanUrl = normalizeUrl(rawUrl);
    const label = markdownUrl ? match[1] : cleanUrl;
    const link = document.createElement("a");
    link.className = "message-link";
    link.href = cleanUrl;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = label;
    container.append(link);
    if (bareUrl && rawUrl.length > cleanUrl.length) container.append(document.createTextNode(rawUrl.slice(cleanUrl.length)));
    index = start + match[0].length;
  }
  if (index < text.length) container.append(document.createTextNode(text.slice(index)));
}
function mediaPreview(text: string): HTMLElement | null {
  const mediaRefs = extractMedia(text).slice(0, 4);
  if (!mediaRefs.length) return null;
  const wrap = div("media-preview", "");
  for (const media of mediaRefs) {
    const url = media.url;
    if (media.kind === "video") {
      const video = document.createElement("video");
      video.src = url;
      video.controls = true;
      video.playsInline = true;
      wrap.append(video);
    } else {
      const image = document.createElement("img");
      image.src = url;
      image.alt = media.alt || "";
      image.loading = "lazy";
      image.decoding = "async";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("load", () => {
        refs.messages.scrollTop = refs.messages.scrollHeight;
      });
      wrap.append(image);
    }
  }
  return wrap;
}
function extractMedia(text: string): MediaRef[] {
  const refs: MediaRef[] = [];
  const seen = new Set<string>();
  const add = (rawUrl: string, alt?: string) => {
    const url = normalizeUrl(rawUrl);
    const kind = mediaKind(url);
    if (!kind || seen.has(url)) return;
    seen.add(url);
    refs.push({ url, kind, alt });
  };
  for (const match of text.matchAll(/!\[([^\]]*)]\((https?:\/\/[^)\s]+)\)/g)) add(match[2], match[1]);
  for (const match of text.matchAll(/\[([^\]]+)]\((https?:\/\/[^)\s]+)\)/g)) add(match[2], match[1]);
  for (const match of text.matchAll(/https?:\/\/[^\s<>"']+/g)) add(match[0]);
  return refs;
}
function normalizeUrl(raw: string): string {
  return raw.trim().replace(/[),.;!?\uFF0C\u3002\uFF1B\uFF01\uFF1F]+$/u, "");
}
function mediaKind(url: string): "image" | "video" | null {
  const path = url.split(/[?#]/, 1)[0].toLowerCase();
  if (/\.(mp4|webm|mov)$/.test(path)) return "video";
  if (/\.(png|jpe?g|webp|gif)$/.test(path)) return "image";
  return null;
}
function renderTools(): void {
  refs.tools.replaceChildren();
  if (!tools.length) return refs.tools.append(div("empty-tools", "Idle"));
  for (const tool of [...tools].reverse()) {
    const card = document.createElement("article");
    card.className = "tool-card";
    card.dataset.status = tool.status;
    card.append(div("tool-title", tool.name), div("tool-meta", tool.context || tool.status));
    const detail = tool.error || tool.summary || tool.preview;
    if (detail) {
      const body = document.createElement("p");
      body.textContent = detail;
      card.append(body);
    }
    refs.tools.append(card);
  }
}
function renderAttachments(): void {
  refs.shelf.replaceChildren();
  refs.shelf.hidden = !attached.length;
  for (const item of attached) {
    const chip = div("attachment-chip", "");
    if (item.previewUrl) {
      const image = document.createElement("img");
      image.src = item.previewUrl;
      image.alt = "";
      chip.append(image);
    }
    chip.append(div("", item.meta ? `${item.name} / ${item.meta}` : item.name, "span"));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.title = "Remove";
    remove.textContent = "x";
    remove.addEventListener("click", () => {
      attached = attached.filter((entry) => entry.id !== item.id);
      if (item.previewUrl) URL.revokeObjectURL(item.previewUrl);
      renderAttachments();
      render();
    });
    chip.append(remove);
    refs.shelf.append(chip);
  }
}
function renderPrompt(): void {
  refs.pending.replaceChildren();
  refs.pending.hidden = !pendingPrompt;
  if (!pendingPrompt) return;
  refs.pending.append(strong(pendingPrompt.kind));
  if (pendingPrompt.kind === "approval") {
    refs.pending.append(para(pendingPrompt.description || pendingPrompt.command), promptButton("Approve once", () => answerApproval("once")), promptButton("Deny", () => answerApproval("deny")));
    return;
  }
  refs.pending.append(para(pendingPrompt.kind === "clarify" ? pendingPrompt.question : pendingPrompt.kind === "secret" ? pendingPrompt.prompt : "Password"));
  if (pendingPrompt.kind === "clarify" && pendingPrompt.choices?.length) {
    for (const choice of pendingPrompt.choices) refs.pending.append(promptButton(choice, () => answerClarify(choice)));
    return;
  }
  const input = document.createElement("input");
  input.type = pendingPrompt.kind === "clarify" ? "text" : "password";
  input.autocomplete = "off";
  refs.pending.append(input, promptButton("Send", () => pendingPrompt?.kind === "clarify" ? answerClarify(input.value) : answerSecret(input.value)));
}
function div(className: string, text: string, tag = "div"): HTMLElement {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}
function para(text: string): HTMLParagraphElement {
  const node = document.createElement("p");
  node.textContent = text;
  return node;
}
function strong(text: string): HTMLElement {
  const node = document.createElement("strong");
  node.textContent = text;
  return node;
}
function promptButton(text: string, fn: () => Promise<void>): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "prompt-action";
  button.textContent = text;
  button.addEventListener("click", () => void fn());
  return button;
}
function resizeInput(): void {
  refs.input.style.height = "0px";
  refs.input.style.height = `${Math.min(180, refs.input.scrollHeight)}px`;
}
function renderInputState(): void {
  refs.send.disabled = conn !== "open" || !sid || run || (!refs.input.value.trim() && !attached.length);
}

refs.form.addEventListener("submit", (event) => { event.preventDefault(); void submit(); });
refs.input.addEventListener("input", () => { resizeInput(); renderInputState(); });
refs.input.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); } });
refs.attach.addEventListener("click", () => refs.file.click());
refs.file.addEventListener("change", () => { const file = refs.file.files?.[0]; refs.file.value = ""; if (file) void upload(file); });
refs.form.addEventListener("dragover", (event) => { event.preventDefault(); refs.form.classList.add("drop-active"); });
refs.form.addEventListener("dragleave", () => refs.form.classList.remove("drop-active"));
refs.form.addEventListener("drop", (event) => { event.preventDefault(); refs.form.classList.remove("drop-active"); const file = event.dataTransfer?.files?.[0]; if (file) void upload(file); });
refs.stop.addEventListener("click", () => void stopRun());
refs.reconnect.addEventListener("click", () => void connect());
refs.fresh.addEventListener("click", () => void connect());
window.addEventListener("beforeunload", () => { gw?.close(); for (const item of attached) if (item.previewUrl) URL.revokeObjectURL(item.previewUrl); });

render();
void connect();
