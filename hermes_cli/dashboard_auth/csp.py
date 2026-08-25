"""Content-Security-Policy header builders for dashboard HTML pages.

The dashboard SPA is the primary XSS surface in the loopback web server: any
script injection there runs with the session token in scope and the managed
files API a fetch away. A strict ``script-src`` (the directive that actually
kills XSS) is therefore the core of the policy. Details, per surface:

* **SPA** (``web_server._serve_index``) — the Vite bundle is self-hosted
  (``'self'``); the session-token bootstrap is an inline ``<script>`` that
  carries a per-response nonce. ``style-src 'unsafe-inline'`` is required by
  React inline style attributes and the theme flash-mitigation block; Google
  Fonts stylesheets/woff2s are loaded by built-in and user themes.
* **Login page** (``dashboard_auth.routes``) — server-rendered, pre-auth.
  Inline ``<style>`` + the optional password-form ``<script>`` both carry the
  nonce, so styles can be nonce-gated (no ``unsafe-inline`` needed here).
* **Tiny MCP OAuth status pages** — static, no inline anything:
  ``default-src 'none'``.

``frame-ancestors 'none'`` on every page stops clickjacking of the dashboard
and complements the DNS-rebinding Host check.
"""


def build_spa_csp(nonce: str) -> str:
    """CSP for the React dashboard SPA (``_serve_index``)."""
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        # React inline style attributes + theme bootstrap <style> need
        # 'unsafe-inline'; Google Fonts stylesheets load from googleapis.
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.bunny.net; "
        "font-src 'self' data: https://fonts.gstatic.com https://fonts.bunny.net; "
        "img-src 'self' data: blob:; "
        # Same-origin REST + WebSockets (/api/pty, /api/ws). 'self' covers
        # same-host ws:// in CSP3 browsers.
        "connect-src 'self'; "
        # Docs tab embeds the public docs site in an iframe.
        "frame-src https://hermes-agent.nousresearch.com; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


def build_login_csp(nonce: str) -> str:
    """CSP for the server-rendered pre-auth login page."""
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )


def build_none_csp() -> str:
    """CSP for static status pages that contain no inline or external content."""
    return "default-src 'none'; frame-ancestors 'none'"
