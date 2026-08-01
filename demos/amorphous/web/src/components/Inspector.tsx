/* Inspector — the right rail from the reference design: agent identity,
   capability radar, permissions-style lists, live activity. All real data. */
import { useEffect, useState } from "react";
import { X, Circle, ShieldCheck, Zap, Workflow, MessageSquare } from "lucide-react";
import { RadarChart, EngravedBust } from "./Radar";
import { api, when, USER, type StationState } from "../lib/api";

export default function Inspector({ state, onClose }: { state: StationState; onClose: () => void }) {
  const [activity, setActivity] = useState<any[]>([]);
  useEffect(() => {
    let live = true;
    const load = async () => {
      try {
        const comps = state.layout.components || [];
        const feedComp = comps.find((c) => c.props?.source === "station.activity");
        if (feedComp) {
          const d = await api(`/api/component/${feedComp.id}/data?user_id=${USER}`);
          if (live && d.kind === "feed") setActivity(d.items.slice(0, 6));
        }
      } catch { /* ignore */ }
    };
    load();
    const t = setInterval(load, 30000);
    return () => { live = false; clearInterval(t); };
  }, [state]);

  const comps = (state.layout.components || []).filter((c) => !c.hidden);
  const byType = (t: string[]) => comps.filter((c) => t.includes(c.type)).length;
  const denom = Math.max(comps.length, 1);
  const axes = [
    { label: "Data", value: Math.min(byType(["table", "kv", "metric"]) / denom * 2, 1) },
    { label: "Charts", value: Math.min(byType(["timeseries"]) / denom * 3, 1) },
    { label: "Workflows", value: Math.min(state.workflows.length / 6, 1) },
    { label: "Feeds", value: Math.min(byType(["links", "feed"]) / denom * 2.5, 1) },
    { label: "External", value: Math.min(state.connections.filter((c) => c.connected).length / 8, 1) },
  ];

  const conn = state.connections;

  return (
    <aside className="w-[300px] shrink-0 border-l border-line bg-panel flex flex-col overflow-hidden">
      {/* identity */}
      <div className="flex items-center gap-3 px-4 h-14 border-b border-line shrink-0">
        <EngravedBust size={34} />
        <div className="leading-tight min-w-0 flex-1">
          <div className="text-[13.5px] w590 truncate">{USER}'s Hermes</div>
          <div className="flex items-center gap-1.5 text-[11px] text-green">
            <Circle size={7} fill="currentColor" strokeWidth={0} /> Active now
          </div>
        </div>
        <button onClick={onClose} className="text-ink-4 hover:text-ink"><X size={15} /></button>
      </div>

      <div className="flex-1 overflow-auto">
        {/* radar */}
        <div className="px-4 pt-4 pb-1 flex flex-col items-center">
          <RadarChart axes={axes} size={230} />
          <div className="microlabel pb-1">station coverage</div>
        </div>

        {/* capabilities */}
        <Section icon={<ShieldCheck size={13} />} title="Agent surface">
          <Row k="Model" v={state.agent.model || "—"} mono />
          <Row k="Toolsets" v="terminal · web · files · station" />
          <Row k="Layout authority" v="main chat" ok />
          <Row k="Component scope" v="per-card chat" ok />
        </Section>

        {/* connections */}
        <Section icon={<Zap size={13} />} title="Connections">
          {conn.map((c) => (
            <div key={c.id} className="flex items-center justify-between py-[5px] text-[12.5px]">
              <span className="text-ink-2">{c.name}</span>
              <span className={`text-[10px] w590 uppercase tracking-wider ${c.connected ? "text-green" : "text-ink-4"}`}>
                {c.connected ? "enabled" : "off"}
              </span>
            </div>
          ))}
        </Section>

        {/* live activity */}
        <Section icon={<Workflow size={13} />} title="Live activity">
          {activity.length === 0 && (
            <div className="text-[12.5px] text-ink-4 py-1">Quiet so far — run a workflow or ask Hermes something.</div>
          )}
          {activity.map((a, i) => (
            <div key={i} className="flex gap-2.5 items-baseline py-[5px] text-[12.5px]">
              <span className="text-ink-4 text-[10.5px] font-mono tabular-nums shrink-0">{when(a.when)}</span>
              <span className="shrink-0">{a.icon === "⚙" ? <Workflow size={11} className="text-blue-2" /> : <MessageSquare size={11} className="text-ink-4" />}</span>
              <span className="text-ink-3 truncate">{a.text}</span>
            </div>
          ))}
        </Section>
      </div>
    </aside>
  );
}

function Section({ icon, title, children }: any) {
  return (
    <div className="px-4 py-3 border-t border-line">
      <div className="microlabel flex items-center gap-1.5 pb-2">{icon}{title}</div>
      {children}
    </div>
  );
}

function Row({ k, v, mono, ok }: { k: string; v: string; mono?: boolean; ok?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 py-[5px] text-[12.5px]">
      <span className="text-ink-3 shrink-0">{k}</span>
      <span className={`truncate text-right ${mono ? "font-mono text-[11.5px]" : ""} ${ok ? "text-green" : "text-ink-2"}`}>{v}</span>
    </div>
  );
}
