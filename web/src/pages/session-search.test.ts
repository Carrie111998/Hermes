import { describe, expect, it } from "vitest";

import type { SessionInfo, SessionSearchResult } from "@/lib/api";
import { sessionsForSearchResults } from "./session-search";

const archivedSearchHit: SessionSearchResult = {
  session_id: "20260722_185815_27a418",
  snippet: "Automação CRM Bonus → WhatsApp",
  role: "assistant",
  source: "tui",
  model: "deepseek/deepseek-v4-pro",
  session_started: 1784746695,
};

describe("sessionsForSearchResults", () => {
  it("renders a matching session even when it is outside the loaded page", () => {
    const currentPage: SessionInfo[] = [];

    expect(sessionsForSearchResults(currentPage, [archivedSearchHit])).toEqual([
      {
        id: "20260722_185815_27a418",
        source: "tui",
        model: "deepseek/deepseek-v4-pro",
        title: null,
        started_at: 1784746695,
        ended_at: null,
        last_active: 1784746695,
        is_active: false,
        message_count: 0,
        tool_call_count: 0,
        input_tokens: 0,
        output_tokens: 0,
        preview: "Automação CRM Bonus → WhatsApp",
      },
    ]);
  });
});
