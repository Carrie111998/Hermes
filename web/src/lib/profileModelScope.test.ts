import { describe, expect, it } from "vitest";
import { describeProfileModelSave } from "./profileModelScope";

describe("describeProfileModelSave", () => {
  const copy = {
    savedForNewSessions: "Default model updated for new sessions",
    existingSessionsUnchanged:
      "Existing sessions keep their current model and any /model override.",
    scopeUnconfirmed: "Default model saved; affected session scope unconfirmed",
  };

  it("states both the saved default and unchanged live-session scope", () => {
    expect(
      describeProfileModelSave(
        "gpt-5.6-luna",
        { applies_to: "new_sessions" },
        copy,
      ),
    ).toBe(
      "Default model updated for new sessions: gpt-5.6-luna. Existing sessions keep their current model and any /model override.",
    );
  });

  it("fails closed when an older server omits the scope", () => {
    expect(describeProfileModelSave("gpt-5.6-luna", {}, copy)).toBe(
      "Default model saved; affected session scope unconfirmed: gpt-5.6-luna",
    );
  });
});
