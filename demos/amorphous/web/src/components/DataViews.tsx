/* Card content renderers — typed views for each data kind. */
import { useEffect, useRef, useState } from "react";
import { api, post, when, USER, type Component } from "../lib/api";
import { Play, ExternalLink, LoaderCircle } from "lucide-react";

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
    case "metric":
      return (
        <div>
          <div className="text-[30px] font-bold tracking-tight leading-none">
            {String(data.value)}
            <span className="text-[14px] text-ink-2 font-medium ml-1">{data.unit}</span>
          </div>
          {data.delta != null && (
            <div className={`mt-2 text-[13px] font-medium ${data.delta > 0 ? "text-red" : "text-green"}`}>
              {data.delta > 0 ? "▲" : "▼"} {Math.abs(data.delta)}
            </div>
          )}
        </div>
      );
    case "kv":
      return (
        <div className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-2 text-[13.5px] content-start">
          {data.pairs.map(([k, v]: [string, string], i: number) => (
            <FragmentKV key={i} k={k} v={v} />
          ))}
        </div>
      );
    case "table":
      return <TableView data={data} />;
    case "timeseries":
      return <Sparkline data={data} />;
    case "links":
      return (
        <div>
          {data.links.map((l: any, i: number) => (
            <a
              key={i}
              href={l.url}
              target="_blank"
              rel="noreferrer"
              className="flex items-center gap-2 py-[7px] text-[13.5px] text-ink hover:text-teal border-b border-line last:border-0 truncate"
            >
              <ExternalLink size={13} className="text-ink-3 shrink-0" />
              <span className="truncate">{l.title}</span>
            </a>
          ))}
        </div>
      );
    case "feed":
      return data.items.length ? (
        <div>
          {data.items.map((it: any, i: number) => (
            <div key={i} className="flex gap-2.5 items-baseline py-1.5 text-[13px] border-b border-line last:border-0">
              <span className="text-ink-3 text-[11.5px] tabular-nums shrink-0">{when(it.when)}</span>
              <span className="text-ink-2 truncate">{it.icon} {it.text}</span>
            </div>
          ))}
        </div>
      ) : (
        <Empty text="No activity yet — run a workflow or chat with Hermes." />
      );
    case "notes":
      return <div className="text-[13.5px] leading-relaxed text-ink-2 whitespace-pre-wrap">{data.markdown}</div>;
    case "connections":
      return (
        <div>
          {data.connections.map((s: any) => (
            <div key={s.id} className="flex items-center justify-between py-1.5 text-[13px] border-b border-line last:border-0">
              <span>{s.name}</span>
              <span className={`text-[10.5px] font-semibold px-2.5 py-0.5 rounded-full ${s.connected ? "bg-green/15 text-green" : "bg-ink-3/15 text-ink-2"}`}>
                {s.connected ? "on" : "off"}
              </span>
            </div>
          ))}
        </div>
      );
    case "workflow":
      return <WorkflowView c={c} data={data} />;
    case "unconnected":
      return (
        <div className="text-[13px] text-ink-2 leading-relaxed">
          <b className="text-ink">{data.source}</b> isn't connected.
          <div className="mt-1.5"><code className="bg-surface-2 px-1.5 py-0.5 rounded text-[12px]">{data.how}</code></div>
        </div>
      );
    default:
      return <div className="text-red text-[13px]">{data.error || "no data"}</div>;
  }
}

function FragmentKV({ k, v }: { k: string; v: string }) {
  return (
    <>
      <span className="text-ink-3">{k}</span>
      <span className="text-right font-semibold tabular-nums truncate">{v}</span>
    </>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="text-[13px] text-ink-3 leading-relaxed">{text}</div>;
}

function TableView({ data }: { data: any }) {
  return (
    <table className="w-full border-collapse text-[13px]">
      <thead>
        <tr>
          {data.columns.map((col: string) => (
            <th key={col} className="sticky -top-3 bg-surface text-left font-semibold text-[10.5px] uppercase tracking-wider text-ink-3 pb-1.5 pr-3 border-b border-line-2">
              {col}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.rows.map((r: any[], i: number) => (
          <tr key={i}>
            {r.map((v, j) => (
              <td key={j} title={String(v)}
                  className={`py-[6px] pr-3 border-b border-line last:border-0 truncate max-w-[280px] ${j === 0 ? "text-ink-2 font-mono text-[12px]" : ""} ${/^[-+$0-9.,%#]/.test(String(v)) ? "tabular-nums" : ""}`}>
                {String(v)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Sparkline({ data }: { data: any }) {
  const ref = useRef<HTMLDivElement>(null);
  const pts: [number, number][] = data.points || [];
  if (!pts.length) return <Empty text="no points" />;
  const w = 600, h = 160, pad = 6;
  const vals = pts.map((p) => p[1]);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const xs = pts.map((_, i) => pad + (i / Math.max(pts.length - 1, 1)) * (w - 2 * pad));
  const ys = vals.map((v) => h - pad - ((v - mn) / (mx - mn || 1)) * (h - 2 * pad - 16));
  const d = xs.map((x, i) => `${i ? "L" : "M"}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(" ");
  return (
    <div ref={ref} className="h-full">
      <div className="text-[11px] text-ink-3 mb-1 tabular-nums">
        {data.label} · {mn.toLocaleString()}–{mx.toLocaleString()} · now <span className="text-ink font-semibold">{vals[vals.length - 1].toLocaleString()}</span>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="w-full h-[calc(100%-22px)] block">
        <path d={`${d} L${xs[xs.length - 1]},${h} L${xs[0]},${h} Z`} fill="rgba(69,213,192,.10)" />
        <path d={d} fill="none" stroke="var(--color-teal)" strokeWidth="1.8" />
      </svg>
    </div>
  );
}

function WorkflowView({ c, data }: { c: Component; data: any }) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<string>(
    data.runs?.length ? `Last run ${when(data.runs[0].ts)}:\n${(data.runs[0].result || "").slice(0, 1500)}` : ""
  );
  const inputsRef = useRef<Record<string, string>>({});
  if (!data.workflow) return <Empty text="workflow missing" />;
  const inputs = c.props.inputs || [];
  const run = async () => {
    setRunning(true);
    setResult("Hermes is working (full agent — may take a minute)…");
    try {
      const r = await post(`/api/workflow/${data.workflow.id}/run`, {
        user_id: USER,
        inputs: inputsRef.current,
      });
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
            <input
              key={inp.name}
              placeholder={inp.label}
              onChange={(e) => (inputsRef.current[inp.name] = e.target.value)}
              className="h-8 px-3 bg-surface-2 border border-line-2 rounded-lg text-[13px] text-ink placeholder:text-ink-3 outline-none focus:border-ink-3 min-w-[150px]"
            />
          ))}
        </div>
      )}
      <button
        onClick={run}
        disabled={running}
        className="inline-flex items-center gap-1.5 h-8 px-3.5 rounded-lg bg-gold text-gold-ink text-[13px] font-semibold w-fit hover:brightness-108 disabled:opacity-60"
      >
        {running ? <LoaderCircle size={14} className="spin" /> : <Play size={13} />}
        {running ? "Running" : "Run"}
      </button>
      {result && (
        <div className="mt-2.5 bg-surface-2 border border-line rounded-lg px-3 py-2.5 text-[12.5px] leading-relaxed text-ink-2 whitespace-pre-wrap overflow-auto flex-1 min-h-0">
          {result}
        </div>
      )}
    </div>
  );
}
