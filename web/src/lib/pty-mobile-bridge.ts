/**
 * Mobile-native bridge between the dashboard chat surface and the embedded
 * Hermes TUI (Ink TextInput composer + optional TUI scroll key helpers).
 *
 * Open-conversation transcript scrolling uses the browser-side xterm
 * scrollback buffer via `term.scrollLines` (custom wheel handler + mobile
 * touch pan). xterm's ScrollableElement only listens for wheel events; the
 * painted `.xterm-screen` sits above `.xterm-viewport`, so Android finger
 * pans never natively scroll the viewport — ChatPage must translate them.
 *
 * CSI Page/Shift+Arrow encoders remain for tests and any path that must
 * drive the TUI's own Ink ScrollBox rather than browser scrollback.
 *
 * User-controlled draft text is sanitized before it is concatenated with
 * trusted structural controls (clear + submit). Never let paste/IME inject
 * ESC/CSI/BEL/NUL/Ctrl+A/K into the PTY key stream.
 */

/** CSI PageUp — useInputHandlers → scrollTranscript(-halfViewport). */
export const PTY_PAGE_UP = "\x1b[5~";
/** CSI PageDown — useInputHandlers → scrollTranscript(+halfViewport). */
export const PTY_PAGE_DOWN = "\x1b[6~";
/** Shift+Up — useInputHandlers → scrollTranscript(-1). */
export const PTY_SCROLL_LINE_UP = "\x1b[1;2A";
/** Shift+Down — useInputHandlers → scrollTranscript(+1). */
export const PTY_SCROLL_LINE_DOWN = "\x1b[1;2B";

/**
 * Readline-style full-line clear for the TUI TextInput:
 *   Ctrl+A (home) then Ctrl+K (kill-to-end).
 * Trusted structural prefix — never taken from user text.
 */
export const PTY_CLEAR_COMPOSER = "\x01\x0b";

/**
 * Bare Enter / Return — TextInput submits the current value. Trusted suffix.
 *
 * MUST be its own PTY/WebSocket frame, after CLEAR and body have each been
 * written as separate frames (with a short coalesce delay before this one).
 * - CLEAR+body+\r in one write → paste path, CR becomes LF, no submit.
 * - CLEAR+body in one write then delayed \r → body often dropped (controls
 *   poison the printable burst) and \r submits an empty line.
 */
export const PTY_SUBMIT = "\r";

/** Pixels of vertical drag per single Shift+Arrow scroll step. */
export const MOBILE_SCROLL_PX_PER_LINE = 28;

/** Movement (px) before a gesture locks to vertical or horizontal. */
export const MOBILE_GESTURE_LOCK_SLOP_PX = 10;

/** Ignore residual jitter smaller than this after lock. */
export const MOBILE_GESTURE_MIN_STEP_PX = 8;

/**
 * Cap absolute line-steps encoded into one scroll payload (per rAF frame or
 * button click) so a long fling cannot flood the PTY with hundreds of CSIs.
 */
export const MOBILE_SCROLL_MAX_LINES_PER_PAYLOAD = 24;

export type MobileScrollDirection = "older" | "newer";

export interface MobileGestureState {
  startX: number;
  startY: number;
  lastY: number;
  axis: "none" | "vertical" | "horizontal";
  /** Accumulated vertical remainder not yet converted into a step. */
  carryPx: number;
}

export function createMobileGestureState(
  clientX: number,
  clientY: number,
): MobileGestureState {
  return {
    startX: clientX,
    startY: clientY,
    lastY: clientY,
    axis: "none",
    carryPx: 0,
  };
}

/**
 * Sanitize user draft text before it is placed on the PTY key stream.
 *
 * Rules:
 * 1. CRLF / CR → LF
 * 2. TAB → two spaces (never a real Tab key)
 * 3. Strip C0 (U+0000–U+001F) except LF
 * 4. Strip DEL (U+007F)
 * 5. Strip C1 (U+0080–U+009F), including U+009B single-byte CSI
 * 6. Preserve ordinary Unicode, emoji, punctuation, slash commands, LF
 *
 * Structural PTY controls (clear/submit) are added only by
 * buildMobileComposerSubmitPayload and must never appear from this step
 * as trusted framing — any user-sourced SOH/VT/CR/ESC is removed here.
 */
export function sanitizeMobileComposerText(text: string): string {
  if (!text) return "";

  let out = "";
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    const ch = text[i];

    // CRLF → single LF
    if (code === 0x0d) {
      const next = text.charCodeAt(i + 1);
      if (next === 0x0a) i += 1;
      out += "\n";
      continue;
    }

    // TAB → spaces
    if (code === 0x09) {
      out += "  ";
      continue;
    }

    // Keep LF only among C0.
    if (code === 0x0a) {
      out += "\n";
      continue;
    }

    // Strip remaining C0 (includes NUL, BEL, ESC, SOH/Ctrl+A, VT/Ctrl+K, …).
    if (code < 0x20) continue;

    // DEL
    if (code === 0x7f) continue;

    // C1 controls (includes single-byte CSI U+009B).
    if (code >= 0x80 && code <= 0x9f) continue;

    out += ch;
  }

  return out;
}

/**
 * Build the PTY payload that replaces whatever is in the TUI composer with
 * sanitized `text` and submits it.
 *
 * Empty / whitespace-only / control-only drafts return null.
 * Finished payload is always: CLEAR + sanitizedBody + single SUBMIT.
 */
export function buildMobileComposerSubmitPayload(
  text: string,
): string | null {
  const sanitized = sanitizeMobileComposerText(text);
  // Reject empty or whitespace-only after sanitization (control-only drafts
  // collapse to empty / whitespace and must not submit).
  if (!sanitized || !sanitized.trim()) return null;

  return `${PTY_CLEAR_COMPOSER}${sanitized}${PTY_SUBMIT}`;
}

/**
 * Whether the native composer should treat Enter as submit.
 * Composition-active Enter must insert nothing / not submit (IME confirm).
 */
export function shouldSubmitNativeComposerEnter(options: {
  composing: boolean;
  shiftKey: boolean;
}): boolean {
  if (options.composing) return false;
  if (options.shiftKey) return false;
  return true;
}

/**
 * Map a vertical pixel delta into discrete TUI scroll steps.
 *
 * Sign convention (touch, natural):
 *   finger moves down  → positive deltaY → older content (PageUp / line-up)
 *   finger moves up    → negative deltaY → newer content (PageDown / line-down)
 *
 * Returns the signed line count to apply (negative = older, positive = newer
 * in TUI scrollTranscript terms where negative is up/older). Also returns the
 * leftover pixel carry so sub-threshold motion accumulates across events.
 */
export function accumulateMobileScrollDelta(
  prevCarryPx: number,
  deltaY: number,
  pxPerLine: number = MOBILE_SCROLL_PX_PER_LINE,
): { lines: number; nextCarryPx: number } {
  if (!Number.isFinite(deltaY) || pxPerLine <= 0) {
    return { lines: 0, nextCarryPx: prevCarryPx };
  }
  // Finger down (deltaY > 0) → older → negative TUI delta.
  const total = prevCarryPx + deltaY;
  const linesTowardOlder = Math.trunc(total / pxPerLine);
  // Normalize -0 to 0 so callers/tests can use Object.is equality.
  const lines = linesTowardOlder === 0 ? 0 : -linesTowardOlder;
  const nextCarryPx = total - linesTowardOlder * pxPerLine;
  return { lines, nextCarryPx };
}

/**
 * Encode `lines` TUI scroll steps as a PTY byte string.
 * Uses line-granularity Shift+Arrow for small moves and PageUp/Down when
 * |lines| is large. Caps absolute magnitude so one frame cannot flood stdin.
 */
export function encodeMobileScrollLines(
  lines: number,
  pageThreshold = 6,
  maxLines: number = MOBILE_SCROLL_MAX_LINES_PER_PAYLOAD,
): string {
  if (!lines) return "";
  const capped =
    Math.sign(lines) *
    Math.min(Math.abs(lines), Math.max(1, maxLines));
  const older = capped < 0;
  const abs = Math.abs(capped);
  if (abs >= pageThreshold) {
    // ceil so a long fling covers at least the requested distance in pages.
    const pages = Math.max(1, Math.ceil(abs / pageThreshold));
    const seq = older ? PTY_PAGE_UP : PTY_PAGE_DOWN;
    return seq.repeat(pages);
  }
  const seq = older ? PTY_SCROLL_LINE_UP : PTY_SCROLL_LINE_DOWN;
  return seq.repeat(abs);
}

/**
 * Advance a gesture with a new touch point. Returns *line delta* (not bytes)
 * so the caller can accumulate across rAF frames and encode once per frame.
 * Horizontal / unlocked tiny moves produce no scroll. When the axis first
 * locks to vertical, `preventDefault` should be requested by the caller.
 */
export function advanceMobileScrollGesture(
  state: MobileGestureState,
  clientX: number,
  clientY: number,
  options?: {
    lockSlopPx?: number;
    pxPerLine?: number;
  },
): {
  state: MobileGestureState;
  /** Signed TUI lines this move contributes (0 if none). */
  lines: number;
  lockVertical: boolean;
  ignore: boolean;
} {
  const lockSlop = options?.lockSlopPx ?? MOBILE_GESTURE_LOCK_SLOP_PX;
  const pxPerLine = options?.pxPerLine ?? MOBILE_SCROLL_PX_PER_LINE;

  const dx = clientX - state.startX;
  const dyFromStart = clientY - state.startY;
  let axis = state.axis;
  let lockVertical = false;

  if (axis === "none") {
    const adx = Math.abs(dx);
    const ady = Math.abs(dyFromStart);
    if (adx < lockSlop && ady < lockSlop) {
      return {
        state: { ...state, lastY: clientY },
        lines: 0,
        lockVertical: false,
        ignore: true,
      };
    }
    if (adx > ady) {
      return {
        state: { ...state, axis: "horizontal", lastY: clientY },
        lines: 0,
        lockVertical: false,
        ignore: true,
      };
    }
    axis = "vertical";
    lockVertical = true;
  }

  if (axis === "horizontal") {
    return {
      state: { ...state, axis, lastY: clientY },
      lines: 0,
      lockVertical: false,
      ignore: true,
    };
  }

  const deltaY = clientY - state.lastY;
  const { lines, nextCarryPx } = accumulateMobileScrollDelta(
    state.carryPx,
    deltaY,
    pxPerLine,
  );

  return {
    state: {
      ...state,
      axis: "vertical",
      lastY: clientY,
      carryPx: nextCarryPx,
    },
    lines,
    lockVertical,
    ignore: false,
  };
}

/** One-shot Older / Newer button payloads (half-viewport each). */
export function mobileScrollButtonPayload(
  direction: MobileScrollDirection,
): string {
  return direction === "older" ? PTY_PAGE_UP : PTY_PAGE_DOWN;
}

/**
 * Map a wheel `deltaY` into xterm `scrollLines` amount (same formula as the
 * ChatPage custom wheel handler). Positive = newer / toward bottom.
 */
export function wheelDeltaToXtermScrollLines(deltaY: number): number {
  if (!deltaY || !Number.isFinite(deltaY)) return 0;
  const step = Math.max(1, Math.round(Math.abs(deltaY) / 50));
  return deltaY > 0 ? step : -step;
}

/**
 * Half-viewport line step for Older / Newer controls on the xterm scrollback
 * surface. Positive = newer, negative = older.
 */
export function xtermScrollButtonLines(
  direction: MobileScrollDirection,
  viewportRows: number,
): number {
  const rows = Number.isFinite(viewportRows) ? Math.floor(viewportRows) : 0;
  const half = Math.max(1, Math.floor(Math.max(rows, 1) / 2));
  return direction === "older" ? -half : half;
}

/**
 * Pixel distance that should count as one terminal row when converting a
 * finger pan into `scrollLines`. Prefers measured host height / rows.
 */
export function xtermTouchPxPerLine(
  hostHeightPx: number,
  viewportRows: number,
  fallback: number = MOBILE_SCROLL_PX_PER_LINE,
): number {
  const rows = Number.isFinite(viewportRows) ? Math.floor(viewportRows) : 0;
  if (hostHeightPx > 0 && rows > 0) {
    return Math.max(12, hostHeightPx / rows);
  }
  return fallback > 0 ? fallback : MOBILE_SCROLL_PX_PER_LINE;
}

/**
 * Decide whether the mobile-native composer layer should be shown.
 * Mirrors the dashboard's existing narrow breakpoint (max-width: 1023px)
 * and also accepts coarse pointers (phones that report a wide CSS width
 * while still being touch-primary).
 */
export function shouldUseMobileNativeComposer(options: {
  narrow: boolean;
  coarsePointer?: boolean;
}): boolean {
  return Boolean(options.narrow || options.coarsePointer);
}
