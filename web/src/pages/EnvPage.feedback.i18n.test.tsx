// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { EnvVarInfo } from "@/lib/api";
import { pl } from "@/i18n/pl";

const mocks = vi.hoisted(() => ({
  getEnvVars: vi.fn(),
  setEnvVar: vi.fn(),
  revealEnvVar: vi.fn(),
  showToast: vi.fn(),
  setAfterTitle: vi.fn(),
  afterTitle: null as ReactNode,
}));

vi.mock("@/i18n", () => ({ useI18n: () => ({ t: pl }) }));
vi.mock("@/lib/api", () => ({
  api: {
    getEnvVars: mocks.getEnvVars,
    setEnvVar: mocks.setEnvVar,
    revealEnvVar: mocks.revealEnvVar,
    deleteEnvVar: vi.fn(),
  },
}));
vi.mock("@nous-research/ui/hooks/use-toast", () => ({
  useToast: () => ({ toast: null, showToast: mocks.showToast }),
}));
vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({
    setAfterTitle: (node: ReactNode) => {
      mocks.afterTitle = node;
      mocks.setAfterTitle(node);
    },
  }),
}));
vi.mock("@/components/OAuthProvidersCard", () => ({ OAuthProvidersCard: () => null }));
vi.mock("@/plugins", () => ({ PluginSlot: () => null }));

import EnvPage from "./EnvPage";

function envInfo(category: string): EnvVarInfo {
  return {
    is_set: true,
    redacted_value: "test...cret",
    description: "Test key",
    url: null,
    category,
    is_password: true,
    tools: [],
    advanced: false,
  };
}

describe("EnvPage Polish feedback and section navigation", () => {
  beforeEach(() => {
    mocks.afterTitle = null;
    mocks.getEnvVars.mockResolvedValue({
      TEST_API_KEY: envInfo("provider"),
      TEST_TOOL_KEY: envInfo("tool"),
      TEST_SETTING: envInfo("setting"),
    });
    mocks.setEnvVar.mockResolvedValue(undefined);
    mocks.revealEnvVar.mockResolvedValue({ value: "secret" });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("renders Polish section labels and announces a complete key-saved message", async () => {
    render(<EnvPage />);

    fireEvent.click(await screen.findByRole("button", { name: /Inne/ }));
    expect(await screen.findByText("TEST_API_KEY")).not.toBeNull();
    expect(mocks.afterTitle).not.toBeNull();
    render(<>{mocks.afterTitle}</>);

    const nav = screen.getByRole("navigation", { name: "Przejdź do sekcji" });
    expect(nav.textContent).toContain("Dostawcy");
    expect(nav.textContent).toContain("Narzędzia");
    expect(nav.textContent).toContain("Ustawienia");

    fireEvent.click(screen.getAllByRole("button", { name: "Zastąp" })[0]);
    fireEvent.change(screen.getByPlaceholderText("Zastąp bieżącą wartość (test...cret)"), {
      target: { value: "new-secret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Zapisz" }));

    await vi.waitFor(() => {
      expect(mocks.setEnvVar).toHaveBeenCalledWith("TEST_API_KEY", "new-secret");
      expect(mocks.showToast).toHaveBeenCalledWith("Zapisano klucz TEST_API_KEY", "success");
    });
    expect(mocks.showToast).not.toHaveBeenCalledWith(expect.stringContaining("zapiszd"), "success");
  });
});
