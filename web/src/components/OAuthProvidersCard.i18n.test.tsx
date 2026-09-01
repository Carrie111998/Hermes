// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pl } from "@/i18n/pl";

const mocks = vi.hoisted(() => ({
  getOAuthProviders: vi.fn(),
  disconnectOAuthProvider: vi.fn(),
}));

vi.mock("@/i18n", () => ({ useI18n: () => ({ t: pl }) }));
vi.mock("@/lib/api", () => ({
  api: {
    getOAuthProviders: mocks.getOAuthProviders,
    disconnectOAuthProvider: mocks.disconnectOAuthProvider,
  },
}));
vi.mock("@/components/OAuthLoginModal", () => ({ OAuthLoginModal: () => null }));
vi.mock("@nous-research/ui/ui/components/confirm-dialog", () => ({
  ConfirmDialog: ({
    open,
    onConfirm,
    title,
    description,
  }: {
    open: boolean;
    onConfirm: () => void;
    title: string;
    description: string;
  }) =>
    open ? (
      <div role="dialog">
        <h2>{title}</h2>
        <p>{description}</p>
        <button onClick={onConfirm}>Potwierdź rozłączenie</button>
      </div>
    ) : null,
}));

import { OAuthProvidersCard } from "./OAuthProvidersCard";

const provider = {
  id: "openai",
  name: "OpenAI",
  flow: "pkce" as const,
  cli_command: "hermes login openai",
  docs_url: "https://platform.openai.com/docs",
  status: { logged_in: true },
};

async function disconnect() {
  fireEvent.click(await screen.findByRole("button", { name: "Rozłącz" }));
  fireEvent.click(screen.getByRole("button", { name: "Potwierdź rozłączenie" }));
}

describe("OAuthProvidersCard Polish disconnect feedback", () => {
  beforeEach(() => {
    mocks.getOAuthProviders.mockResolvedValue({ providers: [provider] });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("uses a locale-owned complete success message", async () => {
    mocks.disconnectOAuthProvider.mockResolvedValue(undefined);
    const onSuccess = vi.fn();
    render(<OAuthProvidersCard onSuccess={onSuccess} />);

    await disconnect();

    await vi.waitFor(() => {
      expect(onSuccess).toHaveBeenCalledWith("Rozłączono dostawcę OpenAI");
    });
    expect(onSuccess).not.toHaveBeenCalledWith(expect.stringContaining("Rozłączed"));
  });

  it("renders the exact Polish docs tooltip and disconnect dialog copy", async () => {
    render(<OAuthProvidersCard />);

    expect(
      (await screen.findByTitle("Otwórz dokumentację OpenAI")).getAttribute("href"),
    ).toBe("https://platform.openai.com/docs");

    fireEvent.click(screen.getByRole("button", { name: "Rozłącz" }));

    expect(screen.getByRole("dialog")).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Rozłączyć dostawcę OpenAI?" })).not.toBeNull();
    expect(
      screen.getByText(
        "Zapisane tokeny OAuth dostawcy OpenAI zostaną usunięte. Aby ponownie z niego korzystać, trzeba będzie się uwierzytelnić.",
      ),
    ).not.toBeNull();
    expect(screen.queryByText(/This will remove/)).toBeNull();
  });

  it("uses a locale-owned complete error message", async () => {
    mocks.disconnectOAuthProvider.mockRejectedValue(new Error("brak połączenia"));
    const onError = vi.fn();
    render(<OAuthProvidersCard onError={onError} />);

    await disconnect();

    await vi.waitFor(() => {
      expect(onError).toHaveBeenCalledWith(
        "Nie udało się rozłączyć dostawcy: Error: brak połączenia",
      );
    });
    expect(onError).not.toHaveBeenCalledWith(expect.stringContaining("failed"));
  });

  it("uses the locale-owned provider load error callback", async () => {
    mocks.getOAuthProviders.mockRejectedValueOnce(new Error("serwer niedostępny"));
    const onError = vi.fn();

    render(<OAuthProvidersCard onError={onError} />);

    await vi.waitFor(() => {
      expect(onError).toHaveBeenCalledWith(
        "Nie udało się wczytać dostawców: Error: serwer niedostępny",
      );
    });
    expect(onError).not.toHaveBeenCalledWith(
      expect.stringContaining("Failed to load providers"),
    );
  });
});
