import { cn } from '@/lib/utils'

// ── Trace colors (deuteranopia-safe: green → teal → blue) ─────────────
// Position 0.0 = top (positive) → green
// Position 0.5 = middle (neutral) → teal
// Position 1.0 = bottom (constructive) → blue
const TRACE_RAMP = [
  [0.0, 100, 230, 100], // green
  [0.15, 100, 230, 100], // green
  [0.42, 61, 170, 148], // teal
  [0.58, 55, 120, 180], // blue-steel
  [0.85, 55, 100, 200], // blue
  [1.0, 55, 100, 200] // blue
] as const

const rampColor = (t: number, alpha: number): string => {
  // Clamp to [0, 1]
  const pos = Math.max(0, Math.min(1, t))

  // Find bracketing stops
  let lo = TRACE_RAMP[0]
  let hi = TRACE_RAMP[TRACE_RAMP.length - 1]

  for (let i = 0; i < TRACE_RAMP.length - 1; i++) {
    if (pos >= TRACE_RAMP[i][0] && pos <= TRACE_RAMP[i + 1][0]) {
      lo = TRACE_RAMP[i]
      hi = TRACE_RAMP[i + 1]
      break
    }
  }

  const span = hi[0] - lo[0]
  const frac = span === 0 ? 0 : (pos - lo[0]) / span

  const r = Math.round(lo[1] + (hi[1] - lo[1]) * frac)
  const g = Math.round(lo[2] + (hi[2] - lo[2]) * frac)
  const b = Math.round(lo[3] + (hi[3] - lo[3]) * frac)

  return `rgba(${r},${g},${b},${alpha})`
}

// ── Types ─────────────────────────────────────────────────────────────

export interface TracePosition {
  /** 0.0 (top) to 1.0 (bottom) */
  y: number
  /** 0–100 score derived from position */
  score: number
}

export interface MessageTraceData {
  /** The trace score (0–100), or null if not traced */
  score: number | null
}

// ── Component ──────────────────────────────────────────────────────────

interface MessageTraceProps {
  /** Called when the user clicks a position on the trace strip */
  onTrace: (position: TracePosition) => void
  /** Existing trace data, or null if not traced yet */
  trace?: MessageTraceData | null
  className?: string
}

/**
 * Ambient reaction trace — a clickable gradient strip on the message edge.
 *
 * Replaces discrete emoji reactions with a continuous color trace.
 * The click position maps to a 0–100 score and a green→blue gradient color.
 * Deuteranopia-safe: the ramp goes green→teal→blue, never red-green.
 *
 * @example
 * ```tsx
 * <MessageTrace
 *   onTrace={pos => storeTrace(messageId, pos.score)}
 *   trace={tracedMessages.get(messageId)}
 * />
 * ```
 */
export function MessageTrace({ className, onTrace, trace }: MessageTraceProps) {
  const scored = trace?.score != null

  const handleClick = (event: React.MouseEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const y = (event.clientY - rect.top) / rect.height
    // Top = high score (100), bottom = low (0)
    const score = Math.round((1 - y) * 100)

    onTrace({ y, score })
  }

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      // Keyboard: score at midpoint (50)
      onTrace({ y: 0.5, score: 50 })
    }
  }

  // Hover gradient: position-mapped colors at higher opacity
  const hoverBg = `linear-gradient(180deg,
    ${rampColor(0.0, 0.2)} 0%,
    ${rampColor(0.15, 0.55)} 15%,
    ${rampColor(0.42, 0.4)} 42%,
    ${rampColor(0.58, 0.45)} 58%,
    ${rampColor(0.85, 0.55)} 85%,
    ${rampColor(1.0, 0.2)} 100%
  )`

  // Idle gradient: subtle, barely visible
  const idleBg = `linear-gradient(180deg,
    ${rampColor(0.0, 0)} 0%,
    ${rampColor(0.15, 0.1)} 20%,
    ${rampColor(0.42, 0.1)} 45%,
    ${rampColor(0.58, 0.1)} 55%,
    ${rampColor(0.85, 0.1)} 80%,
    ${rampColor(1.0, 0)} 100%
  )`

  // Scored: left-border glow
  const traceColor = scored ? rampColor(1 - (trace!.score! / 100), 0.7) : 'transparent'

  return (
    <>
      {/* Left border glow — only when traced */}
      {scored && (
        <div
          aria-hidden
          className="animate-settle absolute inset-y-0 left-0 w-[3px] rounded-l-[3px]"
          style={{
            background: traceColor,
            boxShadow: `0 0 10px ${traceColor}, 0 0 28px ${traceColor}`
          }}
        />
      )}

      {/* Right edge trace strip */}
      <div
        aria-label={`Message trace ${scored ? `— score ${trace!.score!}` : '— click to score'}`}
        className={cn(
          'absolute inset-y-0 right-0 w-5 cursor-col-resize z-[3]',
          'after:absolute after:inset-y-0 after:right-0 after:w-px after:rounded-r after:transition-all after:duration-600',
          'focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-(--chrome-action-focus)',
          className
        )}
        onClick={handleClick}
        onKeyDown={handleKeyDown}
        role="slider"
        tabIndex={0}
        title={scored ? `Score: ${trace!.score!}/100` : 'Click to trace'}
        // CSS custom properties for the hover state
        style={
          {
            '--trace-hover-bg': hoverBg,
            '--trace-idle-bg': idleBg
          } as React.CSSProperties
        }
      />

      <style>{`
        [role="slider"]:hover::after {
          width: 4px !important;
          background: var(--trace-hover-bg) !important;
          box-shadow: -2px 0 14px rgba(255,255,255,0.06), -4px 0 8px rgba(0,0,0,0.15) !important;
          opacity: 1 !important;
        }

        [role="slider"]::after {
          background: var(--trace-idle-bg);
          opacity: 0.6;
        }

        .animate-settle {
          animation: trace-settle 2.5s ease forwards;
        }

        @keyframes trace-settle {
          0% { opacity: 0; filter: brightness(2.8); }
          30% { opacity: 1; filter: brightness(1.25); }
          100% { opacity: 1; filter: brightness(1); }
        }
      `}</style>
    </>
  )
}
