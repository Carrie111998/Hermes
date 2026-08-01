/* Card content renderers — recharts visualizations, rich tables, badges. */
import { useEffect, useMemo, useRef, useState } from "react";
import {
  AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
} from "recharts";
import { api, post, when, USER, type Component } from "../lib/api";
import { badgeClass, avatarHue, faviconFor } from "../lib/accents";
import {
  Play, ExternalLink, LoaderCircle, ArrowUpRight, ArrowDownRight,
  ChevronUp, ChevronDown, Workflow, MessageSquare, MousePointerClick,
} from "lucide-react";

export function useComponentData(c: Component, proposalId?: string) {
  const [data, setData] = useState<any>(null);
  const [err, setErr] = useState<string>("");
  const refresh = async () => {
    try {
      const pv = proposalId ? `&proposal_id=${proposalId}` : "";
      setData(await api(`/api/component/${c.id}/data?user_id=${USER}${pv}`));
      setErr("");
    } catch (e: any) {
      setErr(String(e.message || e));
    }
  };
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 45000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [c.id, JSON.stringify(c.props), proposalId]);
  return { data, err, refresh };
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
    case "feed": return <FeedView data={data} />;
    case "notes":
      return <div className="text-[13.5px] leading-relaxed text-ink-2 whitespace-pre-wrap">{data.markdown}</div>;
    case "connections": return <ConnectionsView data={data} />;
    case "workflow": return <WorkflowView c={c} data={data} />;
    case "unconnected":
      return (
        <div className="text-[13px] text-ink-2 leading-relaxed">
          <b className="text-ink">{data.source}</b> isn't connected.
          <div className="mt-1.5"><code className="bg-white/[0.06] px-1.5 py-0.5 rounded text-[12px]">{data.how}</code></div>
        </div>
      );
    default:
      return <div className="text-red text-[13px]">{data.error || "no data"}</div>;
  }
}

/* ---------- metric ---------- */
function MetricView({ data }: { data: any }) {
  const up = data.delta > 0;
  /* up=bad for latency/error-style metrics; otherwise up=good */
  const inverse = /ms|error|%err|latency|p9\d/i.test(String(data.unit || "") + String(data.label || ""));
  const good = inverse ? !up : up;
  return (
    <div className="flex flex-col justify-center h-full">
      <div className="text-[34px] font-bold tracking-tight leading-none tabular-nums">
        {String(data.value)}
        <span className="text-[15px] text-ink-3 font-medium ml-1.5">{data.unit}</span>
      </div>
      {data.delta != null && (
        <div className={`mt-2.5 inline-flex items-center gap-1 text-[13px] font-semibold w-fit px-2 py-0.5 rounded-md ${good ? "bg-green/10 text-green" : "bg-red/10 text-red"}`}>
          {up ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}
          {Math.abs(data.delta)}
        </div>
      )}
    </div>
  );
}

/* ---------- kv with smart bars ---------- */
function KVView({ data }: { data: any }) {
  return (
    <div className="flex flex-col gap-2 content-start">
      {data.pairs.map(([k, v]: [string, string], i: number) => {
        const bar = parseBar(String(v));
        return (
          <div key={i}>
            <div className="flex items-baseline justify-between gap-4 text-[13.5px]">
              <span className="text-ink-3 shrink-0">{k}</span>
              <span className="font-semibold tabular-nums text-right truncate">{v}</span>
            </div>
            {bar !== null && (
              <div className="mt-1 h-1.5 rounded-full bg-white/[0.06] overflow-hidden">
                <div
                  className={`h-full rounded-full ${bar > 0.85 ? "bg-red" : bar > 0.65 ? "bg-amber" : "bg-accent"}`}
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

  return (
    <table className="w-full border-collapse text-[13px]">
      <thead>
        <tr>
          {data.columns.map((col: string, i: number) => (
            <th key={col}
                onClick={() => setSort(sort?.col === i ? (sort.dir === 1 ? { col: i, dir: -1 } : null) : { col: i, dir: 1 })}
                className="sticky -top-3 bg-[#101112] z-[1] text-left font-semibold text-[10.5px] uppercase tracking-wider text-ink-3 pb-1.5 pr-3 border-b border-line-2 cursor-pointer select-none hover:text-ink-2">
              <span className="inline-flex items-center gap-0.5">
                {col}
                {sort?.col === i && (sort.dir === 1 ? <ChevronUp size={11} /> : <ChevronDown size={11} />)}
              </span>
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((r: any[], i: number) => (
          <tr key={i} className="hover:bg-white/[0.03] transition-colors">
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
function ChartView({ data }: { data: any }) {
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
      <div className="flex-1 min-h-0 -mx-1">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={pts} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
            <defs>
              <linearGradient id="accentFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#7170ff" stopOpacity={0.28} />
                <stop offset="100%" stopColor="#7170ff" stopOpacity={0.02} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgba(255,255,255,0.05)" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="label" hide />
            <YAxis domain={[mn, mx]} hide />
            <Tooltip
              contentStyle={{
                background: "#191a1b", border: "1px solid rgba(255,255,255,0.1)", borderRadius: 10,
                fontSize: 12, color: "#f4f4f5", padding: "6px 10px",
              }}
              labelStyle={{ color: "#7b7e87", fontSize: 11 }}
              formatter={(v: any) => [fmt(v), ""]}
              separator=""
            />
            <Area type="monotone" dataKey="v" stroke="#7170ff" strokeWidth={2}
                  fill="url(#accentFill)" dot={false} activeDot={{ r: 3.5, fill: "#7170ff" }} />
          </AreaChart>
        </ResponsiveContainer>
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
  return (
    <div>
      {data.links.map((l: any, i: number) => {
        const fav = faviconFor(l.url);
        return (
          <a key={i} href={l.url} target="_blank" rel="noreferrer"
             className="group flex items-center gap-2.5 py-[7px] text-[13.5px] text-ink hover:text-accent-2 border-b border-line last:border-0">
            {fav
              ? <img src={fav} className="w-4 h-4 rounded-sm shrink-0 opacity-80" alt="" loading="lazy"
                     onError={(e) => ((e.target as HTMLImageElement).style.display = "none")} />
              : <ExternalLink size={13} className="text-ink-3 shrink-0" />}
            <span className="truncate flex-1">{l.title}</span>
            <ArrowUpRight size={13} className="text-ink-3 opacity-0 group-hover:opacity-100 shrink-0 transition-opacity" />
          </a>
        );
      })}
    </div>
  );
}

/* ---------- feed with icon timeline ---------- */
function FeedView({ data }: { data: any }) {
  if (!data.items.length)
    return <div className="text-[13px] text-ink-3 leading-relaxed">No activity yet — run a workflow or chat with Hermes.</div>;
  const iconFor = (icon: string) =>
    icon === "⚙" ? <Workflow size={12} className="text-accent-2" />
      : icon === "◎" ? <MessageSquare size={12} className="text-ink-3" />
      : <MousePointerClick size={12} className="text-ink-3" />;
  return (
    <div className="relative pl-5">
      <div className="absolute left-[7px] top-1 bottom-1 w-px bg-line" />
      {data.items.map((it: any, i: number) => (
        <div key={i} className="relative py-1.5 text-[13px]">
          <span className="absolute -left-5 top-[9px] w-[15px] h-[15px] rounded-full bg-surface-2 border border-line-2 flex items-center justify-center">
            {iconFor(it.icon)}
          </span>
          <div className="flex items-baseline gap-2 min-w-0">
            <span className="text-ink-2 truncate flex-1">{it.text}</span>
            <span className="text-ink-3 text-[11px] tabular-nums shrink-0">{when(it.when)}</span>
          </div>
        </div>
      ))}
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
                   className="h-8 px-3 bg-white/[0.03] border border-line rounded-md text-[13px] text-ink placeholder:text-ink-4 outline-none focus:border-line-2 min-w-[150px]" />
          ))}
        </div>
      )}
      <button onClick={run} disabled={running}
              className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-lg bg-brand text-white text-[13px] w510 w-fit hover:bg-accent-2 disabled:opacity-60 transition-colors">
        {running ? <LoaderCircle size={14} className="spin" /> : <Play size={13} />}
        {running ? "Running" : "Run"}
      </button>
      {result && (
        <div className="mt-2.5 bg-white/[0.03] border border-line rounded-lg px-3 py-2.5 text-[12.5px] leading-relaxed text-ink-2 whitespace-pre-wrap overflow-auto flex-1 min-h-0">
          {result}
        </div>
      )}
    </div>
  );
}
