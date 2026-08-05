// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DashboardContextSidebar } from "./App";
import type { DashboardContext } from "./lib/dashboard-pages";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const page = {
  id: "mcp",
  label: "MCP Servers",
  path: "/mcp",
  group: "integrations" as const,
  description: "Manage MCP servers.",
};

const context: DashboardContext = {
  group: "integrations",
  active: page,
  pages: [page],
};

let container: HTMLDivElement;
let root: Root;

afterEach(async () => {
  if (root) await act(() => root.unmount());
  container?.remove();
});

async function renderSidebar(mobileOpen: boolean, onClose = vi.fn()) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(() =>
    root.render(
      <MemoryRouter>
        <DashboardContextSidebar
          context={context}
          mobileOpen={mobileOpen}
          onClose={onClose}
        />
      </MemoryRouter>,
    ),
  );
}

describe("DashboardContextSidebar", () => {
  it("does not mount a focusable mobile drawer while closed", async () => {
    await renderSidebar(false);

    expect(document.querySelector("#dashboard-context-sidebar")).toBeNull();
    expect(document.querySelector("#dashboard-context-sidebar-desktop")).not.toBeNull();
    expect(document.querySelector('[aria-label="Close related pages"]')).toBeNull();
  });

  it("does not invoke the mobile close callback from desktop navigation", async () => {
    const onClose = vi.fn();
    await renderSidebar(false, onClose);

    const link = document.querySelector<HTMLAnchorElement>(
      "#dashboard-context-sidebar-desktop a[href]",
    );
    await act(() =>
      link?.dispatchEvent(
        new MouseEvent("click", { bubbles: true, cancelable: true }),
      ),
    );

    expect(onClose).not.toHaveBeenCalled();
  });

  it("mounts the mobile drawer as a modal while open", async () => {
    await renderSidebar(true);

    const drawer = document.querySelector("#dashboard-context-sidebar");
    expect(drawer?.getAttribute("role")).toBe("dialog");
    expect(drawer?.getAttribute("aria-modal")).toBe("true");
    expect(document.querySelector('[aria-label="Close related pages"]')).not.toBeNull();
  });

  it("keeps keyboard focus inside the open mobile drawer", async () => {
    await renderSidebar(true);

    const close = document.querySelector<HTMLButtonElement>(
      '[aria-label="Close related pages"]',
    );
    const link = document.querySelector<HTMLAnchorElement>(
      "#dashboard-context-sidebar a[href]",
    );
    expect(close).not.toBeNull();
    expect(link).not.toBeNull();

    link?.focus();
    await act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Tab", bubbles: true }),
      );
    });

    expect(document.activeElement).toBe(close);
  });

  it("redirects outside Tab focus back into the open mobile drawer", async () => {
    await renderSidebar(true);
    const outside = document.createElement("button");
    document.body.appendChild(outside);
    outside.focus();

    await act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Tab", bubbles: true }),
      );
    });

    expect(document.activeElement).toBe(
      document.querySelector('[aria-label="Close related pages"]'),
    );
    outside.remove();
  });
});
