import { describe, expect, it } from "vitest";
import type { DashboardPage } from "./api";
import { resolveDashboardContext } from "./dashboard-pages";

const pages: DashboardPage[] = [
  {
    id: "sessions",
    label: "Sessions",
    path: "/sessions",
    group: "workspace",
    description: "Conversations",
  },
  {
    id: "chat",
    label: "Chat",
    path: "/chat",
    group: "workspace",
    description: "Live chat",
  },
  {
    id: "channels",
    label: "Channels",
    path: "/channels",
    group: "integrations",
    description: "Messaging",
  },
  {
    id: "mcp",
    label: "MCP servers",
    path: "/mcp",
    group: "integrations",
    description: "Tools",
  },
];

describe("resolveDashboardContext", () => {
  it("returns sibling pages for the active section", () => {
    const context = resolveDashboardContext(pages, "/mcp");

    expect(context?.group).toBe("integrations");
    expect(context?.active.id).toBe("mcp");
    expect(context?.pages.map((page) => page.id)).toEqual(["channels", "mcp"]);
  });

  it("uses the longest parent route for nested pages", () => {
    expect(resolveDashboardContext(pages, "/mcp/catalog")?.active.id).toBe("mcp");
  });

  it("suppresses the shell rail for chat and unknown pages", () => {
    expect(resolveDashboardContext(pages, "/chat")).toBeNull();
    expect(resolveDashboardContext(pages, "/plugin-only")).toBeNull();
  });
});
