// @vitest-environment jsdom
/**
 * Rendered tests for the ReasoningPicker per-skill editor.
 *
 * Addresses review #93378 blocker #4 — asserts the exact contract called out:
 *   "Add a rendered component test that types a multi-character name, selects
 *    Off, and asserts one saved map of { "plan": "off" } with no empty-string
 *    key."
 *
 * Also covers the other two editor defects from that review:
 *   - Selecting Off persists the literal "off" sentinel (NOT a delete, which
 *     would let the skill's frontmatter suggestion fire).
 *   - addRow() does not persist an empty-string key before a valid name is
 *     committed.
 */
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// --- Mocks ---------------------------------------------------------------

const apiMocks = vi.hoisted(() => {
  const saves: Array<Record<string, unknown>> = [];
  let cfg: Record<string, unknown> = {};
  return {
    savedMaps: () => saves,
    getConfig: vi.fn(async () => cfg),
    saveConfig: vi.fn(async (next: Record<string, unknown>) => {
      cfg = next;
      saves.push(next);
      return next;
    }),
    reset: () => {
      saves.length = 0;
      cfg = {};
    },
  };
});

vi.mock("@/lib/api", () => ({
  api: {
    getConfig: apiMocks.getConfig,
    saveConfig: apiMocks.saveConfig,
  },
}));

vi.mock("@nous-research/ui/ui/components/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: (v: string) => void;
    children?: ReactNode;
  }) => {
    const options = (Array.isArray(children) ? children : []).map((c) =>
      (c as { props?: { value?: string; children?: ReactNode } })?.props ?? {},
    );
    return (
      <div data-testid="select" data-value={value ?? ""}>
        {options.map((o) => (
          <button
            key={o.value}
            data-option={o.value}
            data-label={String(o.children ?? "")}
            onClick={() => onValueChange?.(o.value ?? "")}
          >
            {o.children}
          </button>
        ))}
      </div>
    );
  },
  SelectOption: ({ value, children }: { value?: string; children?: ReactNode }) =>
    null,
}));

// --- Render helper --------------------------------------------------------

let container: HTMLDivElement;
let root: Root;

async function render(ui: ReactNode) {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(ui));
}

async function click(selector: string) {
  const el = container!.querySelector(selector);
  if (!el) throw new Error(`no element for ${selector}`);
  await act(async () => (el as HTMLElement).click());
}

async function type(selector: string, value: string) {
  const el = container!.querySelector(selector) as HTMLInputElement;
  if (!el) throw new Error(`no input for ${selector}`);
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeEach(() => {
  apiMocks.reset();
  apiMocks.getConfig.mockResolvedValueOnce({
    agent: { reasoning_effort: "medium", reasoning_by_skill: {} },
  });
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
});

// --- Tests ----------------------------------------------------------------

describe("ReasoningPicker per-skill editor", () => {
  it("persists exactly {'plan':'off'} — no empty-string key — when a name is typed and Off is selected", async () => {
    const { ReasoningPicker } = await import("./ReasoningPicker");
    await render(
      <ReasoningPicker currentModel="gpt-5" />,
    );
    // Add a blank row (addRow) — must not persist anything yet.
    await click("button[data-testid=add-row]");
    expect(apiMocks.savedMaps()).toHaveLength(0);

    // Type a multi-character skill name.
    await type("input", "plan");

    // Select Off on that row's effort select (the row's select contains the
    // Off option button).
    await click("button[data-option='off']");

    // Commit the name on blur.
    const input = container!.querySelector("input") as HTMLInputElement;
    await act(async () => {
      input.dispatchEvent(new Event("blur", { bubbles: true }));
    });

    // Every persisted map must have no empty-string key, and the final map
    // must be exactly { plan: "off" } — no delete that lets the skill's
    // frontmatter suggestion fire.
    const saved = apiMocks.savedMaps();
    expect(saved.length).toBeGreaterThan(0);
    for (const s of saved) {
      const map = s.agent.reasoning_by_skill as Record<string, string>;
      expect(Object.keys(map)).not.toContain("");
    }
    const last = saved[saved.length - 1];
    expect(last.agent.reasoning_by_skill).toEqual({ plan: "off" });
  });
});
