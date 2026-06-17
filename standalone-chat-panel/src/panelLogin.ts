import {
  authBootstrap,
  authLogin,
  authMe,
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
    const wrap = document.createElement("div");
    wrap.className = "auth-overlay";
    wrap.innerHTML = `
      <form class="auth-card">
        <img src="/atlas-avatar.png" alt="">
        <h2><span>Bringing</span> it to life</h2>
        <p>${messageForStatus(status, bootstrap)}</p>
        <label>Username</label>
        <input name="username" autocomplete="username" required>
        <label>Password</label>
        <input name="password" type="password" autocomplete="${bootstrap ? "new-password" : "current-password"}" required>
        ${bootstrap ? `
          <label>Display name</label>
          <input name="label" autocomplete="name">
          <label>Workspace</label>
          <input name="workspace" value="workspace-main">
        ` : ""}
        <button type="submit">${bootstrap ? "Create first account" : "Login"}</button>
        <small>${bootstrap ? "SQLite auth store is empty. This creates the first real local account." : "Use an account from the SQLite auth store."}</small>
      </form>`;
    const form = wrap.querySelector("form") as HTMLFormElement;
    const note = wrap.querySelector("small") as HTMLElement;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = new FormData(form);
      const username = String(data.get("username") || "").trim();
      const password = String(data.get("password") || "");
      const label = String(data.get("label") || "").trim();
      const workspace = String(data.get("workspace") || "").trim();
      const action = bootstrap
        ? authBootstrap({ username, password, label, workspace })
        : authLogin(username, password);
      void action.then((auth) => {
        saveAuth(auth);
        wrap.remove();
        resolve(auth);
      }).catch((error) => {
        note.textContent = error instanceof Error ? error.message : "Authentication failed";
      });
    });
    document.body.append(wrap);
  });
}

function messageForStatus(status: PanelAuthStatus, bootstrap: boolean): string {
  if (!status.configured) {
    return "API Server key is missing. Set API_SERVER_KEY or HERMES_API_SERVER_KEY, then start the API server.";
  }
  if (bootstrap) {
    return "Create the first account. Principal scope will be stored server-side in SQLite.";
  }
  return "Login to your isolated Hermes workspace.";
}
