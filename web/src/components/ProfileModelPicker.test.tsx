import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ProfileModelPicker } from "./ProfileModelPicker";
import {
  filterProfileModelChoices,
  type ProfileModelChoice,
} from "@/lib/profile-model-picker";

const choices: ProfileModelChoice[] = [
  {
    provider: "nous",
    model: "deepseek-v4-pro",
    label: "Nous Portal · DeepSeek V4 Pro",
  },
  {
    provider: "openai-codex",
    model: "gpt-5.6-codex",
    label: "OpenAI Codex · GPT-5.6 Codex",
  },
];

describe("ProfileModelPicker", () => {
  it.each([
    ["NOUS", "deepseek-v4-pro"],
    ["V4-PRO", "deepseek-v4-pro"],
    ["deepseek v4 pro", "deepseek-v4-pro"],
    ["OPENAI-CODEX", "gpt-5.6-codex"],
  ])(
    "matches provider, model id, and rendered label case-insensitively",
    (query, model) => {
      expect(
        filterProfileModelChoices(choices, query).map((choice) => choice.model),
      ).toEqual([model]);
    },
  );

  it("renders every selectable row as a native button", () => {
    const html = renderToStaticMarkup(
      <ProfileModelPicker
        choices={choices}
        emptyLabel="No models"
        inheritLabel="Use default"
        loadingLabel="Loading models"
        selected={"nous\u0000deepseek-v4-pro"}
        onSelect={() => undefined}
      />,
    );

    expect(html).toContain('aria-label="Search models"');
    expect(html).toContain("max-h-80");
    expect(html.match(/<button/g)).toHaveLength(3);
    expect(html.match(/type="button"/g)).toHaveLength(3);
    expect(html).toContain('aria-pressed="true"');
  });
});
