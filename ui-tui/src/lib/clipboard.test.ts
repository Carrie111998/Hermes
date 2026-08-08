import { describe, expect, it } from "vitest";
import { normalizeClipboardText } from "./clipboard";

describe("normalizeClipboardText", () => {
  it("copies the textual payload when a message wrapper is passed", () => {
    expect(
      normalizeClipboardText({
        text: "```ts\nconst answer = 42;\n```",
        metadata: { source: "assistant" },
      }),
    ).toBe("```ts\nconst answer = 42;\n```");
  });

  it("never stringifies an internal object as [object Object]", () => {
    expect(normalizeClipboardText({ metadata: { source: "assistant" } })).toBe("");
    expect(normalizeClipboardText({ value: { text: "nested" } })).toBe("nested");
  });
});
