/* Signature components: engraved-identity radar chart + agent inspector.
   The radar is a custom SVG (concentric rings, radial axis labels, glowing
   data dots) with the engraved Hermes helm at center — the design signature. */
import { useMemo } from "react";

export interface RadarAxis { label: string; value: number /* 0..1 */; }

export function RadarChart({ axes, size = 210 }: { axes: RadarAxis[]; size?: number }) {
  const cx = size / 2, cy = size / 2;
  const R = size / 2 - 46;
  const rings = [0.33, 0.66, 1.0];
  const pts = useMemo(() => axes.map((a, i) => {
    const ang = (Math.PI * 2 * i) / axes.length - Math.PI / 2;
    return {
      ...a,
      x: cx + Math.cos(ang) * R * Math.max(a.value, 0.08),
      y: cy + Math.sin(ang) * R * Math.max(a.value, 0.08),
      lx: cx + Math.cos(ang) * (R + 16),
      ly: cy + Math.sin(ang) * (R + 16),
      ax: cx + Math.cos(ang) * R,
      ay: cy + Math.sin(ang) * R,
    };
  }), [axes, cx, cy, R]);
  const poly = pts.map((p) => `${p.x},${p.y}`).join(" ");

  return (
    <div className="relative flex items-center justify-center">
      <svg width={size} height={size} className="block" style={{ overflow: "visible" }}>
        {rings.map((r) => (
          <circle key={r} cx={cx} cy={cy} r={R * r} fill="none"
                  stroke="#24304a" strokeWidth={1} strokeDasharray={r === 1 ? "" : "3 4"} />
        ))}
        {pts.map((p, i) => (
          <line key={i} x1={cx} y1={cy} x2={p.ax} y2={p.ay} stroke="#1c2740" strokeWidth={1} />
        ))}
        <polygon points={poly} fill="rgba(59,130,246,0.14)" stroke="#3b82f6" strokeWidth={1.5} strokeLinejoin="round" />
        {pts.map((p, i) => (
          <g key={i}>
            <circle cx={p.x} cy={p.y} r={7} fill="rgba(96,165,250,0.25)" />
            <circle cx={p.x} cy={p.y} r={3} fill="#60a5fa" />
          </g>
        ))}
        {pts.map((p, i) => (
          <text key={i} x={p.lx} y={p.ly}
                textAnchor={Math.abs(p.lx - cx) < 8 ? "middle" : p.lx > cx ? "start" : "end"}
                dominantBaseline={Math.abs(p.ly - cy) < 8 ? "middle" : p.ly > cy ? "hanging" : "auto"}
                fill="#94a3b8" fontSize={9.5} fontWeight={590} letterSpacing="0.08em">
            {p.label.toUpperCase()}
          </text>
        ))}
      </svg>
      {/* engraved helm at center */}
      <img src="/art/hermes-helm.png" alt=""
           className="absolute w-12 h-12 rounded-full opacity-90 pointer-events-none"
           style={{ mixBlendMode: "screen" }} />
    </div>
  );
}

/* Engraved bust avatar — the identity mark used in inspector + empty states */
export function EngravedBust({ size = 64, className = "" }: { size?: number; className?: string }) {
  return (
    <img src="/art/hermes-bust.png" alt="" width={size} height={size}
         className={`rounded-full border border-line-2 object-cover ${className}`}
         style={{ mixBlendMode: "screen" }} />
  );
}
