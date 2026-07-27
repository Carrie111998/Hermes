import { describe, expect, it } from "vitest";
import {
  PTY_CLEAR_COMPOSER,
  PTY_PAGE_DOWN,
  PTY_PAGE_UP,
  PTY_SCROLL_LINE_DOWN,
  PTY_SCROLL_LINE_UP,
  PTY_SUBMIT,
  accumulateMobileScrollDelta,
  advanceMobileScrollGesture,
  buildMobileComposerSubmitPayload,
  createMobileGestureState,
  encodeMobileScrollLines,
  mobileScrollButtonPayload,
  sanitizeMobileComposerText,
  shouldSubmitNativeComposerEnter,
  shouldUseMobileNativeComposer,
  wheelDeltaToXtermScrollLines,
  xtermScrollButtonLines,
  xtermTouchPxPerLine,
} from "./pty-mobile-bridge";

describe("sanitizeMobileComposerText", () => {
  it("normalizes CR and CRLF to LF", () => {
    expect(sanitizeMobileComposerText("a\r\nb\rc")).toBe("a\nb\nc");
  });

  it("converts TAB to spaces", () => {
    expect(sanitizeMobileComposerText("a\tb")).toBe("a  b");
  });

  it("strips ESC and CSI sequences to remaining printable fragments", () => {
    // ESC + "[5~" leaves the printable "[5~" after ESC is dropped — controls
    // themselves never reach the PTY as CSI introducers.
    expect(sanitizeMobileComposerText("hi\x1b[5~there")).toBe("hi[5~there");
    expect(sanitizeMobileComposerText("\x1b")).toBe("");
  });

  it("strips BEL and NUL", () => {
    expect(sanitizeMobileComposerText("a\x07b\x00c")).toBe("abc");
  });

  it("strips Ctrl+A and Ctrl+K (clear-sequence body bytes)", () => {
    expect(sanitizeMobileComposerText("x\x01y\x0bz")).toBe("xyz");
  });

  it("strips DEL", () => {
    expect(sanitizeMobileComposerText("ab\x7fcd")).toBe("abcd");
  });

  it("strips C1 CSI (U+009B)", () => {
    expect(sanitizeMobileComposerText(`pre\u009B[5~post`)).toBe("pre[5~post");
  });

  it("preserves Unicode, emoji, punctuation, slash commands, and LF", () => {
    const sample = "/help 你好 🎉 — line\ntwo!";
    expect(sanitizeMobileComposerText(sample)).toBe(sample);
  });
});

describe("buildMobileComposerSubmitPayload", () => {
  it("returns null for empty or whitespace-only drafts", () => {
    expect(buildMobileComposerSubmitPayload("")).toBeNull();
    expect(buildMobileComposerSubmitPayload("   \n  ")).toBeNull();
  });

  it("returns null for control-only drafts", () => {
    expect(buildMobileComposerSubmitPayload("\x1b\x07\x00\x01\x0b\x7f")).toBeNull();
  });

  it("clears the TUI composer, sends sanitized text once, then submits", () => {
    const payload = buildMobileComposerSubmitPayload("hello world");
    expect(payload).toBe(`${PTY_CLEAR_COMPOSER}hello world${PTY_SUBMIT}`);
    expect(payload!.startsWith(PTY_CLEAR_COMPOSER)).toBe(true);
    expect(payload!.endsWith(PTY_SUBMIT)).toBe(true);
    // Exactly one submit terminator (trusted suffix only).
    expect(payload!.slice(0, -1).includes("\r")).toBe(false);
    // Exactly one trusted clear prefix — body cannot reintroduce SOH/VT.
    const body = payload!.slice(
      PTY_CLEAR_COMPOSER.length,
      payload!.length - PTY_SUBMIT.length,
    );
    expect(body.includes("\x01")).toBe(false);
    expect(body.includes("\x0b")).toBe(false);
    expect(body.includes("\x1b")).toBe(false);
  });

  it("preserves slash commands verbatim", () => {
    const payload = buildMobileComposerSubmitPayload("/help");
    expect(payload).toBe(`${PTY_CLEAR_COMPOSER}/help${PTY_SUBMIT}`);
  });

  it("preserves internal newlines for multiline drafts", () => {
    const payload = buildMobileComposerSubmitPayload("line one\nline two");
    expect(payload).toBe(
      `${PTY_CLEAR_COMPOSER}line one\nline two${PTY_SUBMIT}`,
    );
  });

  it("normalizes CRLF to LF before send", () => {
    const payload = buildMobileComposerSubmitPayload("a\r\nb\rc");
    expect(payload).toBe(`${PTY_CLEAR_COMPOSER}a\nb\nc${PTY_SUBMIT}`);
  });

  it("strips ESC/CSI/BEL/NUL/Ctrl+A/Ctrl+K/DEL/C1 from the body", () => {
    const dirty =
      "ok\x1b[5~\x07\x00\x01\x0b\x7f\u009B/world\t🎉";
    const payload = buildMobileComposerSubmitPayload(dirty);
    expect(payload).toBe(`${PTY_CLEAR_COMPOSER}ok[5~/world  🎉${PTY_SUBMIT}`);
  });

  it("never lets user text add a second submit CR", () => {
    const payload = buildMobileComposerSubmitPayload("a\rb\rc");
    expect(payload).toBe(`${PTY_CLEAR_COMPOSER}a\nb\nc${PTY_SUBMIT}`);
    const crs = [...payload!].filter((c) => c === "\r");
    expect(crs).toHaveLength(1);
  });
});

describe("shouldSubmitNativeComposerEnter", () => {
  it("does not submit while IME composition is active", () => {
    expect(
      shouldSubmitNativeComposerEnter({ composing: true, shiftKey: false }),
    ).toBe(false);
  });

  it("does not submit on Shift+Enter (newline in the native field)", () => {
    expect(
      shouldSubmitNativeComposerEnter({ composing: false, shiftKey: true }),
    ).toBe(false);
  });

  it("submits on plain Enter when not composing", () => {
    expect(
      shouldSubmitNativeComposerEnter({ composing: false, shiftKey: false }),
    ).toBe(true);
  });
});

describe("mobile scroll encoding", () => {
  it("maps finger-down motion to older (negative TUI lines)", () => {
    const { lines, nextCarryPx } = accumulateMobileScrollDelta(0, 56, 28);
    expect(lines).toBe(-2);
    expect(nextCarryPx).toBe(0);
  });

  it("maps finger-up motion to newer (positive TUI lines)", () => {
    const { lines } = accumulateMobileScrollDelta(0, -28, 28);
    expect(lines).toBe(1);
  });

  it("carries sub-threshold motion across events", () => {
    const a = accumulateMobileScrollDelta(0, 10, 28);
    expect(a.lines).toBe(0);
    const b = accumulateMobileScrollDelta(a.nextCarryPx, 20, 28);
    expect(b.lines).toBe(-1);
  });

  it("encodes small moves as Shift+Arrow and large moves as Page keys", () => {
    expect(encodeMobileScrollLines(-1)).toBe(PTY_SCROLL_LINE_UP);
    expect(encodeMobileScrollLines(2)).toBe(
      PTY_SCROLL_LINE_DOWN + PTY_SCROLL_LINE_DOWN,
    );
    expect(encodeMobileScrollLines(-8)).toBe(PTY_PAGE_UP + PTY_PAGE_UP);
    expect(encodeMobileScrollLines(6)).toBe(PTY_PAGE_DOWN);
  });

  it("caps extreme line counts so one payload cannot flood the PTY", () => {
    const payload = encodeMobileScrollLines(-500);
    // Default max 24 lines → ceil(24/6)=4 page-ups
    expect(payload).toBe(PTY_PAGE_UP.repeat(4));
  });

  it("exposes Older/Newer button payloads as PageUp/PageDown", () => {
    expect(mobileScrollButtonPayload("older")).toBe(PTY_PAGE_UP);
    expect(mobileScrollButtonPayload("newer")).toBe(PTY_PAGE_DOWN);
  });
});

describe("advanceMobileScrollGesture", () => {
  it("ignores tiny movement before axis lock", () => {
    const state = createMobileGestureState(100, 100);
    const result = advanceMobileScrollGesture(state, 102, 104);
    expect(result.ignore).toBe(true);
    expect(result.lines).toBe(0);
    expect(result.state.axis).toBe("none");
  });

  it("locks horizontal gestures and produces no scroll", () => {
    const state = createMobileGestureState(100, 100);
    const result = advanceMobileScrollGesture(state, 140, 105);
    expect(result.ignore).toBe(true);
    expect(result.state.axis).toBe("horizontal");
    expect(result.lines).toBe(0);
  });

  it("locks vertical gestures and emits older lines for finger-down", () => {
    let state = createMobileGestureState(100, 100);
    const lock = advanceMobileScrollGesture(state, 100, 130);
    expect(lock.lockVertical).toBe(true);
    expect(lock.state.axis).toBe("vertical");
    // delta 30px / 28 → -1 line
    expect(lock.lines).toBe(-1);
    state = lock.state;

    const cont = advanceMobileScrollGesture(state, 100, 158);
    expect(cont.lines).toBe(-1);
  });

  it("emits newer lines for finger-up after vertical lock", () => {
    let state = createMobileGestureState(50, 200);
    const lock = advanceMobileScrollGesture(state, 50, 160);
    expect(lock.state.axis).toBe("vertical");
    // deltaY = -40 → positive (newer) lines
    expect(lock.lines).toBeGreaterThan(0);
    state = lock.state;
    const cont = advanceMobileScrollGesture(state, 50, 132);
    expect(cont.lines).toBe(1);
  });
});

describe("shouldUseMobileNativeComposer", () => {
  it("enables on narrow layouts", () => {
    expect(shouldUseMobileNativeComposer({ narrow: true })).toBe(true);
  });

  it("enables on coarse pointers even when not narrow", () => {
    expect(
      shouldUseMobileNativeComposer({ narrow: false, coarsePointer: true }),
    ).toBe(true);
  });

  it("stays off for desktop fine-pointer wide layouts", () => {
    expect(
      shouldUseMobileNativeComposer({ narrow: false, coarsePointer: false }),
    ).toBe(false);
  });
});

describe("xterm scrollback helpers (wheel / touch / Older-Newer)", () => {
  it("maps wheel deltaY to scrollLines like the ChatPage wheel handler", () => {
    expect(wheelDeltaToXtermScrollLines(0)).toBe(0);
    expect(wheelDeltaToXtermScrollLines(NaN)).toBe(0);
    expect(wheelDeltaToXtermScrollLines(50)).toBe(1);
    expect(wheelDeltaToXtermScrollLines(120)).toBe(2);
    expect(wheelDeltaToXtermScrollLines(-80)).toBe(-2);
  });

  it("maps Older/Newer buttons to half-viewport xterm lines", () => {
    expect(xtermScrollButtonLines("older", 24)).toBe(-12);
    expect(xtermScrollButtonLines("newer", 24)).toBe(12);
    expect(xtermScrollButtonLines("older", 1)).toBe(-1);
    expect(xtermScrollButtonLines("newer", 0)).toBe(1);
  });

  it("derives touch px-per-line from host height and rows", () => {
    expect(xtermTouchPxPerLine(480, 24)).toBe(20);
    expect(xtermTouchPxPerLine(0, 24)).toBe(28);
    expect(xtermTouchPxPerLine(480, 0)).toBe(28);
    // Never thinner than 12px so tiny hosts still accumulate sensibly.
    expect(xtermTouchPxPerLine(100, 50)).toBe(12);
  });

  it("finger-down advances accumulate into older (negative) scrollLines", () => {
    // Same sign convention the host touch handler feeds to term.scrollLines.
    const { lines } = accumulateMobileScrollDelta(0, 40, 20);
    expect(lines).toBe(-2);
  });
});
