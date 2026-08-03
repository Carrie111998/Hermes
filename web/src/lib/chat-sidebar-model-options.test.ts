import { afterEach, describe, expect, it, vi } from "vitest";

import { chatSidebarModelOptionsLoader } from "./chat-sidebar-model-options";
import { api } from "./api";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("chatSidebarModelOptionsLoader", () => {
  it("forwards the picker refresh request with its scoped profile", async () => {
    const getModelOptions = vi
      .spyOn(api, "getModelOptions")
      .mockResolvedValue({ providers: [] } as never);

    await chatSidebarModelOptionsLoader("developer")({ refresh: true });

    expect(getModelOptions).toHaveBeenCalledWith({
      profile: "developer",
      refresh: true,
    });
  });
});
