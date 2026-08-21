"""Regression for #91495 - `hermes serve` 404'd the desktop's own renderer.

Electron spawns its local backend as `hermes serve` (never `dashboard`; see
``apps/desktop/electron/backend-command.ts``) with ``HERMES_DESKTOP=1``,
``HERMES_WEB_DIST=<packaged dist>`` and ``HERMES_DASHBOARD_SESSION_TOKEN``.

``cmd_dashboard``'s env sanitizer already carves the desktop out by name when
it strips Electron-packaged ``HERMES_WEB_DIST`` from inherited environments:

    # The desktop-spawned backend itself (HERMES_DESKTOP=1) keeps its dist.

and then, a couple hundred lines later, forced ``HERMES_SERVE_HEADLESS=1`` for
every ``serve`` invocation - which makes ``mount_spa()`` 404 every frontend
route, including the dist that carve-out just preserved. The two rules
contradicted each other and the desktop shell could not load its renderer.

The exemption is deliberately narrower than "the desktop spawned it": it also
requires a ``HERMES_WEB_DIST`` to serve, because the desktop spawns REMOTE
backends over SSH as ``env HERMES_DESKTOP=1 hermes serve --isolated`` with no
dist (``apps/desktop/electron/remote-lifecycle.ts``). Those must stay headless.

These tests pin the predicate, not a booted server: the bug was a decision, and
a decision is cheaper and more honestly tested in isolation.
"""

from __future__ import annotations

import hermes_cli.main as main_mod


# What apps/desktop/electron/main.ts hands its LOCAL backend.
DESKTOP = {"HERMES_DESKTOP": "1", "HERMES_WEB_DIST": "/Applications/Hermes.app/dist"}
# What remote-lifecycle.ts hands a backend spawned on another host over SSH.
DESKTOP_REMOTE = {"HERMES_DESKTOP": "1"}
PLAIN: dict = {}


class TestServeHeadlessDecision:
    def test_standalone_serve_is_still_headless(self):
        """The whole point of `serve` survives: it is not a dashboard."""
        assert main_mod._serve_should_force_headless(True, PLAIN) is True

    def test_desktop_spawned_serve_keeps_its_spa(self):
        """THE regression (#91495).

        Before the fix this returned True, so the backend 404'd the packaged
        dist the desktop had just handed it via HERMES_WEB_DIST.
        """
        assert main_mod._serve_should_force_headless(True, DESKTOP) is False

    def test_dashboard_is_never_forced_headless(self):
        """`dashboard` is not a headless backend on any host."""
        assert main_mod._serve_should_force_headless(False, PLAIN) is False
        assert main_mod._serve_should_force_headless(False, DESKTOP) is False

    def test_desktop_spawned_remote_backend_stays_headless(self):
        """The SSH backend has no renderer to serve, and must not build one.

        ``remote-lifecycle.ts`` spawns ``env HERMES_DESKTOP=1 hermes serve
        --isolated --host 127.0.0.1 --port 0`` on the remote host. The renderer
        lives on the LOCAL machine; the remote is purely an API server. On a
        HERMES_DESKTOP-only exemption this would not just serve routes it has
        no dist for - it would fall through to the branch that runs
        ``_build_web_ui(..., fatal=True)``, i.e. an npm build on someone's
        server, fatal if it fails.
        """
        assert main_mod._serve_should_force_headless(True, DESKTOP_REMOTE) is True

    def test_an_empty_dist_is_not_a_dist(self):
        """``HERMES_WEB_DIST=""`` (or whitespace) carries nothing to serve."""
        for value in ("", "   "):
            assert (
                main_mod._serve_should_force_headless(
                    True, {"HERMES_DESKTOP": "1", "HERMES_WEB_DIST": value}
                )
                is True
            ), repr(value)

    def test_only_the_exact_marker_exempts(self):
        """HERMES_DESKTOP is a 1/unset marker, not a truthiness test.

        A stale or hand-set ``HERMES_DESKTOP=0`` (or ``true``) must not quietly
        turn a standalone `hermes serve` into an SPA host - that would be a
        surprising way to expose a browser UI on a server.
        """
        for value in ("0", "", "true", "yes", "2"):
            assert (
                main_mod._serve_should_force_headless(
                    True, {"HERMES_DESKTOP": value, "HERMES_WEB_DIST": "/d"}
                )
                is True
            ), value

    def test_defaults_to_the_process_environment(self, monkeypatch):
        """``env=None`` reads os.environ, which is how the call site uses it."""
        monkeypatch.delenv("HERMES_DESKTOP", raising=False)
        monkeypatch.delenv("HERMES_WEB_DIST", raising=False)
        assert main_mod._serve_should_force_headless(True) is True

        monkeypatch.setenv("HERMES_DESKTOP", "1")
        assert main_mod._serve_should_force_headless(True) is True

        monkeypatch.setenv("HERMES_WEB_DIST", "/opt/hermes/dist")
        assert main_mod._serve_should_force_headless(True) is False


class TestTheExemptionIsAuthoritative:
    """Not setting the flag is not the same as the flag not being set.

    The sanitizer's `os.environ.pop("HERMES_SERVE_HEADLESS", None)` earlier in
    cmd_dashboard is gated on `not _headless_backend`, so it never runs on the
    `serve` path. An inherited HERMES_SERVE_HEADLESS=1 therefore survives into
    mount_spa and 404s the dist even when the desktop is exempt, which makes a
    "just don't set it" exemption silently useless. Electron spawns its backend
    with `...process.env`, so anything exported in the user's shell is
    inherited. Raised by @Enough1122 on #84444.
    """

    def test_exempt_path_clears_an_inherited_flag(self):
        import inspect

        src = inspect.getsource(main_mod.cmd_dashboard)
        gate = src.index("_serve_should_force_headless(_headless_backend)")
        build = src.index('"HERMES_WEB_DIST" not in os.environ')
        between = src[gate:build]
        assert 'os.environ.pop("HERMES_SERVE_HEADLESS", None)' in between, (
            "the desktop-exempt branch must CLEAR an inherited "
            "HERMES_SERVE_HEADLESS, not merely decline to set it"
        )
        assert "elif _headless_backend:" in between

    def test_clearing_is_scoped_to_the_headless_backend(self):
        """`dashboard` must not be routed through the exempt branch.

        It has its own pop earlier (gated on `not _headless_backend`), and
        reaching this one would mean the build branch below stopped running
        for a plain `hermes dashboard`.
        """
        import inspect

        src = inspect.getsource(main_mod.cmd_dashboard)
        assert "elif _headless_backend:" in src
        assert src.index("elif _headless_backend:") < src.index(
            '"HERMES_WEB_DIST" not in os.environ'
        )


class TestCallSiteStillGatesTheEnvVar:
    def test_predicate_is_what_cmd_dashboard_consults(self):
        """Pin the wiring, so the predicate cannot be tested into a vacuum.

        A pure predicate nobody calls is the classic way a fix like this rots;
        assert the call site names it.
        """
        import inspect

        src = inspect.getsource(main_mod.cmd_dashboard)
        assert "_serve_should_force_headless(_headless_backend)" in src
        assert 'os.environ["HERMES_SERVE_HEADLESS"] = "1"' in src
