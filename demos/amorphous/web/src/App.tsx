/* Hermes Station — main app: draggable/resizable grid, proposals, onboarding. */
import { useCallback, useEffect, useRef, useState } from "react";
import { ReactGridLayout, WidthProvider } from "react-grid-layout/legacy";
import type { LayoutItem } from "react-grid-layout";
import {
  FlaskConical, LayoutGrid, MessageSquare, Eye, Check, X as XIcon, Plus,
} from "lucide-react";
import Card from "./components/Card";
import ChatDock, { type ChatMsg } from "./components/ChatDock";
import Onboarding from "./Onboarding";
import { api, post, track, when, USER, type Component, type Proposal, type StationState } from "./lib/api";

export default function App() {
  const [state, setState] = useState<StationState | null>(null);
  const [preview, setPreview] = useState<{ p: Proposal; spec: any; diff: any[] } | null>(null);
  const [trayOpen, setTrayOpen] = useState(false);
  const [dockPos, setDockPos] = useState<"bottom" | "right">("bottom");
  const [dockCollapsed, setDockCollapsed] = useState(false);
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [chatBusy, setChatBusy] = useState(false);
  const [toast, setToast] = useState("");
  const toastTimer = useRef<any>(null);

  const showToast = useCallback((t: string) => {
    setToast(t);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(""), 2800);
  }, []);

  const load = useCallback(async () => {
    const s = await api<StationState>(`/api/state?user_id=${USER}`);
    setState(s);
    if (s.onboarded) setDockPos((s.layout.chat_dock?.position as any) || "bottom");
    return s;
  }, []);

  useEffect(() => {
    load().then((s) => {
      if (s?.onboarded && msgs.length === 0) {
        setMsgs([{ who: "hermes", text: "Station online. Drag cards by their header to rearrange; pull the corner to resize. Right-click a card to work with just that component. Tell me anything you want changed." }]);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* SSE live updates */
  useEffect(() => {
    let es: EventSource | null = null;
    let stop = false;
    const connect = () => {
      es = new EventSource("/api/events");
      es.onmessage = async (e) => {
        try {
          const ev = JSON.parse(e.data);
          if (preview) return;
          if (ev.kind === "layout_changed") {
            await load();
            showToast("Dashboard updated by Hermes");
          } else if (ev.kind === "proposal") {
            await load();
            setMsgs((m) => [...m, { who: "hermes", text: "⚗ New evolution proposal — open the Proposals tray to preview it." }]);
          } else if (ev.kind === "tool" && ev.scope === "main") {
            setMsgs((m) => [...m, { who: "tool", text: ev.name }]);
          }
        } catch { /* ignore */ }
      };
      es.onerror = () => { es?.close(); if (!stop) setTimeout(connect, 4000); };
    };
    connect();
    return () => { stop = true; es?.close(); };
  }, [load, preview, showToast]);

  if (!state) return <Center><span className="shimmer h-4 w-40 rounded" /></Center>;
  if (!state.onboarded) return <Onboarding onDone={load} />;

  const spec = preview ? preview.spec : state.layout;
  const visible: Component[] = (spec.components || []).filter((c: Component) => !c.hidden);
  const hidden: Component[] = (spec.components || []).filter((c: Component) => c.hidden);

  /* ---- grid interop ---- */
  const rglLayout: LayoutItem[] = visible.map((c) => ({
    i: c.id, x: c.col ?? 0, y: c.row ?? 0, w: c.w, h: c.h, minW: 2, minH: 1,
  }));

  const persistLayout = async (items: LayoutItem[]) => {
    if (preview) return;
    const next = JSON.parse(JSON.stringify(state.layout));
    for (const it of items) {
      const c = next.components.find((x: Component) => x.id === it.i);
      if (c) { c.col = it.x; c.row = it.y; c.w = it.w; c.h = it.h; }
    }
    setState({ ...state, layout: next });
    await post("/api/layout", { user_id: USER, spec: next });
  };

  const mutateLocal = async (fn: (spec: any) => void, evt?: [string, string]) => {
    const next = JSON.parse(JSON.stringify(state.layout));
    fn(next);
    setState({ ...state, layout: next });
    if (evt) track(evt[0], evt[1]);
    await post("/api/layout", { user_id: USER, spec: next });
    await load();
  };

  const hideComp = (id: string) =>
    mutateLocal((s) => { const c = s.components.find((x: any) => x.id === id); if (c) c.hidden = true; }, ["hide", id]);
  const showComp = (id: string) =>
    mutateLocal((s) => { const c = s.components.find((x: any) => x.id === id); if (c) { c.hidden = false; c.col = undefined; c.row = undefined; } }, ["show", id]);
  const removeComp = (id: string) => {
    if (!confirm("Remove this component?")) return;
    track("remove", id);
    mutateLocal((s) => { s.components = s.components.filter((x: any) => x.id !== id); });
  };

  /* ---- chat ---- */
  const sendChat = async (text: string) => {
    setMsgs((m) => [...m, { who: "you", text }]);
    setChatBusy(true);
    try {
      const r = await post("/api/chat", { user_id: USER, text });
      setMsgs((m) => [...m, { who: "hermes", text: r.reply }]);
      if (r.proposal) { await load(); setTrayOpen(true); }
      else await load();
    } catch (e: any) {
      setMsgs((m) => [...m, { who: "hermes", text: `⚠ ${e.message}` }]);
    }
    setChatBusy(false);
  };

  /* ---- proposals ---- */
  const tryProposal = async (p: Proposal) => {
    const pv = await api(`/api/proposal/${p.id}/preview`);
    setPreview({ p, spec: pv.preview, diff: pv.diff });
    setTrayOpen(false);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  const actProposal = async (pid: string, action: string, feedback = "", sentiment = "") => {
    await post(`/api/proposal/${pid}`, { action, feedback, sentiment });
    setPreview(null);
    await load();
    showToast(action === "approve" ? "Applied — dashboard updated" : "Rejected — the curator will steer away from this");
  };
  const evolveNow = async () => {
    showToast("Curator reviewing your usage…");
    const r = await post(`/api/curator/run?user_id=${USER}`, {});
    await load();
    if (r.proposal) setTrayOpen(true);
    else showToast("Curator: nothing worth changing yet");
  };

  const moveDock = async () => {
    const next = dockPos === "bottom" ? "right" : "bottom";
    setDockPos(next);
    track("move", "chat-dock", { position: next });
    const spec2 = JSON.parse(JSON.stringify(state.layout));
    spec2.chat_dock = { ...(spec2.chat_dock || {}), position: next };
    await post("/api/layout", { user_id: USER, spec: spec2 });
  };

  const gridPadRight = dockPos === "right" ? 418 : 0;

  /* stats strip numbers */
  const nComps = visible.length;
  const nWorkflows = state.workflows.length;
  const nConnected = state.connections.filter((c) => c.connected).length;
  const layoutVersion = state.layout._meta?.version ?? 1;

  return (
    <div className="min-h-screen flex">
      {/* ===== left sidebar ===== */}
      <aside className="w-[228px] shrink-0 bg-panel border-r border-line flex flex-col fixed top-0 bottom-0 left-0 z-40">
        <div className="h-14 flex items-center gap-2.5 px-4 border-b border-line shrink-0">
          <span className="w-7 h-7 rounded-lg bg-blue/15 text-blue-2 flex items-center justify-center text-[16px]">☤</span>
          <div className="leading-tight">
            <div className="text-[14px] w590">Hermes Station</div>
            <div className="text-[10px] text-ink-4 uppercase tracking-[0.08em]">amorphous apps</div>
          </div>
        </div>
        <nav className="flex-1 overflow-auto py-3">
          <div className="microlabel px-4 pb-1.5">Station</div>
          <SideItem active icon={<LayoutGrid size={15} />} label="Dashboard" />
          <SideItem icon={<FlaskConical size={15} />} label="Evolve now" onClick={evolveNow} />
          <SideItem icon={<Eye size={15} />} label={`Proposals`} badge={state.proposals.length}
                    onClick={() => setTrayOpen(!trayOpen)} />
          <div className="microlabel px-4 pb-1.5 pt-4">Connections</div>
          {state.connections.slice(0, 7).map((cn) => (
            <div key={cn.id} className="flex items-center gap-2.5 px-4 py-[5px] text-[12.5px] text-ink-3">
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${cn.connected ? "bg-green shadow-[0_0_5px] shadow-green" : "bg-ink-4/40"}`} />
              <span className="truncate">{cn.name}</span>
            </div>
          ))}
        </nav>
        <div className="border-t border-line px-4 py-3 shrink-0">
          <div className="flex items-center gap-2.5">
            <span className="w-7 h-7 rounded-full bg-blue/20 text-blue-2 flex items-center justify-center text-[11px] w590 uppercase">{USER.slice(0, 2)}</span>
            <div className="leading-tight min-w-0">
              <div className="text-[12.5px] w510 truncate">{USER}</div>
              <div className="text-[10.5px] text-ink-4 truncate">{state.agent.model || "agent"}</div>
            </div>
            <span className="ml-auto w-1.5 h-1.5 rounded-full bg-green shadow-[0_0_5px] shadow-green" />
          </div>
        </div>
      </aside>

      {/* ===== main column ===== */}
      <div className="flex-1 min-w-0 ml-[228px] h-screen flex flex-col">
      {/* top bar */}
      <header className="h-14 shrink-0 flex items-center justify-between px-5 border-b border-line bg-background/85 backdrop-blur-xl">
        <div className="flex items-center gap-3">
          <span className="text-[15.5px] w590">{state.layout.title || "Dashboard"}</span>
          <span className="hidden md:inline-flex items-center gap-1.5 h-6 px-2.5 rounded-full bg-green/10 text-green text-[11px] w510">
            <span className="w-1.5 h-1.5 rounded-full bg-green" /> live
          </span>
        </div>
        <div className="flex items-center gap-2">
          <TopBtn onClick={evolveNow}><FlaskConical size={14} /> Evolve</TopBtn>
          <TopBtn onClick={() => setTrayOpen(!trayOpen)}>
            <LayoutGrid size={14} /> Proposals
            <span className={`min-w-[18px] h-[18px] px-1 rounded-full text-[11px] font-bold inline-flex items-center justify-center ${state.proposals.length ? "bg-blue text-white" : "bg-line-2 text-ink-3"}`}>
              {state.proposals.length}
            </span>
          </TopBtn>
          <button onClick={() => setDockCollapsed(!dockCollapsed)}
                  className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-md bg-blue text-white text-[13px] w510 hover:bg-blue-2 transition-colors">
            <MessageSquare size={14} /> Chat
          </button>
        </div>
      </header>

      {/* stats strip */}
      {!preview && (
        <div className="flex items-stretch border-b border-line bg-panel/40">
          <Stat label="components" value={nComps} />
          <Stat label="workflows" value={nWorkflows} />
          <Stat label="connections live" value={nConnected} dot="green" />
          <Stat label="layout version" value={`v${layoutVersion}`} />
          <Stat label="curator runs" value={state.curator.runs} last />
        </div>
      )}

      {/* preview banner */}
      {preview && (
        <div className="sticky top-14 z-40 flex items-center gap-3 px-4 py-2.5 bg-blue/10 border-b border-blue/40 text-[13.5px]">
          <Eye size={15} className="text-blue-2 shrink-0" />
          <b className="text-blue-2 w590 shrink-0">Previewing proposal</b>
          <span className="flex-1 text-ink-2 truncate">
            {preview.diff.map((d) => `${d.change}: ${d.title}`).join(" · ") || "reflow only"}
          </span>
          <button onClick={() => actProposal(preview.p.id, "approve", "", "up")}
                  className="h-8 px-3.5 rounded-md bg-blue text-white text-[13px] w510 inline-flex items-center gap-1.5 hover:bg-blue-2 transition-colors">
            <Check size={13} /> Keep
          </button>
          <button onClick={() => {
            const why = prompt("Why keep the current layout? (optional — steers the curator)") || "";
            actProposal(preview.p.id, "reject", why, "down");
          }} className="h-8 px-3.5 rounded-md border border-line-2 bg-surface text-[13px] text-ink-2 inline-flex items-center gap-1.5 hover:bg-surface-2">
            <XIcon size={13} /> Go back
          </button>
        </div>
      )}

      {/* grid — scrolls between stats strip and docked chat */}
      <main style={{ paddingRight: gridPadRight }} className="flex-1 min-h-0 overflow-y-auto">
        <div>
        <GridBody
          visible={visible}
          rglLayout={rglLayout}
          preview={preview?.p.id}
          onPersist={persistLayout}
          onHide={hideComp}
          onRemove={removeComp}
        />
        {hidden.length > 0 && !preview && (
          <div className="flex items-center gap-2 flex-wrap px-4 py-2 text-[12.5px] text-ink-3">
            Hidden:
            {hidden.map((c) => (
              <button key={c.id} onClick={() => showComp(c.id)}
                      className="inline-flex items-center gap-1 h-7 px-2.5 rounded-md border border-line bg-surface text-ink-3 hover:border-line-2 hover:text-ink-2">
                <Plus size={12} /> {c.title}
              </button>
            ))}
          </div>
        )}
        </div>
      </main>

      {/* docked chat: bottom = in-flow structural panel; right = fixed side panel */}
      <ChatDock
        position={dockPos}
        collapsed={dockCollapsed}
        msgs={msgs}
        busy={chatBusy}
        onSend={sendChat}
        onMove={moveDock}
        onCollapse={() => setDockCollapsed(!dockCollapsed)}
      />

      {/* proposals tray */}
      {trayOpen && (
        <aside className="fixed top-14 right-0 bottom-0 w-[420px] z-[60] bg-panel border-l border-line overflow-auto p-4"
               style={{ paddingBottom: 40 }}>
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-[15px] font-semibold m-0">Evolution proposals</h3>
            <button onClick={() => setTrayOpen(false)} className="text-ink-3 hover:text-ink"><XIcon size={16} /></button>
          </div>
          {state.proposals.length === 0 && (
            <p className="text-[13px] text-ink-3 leading-relaxed">
              No pending proposals. The curator reviews your usage on a schedule, or press Evolve.
            </p>
          )}
          {state.proposals.map((p) => (
            <ProposalCard key={p.id} p={p} onTry={() => tryProposal(p)}
                          onAct={(a, f) => actProposal(p.id, a, f, a === "approve" ? "up" : "down")} />
          ))}
        </aside>
      )}

      {toast && (
        <div className="fixed left-1/2 -translate-x-1/2 z-[95] bg-surface-2 border border-line-2 rounded-lg px-4 py-2.5 text-[13.5px] shadow-[0_10px_36px_rgba(0,0,0,.5)]"
             style={{ bottom: 260 }}>
          {toast}
        </div>
      )}
      </div>
    </div>
  );
}

function SideItem({ icon, label, active, badge, onClick }: {
  icon: React.ReactNode; label: string; active?: boolean; badge?: number; onClick?: () => void;
}) {
  return (
    <button onClick={onClick}
            className={`relative w-full flex items-center gap-2.5 px-4 py-[7px] text-[13px] text-left transition-colors
              ${active ? "text-ink bg-blue/[0.08]" : "text-ink-3 hover:text-ink-2 hover:bg-surface"}`}>
      {active && <span className="absolute left-0 top-1 bottom-1 w-[3px] rounded-r bg-blue" />}
      <span className={active ? "text-blue-2" : ""}>{icon}</span>
      <span className={active ? "w510" : ""}>{label}</span>
      {badge != null && badge > 0 && (
        <span className="ml-auto min-w-[18px] h-[18px] px-1 rounded-full bg-blue text-white text-[10.5px] font-bold inline-flex items-center justify-center">
          {badge}
        </span>
      )}
    </button>
  );
}

function Stat({ label, value, dot, last }: { label: string; value: any; dot?: string; last?: boolean }) {
  return (
    <div className={`flex items-baseline gap-2.5 px-5 py-3 ${last ? "" : "border-r border-line"}`}>
      <span className="text-[22px] w590 tabular-nums leading-none">{value}</span>
      <span className="microlabel flex items-center gap-1.5">
        {dot && <span className="w-1.5 h-1.5 rounded-full bg-green" />}
        {label}
      </span>
    </div>
  );
}

const RGL = WidthProvider(ReactGridLayout);

function GridBody({ visible, rglLayout, preview, onPersist, onHide, onRemove }: {
  visible: Component[]; rglLayout: LayoutItem[]; preview?: string;
  onPersist: (l: LayoutItem[]) => void; onHide: (id: string) => void; onRemove: (id: string) => void;
}) {
  return (
    <RGL
      className="layout"
      layout={rglLayout as any}
      cols={12}
      rowHeight={96}
      margin={[10, 10]}
      containerPadding={[10, 10]}
      draggableHandle=".drag-handle"
      isDraggable={!preview}
      isResizable={!preview}
      compactType="vertical"
      onDragStop={(l: any) => onPersist([...l])}
      onResizeStop={(l: any) => onPersist([...l])}
    >
      {visible.map((c) => (
        <div key={c.id}>
          <Card c={c} preview={preview} onHide={onHide} onRemove={onRemove} />
        </div>
      ))}
    </RGL>
  );
}

function TopBtn({ children, onClick }: any) {
  return (
    <button onClick={onClick}
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg text-[13px] text-ink-2 hover:text-ink hover:bg-surface-2 border border-transparent">
      {children}
    </button>
  );
}

function ProposalCard({ p, onTry, onAct }: { p: Proposal; onTry: () => void; onAct: (a: string, f: string) => void }) {
  const [fb, setFb] = useState("");
  const label = (m: any) => {
    const t: Record<string, string> = {
      promote: `Promote ${m.component_id}`, shrink: `Shrink ${m.component_id}`,
      resize: `Resize ${m.component_id}`, hide: `Hide ${m.component_id}`,
      show: `Show ${m.component_id}`, remove: `Remove ${m.component_id}`,
      retitle: `Rename ${m.component_id} → "${m.title}"`,
      add: `Add ${m.component?.type}: "${m.component?.title}"`,
      set_props: `Reconfigure ${m.component_id}`, set_notes: "Refresh briefing",
      replace_spec: `Full rebuild (${m.spec?.components?.length ?? 0} components)`,
      move_chat_dock: `Chat dock → ${m.position}`,
    };
    return t[m.op] || m.op;
  };
  return (
    <div className="card-surface rounded-[10px] p-3.5 mt-3">
      <div className="text-[10.5px] uppercase tracking-wider text-ink-3 mb-1.5">{p.engine} · {when(p.created_at)}</div>
      <div className="text-[13.5px] w590 leading-snug">{p.summary}</div>
      <ul className="my-2.5 pl-4 text-[13px] text-ink-2 leading-relaxed list-disc">
        {p.mutations.slice(0, 8).map((m, i) => <li key={i}>{label(m)}</li>)}
      </ul>
      {p.rationale && <div className="text-[12.5px] text-ink-3 leading-relaxed">{p.rationale}</div>}
      <textarea value={fb} onChange={(e) => setFb(e.target.value)}
                placeholder="Optional feedback — steers the next evolution"
                className="w-full min-h-[44px] mt-2.5 px-2.5 py-2 bg-[#101a30] border border-line rounded-md text-[13px] text-ink placeholder:text-ink-4 outline-none focus:border-line-2 resize-y" />
      <div className="flex gap-2 mt-2.5">
        <ActionBtn onClick={onTry}><Eye size={13} /> Try it</ActionBtn>
        <ActionBtn primary onClick={() => onAct("approve", fb)}><Check size={13} /> Apply</ActionBtn>
        <ActionBtn destructive onClick={() => onAct("reject", fb)}><XIcon size={13} /> Reject</ActionBtn>
      </div>
    </div>
  );
}

function ActionBtn({ children, onClick, primary, destructive }: any) {
  return (
    <button onClick={onClick}
            className={`inline-flex items-center gap-1.5 h-8 px-3 rounded-lg text-[13px] font-medium border
              ${primary ? "bg-blue text-white border-blue w510" :
                destructive ? "border-red/40 text-red hover:bg-red/10" :
                "border-line-2 bg-surface text-ink-2 hover:bg-surface-2"}`}>
      {children}
    </button>
  );
}

function Center({ children }: any) {
  return <div className="h-screen flex items-center justify-center">{children}</div>;
}
