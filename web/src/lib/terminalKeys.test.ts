import { describe, expect, it } from "vitest";

import { SHIFT_ENTER_SEQUENCE, shiftEnterSequence } from "./terminalKeys";

describe("shiftEnterSequence", () => {
  it("maps Shift+Enter to the TUI sequence", () => {
    expect(shiftEnterSequence({ key: "Enter", shiftKey: true })).toBe(
      SHIFT_ENTER_SEQUENCE,
    );
  });

  it("returns null for bare Enter", () => {
    expect(shiftEnterSequence({ key: "Enter", shiftKey: false })).toBeNull();
  });

  it("returns null for non-Enter keys even with Shift held", () => {
    expect(shiftEnterSequence({ key: "Enter", shiftKey: false })).toBeNull();
    expect(shiftEnterSequence({ key: "a", shiftKey: true })).toBeNull();
  });

  it("emits the exact sequence the TUI parser recognises (ESC[13;2u)", () => {
    expect(SHIFT_ENTER_SEQUENCE).toBe("\u001b[13;2u");
  });
});
