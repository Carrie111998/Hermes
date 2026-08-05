import { describe, expect, it } from "vitest";
import {
  BUILTIN_THEMES,
  resolveBuiltinTheme,
  studioDarkTheme,
  studioLightTheme,
  studioSystemTheme,
} from "./presets";

describe("Hermes Studio themes", () => {
  it("registers system, light, and dark appearance presets", () => {
    expect(BUILTIN_THEMES["studio-system"]).toBe(studioSystemTheme);
    expect(BUILTIN_THEMES["studio-light"]).toBe(studioLightTheme);
    expect(BUILTIN_THEMES["studio-dark"]).toBe(studioDarkTheme);
  });

  it("uses the approved prototype palette and restrained layout", () => {
    expect(studioLightTheme.palette.background.hex).toBe("#f5f7fa");
    expect(studioLightTheme.colorOverrides?.primary).toBe("#635bff");
    expect(studioDarkTheme.palette.background.hex).toBe("#090c12");
    expect(studioDarkTheme.colorOverrides?.primary).toBe("#8b83ff");
    expect(studioLightTheme.layout).toEqual({
      radius: "0.75rem",
      density: "comfortable",
    });
  });

  it("resolves system mode from the OS while explicit modes remain stable", () => {
    expect(resolveBuiltinTheme("studio-system", false)).toBe(studioLightTheme);
    expect(resolveBuiltinTheme("studio-system", true)).toBe(studioDarkTheme);
    expect(resolveBuiltinTheme("studio-light", true)).toBe(studioLightTheme);
    expect(resolveBuiltinTheme("studio-dark", false)).toBe(studioDarkTheme);
  });
});
