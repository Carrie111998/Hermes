// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/i18n";

const mocks = vi.hoisted(() => ({
  getSkills: vi.fn(),
  getToolsets: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getSkills: mocks.getSkills,
    getToolsets: mocks.getToolsets,
    toggleSkill: vi.fn(),
  },
}));
vi.mock("react-router", () => ({ useNavigate: () => vi.fn() }));
vi.mock("@/contexts/useProfileScope", () => ({
  useProfileScope: () => ({ profile: "" }),
}));
vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setAfterTitle: vi.fn(), setEnd: vi.fn() }),
}));
vi.mock("@/components/ToolsetConfigDrawer", () => ({ ToolsetConfigDrawer: () => null }));
vi.mock("@/plugins", () => ({ PluginSlot: () => null }));

import SkillsPage from "./SkillsPage";

describe("SkillsPage Polish active controls", () => {
  beforeEach(() => {
    localStorage.setItem("hermes-locale", "pl");
    mocks.getSkills.mockResolvedValue([
      { name: "test-skill", description: "Test", category: "test", enabled: true },
    ]);
    mocks.getToolsets.mockResolvedValue([]);
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("renders the page actions and learn dialog entirely from the Polish catalog", async () => {
    render(
      <I18nProvider>
        <SkillsPage />
      </I18nProvider>,
    );

    expect(await screen.findByText("Przeglądaj centrum")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Naucz umiejętności" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Nowa umiejętność" })).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Naucz umiejętności" }));

    expect(screen.getByRole("dialog")).not.toBeNull();
    expect(screen.getByText("Lokalny plik lub katalog")).not.toBeNull();
    expect(screen.getAllByRole("textbox")[0].getAttribute("placeholder")).toBe(
      "~/projekty/jakis-sdk  (odczyt przez read_file / search_files)",
    );
    expect(screen.getByText("Inne informacje — opisz proces, wklej notatki lub napisz „to, co właśnie zrobiliśmy”")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Anuluj" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Naucz" })).not.toBeNull();

    expect(screen.queryByText("Browse hub")).toBeNull();
    expect(screen.queryByText("New skill")).toBeNull();
    expect(screen.queryByText("Local file or directory")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Anuluj" }));
    await vi.waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Nowa umiejętność" }));

    expect(screen.getByRole("dialog")).not.toBeNull();
    expect(screen.getByText("Nazwa")).not.toBeNull();
    expect(screen.getByText("Kategoria (opcjonalnie)")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Utwórz umiejętność" })).not.toBeNull();
    expect(screen.getByDisplayValue(/# Moja umiejętność/)).not.toBeNull();
    expect(screen.queryByText("Category (optional)")).toBeNull();
    expect(screen.queryByRole("button", { name: "Create skill" })).toBeNull();
  });
});
