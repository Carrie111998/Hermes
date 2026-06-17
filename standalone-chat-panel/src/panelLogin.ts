import {
  authBootstrap,
  authLogin,
  authMe,
  authSignup,
  authStatus,
  saveAuth,
  type PanelAuthState,
  type PanelAuthStatus,
} from "./panelAuthClient";

export async function resolvePanelAuth(existing: PanelAuthState | null): Promise<PanelAuthState> {
  if (existing) {
    try {
      const refreshed = await authMe(existing);
      saveAuth(refreshed);
      return refreshed;
    } catch {
      saveAuth(null);
    }
  }
  const status = await authStatus().catch(() => ({ configured: false }) as PanelAuthStatus);
  return showLogin(status);
}

function showLogin(status: PanelAuthStatus): Promise<PanelAuthState> {
  document.querySelector(".auth-overlay")?.remove();
  return new Promise((resolve) => {
    const bootstrap = Boolean(status.needs_bootstrap);
    const signup = Boolean(status.signup_enabled);
    let mode: "login" | "signup" | "bootstrap" = bootstrap ? "bootstrap" : "login";
    const wrap = document.createElement("div");
    wrap.className = "auth-overlay";
    const render = () => {
      const creating = mode === "signup" || mode === "bootstrap";
      wrap.innerHTML = `
        <form class="auth-card">
          <img src="/atlas-avatar.png" alt="">
          <h2><span>Bringing</span> it to life</h2>
          <p>${messageForStatus(status, mode)}</p>
          <label>Username</label>
          <input name="username" autocomplete="username" required>
          <label>Password</label>
          <input name="password" type="password" autocomplete="${creating ? "new-password" : "current-password"}" required>
          ${creating ? `
            <label>Display name</label>
            <input name="label" autocomplete="name">
            <label>Workspace</label>
            <input name="workspace" value="workspace-main">
          ` : ""}
          <button type="submit">${buttonText(mode)}</button>
          ${signup && !bootstrap ? `<button class="auth-secondary" type="button">${mode === "signup" ? "Back to login" : "Create account"}</button>` : ""}
          <small>${smallText(mode, signup)}</small>
        </form>`;
      const form = wrap.querySelector("form") as HTMLFormElement;
      const note = wrap.querySelector("small") as HTMLElement;
      wrap.querySelector(".auth-secondary")?.addEventListener("click", () => {
        mode = mode === "signup" ? "login" : "signup";
        render();
      });
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const data = new FormData(form);
        const username = String(data.get("username") || "").trim();
        const password = String(data.get("password") || "");
        const label = String(data.get("label") || "").trim();
        const workspace = String(data.get("workspace") || "").trim();
        const action = mode === "bootstrap"
          ? authBootstrap({ username, password, label, workspace })
          : mode === "signup"
            ? authSignup({ username, password, label, workspace })
            : authLogin(username, password);
        void action.then((auth) => {
          saveAuth(auth);
          wrap.remove();
          resolve(auth);
        }).catch((error) => {
          note.textContent = error instanceof Error ? error.message : "Authentication failed";
        });
      });
    };
    render();
    document.body.append(wrap);
  });
}

function messageForStatus(status: PanelAuthStatus, mode: "login" | "signup" | "bootstrap"): string {
  if (!status.configured) {
    return "API Server key is missing. Set API_SERVER_KEY or HERMES_API_SERVER_KEY, then start the API server.";
  }
  if (mode === "bootstrap") {
    return "Create the first account. Principal scope will be stored server-side in SQLite.";
  }
  if (mode === "signup") {
    return "Create a separate local account with its own Hermes workspace scope.";
  }
  return "Login to your isolated Hermes workspace.";
}

function buttonText(mode: "login" | "signup" | "bootstrap"): string {
  if (mode === "bootstrap") return "Create first account";
  if (mode === "signup") return "Create account";
  return "Login";
}

function smallText(mode: "login" | "signup" | "bootstrap", signupEnabled: boolean): string {
  if (mode === "bootstrap") return "SQLite auth store is empty. This creates the first real local account.";
  if (mode === "signup") return "New accounts receive creator role by default.";
  return signupEnabled ? "Use an existing account, or create a separate local account." : "Use an account from the SQLite auth store.";
}
