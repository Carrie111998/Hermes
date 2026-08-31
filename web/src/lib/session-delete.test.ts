import { describe, expect, it } from "vitest";

import { sessionDeleteTarget } from "./session-delete";

describe("sessionDeleteTarget", () => {
  it("carries the owning profile into the confirmed delete", () => {
    expect(sessionDeleteTarget({ id: "session-1", profile: "worker" })).toEqual({
      id: "session-1",
      profile: "worker",
    });
  });
});
