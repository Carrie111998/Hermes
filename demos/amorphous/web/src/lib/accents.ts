/* Visual vocabulary: per-component-type accents, icons, badge colorers. */
import {
  GitBranch, Cpu, Play, SlidersHorizontal, Table2, LineChart, ListTodo,
  Newspaper, Activity, StickyNote, Plug, KeyRound, type LucideIcon,
} from "lucide-react";

export interface Accent {
  icon: LucideIcon;
  /** tailwind text color for the icon */ fg: string;
  /** icon chip background */ bg: string;
  /** top hairline gradient */ line: string;
}

/* Tailwind can't see dynamic classes — enumerate them literally instead. */
export const ACCENTS: Record<string, Accent> = {
  kv:              { icon: SlidersHorizontal, fg: "text-sky-400",     bg: "bg-sky-400/10",     line: "from-sky-400/60" },
  table:           { icon: Table2,            fg: "text-violet-400",  bg: "bg-violet-400/10",  line: "from-violet-400/60" },
  timeseries:      { icon: LineChart,         fg: "text-teal",        bg: "bg-teal/10",        line: "from-teal/60" },
  metric:          { icon: Activity,          fg: "text-amber-400",   bg: "bg-amber-400/10",   line: "from-amber-400/60" },
  links:           { icon: Newspaper,         fg: "text-orange-400",  bg: "bg-orange-400/10",  line: "from-orange-400/60" },
  feed:            { icon: Activity,          fg: "text-emerald-400", bg: "bg-emerald-400/10", line: "from-emerald-400/60" },
  workflow_button: { icon: Play,              fg: "text-gold",        bg: "bg-gold/10",        line: "from-gold/60" },
  workflow_panel:  { icon: ListTodo,          fg: "text-gold",        bg: "bg-gold/10",        line: "from-gold/60" },
  notes:           { icon: StickyNote,        fg: "text-rose-300",    bg: "bg-rose-300/10",    line: "from-rose-300/60" },
  connections:     { icon: Plug,              fg: "text-emerald-400", bg: "bg-emerald-400/10", line: "from-emerald-400/60" },
};

/* Sources refine the icon (a git table ≠ a market table). */
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
  weather: KeyRound,
};

export function accentFor(type: string): Accent {
  return ACCENTS[type] || ACCENTS.kv;
}

/* status/state → colored badge classes */
export function badgeClass(v: string): string | null {
  const s = v.toLowerCase().trim();
  const map: [RegExp, string][] = [
    [/^(open|up|ok|connected|passing|active|online|success)$/, "bg-emerald-400/15 text-emerald-300"],
    [/^(merged|shipped)$/, "bg-violet-400/15 text-violet-300"],
    [/^(draft|pending|paused|idle)$/, "bg-zinc-400/15 text-zinc-300"],
    [/^(investigating|triage|monitoring|warn|warning|degraded)$/, "bg-amber-400/15 text-amber-300"],
    [/^(down|error|failed|closed|critical|blocked)$/, "bg-red-400/15 text-red-300"],
  ];
  for (const [re, cls] of map) if (re.test(s)) return cls;
  return null;
}

/* deterministic avatar hue from a username */
export function avatarHue(name: string): string {
  const hues = ["#f59e0b", "#22d3ee", "#a78bfa", "#34d399", "#fb7185", "#60a5fa", "#facc15", "#4fd8c4"];
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
