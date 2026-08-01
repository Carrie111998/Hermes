export const USER =
  new URLSearchParams(location.search).get("user") || "demo";

export interface Component {
  id: string;
  type: string;
  title: string;
  col?: number;
  row?: number;
  w: number;
  h: number;
  hidden?: boolean;
  props: Record<string, any>;
}

export interface Layout {
  title: string;
  components: Component[];
  chat_dock?: { position: string; visible?: boolean };
  grid?: { columns: number };
  _meta?: { version: number; source: string };
}

export interface Proposal {
  id: string;
  engine: string;
  summary: string;
  rationale?: string;
  created_at: number;
  mutations: any[];
}

export interface StationState {
  onboarded: boolean;
  layout: Layout;
  workflows: any[];
  proposals: Proposal[];
  agent: { model?: string; live?: boolean };
  connections: { id: string; name: string; connected: boolean; detail: string }[];
  curator: { interval_s: number; last_run: number; runs: number };
}

export async function api<T = any>(path: string, opts?: RequestInit): Promise<T> {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()).slice(0, 300));
  return r.json();
}

export function post<T = any>(path: string, body: any): Promise<T> {
  return api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/* telemetry batcher */
const queue: any[] = [];
export function track(type: string, componentId?: string | null, payload?: any) {
  queue.push({ type, component_id: componentId || null, payload: payload || null });
}
setInterval(async () => {
  if (!queue.length) return;
  const events = queue.splice(0);
  try {
    await post("/api/telemetry", { user_id: USER, events });
  } catch {
    queue.unshift(...events);
  }
}, 4000);

export function when(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
}
