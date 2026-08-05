// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { DashboardContextSidebar } from "./App";
import * as AppModule from "./App";
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
let primaryTrigger: HTMLElement | undefined;

afterEach(async () => {
  if (root) await act(() => root.unmount());
  container?.remove();
  primaryTrigger?.remove();
  primaryTrigger = undefined;
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

  it("uses the drawer below xl and reserves the persistent rail for wide screens", async () => {
    await renderSidebar(true);

    const desktop = document.querySelector("#dashboard-context-sidebar-desktop");
    const drawer = document.querySelector("#dashboard-context-sidebar");
    expect(desktop?.className).toContain("xl:flex");
    expect(desktop?.className).not.toContain("lg:flex");
    expect(drawer?.className).toContain("xl:hidden");
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

describe("PrimarySidebarFrame", () => {
  it("is available as the shared desktop/mobile sidebar boundary", () => {
    expect("PrimarySidebarFrame" in AppModule).toBe(true);
  });

  it("makes the closed mobile sidebar inert without hiding desktop navigation", async () => {
    primaryTrigger = document.createElement("button");
    document.body.appendChild(primaryTrigger);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(() =>
      root.render(
        <AppModule.PrimarySidebarFrame
          id="primary-test"
          isMobile
          mobileOpen={false}
          onClose={vi.fn()}
          triggerRef={{ current: primaryTrigger! }}
        >
          <a href="/sessions">Sessions</a>
        </AppModule.PrimarySidebarFrame>,
      ),
    );

    const sidebar = document.querySelector("#primary-test");
    expect(sidebar?.getAttribute("aria-hidden")).toBe("true");
    expect(sidebar?.hasAttribute("inert")).toBe(true);

    await act(() =>
      root.render(
        <AppModule.PrimarySidebarFrame
          id="primary-test"
          isMobile={false}
          mobileOpen={false}
          onClose={vi.fn()}
          triggerRef={{ current: primaryTrigger! }}
        >
          <a href="/sessions">Sessions</a>
        </AppModule.PrimarySidebarFrame>,
      ),
    );
    expect(sidebar?.hasAttribute("aria-hidden")).toBe(false);
    expect(sidebar?.hasAttribute("inert")).toBe(false);
  });

  it("opens mobile navigation as a modal and moves focus inside", async () => {
    primaryTrigger = document.createElement("button");
    document.body.appendChild(primaryTrigger);
    primaryTrigger.focus();
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(() =>
      root.render(
        <AppModule.PrimarySidebarFrame
          id="primary-test"
          isMobile
          mobileOpen
          onClose={vi.fn()}
          triggerRef={{ current: primaryTrigger! }}
        >
          <button data-primary-sidebar-initial-focus>Close</button>
          <a href="/sessions">Sessions</a>
        </AppModule.PrimarySidebarFrame>,
      ),
    );

    const sidebar = document.querySelector("#primary-test");
    expect(sidebar?.getAttribute("role")).toBe("dialog");
    expect(sidebar?.getAttribute("aria-modal")).toBe("true");
    expect(document.activeElement).toBe(
      document.querySelector("[data-primary-sidebar-initial-focus]"),
    );
  });

  it("wraps Tab focus inside open mobile navigation", async () => {
    primaryTrigger = document.createElement("button");
    document.body.appendChild(primaryTrigger);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(() =>
      root.render(
        <AppModule.PrimarySidebarFrame
          id="primary-test"
          isMobile
          mobileOpen
          onClose={vi.fn()}
          triggerRef={{ current: primaryTrigger! }}
        >
          <button data-primary-sidebar-initial-focus>Close</button>
          <a href="/sessions">Sessions</a>
        </AppModule.PrimarySidebarFrame>,
      ),
    );

    const close = document.querySelector<HTMLButtonElement>(
      "[data-primary-sidebar-initial-focus]",
    );
    const link = document.querySelector<HTMLAnchorElement>(
      "#primary-test a[href]",
    );
    link?.focus();
    await act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Tab", bubbles: true }),
      );
    });
    expect(document.activeElement).toBe(close);
  });

  it("closes open mobile navigation on Escape", async () => {
    const onClose = vi.fn();
    primaryTrigger = document.createElement("button");
    document.body.appendChild(primaryTrigger);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(() =>
      root.render(
        <AppModule.PrimarySidebarFrame
          isMobile
          mobileOpen
          onClose={onClose}
          triggerRef={{ current: primaryTrigger! }}
        >
          <button data-primary-sidebar-initial-focus>Close</button>
        </AppModule.PrimarySidebarFrame>,
      ),
    );

    await act(() => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true }),
      );
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("restores focus to the primary navigation trigger after closing", async () => {
    primaryTrigger = document.createElement("span");
    const triggerButton = document.createElement("button");
    primaryTrigger.appendChild(triggerButton);
    document.body.appendChild(primaryTrigger);
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    const render = (mobileOpen: boolean) =>
      root.render(
        <AppModule.PrimarySidebarFrame
          isMobile
          mobileOpen={mobileOpen}
          onClose={vi.fn()}
          triggerRef={{ current: primaryTrigger! }}
        >
          <button data-primary-sidebar-initial-focus>Close</button>
        </AppModule.PrimarySidebarFrame>,
      );

    await act(() => render(true));
    expect(document.activeElement).not.toBe(triggerButton);
    await act(() => render(false));
    expect(document.activeElement).toBe(triggerButton);
  });
});

describe("context shell breakpoints", () => {
  it("keeps the context trigger and backdrop available until the xl rail", () => {
    expect(
      (AppModule as unknown as { CONTEXT_SHELL_BREAKPOINTS?: unknown })
        .CONTEXT_SHELL_BREAKPOINTS,
    ).toEqual({
      headerHidden: "xl:hidden",
      mainPaddingReset: "xl:pt-0",
      contextBackdropHidden: "xl:hidden",
    });
  });
});
