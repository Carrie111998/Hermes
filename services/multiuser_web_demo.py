"""Authenticated multi-user demo site for Hermes API-server ACL testing."""

from __future__ import annotations

import hmac
import os
import secrets
import time
from urllib.parse import parse_qs
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles


API_BASE = os.getenv("ULTRA_DEMO_API_BASE", "http://127.0.0.1:9120").rstrip("/")
API_KEY = os.getenv("ULTRA_DEMO_API_KEY", "dev-multiuser-test-key")
COOKIE_NAME = "ultra_demo_sid"
COOKIE_MAX_AGE = 60 * 60 * 8
PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class DemoUser:
    username: str
    password: str
    display_name: str
    tenant_id: str
    workspace_id: str
    project_id: str
    user_id: str
    role: str = "creator"


USERS: dict[str, DemoUser] = {
    "alice": DemoUser(
        username="alice",
        password=os.getenv("ULTRA_DEMO_ALICE_PASSWORD", "alice123"),
        display_name="Alice / Brand Studio",
        tenant_id="tenant-demo",
        workspace_id="workspace-brand",
        project_id="project-ultra",
        user_id="user-alice",
    ),
    "bob": DemoUser(
        username="bob",
        password=os.getenv("ULTRA_DEMO_BOB_PASSWORD", "bob123"),
        display_name="Bob / Video Studio",
        tenant_id="tenant-demo",
        workspace_id="workspace-video",
        project_id="project-ultra",
        user_id="user-bob",
    ),
}


class LoginSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, tuple[str, float]] = {}

    def create(self, username: str) -> str:
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = (username, time.time() + COOKIE_MAX_AGE)
        return sid

    def get_user(self, sid: str | None) -> DemoUser | None:
        if not sid:
            return None
        row = self._sessions.get(sid)
        if not row:
            return None
        username, expires_at = row
        if expires_at < time.time():
            self._sessions.pop(sid, None)
            return None
        return USERS.get(username)

    def delete(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(sid, None)


session_store = LoginSessionStore()
app = FastAPI(title="Ultra Studio Login Demo")

static_dir = PROJECT_ROOT / "standalone-chat-panel" / "public"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def _principal_headers(user: DemoUser) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-Hermes-Tenant-Id": user.tenant_id,
        "X-Hermes-Workspace-Id": user.workspace_id,
        "X-Hermes-Project-Id": user.project_id,
        "X-Hermes-User-Id": user.user_id,
        "X-Hermes-Roles": user.role,
    }


def _current_user(request: Request) -> DemoUser | None:
    return session_store.get_user(request.cookies.get(COOKIE_NAME))


def _require_user(request: Request) -> DemoUser | JSONResponse:
    user = _current_user(request)
    if user is None:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    return user


async def _call_hermes(
    user: DemoUser,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    timeout_s: float = 120.0,
) -> JSONResponse:
    url = f"{API_BASE}{path}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
        try:
            resp = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=_principal_headers(user),
            )
        except httpx.RequestError as exc:
            return JSONResponse(
                {"error": "hermes_api_unreachable", "detail": str(exc), "api_base": API_BASE},
                status_code=502,
            )
    try:
        payload = resp.json()
    except ValueError:
        payload = {"text": resp.text}
    return JSONResponse(payload, status_code=resp.status_code)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    user = _current_user(request)
    return HTMLResponse(APP_HTML if user else _login_html())


@app.post("/login")
async def login(request: Request) -> Response:
    form = parse_qs((await request.body()).decode("utf-8"))
    username = form.get("username", [""])[0]
    password = form.get("password", [""])[0]
    user = USERS.get(username)
    if user is None or not hmac.compare_digest(password, user.password):
        return HTMLResponse(_login_html("用户名或密码不正确"), status_code=401)
    sid = session_store.create(user.username)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        sid,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.post("/logout")
async def logout(request: Request) -> Response:
    session_store.delete(request.cookies.get(COOKIE_NAME))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/app/api/me")
async def me(request: Request) -> JSONResponse:
    user = _require_user(request)
    if isinstance(user, JSONResponse):
        return user
    return JSONResponse({
        "username": user.username,
        "display_name": user.display_name,
        "tenant_id": user.tenant_id,
        "workspace_id": user.workspace_id,
        "project_id": user.project_id,
        "user_id": user.user_id,
    })


@app.get("/app/api/sessions")
async def list_sessions(request: Request, limit: int = 50, offset: int = 0) -> JSONResponse:
    user = _require_user(request)
    if isinstance(user, JSONResponse):
        return user
    return await _call_hermes(
        user,
        "GET",
        "/api/sessions",
        params={"limit": str(limit), "offset": str(offset)},
        timeout_s=20.0,
    )


@app.post("/app/api/sessions")
async def create_session(request: Request) -> JSONResponse:
    user = _require_user(request)
    if isinstance(user, JSONResponse):
        return user
    body = await request.json()
    return await _call_hermes(user, "POST", "/api/sessions", json_body=body, timeout_s=20.0)


@app.get("/app/api/sessions/{session_id}")
async def get_session(request: Request, session_id: str) -> JSONResponse:
    user = _require_user(request)
    if isinstance(user, JSONResponse):
        return user
    return await _call_hermes(user, "GET", f"/api/sessions/{session_id}", timeout_s=20.0)


@app.get("/app/api/sessions/{session_id}/messages")
async def get_messages(request: Request, session_id: str) -> JSONResponse:
    user = _require_user(request)
    if isinstance(user, JSONResponse):
        return user
    return await _call_hermes(user, "GET", f"/api/sessions/{session_id}/messages", timeout_s=20.0)


@app.post("/app/api/sessions/{session_id}/chat")
async def session_chat(request: Request, session_id: str) -> JSONResponse:
    user = _require_user(request)
    if isinstance(user, JSONResponse):
        return user
    body = await request.json()
    return await _call_hermes(
        user,
        "POST",
        f"/api/sessions/{session_id}/chat",
        json_body=body,
        timeout_s=180.0,
    )


def _login_html(error: str = "") -> str:
    return LOGIN_HTML_TEMPLATE.replace("__ERROR__", error)


LOGIN_HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ultra Studio Login</title>
  <style>
    :root { color-scheme: dark; font-family: ui-serif, Georgia, Cambria, serif; }
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #11130f; color: #f5f0e7; }
    body:before { content: ""; position: fixed; inset: 0; background: radial-gradient(circle at 20% 15%, rgba(194,255,43,.12), transparent 28%), linear-gradient(90deg, rgba(255,255,255,.045) 1px, transparent 1px), linear-gradient(0deg, rgba(255,255,255,.035) 1px, transparent 1px); background-size: auto, 32px 32px, 32px 32px; pointer-events: none; }
    main { width: min(920px, calc(100vw - 36px)); display: grid; grid-template-columns: 1fr 420px; gap: 34px; align-items: center; position: relative; }
    .brand { display: flex; align-items: center; gap: 18px; margin-bottom: 34px; }
    .brand img { width: 72px; height: 72px; border-radius: 18px; box-shadow: 0 22px 80px rgba(0,0,0,.35); }
    h1 { font-size: clamp(42px, 7vw, 86px); line-height: .92; margin: 0; letter-spacing: 0; }
    h1 span { color: #c7ff2e; }
    p { max-width: 560px; color: #b7b0a5; font-size: 18px; line-height: 1.6; }
    form { background: #1d1f1b; border: 1px solid #41443b; border-radius: 8px; padding: 24px; box-shadow: 0 28px 100px rgba(0,0,0,.3); }
    label { display: block; color: #9d998f; font: 12px ui-monospace, Menlo, monospace; margin: 14px 0 7px; }
    input, button { box-sizing: border-box; width: 100%; border-radius: 6px; border: 1px solid #4b4d45; background: #11130f; color: #f5f0e7; padding: 13px 14px; font: 16px ui-monospace, Menlo, monospace; }
    button { margin-top: 18px; border: 0; color: #11130f; background: #c7ff2e; font-weight: 800; cursor: pointer; }
    .hint { font: 13px ui-monospace, Menlo, monospace; color: #a9a49a; border-top: 1px solid #363832; margin-top: 18px; padding-top: 16px; }
    .error { color: #ff8f8f; min-height: 18px; font: 13px ui-monospace, Menlo, monospace; }
    @media (max-width: 820px) { main { grid-template-columns: 1fr; padding: 24px 0; } }
  </style>
</head>
<body>
  <main>
    <section>
      <div class="brand"><img src="/static/atlas-avatar.png" alt=""><div>Ultra Studio</div></div>
      <h1><span>Bringing</span><br>it to life</h1>
      <p>登录后，服务端会把你的 workspace 身份绑定到 Hermes agent turn。浏览器只拿到会话 cookie，不接触底层 principal headers。</p>
    </section>
    <form method="post" action="/login">
      <div class="error">__ERROR__</div>
      <label>用户名</label>
      <input name="username" autocomplete="username" autofocus placeholder="alice 或 bob">
      <label>密码</label>
      <input name="password" type="password" autocomplete="current-password" placeholder="alice123 或 bob123">
      <button type="submit">登录</button>
      <div class="hint">测试账号：alice / alice123，bob / bob123。建议用普通窗口和无痕窗口分别登录，验证历史隔离。</div>
    </form>
  </main>
</body>
</html>"""


APP_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ultra Studio</title>
  <style>
    :root { color-scheme: dark; --bg:#10120f; --panel:#1b1d19; --line:#3c4038; --text:#f4f0e8; --muted:#a8a299; --accent:#c7ff2e; --cyan:#8bdde3; font-family: ui-serif, Georgia, Cambria, serif; }
    * { box-sizing: border-box; }
    body { margin: 0; height: 100vh; overflow: hidden; background: var(--bg); color: var(--text); }
    body:before { content:""; position: fixed; inset: 0; background: linear-gradient(90deg, rgba(255,255,255,.04) 1px, transparent 1px), linear-gradient(0deg, rgba(255,255,255,.035) 1px, transparent 1px); background-size: 30px 30px; pointer-events: none; }
    #app { position: relative; display: grid; grid-template-columns: 300px 1fr; height: 100vh; }
    aside { border-right: 1px solid var(--line); background: rgba(27,29,25,.94); padding: 18px; overflow: auto; }
    .brand { display: grid; grid-template-columns: 54px 1fr; gap: 12px; align-items: center; margin-bottom: 20px; }
    .brand img { width: 54px; height: 54px; border-radius: 14px; }
    .brand strong { display: block; font-size: 20px; line-height: 1; }
    .brand strong span { color: var(--accent); }
    .brand small, .meta, label { color: var(--muted); font: 12px ui-monospace, Menlo, monospace; }
    button, input, textarea { font: 14px ui-monospace, Menlo, monospace; }
    button { border: 1px solid var(--line); background: #252820; color: var(--text); border-radius: 6px; padding: 10px 12px; cursor: pointer; }
    button.primary { background: var(--accent); color: #11130f; border: 0; font-weight: 800; }
    .logout { width: 100%; margin: 12px 0 18px; }
    .session-list { display: grid; gap: 8px; margin-top: 12px; }
    .session { text-align: left; width: 100%; min-height: 62px; }
    .session.active { border-color: var(--accent); background: rgba(199,255,46,.1); }
    main { display: grid; grid-template-rows: auto 1fr auto; min-width: 0; }
    header { border-bottom: 1px solid var(--line); padding: 22px 28px; display: flex; justify-content: space-between; gap: 20px; align-items: center; }
    h1 { margin: 0; font-size: 34px; line-height: 1; }
    h1 span { color: var(--accent); }
    #messages { overflow: auto; padding: 28px; display: flex; flex-direction: column; gap: 14px; }
    .empty { margin: auto; color: var(--muted); font: 17px ui-monospace, Menlo, monospace; }
    .msg { max-width: min(760px, 82%); border: 1px solid var(--line); border-radius: 8px; padding: 14px 16px; white-space: pre-wrap; line-height: 1.55; background: rgba(27,29,25,.88); }
    .msg.user { align-self: flex-end; border-color: rgba(139,221,227,.45); background: rgba(139,221,227,.1); }
    .msg.assistant { align-self: flex-start; border-color: rgba(199,255,46,.25); }
    .composer { border-top: 1px solid var(--line); padding: 16px 28px 22px; display: grid; grid-template-columns: 1fr 96px; gap: 12px; align-items: end; }
    textarea { width: 100%; min-height: 74px; resize: vertical; border: 1px solid var(--line); border-radius: 8px; background: #171915; color: var(--text); padding: 14px; }
    .toast { color: #ffb1b1; font: 13px ui-monospace, Menlo, monospace; margin-top: 10px; min-height: 18px; }
    @media (max-width: 860px) { #app { grid-template-columns: 1fr; } aside { display: none; } }
  </style>
</head>
<body>
  <div id="app">
    <aside>
      <div class="brand"><img src="/static/atlas-avatar.png" alt=""><div><strong><span>Ultra</span> Studio</strong><small id="who">Loading</small></div></div>
      <form method="post" action="/logout"><button class="logout" type="submit">退出登录</button></form>
      <button id="newSession" class="primary">+ New Session</button>
      <div class="meta" style="margin-top:18px">SESSION HISTORY</div>
      <div id="sessions" class="session-list"></div>
    </aside>
    <main>
      <header>
        <div><div class="meta">AUTHENTICATED AGENT</div><h1><span>Creative</span> Session</h1></div>
        <button id="refresh">Refresh</button>
      </header>
      <section id="messages"><div class="empty">登录态已建立。新建或选择一个 session 开始。</div></section>
      <section>
        <div class="composer">
          <textarea id="prompt" placeholder="输入消息。这里会调用真实 Hermes session chat API。"></textarea>
          <button id="send" class="primary">Send</button>
        </div>
        <div id="error" class="toast"></div>
      </section>
    </main>
  </div>
  <script>
    const state = { me: null, sessions: [], active: null, messages: [] };
    const $ = (id) => document.getElementById(id);
    async function api(path, options = {}) {
      const res = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
      const text = await res.text();
      let body = text;
      try { body = JSON.parse(text); } catch {}
      if (!res.ok) throw new Error(typeof body === "object" ? JSON.stringify(body) : body);
      return body;
    }
    function showError(message = "") { $("error").textContent = message; }
    function renderSessions() {
      $("who").textContent = state.me ? `${state.me.display_name} · ${state.me.user_id}` : "";
      $("sessions").replaceChildren(...state.sessions.map((item) => {
        const button = document.createElement("button");
        button.className = `session${state.active === item.id ? " active" : ""}`;
        button.onclick = () => selectSession(item.id);
        const title = document.createElement("div");
        title.textContent = item.title || item.id;
        const meta = document.createElement("div");
        meta.className = "meta";
        meta.textContent = `${item.id.slice(0, 18)} · ${item.message_count || 0} msgs`;
        button.append(title, meta);
        return button;
      }));
    }
    function renderMessages() {
      const root = $("messages");
      if (!state.active) {
        root.innerHTML = '<div class="empty">登录态已建立。新建或选择一个 session 开始。</div>';
        return;
      }
      if (!state.messages.length) {
        root.innerHTML = '<div class="empty">Session ready.</div>';
        return;
      }
      root.replaceChildren(...state.messages.map((msg) => {
        const div = document.createElement("div");
        div.className = `msg ${msg.role === "user" ? "user" : "assistant"}`;
        div.textContent = msg.content || "";
        return div;
      }));
      root.scrollTop = root.scrollHeight;
    }
    async function loadMe() { state.me = await api("/app/api/me"); renderSessions(); }
    async function refreshSessions() {
      const payload = await api("/app/api/sessions?limit=50");
      state.sessions = payload.data || [];
      renderSessions();
    }
    async function createSession() {
      showError("");
      const payload = await api("/app/api/sessions", {
        method: "POST",
        body: JSON.stringify({ title: `Studio session ${new Date().toLocaleTimeString()}` }),
      });
      const session = payload.session;
      state.active = session.id;
      state.messages = [];
      await refreshSessions();
      renderMessages();
    }
    async function selectSession(id) {
      showError("");
      state.active = id;
      const payload = await api(`/app/api/sessions/${encodeURIComponent(id)}/messages`);
      state.messages = (payload.data || []).map((m) => ({ role: m.role, content: m.content }));
      renderSessions();
      renderMessages();
    }
    async function send() {
      showError("");
      if (!state.active) await createSession();
      const text = $("prompt").value.trim();
      if (!text) return;
      $("prompt").value = "";
      state.messages.push({ role: "user", content: text });
      renderMessages();
      try {
        const payload = await api(`/app/api/sessions/${encodeURIComponent(state.active)}/chat`, {
          method: "POST",
          body: JSON.stringify({ message: text }),
        });
        state.messages.push(payload.message || { role: "assistant", content: "(empty)" });
        await refreshSessions();
      } catch (err) {
        showError(err.message || String(err));
      }
      renderMessages();
    }
    $("newSession").onclick = createSession;
    $("refresh").onclick = refreshSessions;
    $("send").onclick = send;
    $("prompt").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) send();
    });
    loadMe().then(refreshSessions).catch((err) => showError(err.message || String(err)));
  </script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("ULTRA_DEMO_PORT", "9140")))
