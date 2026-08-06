import { describe, expect, it } from "vitest";

import type { WorkspaceProject } from "@/lib/api";
import {
  primaryOnlineBinding,
  workspaceChatHref,
  workspaceResumeHref,
} from "./workspace-page-model";

const project: WorkspaceProject = {
  archived: false,
  bindings: [
    {
      binding_id: "b_offline",
      is_primary: true,
      label: "Laptop",
      runner_id: "r_1",
      status: "offline",
    },
    {
      binding_id: "b_live/value",
      is_primary: false,
      label: "Mac",
      runner_id: "r_2",
      status: "online",
    },
  ],
  color: null,
  conversations: [],
  created_at: 1,
  description: null,
  icon: null,
  id: "project-1",
  name: "Launch",
  slug: "launch",
};

describe("workspace page routing", () => {
  it("falls back from an offline primary binding to an online device", () => {
    expect(primaryOnlineBinding(project)?.binding_id).toBe("b_live/value");
    expect(workspaceChatHref(primaryOnlineBinding(project)!)).toBe(
      "/chat?binding=b_live%2Fvalue",
    );
  });

  it("encodes a conversation resume target", () => {
    expect(workspaceResumeHref("session/one")).toBe("/chat?resume=session%2Fone");
  });

  it("returns null when every runner binding is offline", () => {
    expect(
      primaryOnlineBinding({
        ...project,
        bindings: project.bindings.map((binding) => ({
          ...binding,
          status: "offline" as const,
        })),
      }),
    ).toBeNull();
  });
});
