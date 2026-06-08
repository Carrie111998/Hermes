export type PanelHistoryRole = "user" | "assistant" | "system" | "tool";

export interface PanelHistoryRow {
  id: string;
  title: string;
  preview: string;
  startedAt: number;
  messageCount: number;
  source: string;
}

export interface PanelHistoryMessage {
  id: string;
  role: PanelHistoryRole;
  text: string;
  status: "complete";
}

export interface PanelHistoryActivation {
  sessionId: string;
  activeId: string;
  info?: unknown;
  messages: PanelHistoryMessage[];
}

interface PanelHistoryListResult { sessions?: unknown[] }
interface PanelHistoryResumeResult { session_id: string; resumed?: string; messages?: unknown[]; info?: unknown }

interface PanelHistoryControllerOptions {
  listElement: HTMLElement;
  refreshButton: HTMLButtonElement;
  request<T>(method: string, params: Record<string, unknown>, timeoutMs?: number): Promise<T>;
  makeId(prefix: string): string;
  getSessionId(): string | null;
  getMessages(): Array<{ role: string; text: string }>;
  isRunning(): boolean;
  activate(view: PanelHistoryActivation): void;
  onError(message: string): void;
}

const CACHE_KEY = "ultra-studio-agent.history.v1";

export function createPanelHistoryController(options: PanelHistoryControllerOptions) {
  let rows = loadPanelHistoryCache();
  let activeId = "";

  const render = () => {
    options.listElement.replaceChildren();
    if (!rows.length) {
      options.listElement.append(node("history-empty", "No saved chats yet."));
      return;
    }
    for (const row of rows.slice(0, 30)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "history-item";
      button.disabled = options.isRunning();
      if (row.id === activeId) button.dataset.active = "true";
      button.append(node("history-title", row.title), node("history-preview", row.preview || "No preview"), node("history-meta", formatPanelHistoryMeta(row)));
      button.addEventListener("click", () => void open(row));
      options.listElement.append(button);
    }
  };

  const refresh = async () => {
    options.refreshButton.disabled = true;
    try {
      const result = await options.request<PanelHistoryListResult>("session.list", { limit: 40 }, 15_000);
      rows = mergePanelHistory(normalizePanelHistory(result.sessions), rows);
      savePanelHistoryCache(rows);
    } catch (err) {
      options.onError(`history refresh failed: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      options.refreshButton.disabled = false;
      render();
    }
  };

  const remember = (seed = "") => {
    const sid = options.getSessionId();
    if (!sid) return;
    rows = mergePanelHistory([rowFromPanelMessages(activeId || sid, options.getMessages(), seed)], rows);
    savePanelHistoryCache(rows);
    render();
  };

  const setActive = (id: string) => {
    activeId = id;
    render();
  };

  const ensureActive = (id: string) => {
    if (!activeId) setActive(id);
  };

  const open = async (row: PanelHistoryRow) => {
    if (options.isRunning()) return;
    try {
      const result = await options.request<PanelHistoryResumeResult>("session.resume", { session_id: row.id }, 30_000);
      const nextActive = result.resumed || row.id;
      activeId = nextActive;
      options.activate({
        sessionId: result.session_id,
        activeId: nextActive,
        info: result.info,
        messages: panelMessagesFromHistory(result.messages, options.makeId),
      });
      void refresh();
    } catch (err) {
      options.onError(err instanceof Error ? err.message : String(err));
    }
  };

  options.refreshButton.addEventListener("click", () => void refresh());
  render();
  return { render, refresh, remember, setActive, ensureActive };
}

export function normalizePanelHistory(input: unknown): PanelHistoryRow[] {
  if (!Array.isArray(input)) return [];
  const rows: PanelHistoryRow[] = [];
  for (const item of input) {
    if (!rec(item)) continue;
    const id = txt(item.id);
    if (!id) continue;
    rows.push({
      id,
      title: sliceText(txt(item.title) || txt(item.preview) || "Untitled session", 56),
      preview: sliceText(txt(item.preview), 96),
      startedAt: num(item.started_at) || num(item.startedAt) || 0,
      messageCount: num(item.message_count) || num(item.messageCount) || 0,
      source: txt(item.source),
    });
  }
  return rows;
}

export function loadPanelHistoryCache(): PanelHistoryRow[] {
  try {
    return normalizePanelHistory(JSON.parse(localStorage.getItem(CACHE_KEY) || "[]"));
  } catch {
    return [];
  }
}

export function savePanelHistoryCache(rows: PanelHistoryRow[]): void {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(rows.slice(0, 50)));
  } catch {
    return;
  }
}

export function mergePanelHistory(primary: PanelHistoryRow[], secondary: PanelHistoryRow[] = []): PanelHistoryRow[] {
  const byId = new Map<string, PanelHistoryRow>();
  for (const row of secondary) byId.set(row.id, row);
  for (const row of primary) byId.set(row.id, row);
  return [...byId.values()]
    .sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0))
    .slice(0, 50);
}

export function rowFromPanelMessages(id: string, messages: Array<{ role: string; text: string }>, seed = ""): PanelHistoryRow {
  const userText = messages.find((msg) => msg.role === "user" && msg.text.trim())?.text || seed;
  const preview = [...messages].reverse().find((msg) => msg.text.trim())?.text || seed;
  return {
    id,
    title: sliceText(cleanText(userText) || "New creative session", 56),
    preview: sliceText(cleanText(preview), 96),
    startedAt: Date.now() / 1000,
    messageCount: messages.length,
    source: "panel",
  };
}

export function panelMessagesFromHistory(input: unknown, makeId: (prefix: string) => string): PanelHistoryMessage[] {
  if (!Array.isArray(input)) return [];
  const out: PanelHistoryMessage[] = [];
  for (const item of input) {
    if (!rec(item)) continue;
    const role = normalizeRole(txt(item.role));
    const text = cleanText(txt(item.text) || txt(item.content) || [txt(item.name), txt(item.context)].filter(Boolean).join("\n"));
    if (!role || !text) continue;
    out.push({ id: makeId(role), role, text, status: "complete" });
  }
  return out;
}

export function formatPanelHistoryMeta(row: PanelHistoryRow): string {
  const date = row.startedAt ? new Date(row.startedAt * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "local";
  const count = row.messageCount ? `${row.messageCount} msgs` : "draft";
  return row.source ? `${date} / ${count} / ${row.source}` : `${date} / ${count}`;
}

function normalizeRole(role: string): PanelHistoryRole | null {
  return role === "user" || role === "assistant" || role === "system" || role === "tool" ? role : null;
}

function cleanText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

function sliceText(text: string, limit: number): string {
  const clean = cleanText(text);
  return clean.length > limit ? `${clean.slice(0, limit - 3)}...` : clean;
}

function rec(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function txt(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function node(className: string, text: string): HTMLElement {
  const element = document.createElement("div");
  element.className = className;
  element.textContent = text;
  return element;
}
