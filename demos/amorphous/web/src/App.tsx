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
  const gridPadBottom = dockPos === "bottom" ? (dockCollapsed ? 58 : 250) : 12;

  return (
    <div className="min-h-screen">
      {/* top bar */}
      <header className="sticky top-0 z-40 h-14 flex items-center justify-between px-4 border-b border-line bg-background/90 backdrop-blur-md">
        <div className="flex items-center gap-2.5 font-semibold text-[15px]">
          <span className="text-gold text-[17px]">☤</span> Hermes Station
          <span className="text-ink-3 text-[11.5px] font-normal hidden sm:inline">amorphous applications</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="hidden md:inline-flex items-center gap-2 h-7 px-3 rounded-full border border-line text-[12px] text-ink-2 max-w-[240px]">
            <span className="w-1.5 h-1.5 rounded-full bg-green shadow-[0_0_7px] shadow-green shrink-0" />
            <span className="truncate">{state.agent.model || "agent"}</span>
          </span>
          <TopBtn onClick={evolveNow}><FlaskConical size={14} /> Evolve</TopBtn>
          <TopBtn onClick={() => setTrayOpen(!trayOpen)}>
            <LayoutGrid size={14} /> Proposals
            <span className={`min-w-[18px] h-[18px] px-1 rounded-full text-[11px] font-bold inline-flex items-center justify-center ${state.proposals.length ? "bg-gold text-gold-ink" : "bg-line text-ink-3"}`}>
              {state.proposals.length}
            </span>
          </TopBtn>
          <TopBtn onClick={() => setDockCollapsed(!dockCollapsed)}><MessageSquare size={14} /> Chat</TopBtn>
        </div>
      </header>

      {/* preview banner */}
      {preview && (
        <div className="sticky top-14 z-40 flex items-center gap-3 px-4 py-2.5 bg-gold/10 border-b border-gold/40 text-[13.5px]">
          <Eye size={15} className="text-gold shrink-0" />
          <b className="text-gold shrink-0">Previewing proposal</b>
          <span className="flex-1 text-ink-2 truncate">
            {preview.diff.map((d) => `${d.change}: ${d.title}`).join(" · ") || "reflow only"}
          </span>
          <button onClick={() => actProposal(preview.p.id, "approve", "", "up")}
                  className="h-8 px-3.5 rounded-lg bg-gold text-gold-ink text-[13px] font-semibold inline-flex items-center gap-1.5">
            <Check size={13} /> Keep
          </button>
          <button onClick={() => {
            const why = prompt("Why keep the current layout? (optional — steers the curator)") || "";
            actProposal(preview.p.id, "reject", why, "down");
          }} className="h-8 px-3.5 rounded-lg border border-line-2 text-[13px] inline-flex items-center gap-1.5 hover:bg-surface-2">
            <XIcon size={13} /> Go back
          </button>
        </div>
      )}

      {/* grid */}
      <main style={{ paddingRight: gridPadRight, paddingBottom: gridPadBottom }} className="transition-[padding] duration-200">
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
                      className="inline-flex items-center gap-1 h-7 px-2.5 rounded-lg border border-line text-ink-2 hover:border-line-2 hover:text-ink">
                <Plus size={12} /> {c.title}
              </button>
            ))}
          </div>
        )}
      </main>

      {/* proposals tray */}
      {trayOpen && (
        <aside className="fixed top-14 right-0 bottom-0 w-[420px] z-[60] bg-surface border-l border-line overflow-auto p-4"
               style={{ paddingBottom: gridPadBottom + 20 }}>
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

      <ChatDock
        position={dockPos}
        collapsed={dockCollapsed}
        msgs={msgs}
        busy={chatBusy}
        onSend={sendChat}
        onMove={moveDock}
        onCollapse={() => setDockCollapsed(!dockCollapsed)}
      />

      {toast && (
        <div className="fixed left-1/2 -translate-x-1/2 z-[95] bg-surface-2 border border-line-2 rounded-lg px-4 py-2.5 text-[13.5px] shadow-2xl shadow-black/50"
             style={{ bottom: gridPadBottom + 16 }}>
          {toast}
        </div>
      )}
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
            className="inline-flex items-center gap-1.5 h-8 px-3 rounded-lg text-[13px] text-ink-2 hover:text-ink hover:bg-surface-2 border border-transparent hover:border-line">
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
    <div className="border border-line rounded-xl bg-surface-2 p-3.5 mt-3">
      <div className="text-[10.5px] uppercase tracking-wider text-ink-3 mb-1.5">{p.engine} · {when(p.created_at)}</div>
      <div className="text-[13.5px] font-semibold leading-snug">{p.summary}</div>
      <ul className="my-2.5 pl-4 text-[13px] text-ink-2 leading-relaxed list-disc">
        {p.mutations.slice(0, 8).map((m, i) => <li key={i}>{label(m)}</li>)}
      </ul>
      {p.rationale && <div className="text-[12.5px] text-ink-3 leading-relaxed">{p.rationale}</div>}
      <textarea value={fb} onChange={(e) => setFb(e.target.value)}
                placeholder="Optional feedback — steers the next evolution"
                className="w-full min-h-[44px] mt-2.5 px-2.5 py-2 bg-background border border-line rounded-lg text-[13px] text-ink placeholder:text-ink-3 outline-none focus:border-line-2 resize-y" />
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
              ${primary ? "bg-gold text-gold-ink border-gold font-semibold" :
                destructive ? "border-red/40 text-red hover:bg-red/10" :
                "border-line-2 text-ink hover:bg-surface"}`}>
      {children}
    </button>
  );
}

function Center({ children }: any) {
  return <div className="h-screen flex items-center justify-center">{children}</div>;
}
