// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/i18n";

const mocks = vi.hoisted(() => ({
  getSkills: vi.fn(),
  getToolsets: vi.fn(),
  getSkillHubSources: vi.fn(),
  searchSkillsHub: vi.fn(),
  installSkillFromHub: vi.fn(),
  updateSkillsFromHub: vi.fn(),
  getActionStatus: vi.fn(),
  previewSkillFromHub: vi.fn(),
  scanSkillFromHub: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    getSkills: mocks.getSkills,
    getToolsets: mocks.getToolsets,
    getSkillHubSources: mocks.getSkillHubSources,
    searchSkillsHub: mocks.searchSkillsHub,
    installSkillFromHub: mocks.installSkillFromHub,
    updateSkillsFromHub: mocks.updateSkillsFromHub,
    getActionStatus: mocks.getActionStatus,
    previewSkillFromHub: mocks.previewSkillFromHub,
    scanSkillFromHub: mocks.scanSkillFromHub,
    toggleSkill: vi.fn(),
  },
}));
vi.mock("@nous-research/ui/hooks/use-toast", () => ({
  useToast: () => ({ toast: null, showToast: mocks.showToast }),
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

const hubResult = {
  name: "audit-skill",
  description: "External skill description",
  source: "GitHub",
  identifier: "owner/repo/audit-skill",
  trust_level: "community",
  repo: "owner/repo",
  tags: ["security"],
};

function renderPolishPage() {
  render(
    <I18nProvider>
      <SkillsPage />
    </I18nProvider>,
  );
}

async function openHub() {
  fireEvent.click(await screen.findByText("Przeglądaj centrum"));
  await screen.findByPlaceholderText(
    "Szukaj w centrum umiejętności (GitHub, oficjalne, społecznościowe)…",
  );
}

describe("SkillsPage Polish active controls", () => {
  beforeEach(() => {
    localStorage.setItem("hermes-locale", "pl");
    mocks.getSkills.mockResolvedValue([
      { name: "test-skill", description: "Test", category: "test", enabled: true },
    ]);
    mocks.getToolsets.mockResolvedValue([
      {
        name: "web",
        label: "Web",
        description: "External toolset description",
        enabled: true,
        configured: true,
        tools: ["web_search"],
      },
    ]);
    mocks.getSkillHubSources.mockResolvedValue({
      sources: [
        { id: "github", label: "GitHub", rate_limited: true },
        { id: "hermes-index", label: "Hermes Index", available: false },
      ],
      index_available: false,
      featured: [hubResult],
      installed: {},
    });
    mocks.searchSkillsHub.mockResolvedValue({
      results: [hubResult],
      source_counts: { github: 1 },
      timed_out: ["skillsmp"],
      installed: {},
    });
    mocks.previewSkillFromHub.mockResolvedValue({
      ...hubResult,
      skill_md: "# External SKILL.md body",
      files: ["SKILL.md", "scripts/check.py"],
    });
    mocks.scanSkillFromHub.mockResolvedValue({
      name: hubResult.name,
      identifier: hubResult.identifier,
      source: hubResult.source,
      trust_level: "community",
      verdict: "dangerous",
      summary: "External scan summary",
      policy: "ask",
      policy_reason: "External policy reason",
      findings: [
        {
          severity: "high",
          category: "shell_injection",
          file: "scripts/check.py",
          line: 7,
          description: "External finding description",
        },
      ],
      severity_counts: { high: 1 },
    });
    mocks.installSkillFromHub.mockResolvedValue({ name: "install-action" });
    mocks.updateSkillsFromHub.mockResolvedValue({ name: "update-action" });
    mocks.getActionStatus.mockResolvedValue({ lines: [], running: true });
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
    vi.clearAllMocks();
  });

  it("renders page, configuration, edit accessibility, and learn controls from the Polish catalog", async () => {
    renderPolishPage();

    expect(await screen.findByText("Przeglądaj centrum")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Naucz umiejętności" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Nowa umiejętność" })).not.toBeNull();
    expect(
      screen
        .getByRole("button", { name: "Edytuj umiejętność: test-skill" })
        .getAttribute("title"),
    ).toBe("Edytuj SKILL.md");

    fireEvent.click(screen.getByText(/Zestawy narzędzi/));
    expect(await screen.findByRole("button", { name: "Konfiguruj" })).not.toBeNull();

    fireEvent.click(screen.getByText(/Wszystkie \(/));
    fireEvent.click(screen.getByRole("button", { name: "Naucz umiejętności" }));

    expect(screen.getByRole("dialog")).not.toBeNull();
    expect(screen.getByText("Lokalny plik lub katalog")).not.toBeNull();
    expect(screen.getAllByRole("textbox")[0].getAttribute("placeholder")).toBe(
      "~/projekty/jakis-sdk  (odczyt przez read_file / search_files)",
    );
    expect(
      screen.getByText(
        "Inne informacje — opisz proces, wklej notatki lub napisz „to, co właśnie zrobiliśmy”",
      ),
    ).not.toBeNull();
    expect(screen.queryByText("Browse hub")).toBeNull();
    expect(screen.queryByText("Configure")).toBeNull();
    expect(screen.queryByTitle("Edit SKILL.md")).toBeNull();
  });

  it("localizes known category labels while preserving the fallback for unknown external categories", async () => {
    mocks.getSkills.mockResolvedValue([
      { name: "code", description: "Test", category: "software-development", enabled: true },
      { name: "agents", description: "Test", category: "autonomous-ai-agents", enabled: true },
      { name: "notes", description: "Test", category: "note-taking", enabled: true },
      { name: "home", description: "Test", category: "smart-home", enabled: true },
      { name: "social", description: "Test", category: "social-media", enabled: true },
      { name: "cloud", description: "Test", category: "mlops/cloud", enabled: true },
      { name: "evaluation", description: "Test", category: "mlops/evaluation", enabled: true },
      { name: "vectors", description: "Test", category: "mlops/vector-databases", enabled: true },
      { name: "red", description: "Test", category: "red-teaming", enabled: true },
      { name: "external", description: "Test", category: "partner/custom-tools", enabled: true },
    ]);

    renderPolishPage();

    expect(await screen.findByText("Tworzenie oprogramowania")).not.toBeNull();
    expect(screen.getByText("Autonomiczne agenty AI")).not.toBeNull();
    expect(screen.getByText("Notatki")).not.toBeNull();
    expect(screen.getByText("Inteligentny dom")).not.toBeNull();
    expect(screen.getByText("Media społecznościowe")).not.toBeNull();
    expect(screen.getByText("MLOps / Chmura")).not.toBeNull();
    expect(screen.getByText("MLOps / Ewaluacja")).not.toBeNull();
    expect(screen.getByText("MLOps / Bazy wektorowe")).not.toBeNull();
    expect(screen.getByText("Red teaming")).not.toBeNull();
    expect(screen.getByText("Partner Custom Tools")).not.toBeNull();
    expect(screen.queryByText("MLOps / Cloud")).toBeNull();
    expect(screen.queryByText("MLOps / Evaluation")).toBeNull();
    expect(screen.queryByText("Software Development")).toBeNull();
    expect(screen.queryByText("Autonomous Ai Agents")).toBeNull();
  });

  it("localizes the hub landing, connected-source status, cards, and accessibility names", async () => {
    renderPolishPage();
    await openHub();

    expect(await screen.findByText("Polecane umiejętności")).not.toBeNull();
    expect(screen.getByText("Połączone centra:")).not.toBeNull();
    expect(screen.getByText("GitHub (osiągnięto limit)").getAttribute("title")).toBe(
      "Osiągnięto limit API GitHub — ustaw GITHUB_TOKEN, aby go zwiększyć",
    );
    expect(screen.getByText("Hermes Index").getAttribute("title")).toBe(
      "Centralny indeks jest niedostępny — używane są źródła pobierane na żywo",
    );
    expect(screen.getByText("społecznościowe")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Otwórz umiejętność audit-skill" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Szczegóły" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Zainstaluj" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "Aktualizuj wszystkie" })).not.toBeNull();
    expect(screen.queryByText("Featured skills")).toBeNull();
    expect(screen.queryByText("community")).toBeNull();
  });

  it("localizes search metadata, detail preview, and known security enums while preserving external values", async () => {
    renderPolishPage();
    await openHub();

    fireEvent.change(screen.getByPlaceholderText(/Szukaj w centrum umiejętności/), {
      target: { value: "audit" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Szukaj" }));

    expect(await screen.findByText("Liczba wyników: 1")).not.toBeNull();
    expect(screen.getByText("Przekroczono limit czasu: skillsmp")).not.toBeNull();
    expect(screen.getByText("External skill description")).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Otwórz umiejętność audit-skill" }));

    expect(await screen.findByText("# External SKILL.md body")).not.toBeNull();
    expect(screen.getByText("Pliki:")).not.toBeNull();
    expect(screen.getByText(/Wyświetl źródło SKILL.md.*audit-skill/)).not.toBeNull();
    expect(screen.getByRole("button", { name: "Czytaj SKILL.md" })).not.toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Skanowanie bezpieczeństwa" }));

    expect(await screen.findByText("Werdykt: Niebezpieczna")).not.toBeNull();
    expect(screen.getByText("Niebezpieczna")).not.toBeNull();
    expect(screen.getByText("Źródło: społecznościowe · liczba wykrytych problemów: 1")).not.toBeNull();
    expect(screen.getByText("Wymaga potwierdzenia")).not.toBeNull();
    expect(screen.getByText("wysokie: 1")).not.toBeNull();
    expect(screen.getByText("wysokie")).not.toBeNull();
    expect(screen.getByText("shell_injection")).not.toBeNull();
    expect(screen.getByText("External finding description")).not.toBeNull();
    expect(screen.getByText("External policy reason")).not.toBeNull();
    expect(screen.getByRole("button", { name: "Skanuj ponownie" })).not.toBeNull();
    expect(screen.queryByText("Verdict: Dangerous")).toBeNull();
  });

  it("uses Polish locale callbacks for hub errors and install/update action feedback", async () => {
    mocks.searchSkillsHub.mockRejectedValueOnce(new Error("brak sieci"));
    mocks.updateSkillsFromHub.mockRejectedValueOnce(new Error("brak dostępu"));
    renderPolishPage();
    await openHub();

    fireEvent.click(screen.getByRole("button", { name: "Zainstaluj" }));
    await vi.waitFor(() =>
      expect(mocks.showToast).toHaveBeenCalledWith(
        "Instalowanie owner/repo/audit-skill…",
        "success",
      ),
    );
    expect(await screen.findByText("w toku")).not.toBeNull();
    expect(screen.getByText("Uruchamianie…")).not.toBeNull();

    fireEvent.change(screen.getByPlaceholderText(/Szukaj w centrum umiejętności/), {
      target: { value: "audit" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Szukaj" }));
    await vi.waitFor(() =>
      expect(mocks.showToast).toHaveBeenCalledWith(
        "Wyszukiwanie w centrum nie powiodło się: Error: brak sieci",
        "error",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Aktualizuj wszystkie" }));
    await vi.waitFor(() =>
      expect(mocks.showToast).toHaveBeenCalledWith(
        "Aktualizacja nie powiodła się: Error: brak dostępu",
        "error",
      ),
    );

    expect(mocks.showToast).not.toHaveBeenCalledWith(expect.stringContaining("failed"), "error");
  });
});
