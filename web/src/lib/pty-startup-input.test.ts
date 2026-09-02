import { describe, expect, it } from "vitest";

import { shouldIgnoreSyntheticStartupInput } from "./pty-startup-input";

describe("shouldIgnoreSyntheticStartupInput", () => {
  it("ignores a lone printable byte during startup with no prior input signal", () => {
    expect(
      shouldIgnoreSyntheticStartupInput({
        data: "l",
        now: 100,
        startupWindowUntil: 400,
        lastKeyboardEventAt: 0,
        lastCompositionEventAt: 0,
      }),
    ).toBe(true);
  });

  it("does not ignore input after the startup window", () => {
    expect(
      shouldIgnoreSyntheticStartupInput({
        data: "l",
        now: 500,
        startupWindowUntil: 400,
        lastKeyboardEventAt: 0,
        lastCompositionEventAt: 0,
      }),
    ).toBe(false);
  });

  it("keeps real keyboard-driven input", () => {
    expect(
      shouldIgnoreSyntheticStartupInput({
        data: "l",
        now: 200,
        startupWindowUntil: 400,
        lastKeyboardEventAt: 120,
        lastCompositionEventAt: 0,
      }),
    ).toBe(false);
  });

  it("keeps recent composition-driven input", () => {
    expect(
      shouldIgnoreSyntheticStartupInput({
        data: "l",
        now: 200,
        startupWindowUntil: 400,
        lastKeyboardEventAt: 0,
        lastCompositionEventAt: 120,
      }),
    ).toBe(false);
  });

  it("does not ignore control bytes", () => {
    expect(
      shouldIgnoreSyntheticStartupInput({
        data: "\r",
        now: 100,
        startupWindowUntil: 400,
        lastKeyboardEventAt: 0,
        lastCompositionEventAt: 0,
      }),
    ).toBe(false);
  });

  it("does not ignore multi-character text", () => {
    expect(
      shouldIgnoreSyntheticStartupInput({
        data: "hello",
        now: 100,
        startupWindowUntil: 400,
        lastKeyboardEventAt: 0,
        lastCompositionEventAt: 0,
      }),
    ).toBe(false);
  });
});
