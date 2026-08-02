/* Visual vocabulary — Linear-style: achromatic chrome, semantic color only in DATA. */
import {
  GitBranch, Cpu, Play, SlidersHorizontal, Table2, LineChart, ListTodo,
  Newspaper, Activity, StickyNote, Plug, CloudSun, CalendarDays, Terminal,
  ListChecks, type LucideIcon,
} from "lucide-react";

export const TYPE_ICONS: Record<string, LucideIcon> = {
  kv: SlidersHorizontal,
  table: Table2,
  timeseries: LineChart,
  metric: Activity,
  links: Newspaper,
  feed: Activity,
  workflow_button: Play,
  workflow_panel: ListTodo,
  notes: StickyNote,
  connections: Plug,
  heatmap: CalendarDays,
  logs: Terminal,
  tasklist: ListChecks,
};

export const SOURCE_ICONS: Record<string, LucideIcon> = {
  "git.log": GitBranch,
  "git.status": GitBranch,
  "github.prs": GitBranch,
  "github.issues": GitBranch,
  "system.stats": Cpu,
  "crypto.price": LineChart,
  "crypto.chart": LineChart,
  rss: Newspaper,
  "datadog.query": Activity,
  "betterstack.monitors": Activity,
  "station.activity": Activity,
  weather: CloudSun,
  "git.heatmap": CalendarDays,
  "log.tail": Terminal,
};

export function iconFor(type: string, source?: string): LucideIcon {
  return (source && SOURCE_ICONS[source]) || TYPE_ICONS[type] || SlidersHorizontal;
}

/* status/state → colored badge classes (semantic data color is allowed) */
export function badgeClass(v: string): string | null {
  const s = v.toLowerCase().trim();
  const map: [RegExp, string][] = [
    [/^(open|up|ok|connected|passing|active|online|success)$/, "bg-green/10 text-green"],
    [/^(merged|shipped)$/, "bg-accent/10 text-accent-2"],
    [/^(draft|pending|paused|idle)$/, "bg-white/[0.06] text-ink-3"],
    [/^(investigating|triage|monitoring|warn|warning|degraded)$/, "bg-amber/10 text-amber"],
    [/^(down|error|failed|closed|critical|blocked)$/, "bg-red/10 text-red"],
  ];
  for (const [re, cls] of map) if (re.test(s)) return cls;
  return null;
}

/* deterministic avatar hue — muted, data-layer color */
export function avatarHue(name: string): string {
  const hues = ["#8b8fd9", "#7fb5a3", "#c99a6b", "#a58fc0", "#6ba3c9", "#c98b8b", "#9aad72", "#7170ff"];
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return hues[h % hues.length];
}

export function faviconFor(url: string): string | null {
  try {
    const u = new URL(url);
    return `https://www.google.com/s2/favicons?domain=${u.hostname}&sz=32`;
  } catch {
    return null;
  }
}
