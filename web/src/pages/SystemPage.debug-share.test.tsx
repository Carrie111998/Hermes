// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  runDebugShare: vi.fn(),
  showToast: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: {
    checkHermesUpdate: vi.fn(async () => ({ update_available: false })),
    getCheckpoints: vi.fn(async () => ({ sessions: [], total_bytes: 0 })),
    getCredentialPool: vi.fn(async () => ({ providers: [] })),
    getCurator: vi.fn(async () => null),
    getHooks: vi.fn(async () => ({ hooks: [], valid_events: [] })),
    getMemory: vi.fn(async () => null),
    getPortal: vi.fn(async () => null),
    getStatus: vi.fn(async () => ({ gateway_running: false })),
    getSystemStats: vi.fn(async () => null),
    runDebugShare: mocks.runDebugShare,
  },
}));
vi.mock("@nous-research/ui/hooks/use-toast", () => ({
  useToast: () => ({ showToast: mocks.showToast, toast: null }),
}));
vi.mock("@/lib/clipboard", () => ({ copyTextToClipboard: vi.fn() }));

import SystemPage from "./SystemPage";

let container: HTMLDivElement;
let root: Root;

async function renderPage() {
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter>
        <SystemPage />
      </MemoryRouter>,
    );
  });
  await act(async () => Promise.resolve());
}

function button(label: string): HTMLButtonElement {
  const match = Array.from(document.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === label,
  );
  if (!(match instanceof HTMLButtonElement)) throw new Error(`Missing button: ${label}`);
  return match;
}

beforeEach(() => {
  mocks.runDebugShare.mockReset();
  mocks.runDebugShare.mockResolvedValue({
    auto_delete_seconds: 21600,
    failures: [],
    redacted: true,
    urls: { Report: "https://paste.rs/example" },
  });
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  vi.clearAllMocks();
});

describe("SystemPage debug share consent", () => {
  it("does not request an upload when confirmation is cancelled", async () => {
    await renderPage();

    await act(async () => button("Generate share link").click());
    await act(async () => button("Cancel").click());

    expect(mocks.runDebugShare).not.toHaveBeenCalled();
  });

  it("requests exactly one consent-bearing upload after confirmation", async () => {
    await renderPage();

    await act(async () => button("Generate share link").click());
    await act(async () => button("Upload").click());

    expect(mocks.runDebugShare).toHaveBeenCalledTimes(1);
    expect(mocks.runDebugShare).toHaveBeenCalledWith();
    expect(document.body.textContent).not.toContain("Redact credential-shaped tokens");
  });
});
