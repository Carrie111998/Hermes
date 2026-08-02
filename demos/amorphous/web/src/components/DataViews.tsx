/* Card content renderers — recharts visualizations, rich tables, badges. */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { api, post, when, USER, onComponentRefresh, refreshCadence, type Component } from "../lib/api";
import { badgeClass, avatarHue, faviconFor } from "../lib/accents";
import { Button } from "./ui";
import {
  Play, ExternalLink, LoaderCircle, ArrowUpRight, ArrowDownRight,
  ChevronUp, ChevronDown, Workflow, MessageSquare, MousePointerClick,
} from "lucide-react";

export function useComponentData(c: Component, proposalId?: string) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string>("");
  const [flash, setFlash] = useState(false);
  const firstLoad = useRef(true);
  const refresh = async () => {
    try {
      const pv = proposalId ? `&proposal_id=${proposalId}` : "";
      setData(await api(`/api/component/${c.id}/data?user_id=${USER}${pv}`));
      setErr("");
      if (!firstLoad.current) { setFlash(true); setTimeout(() => setFlash(false), 900); }
      firstLoad.current = false;
    } catch (e: any) {
      setErr(String(e.message || e));
    }
  };
  useEffect(() => {
    firstLoad.current = true;
    refresh();
    const t = setInterval(refresh, refreshCadence(c));
    const off = onComponentRefresh(c.id, refresh);
    return () => { clearInterval(t); off(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [c.id, JSON.stringify(c.props), proposalId]);
  return { data, err, refresh, flash };
}

/* Size-aware rendering primitive: every renderer can adapt its internals to
   the card's real pixel box (drag-resize, pop-out dialogs, dock changes). */
export function useSize<T extends HTMLElement = HTMLDivElement>() {
  const ref = useRef<T | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const r = entries[0].contentRect;
      setSize({ w: Math.round(r.width), h: Math.round(r.height) });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return { ref, ...size };
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  const widths = [85, 60, 75, 50, 70, 42];
  return (
    <div className="flex flex-col gap-2.5 pt-1">
      {widths.slice(0, rows).map((w, i) => (
        <div key={i} className="shimmer h-3.5 rounded" style={{ width: `${w}%` }} />
      ))}
    </div>
  );
}

export function DataView({ c, data, err }: { c: Component; data: any; err: string }) {
  if (err) return <div className="text-red text-[13px] whitespace-pre-wrap">{err}</div>;
  if (!data) return <Skeleton rows={Math.min(c.h * 2 + 1, 6)} />;
  switch (data.kind) {
    case "metric": return <MetricView data={data} />;
    case "kv": return <KVView data={data} />;
    case "table": return <TableView data={data} />;
    case "timeseries": return <ChartView data={data} />;
    case "links": return <LinksView data={data} />;
    case "feed":
      return data.items.length ? (
        <div className="relative pl-5">
          <div className="absolute left-[7px] top-1 bottom-1 w-px bg-line" />
          {data.items.map((it: any, i: number) => (
            <div key={i} className="relative py-1.5 text-[13px]">
              <span className="absolute -left-5 top-[9px] w-[15px] h-[15px] rounded-full bg-surface-2 border border-line-2 flex items-center justify-center">
                {it.icon === "⚙" ? <Workflow size={12} className="text-blue-2" />
                  : it.icon === "◎" ? <MessageSquare size={12} className="text-ink-3" />
                  : <MousePointerClick size={12} className="text-ink-3" />}
              </span>
              <div className="flex items-baseline gap-2 min-w-0">
                <span className="text-ink-2 truncate flex-1">{it.text}</span>
                <span className="text-ink-3 text-[11px] tabular-nums shrink-0">{when(it.when)}</span>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState
          title="Nothing yet"
          body="Activity appears here as you run workflows and talk to Hermes."
        />
      );
    case "notes":
      return <div className="text-[13.5px] leading-relaxed text-ink-2 whitespace-pre-wrap">{data.markdown}</div>;
    case "connections": return <ConnectionsView data={data} />;
    case "workflow": return <WorkflowView c={c} data={data} />;
    case "heatmap": return <HeatmapView data={data} />;
    case "logs": return <LogsView data={data} />;
    case "tasklist": return <TasklistView c={c} data={data} />;
    case "unconnected":
      return (
        <div className="text-[13px] text-ink-2 leading-relaxed">
          <b className="text-ink">{data.source}</b> isn't connected.
          <div className="mt-1.5"><code className="bg-line px-1.5 py-0.5 rounded text-[12px]">{data.how}</code></div>
        </div>
      );
    default:
      return <div className="text-red text-[13px]">{data.error || "no data"}</div>;
  }
}

/* ---------- metric ---------- */
function MetricView({ data }: { data: any }) {
  const { ref, w, h } = useSize();
  const up = data.delta > 0;
  /* up=bad for latency/error-style metrics; otherwise up=good */
  const inverse = /ms|error|%err|latency|p9\d/i.test(String(data.unit || "") + String(data.label || ""));
  const good = inverse ? !up : up;
  // type scales with the box: ~1/3 of height, bounded, and shrinks for long values
  const chars = String(data.value).length;
  const px = Math.max(24, Math.min(h * 0.42, (w - 40) / Math.max(chars * 0.62, 1), 84));
  return (
    <div ref={ref} className="flex flex-col justify-center h-full">
      <div className="font-bold tracking-tight leading-none tabular-nums"
           style={{ fontSize: px }}>
        {String(data.value)}
        <span className="text-ink-3 font-medium ml-1.5" style={{ fontSize: Math.max(13, px * 0.42) }}>{data.unit}</span>
      </div>
      {data.delta != null && (
        <div className={`mt-2.5 inline-flex items-center gap-1 font-semibold w-fit px-2 py-0.5 rounded-md ${good ? "bg-green/10 text-green" : "bg-red/10 text-red"}`}
             style={{ fontSize: Math.max(12, px * 0.36) }}>
          {up ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}
          {Math.abs(data.delta)}
        </div>
      )}
    </div>
  );
}

/* ---------- kv with smart bars ---------- */
function KVView({ data }: { data: any }) {
  const { ref, w } = useSize();
  const cols = w > 640 ? 3 : w > 380 ? 2 : 1;
  return (
    <div ref={ref} className="grid gap-x-8 gap-y-2 content-start"
         style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
      {data.pairs.map(([k, v]: [string, string], i: number) => {
        const bar = parseBar(String(v));
        return (
          <div key={i}>
            <div className="flex items-baseline justify-between gap-4 text-[13.5px]">
              <span className="text-ink-3 shrink-0">{k}</span>
              <span className="font-semibold tabular-nums text-right truncate">{v}</span>
            </div>
            {bar !== null && (
              <div className="mt-1 h-1.5 rounded-full bg-line overflow-hidden">
                <div
                  className={`h-full rounded-full ${bar > 0.85 ? "bg-red" : bar > 0.65 ? "bg-amber" : "bg-blue"}`}
                  style={{ width: `${Math.min(bar * 100, 100)}%` }}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
function parseBar(v: string): number | null {
  let m = v.match(/^([\d.]+)\s*\/\s*([\d.]+)/);           // "19.9 / 62.6 GB"
  if (m && parseFloat(m[2]) > 0) return parseFloat(m[1]) / parseFloat(m[2]);
  m = v.match(/^(\d{1,3})%/);                               // "37% of ..."
  if (m) return parseInt(m[1]) / 100;
  return null;
}

/* ---------- table: avatars, badges, sorting ---------- */
function TableView({ data }: { data: any }) {
  const { ref, h } = useSize();
  const [sort, setSort] = useState<{ col: number; dir: 1 | -1 } | null>(null);
  const authorCol = data.columns.findIndex((c: string) => /author|user|by/i.test(c));
  const stateCol = data.columns.findIndex((c: string) => /state|status/i.test(c));
  const rows = useMemo(() => {
    if (!sort) return data.rows;
    return [...data.rows].sort((a: any[], b: any[]) => {
      const av = String(a[sort.col]), bv = String(b[sort.col]);
      const an = parseFloat(av.replace(/[^0-9.-]/g, "")), bn = parseFloat(bv.replace(/[^0-9.-]/g, ""));
      if (!isNaN(an) && !isNaN(bn)) return (an - bn) * sort.dir;
      return av.localeCompare(bv) * sort.dir;
    });
  }, [data.rows, sort]);

  const rowH = 30, headH = 30, labelH = 20;
  let fit = h > 0 ? Math.max(1, Math.floor((h - headH) / rowH)) : rows.length;
  if (h > 0 && rows.length > fit) fit = Math.max(1, Math.floor((h - headH - labelH) / rowH));
  const shown = rows.slice(0, fit);
  const extra = rows.length - shown.length;
  return (
    <div ref={ref} className="h-full flex flex-col min-h-0">
    <table className="w-full border-collapse text-[13px]">
      <thead>
        <tr>
          {data.columns.map((col: string, i: number) => (
            <th key={col}
                onClick={() => setSort(sort?.col === i ? (sort.dir === 1 ? { col: i, dir: -1 } : null) : { col: i, dir: 1 })}
                className="sticky -top-3 bg-surface z-[1] text-left font-semibold text-[10.5px] uppercase tracking-wider text-ink-3 pb-1.5 pr-3 border-b border-line-2 cursor-pointer select-none hover:text-ink-2">
              <span className="inline-flex items-center gap-0.5">
                {col}
                {sort?.col === i && (sort.dir === 1 ? <ChevronUp size={11} /> : <ChevronDown size={11} />)}
              </span>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {shown.map((r: any[], i: number) => (
          <tr key={i} className="hover:bg-[#101a30] transition-colors">
            {r.map((v, j) => (
              <td key={j} title={String(v)}
                  className={`py-[7px] pr-3 border-b border-line last:border-0 truncate max-w-[280px] align-middle ${j === 0 ? "font-mono text-[11.5px] text-ink-3" : ""} ${/^[-+$0-9.,%#]/.test(String(v)) ? "tabular-nums" : ""}`}>
                {j === authorCol ? <Avatar name={String(v)} />
                  : j === stateCol && badgeClass(String(v))
                    ? <span className={`text-[10.5px] w510 px-2 py-0.5 rounded-full ${badgeClass(String(v))}`}>{String(v)}</span>
                    : renderDelta(String(v))}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
    {extra > 0 && (
      <div className="text-[10.5px] text-ink-4 pt-1 shrink-0">+{extra} more — resize or pop out</div>
    )}
    </div>
  );
}

function renderDelta(v: string) {
  const m = v.match(/^([+-])(\d+(?:\.\d+)?)%$/);
  if (m) {
    const up = m[1] === "+";
    return (
      <span className={`inline-flex items-center gap-0.5 font-semibold ${up ? "text-green" : "text-red"}`}>
        {up ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}{m[2]}%
      </span>
    );
  }
  return v;
}

function Avatar({ name }: { name: string }) {
  const hue = avatarHue(name);
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="w-[18px] h-[18px] rounded-full inline-flex items-center justify-center text-[9px] font-bold text-black/80 shrink-0"
            style={{ background: hue }}>
        {name.slice(0, 2).toUpperCase()}
      </span>
      <span className="truncate">{name}</span>
    </span>
  );
}

/* ---------- recharts area chart ---------- */
let _chartSeq = 0;
function ChartView({ data }: { data: any }) {
  const [uid] = useState(() => `ch${++_chartSeq}`);
  const pts = (data.points || []).map(([t, v]: [number, number]) => ({
    t, v,
    label: new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" }) +
      " " + new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
  }));
  if (!pts.length) return <div className="text-ink-3 text-[13px]">no points</div>;
  const vals = pts.map((p: any) => p.v);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const last = vals[vals.length - 1];
  const first = vals[0];
  const chg = ((last - first) / (first || 1)) * 100;
  return (
    <div className="h-full flex flex-col">
      <div className="flex items-baseline gap-2.5 mb-1 shrink-0">
        <span className="text-[20px] font-bold tabular-nums tracking-tight">{fmt(last)}</span>
        <span className={`inline-flex items-center text-[12px] font-semibold ${chg >= 0 ? "text-green" : "text-red"}`}>
          {chg >= 0 ? <ArrowUpRight size={13} /> : <ArrowDownRight size={13} />}
          {Math.abs(chg).toFixed(1)}%
        </span>
        <span className="text-[11px] text-ink-3 truncate">{data.label}</span>
      </div>
      <div className="relative flex-1 min-h-[90px] -mx-1">
        <div className="absolute inset-0">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={pts} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
            <defs>
              <linearGradient id={`${uid}-fill`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#3b82f6" stopOpacity={0.32} />
                <stop offset="55%" stopColor="#3b82f6" stopOpacity={0.08} />
                <stop offset="100%" stopColor="#3b82f6" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="#1c2740" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="label" hide />
            <YAxis domain={[mn, mx]} hide />
            <Tooltip
              contentStyle={{
                background: "#16203a", border: "1px solid #334155", borderRadius: 10,
                fontSize: 12, color: "#f8fafc", padding: "6px 10px",
                boxShadow: "0 8px 24px rgba(2,6,23,.6)",
              }}
              labelStyle={{ color: "#64748b", fontSize: 11 }}
              formatter={(v: any) => [fmt(v), ""]}
              separator=""
            />
            {/* glow = wide soft under-stroke, no SVG/CSS filters (those fail to
                rasterize on large surfaces in software rendering) */}
            <Area type="monotone" dataKey="v" stroke="#3b82f6" strokeWidth={7}
                  strokeOpacity={0.22} fill="none" dot={false} activeDot={false}
                  isAnimationActive={false} />
            <Area type="monotone" dataKey="v" stroke="#60a5fa" strokeWidth={2}
                  fill={`url(#${uid}-fill)`} dot={false}
                  activeDot={{ r: 4, fill: "#93c5fd", stroke: "#3b82f6", strokeWidth: 2 }} />
          </AreaChart>
        </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
function fmt(n: number) {
  if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/* ---------- links with favicons ---------- */
function LinksView({ data }: { data: any }) {
  const { ref, w, h } = useSize();
  const cols = w > 560 ? 2 : 1;
  const rowH = 33, labelH = 20;
  let fit = h > 0 ? Math.max(1, Math.floor(h / rowH) * cols) : data.links.length;
  if (h > 0 && data.links.length > fit)
    fit = Math.max(1, Math.floor((h - labelH) / rowH) * cols);
  const shown = data.links.slice(0, fit);
  const extra = data.links.length - shown.length;
  return (
    <div ref={ref} className="h-full min-h-0 grid gap-x-8 content-start"
         style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}>
      {shown.map((l: any, i: number) => {
        const fav = faviconFor(l.url);
        return (
          <a key={i} href={l.url} target="_blank" rel="noreferrer"
             className="group flex items-center gap-2.5 py-[7px] text-[13.5px] text-ink hover:text-blue-2 border-b border-line last:border-0">
            {fav
              ? <img src={fav} className="w-4 h-4 rounded-sm shrink-0 opacity-80" alt="" loading="lazy"
                     onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
              : <ExternalLink size={13} className="text-ink-3 shrink-0" />}
            <span className="truncate flex-1">{l.title}</span>
            <ArrowUpRight size={13} className="text-ink-3 opacity-0 group-hover:opacity-100 shrink-0 transition-opacity" />
          </a>
        );
      })}
      {extra > 0 && (
        <div className="text-[10.5px] text-ink-4 pt-1" style={{ gridColumn: "1 / -1" }}>
          +{extra} more — resize or pop out
        </div>
      )}
    </div>
  );
}

/* ---------- empty state with engraved identity ---------- */
export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="h-full flex flex-col items-center justify-center text-center gap-1.5 py-2">
      <img src="/art/hermes-helm.png" alt="" className="w-10 h-10 opacity-50" style={{ mixBlendMode: "screen" }} />
      <div className="text-[13px] w510 text-ink-2">{title}</div>
      <div className="text-[12px] text-ink-4 leading-relaxed max-w-[220px]">{body}</div>
    </div>
  );
}

/* ---------- connections ---------- */
function ConnectionsView({ data }: { data: any }) {
  return (
    <div>
      {data.connections.map((s: any) => (
        <div key={s.id} className="flex items-center justify-between py-[7px] text-[13px] border-b border-line last:border-0">
          <span className="inline-flex items-center gap-2">
            <span className={`w-1.5 h-1.5 rounded-full ${s.connected ? "bg-green shadow-[0_0_6px] shadow-green" : "bg-ink-3/40"}`} />
            {s.name}
          </span>
          <span className={`text-[10.5px] w510 px-2.5 py-0.5 rounded-full ${s.connected ? "bg-green/15 text-green" : "bg-ink-3/15 text-ink-2"}`}>
            {s.connected ? "connected" : "off"}
          </span>
        </div>
      ))}
    </div>
  );
}

/* ---------- workflow ---------- */
function WorkflowView({ c, data }: { c: Component; data: any }) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<string>(
    data.runs?.length ? `Last run ${when(data.runs[0].ts)}:\n${(data.runs[0].result || "").slice(0, 1500)}` : ""
  );
  const inputsRef = useRef<Record<string, string>>({});
  if (!data.workflow) return <div className="text-ink-3 text-[13px]">workflow missing</div>;
  const inputs = c.props.inputs || [];
  const run = async () => {
    setRunning(true);
    setResult("Hermes is working (full agent — may take a minute)…");
    try {
      const r = await post(`/api/workflow/${data.workflow.id}/run`, { user_id: USER, inputs: inputsRef.current });
      setResult(r.result);
    } catch (e: any) {
      setResult(`⚠ ${e.message}`);
    }
    setRunning(false);
  };
  return (
    <div className="flex flex-col h-full">
      <div className="text-[13px] text-ink-2 leading-snug mb-2.5">{data.workflow.description}</div>
      {inputs.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-2.5">
          {inputs.map((inp: any) => (
            <input key={inp.name} placeholder={inp.label}
                   onChange={(e) => (inputsRef.current[inp.name] = e.target.value)}
                   className="h-8 px-3 bg-[#101a30] border border-line rounded-md text-[13px] text-ink placeholder:text-ink-4 outline-none focus:border-line-2 min-w-[150px]" />
          ))}
        </div>
      )}
      <Button onClick={run} disabled={running} className="w-fit">
        {running ? <LoaderCircle size={14} className="spin" /> : <Play size={13} />}
        {running ? "Running" : "Run"}
      </Button>
      {result && (
        <div className="mt-2.5 bg-[#101a30] border border-line rounded-lg px-3 py-2.5 text-[12.5px] leading-relaxed text-ink-2 whitespace-pre-wrap overflow-auto flex-1 min-h-0">
          {result}
        </div>
      )}
    </div>
  );
}


/* ---------- GitHub-style activity heatmap (fills its box) ---------- */
function HeatmapView({ data }: { data: any }) {
  const { ref, w, h } = useSize();
  const days: { date: string; count: number }[] = data.days || [];
  const weeks: { date: string; count: number }[][] = [];
  for (let i = 0; i < days.length; i += 7) weeks.push(days.slice(i, i + 7));
  const max = Math.max(1, ...days.map((d) => d.count));
  const shade = (c: number) =>
    c === 0 ? "rgba(255,255,255,0.045)"
      : `rgba(59,130,246,${0.25 + 0.75 * Math.min(c / max, 1)})`;
  // cell size derived from the actual box: fit weeks across, 7 rows down
  const gap = Math.max(2, Math.min(4, Math.floor(w / 220)));
  const cell = Math.max(6, Math.min(
    Math.floor((w - gap * (weeks.length - 1)) / Math.max(weeks.length, 1)),
    Math.floor((h - gap * 6) / 7),
    26));
  const radius = cell >= 14 ? 4 : 2.5;
  return (
    <div className="flex flex-col h-full">
      <div className="text-[12px] text-ink-3 mb-2 shrink-0">
        <span className="text-ink w510 tabular-nums">{data.total}</span> commits ·{" "}
        <span className="font-mono text-[11px]">{data.repo}</span>
      </div>
      <div ref={ref} className="flex-1 min-h-0 flex items-center justify-center overflow-hidden">
        {w > 0 && (
          <div className="flex" style={{ gap }}>
            {weeks.map((wk, i) => (
              <div key={i} className="flex flex-col" style={{ gap }}>
                {wk.map((d) => (
                  <div key={d.date} title={`${d.date}: ${d.count} commit${d.count === 1 ? "" : "s"}`}
                       className="transition-colors hover:ring-1 hover:ring-blue-2"
                       style={{ width: cell, height: cell, borderRadius: radius, background: shade(d.count) }} />
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* ---------- live log tail ---------- */
function LogsView({ data }: { data: any }) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => { endRef.current?.scrollIntoView({ block: "end" }); }, [data.lines]);
  const color = (l: string) =>
    /error|fail|critical|traceback/i.test(l) ? "text-red"
      : /warn/i.test(l) ? "text-amber"
      : /\[CLIENT\]|info/i.test(l) ? "text-ink-2" : "text-ink-3";
  return (
    <div className="h-full flex flex-col font-mono text-[11.5px] leading-[1.6]">
      <div className="text-[10.5px] text-ink-4 pb-1.5 truncate shrink-0">{data.path}</div>
      <div className="flex-1 overflow-auto min-h-0 bg-[#0a1322] rounded-lg border border-line px-3 py-2">
        {(data.lines || []).map((l: string, i: number) => (
          <div key={i} className={`whitespace-pre-wrap break-all ${color(l)}`}>{l || " "}</div>
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}

/* ---------- agent-editable task list ---------- */
function TasklistView({ c, data }: { c: Component; data: any }) {
  const [items, setItems] = useState<{ text: string; done: boolean }[]>(data.items || []);
  useEffect(() => { setItems(data.items || []); }, [JSON.stringify(data.items)]);
  const toggle = async (i: number) => {
    const next = items.map((it, j) => (j === i ? { ...it, done: !it.done } : it));
    setItems(next);
    // persist through the layout (tasklist state lives in props.items)
    const st = await api(`/api/state?user_id=${USER}`);
    const spec = st.layout;
    const comp = spec.components.find((x: any) => x.id === c.id);
    if (comp) { comp.props = { ...comp.props, items: next };
      await post("/api/layout", { user_id: USER, spec }); }
  };
  const doneCount = items.filter((i) => i.done).length;
  return (
    <div className="flex flex-col h-full">
      <div className="h-1.5 rounded-full bg-line overflow-hidden mb-2.5 shrink-0">
        <div className="h-full bg-blue rounded-full transition-all duration-300"
             style={{ width: `${items.length ? (doneCount / items.length) * 100 : 0}%` }} />
      </div>
      <div className="flex-1 overflow-auto min-h-0">
        {items.length === 0 && (
          <div className="text-[12.5px] text-ink-4">No tasks — ask Hermes to track something here.</div>
        )}
        {items.map((it, i) => (
          <button key={i} onClick={() => toggle(i)}
                  className="w-full flex items-start gap-2.5 py-[5px] text-left group/task cursor-pointer">
            <span className={`mt-[3px] w-[15px] h-[15px] rounded-[4px] border shrink-0 flex items-center justify-center transition-colors
              ${it.done ? "bg-blue border-blue" : "border-line-2 group-hover/task:border-ink-3"}`}>
              {it.done && <svg viewBox="0 0 10 8" className="w-2.5 h-2"><path d="M1 4l2.5 2.5L9 1" stroke="white" strokeWidth="1.8" fill="none" strokeLinecap="round" /></svg>}
            </span>
            <span className={`text-[13px] leading-snug ${it.done ? "text-ink-4 line-through" : "text-ink-2"}`}>{it.text}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
