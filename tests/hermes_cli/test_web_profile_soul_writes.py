"""``PUT /api/profiles/{name}/soul`` must not destroy an existing SOUL.md.

The dashboard persona editor replaces the whole document on every Save. A bare
``write_text()`` truncates SOUL.md before the new body lands, and the paired
``GET`` reports an unreadable file as ``{"content": "", "exists": False}`` — so
an interrupted save presents as "your persona was never set" and the editor's
next Save persists that empty document over the original.

Lives in its own module rather than ``test_web_server.py`` to keep the harness
small and focused on this one endpoint pair.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


SOUL = "# Persona\n\nYou are a careful, terse assistant.\n"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "soul-test-token")
    from hermes_cli import web_server

    # Pin the resolved token too: _SESSION_TOKEN is resolved at import time, so
    # a later module-level ``import hermes_cli.web_server`` in another test file
    # can freeze it to a different value before this fixture runs. Patching the
    # module attribute (rather than only the env var) makes these tests
    # order-independent within the process.
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "soul-test-token")

    with TestClient(web_server.app, raise_server_exceptions=False) as c:
        c.headers["Authorization"] = "Bearer soul-test-token"
        yield c


@pytest.fixture()
def profile_dir(tmp_path, monkeypatch) -> Path:
    """Create a real profile directory under the test HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli import profiles as profiles_mod

    d = profiles_mod.get_profile_dir("demo")
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestSoulWriteDurability:
    def test_put_replaces_soul(self, client, profile_dir: Path):
        """Happy path: the editor's Save still works."""
        (profile_dir / "SOUL.md").write_text(SOUL, encoding="utf-8")

        r = client.put("/api/profiles/demo/soul", json={"content": "# New\n"})

        assert r.status_code == 200, r.text
        assert (profile_dir / "SOUL.md").read_text(encoding="utf-8") == "# New\n"

    def test_put_creates_soul_when_absent(self, client, profile_dir: Path):
        """A first save has no prior file to preserve permissions from."""
        assert not (profile_dir / "SOUL.md").exists()

        r = client.put("/api/profiles/demo/soul", json={"content": SOUL})

        assert r.status_code == 200, r.text
        assert (profile_dir / "SOUL.md").read_text(encoding="utf-8") == SOUL

    def test_existing_soul_survives_an_interrupted_save(
        self, client, profile_dir: Path
    ):
        soul = profile_dir / "SOUL.md"
        soul.write_text(SOUL, encoding="utf-8")
        original = soul.read_bytes()

        def boom(fd):
            raise OSError("simulated crash mid-write")

        # Scoped context so restoring os.fsync doesn't also undo the
        # HERMES_HOME patch the client/profile_dir fixtures installed.
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "fsync", boom)
            r = client.put(
                "/api/profiles/demo/soul", json={"content": "# clobbered\n"}
            )

        assert r.status_code == 500
        # The persona the user already had must survive verbatim...
        assert soul.read_bytes() == original
        # ...and the paired GET must not report it as never-set, which is what
        # would make the next Save persist an empty document.
        g = client.get("/api/profiles/demo/soul")
        assert g.status_code == 200, g.text
        assert g.json()["exists"] is True
        assert g.json()["content"] == SOUL
        # No temp file left behind in the profile directory.
        assert list(profile_dir.glob("*.tmp")) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_existing_file_mode_is_preserved(self, client, profile_dir: Path):
        """Profile SOUL.md is created 0644 and never run through
        ``_secure_file``; saving from the dashboard must not change that."""
        soul = profile_dir / "SOUL.md"
        soul.write_text(SOUL, encoding="utf-8")
        os.chmod(soul, 0o644)

        r = client.put("/api/profiles/demo/soul", json={"content": "# New\n"})

        assert r.status_code == 200, r.text
        mode = stat.S_IMODE(soul.stat().st_mode)
        assert mode == 0o644, f"mode changed to {oct(mode)}"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_created_file_mode_is_not_tightened(self, client, profile_dir: Path):
        """The first-ever Save must not leave SOUL.md owner-only.

        There is no prior file to copy permissions from, and
        ``atomic_write_text`` swaps in a ``tempfile.mkstemp`` file (0600).
        Profile creation seeds SOUL.md with a plain ``write_text()`` and
        chmods only ``.env`` to 0600, so routing this endpoint through the
        atomic writer must not tighten the persona document as a side effect.
        """
        soul = profile_dir / "SOUL.md"
        assert not soul.exists()

        r = client.put("/api/profiles/demo/soul", json={"content": SOUL})

        assert r.status_code == 200, r.text
        mode = stat.S_IMODE(soul.stat().st_mode)
        assert mode == 0o644, f"first save created SOUL.md as {oct(mode)}"


class TestIsolatedProfileSoulScope:
    """An isolated (``--isolated``) dashboard scoped to one named profile must
    not read or write another profile's SOUL.md (#91330).

    The unified machine dashboard is intentionally a machine-wide management
    surface (cross-profile access is by design). But a server launched with
    ``--isolated`` from a named profile runs scoped to that profile, and
    letting it rewrite another profile's persona is a prompt-injection vector.
    """

    @pytest.fixture()
    def isolated_home(self, tmp_path, monkeypatch):
        """A hermes root with two real profiles; server scoped to ``alice``."""
        monkeypatch.setenv("HOME", str(tmp_path))
        profiles_root = tmp_path / ".hermes" / "profiles"
        for p in ("alice", "bob"):
            (profiles_root / p).mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(profiles_root / "alice"))
        return profiles_root

    @contextlib.contextmanager
    def _client(self, monkeypatch, *, isolated: bool):
        """A TestClient with the shared app scoped as isolated or not.

        ``app.state.isolated`` is a process-global; snapshot it and restore it
        on exit so an isolated test can't leak into a later non-isolated test
        in the same process (its only default is when the attribute is never
        set at all).
        """
        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "soul-test-token")
        from hermes_cli import web_server

        prev = getattr(web_server.app.state, "isolated", None)
        had = hasattr(web_server.app.state, "isolated")
        web_server.app.state.isolated = isolated
        c = TestClient(web_server.app, raise_server_exceptions=False)
        c.headers["Authorization"] = "Bearer soul-test-token"
        try:
            with c:
                yield c
        finally:
            if had:
                web_server.app.state.isolated = prev
            else:
                delattr(web_server.app.state, "isolated")

    def test_cross_profile_soul_write_refused(self, isolated_home, monkeypatch):
        bob = isolated_home / "bob"
        (bob / "SOUL.md").write_text("# Bob's persona\n", encoding="utf-8")
        before = (bob / "SOUL.md").read_text(encoding="utf-8")

        with self._client(monkeypatch, isolated=True) as c:
            r = c.put("/api/profiles/bob/soul", json={"content": "# Pwned\n"})

        assert r.status_code == 403, r.text
        # Bob's persona must be untouched.
        assert (bob / "SOUL.md").read_text(encoding="utf-8") == before

    def test_cross_profile_soul_read_refused(self, isolated_home, monkeypatch):
        (isolated_home / "bob" / "SOUL.md").write_text("# Bob\n", encoding="utf-8")

        with self._client(monkeypatch, isolated=True) as c:
            r = c.get("/api/profiles/bob/soul")

        assert r.status_code == 403, r.text

    def test_isolated_default_profile_blocks_named_profiles(
        self, tmp_path, monkeypatch
    ):
        """An isolated server scoped to the DEFAULT profile must still refuse
        cross-profile persona access (#91330 P1)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        profiles_root = tmp_path / ".hermes" / "profiles"
        bob = profiles_root / "bob"
        bob.mkdir(parents=True, exist_ok=True)
        (bob / "SOUL.md").write_text("# Bob\n", encoding="utf-8")
        # Isolated server running as the default (root) profile.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

        with self._client(monkeypatch, isolated=True) as c:
            r = c.put("/api/profiles/bob/soul", json={"content": "# nope\n"})

        assert r.status_code == 403, r.text
        assert (bob / "SOUL.md").read_text(encoding="utf-8") == "# Bob\n"

    def test_same_profile_soul_still_works(self, isolated_home, monkeypatch):
        with self._client(monkeypatch, isolated=True) as c:
            put = c.put("/api/profiles/alice/soul", json={"content": SOUL})
            get = c.get("/api/profiles/alice/soul")

        assert put.status_code == 200, put.text
        assert get.status_code == 200, get.text
        assert get.json()["content"] == SOUL

    def test_machine_dashboard_keeps_cross_profile_access(self, tmp_path, monkeypatch):
        """Control: the default machine dashboard is intentionally machine-wide
        and must not start refusing cross-profile SOUL edits."""
        monkeypatch.setenv("HOME", str(tmp_path))
        profiles_root = tmp_path / ".hermes" / "profiles"
        bob = profiles_root / "bob"
        bob.mkdir(parents=True, exist_ok=True)
        # Machine dashboard runs from the root home, NOT isolated.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

        with self._client(monkeypatch, isolated=False) as c:
            r = c.put("/api/profiles/bob/soul", json={"content": "# ok\n"})

        assert r.status_code == 200, r.text
        assert (bob / "SOUL.md").read_text(encoding="utf-8") == "# ok\n"

    def test_cross_profile_endpoints_gated_in_isolated(self, isolated_home, monkeypatch):
        """DELETE/rename/model/export for another profile are refused from an
        isolated server — the full isolation boundary, not just SOUL.md."""
        (isolated_home / "bob" / "SOUL.md").write_text("# Bob\n", encoding="utf-8")

        with self._client(monkeypatch, isolated=True) as c:
            d = c.delete("/api/profiles/bob")
            x = c.post("/api/profiles/bob/export", json={})

        assert d.status_code == 403, d.text
        assert x.status_code == 403, x.text

    def test_create_with_clone_from_sibling_refused(self, isolated_home, monkeypatch):
        """Adversarial witness (#91330 review, stop-the-line bypass): an
        isolated server must not read a sibling profile through
        ``POST /api/profiles`` ``clone_from`` — cloning copies the source's
        config/.env/SOUL/skills into a new profile the client controls."""
        bob = isolated_home / "bob"
        bob.mkdir(parents=True, exist_ok=True)
        sentinel = "SECRET_SENTINEL_NEVER_COPY_ME=1\n"
        (bob / ".env").write_text(sentinel, encoding="utf-8")
        (bob / "SOUL.md").write_text("# Bob\n", encoding="utf-8")

        with self._client(monkeypatch, isolated=True) as c:
            r = c.post(
                "/api/profiles",
                json={"name": "evil", "clone_from": "bob"},
            )

        assert r.status_code == 403, r.text
        # No destination profile was created...
        assert not (isolated_home / "evil").exists()
        # ...and the sentinel never left Bob.
        found = [str(p) for p in isolated_home.rglob("*") if p.is_file() and sentinel in p.read_text(encoding="utf-8", errors="ignore")]
        assert found == [str(bob / ".env")], f"sentinel leaked to: {found}"

    def test_create_clone_all_implicit_default_refused(self, tmp_path, monkeypatch):
        """The implicit clone-all source ('default') is also authority-bearing:
        an isolated server scoped to a named profile must not full-copy the
        machine default profile without ever naming it."""
        monkeypatch.setenv("HOME", str(tmp_path))
        hermes_root = tmp_path / ".hermes"
        hermes_root.mkdir(parents=True, exist_ok=True)
        profiles_root = hermes_root / "profiles"
        default_profile = hermes_root  # root HERMES_HOME *is* the default profile
        sentinel = "DEFAULT_SECRET_SENTINEL=1\n"
        (default_profile / ".env").write_text(sentinel, encoding="utf-8")
        (profiles_root / "alice").mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(profiles_root / "alice"))

        with self._client(monkeypatch, isolated=True) as c:
            r = c.post("/api/profiles", json={"name": "evil", "clone_all": True})

        assert r.status_code == 403, r.text
        assert not (profiles_root / "evil").exists()

    def test_import_refused_when_isolated(self, isolated_home, monkeypatch):
        """Archive import creates a full profile directory — machine-global
        control-plane mutation, refused on an isolated server."""
        with self._client(monkeypatch, isolated=True) as c:
            r = c.post("/api/profiles/import", json={"archive": "/tmp/nope.tar.gz"})

        assert r.status_code == 403, r.text

    def test_active_switch_refused_when_isolated(self, isolated_home, monkeypatch):
        """Switching the machine-wide active profile regains authority over
        other profiles' CLI/gateway routing — refused when isolated."""
        with self._client(monkeypatch, isolated=True) as c:
            r = c.post("/api/profiles/active", json={"name": "bob"})

        assert r.status_code == 403, r.text

    def test_control_plane_works_on_machine_dashboard(self, tmp_path, monkeypatch):
        """Control: the unified machine dashboard keeps profile creation and
        active-profile switching (intentional machine-wide management)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        profiles_root = tmp_path / ".hermes" / "profiles"
        profiles_root.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

        with self._client(monkeypatch, isolated=False) as c:
            r = c.post("/api/profiles", json={"name": "fresh"})

        assert r.status_code == 200, r.text

    def test_cross_profile_endpoints_work_on_machine_dashboard(
        self, tmp_path, monkeypatch
    ):
        """Control: the unified machine dashboard keeps cross-profile
        management (delete/export) — the boundary only bites when isolated."""
        monkeypatch.setenv("HOME", str(tmp_path))
        profiles_root = tmp_path / ".hermes" / "profiles"
        (profiles_root / "bob").mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

        with self._client(monkeypatch, isolated=False) as c:
            d = c.delete("/api/profiles/bob")

        # Machine dashboard may delete another profile.
        assert d.status_code == 200, d.text
        assert not (profiles_root / "bob").exists()

class TestIsolatedIdentityAliasing:
    """Adversarial witnesses for the fail-open identity-aliasing repair
    (#91330 review, re-review 2026-08-31).

    ``get_active_profile_name()`` returns the ordinary strings ``"custom"``
    (unrecognized HERMES_HOME) and ``"default"`` (derivation failure), and both
    are valid real profile ids. The isolation guard must therefore authorize by
    canonical *resolved path*, never by a profile-name string: a server with an
    unrecognized home must not be able to prove any named sibling is in scope.
    """

    @contextlib.contextmanager
    def _client(self, monkeypatch, *, isolated: bool, hermes_home: str):
        """A TestClient with the shared app scoped as isolated or not, with an
        explicit launch home (the canonical authority start_server stores)."""
        monkeypatch.setenv("HERMES_HOME", hermes_home)
        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "soul-test-token")
        from hermes_cli import web_server
        from hermes_constants import get_hermes_home

        prev = getattr(web_server.app.state, "isolated", None)
        had = hasattr(web_server.app.state, "isolated")
        prev_scope = getattr(web_server.app.state, "isolated_scope_dir", None)
        had_scope = hasattr(web_server.app.state, "isolated_scope_dir")
        web_server.app.state.isolated = isolated
        web_server.app.state.isolated_scope_dir = get_hermes_home().resolve()
        c = TestClient(web_server.app, raise_server_exceptions=False)
        c.headers["Authorization"] = "Bearer soul-test-token"
        try:
            with c:
                yield c
        finally:
            if had:
                web_server.app.state.isolated = prev
            else:
                delattr(web_server.app.state, "isolated")
            if had_scope:
                web_server.app.state.isolated_scope_dir = prev_scope
            else:
                delattr(web_server.app.state, "isolated_scope_dir")

    def test_unrecognized_home_cannot_alias_onto_profile_named_custom(
        self, tmp_path, monkeypatch
    ):
        """A real profile literally named ``custom`` must NOT become
        reachable just because ``get_active_profile_name()`` returns the
        ``"custom"`` sentinel for an unrecognized launch home."""
        monkeypatch.setenv("HOME", str(tmp_path))
        profiles_root = tmp_path / ".hermes" / "profiles"
        custom = profiles_root / "custom"
        custom.mkdir(parents=True, exist_ok=True)
        (custom / "SOUL.md").write_text("# Real custom profile\n", encoding="utf-8")
        (custom / ".env").write_text("REAL_CUSTOM_SECRET=1\n", encoding="utf-8")
        # Launch home is an arbitrary directory that is neither the default
        # root (~/.hermes) nor a  ~/.hermes/profiles/<name> path. The runtime
        # identifies such a home with the sentinel "custom" — the same string
        # as this real profile's id. Force the sentinel to make the witness
        # explicit even if resolution internals change.
        tenant_home = tmp_path / ".hermes" / "tenant-a"
        tenant_home.mkdir(parents=True, exist_ok=True)
        import hermes_cli.profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: "custom")

        with self._client(
            monkeypatch, isolated=True, hermes_home=str(tenant_home)
        ) as c:
            r = c.get("/api/profiles/custom/soul")

        assert r.status_code == 403, r.text
        # The real profile's data must be untouched either way.
        assert (custom / "SOUL.md").read_text(encoding="utf-8") == "# Real custom profile\n"

        with self._client(
            monkeypatch, isolated=True, hermes_home=str(tenant_home)
        ) as c:
            x = c.post("/api/profiles/custom/export", json={})

        assert x.status_code == 403, x.text

    def test_derivation_failure_cannot_alias_onto_default(
        self, tmp_path, monkeypatch
    ):
        """A scope-derivation exception must not silently authorize requests
        for ``default`` (the old ``_current_profile_name`` fallback returned
        the string ``"default"``, which is a real profile id)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        profiles_root = tmp_path / ".hermes" / "profiles"
        (profiles_root).mkdir(parents=True, exist_ok=True)
        tenant_home = tmp_path / ".hermes" / "tenant-b"
        tenant_home.mkdir(parents=True, exist_ok=True)
        import hermes_cli.profiles as profiles_mod

        def boom():
            raise RuntimeError("active-profile derivation failed")

        monkeypatch.setattr(profiles_mod, "get_active_profile_name", boom)

        with self._client(
            monkeypatch, isolated=True, hermes_home=str(tenant_home)
        ) as c:
            r = c.get("/api/profiles/default/soul")

        assert r.status_code == 403, r.text

    def test_same_profile_still_succeeds_by_path(self, tmp_path, monkeypatch):
        """Control: a request for the exact profile the server was launched
        with still succeeds (path equality), even when the name string would
        be a sentinel in the old scheme."""
        monkeypatch.setenv("HOME", str(tmp_path))
        profiles_root = tmp_path / ".hermes" / "profiles"
        alice = profiles_root / "alice"
        alice.mkdir(parents=True, exist_ok=True)
        import hermes_cli.profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: "alice")

        with self._client(
            monkeypatch, isolated=True, hermes_home=str(alice)
        ) as c:
            put = c.put("/api/profiles/alice/soul", json={"content": SOUL})
            get = c.get("/api/profiles/alice/soul")

        assert put.status_code == 200, put.text
        assert get.status_code == 200, get.text
        assert get.json()["content"] == SOUL

    def test_machine_dashboard_unaffected(self, tmp_path, monkeypatch):
        """Control: with isolation off (the unified machine dashboard), the
        same custom-named profile stays reachable — the boundary only bites
        when --isolated is set."""
        monkeypatch.setenv("HOME", str(tmp_path))
        profiles_root = tmp_path / ".hermes" / "profiles"
        custom = profiles_root / "custom"
        custom.mkdir(parents=True, exist_ok=True)
        import hermes_cli.profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: "custom")

        with self._client(
            monkeypatch, isolated=False, hermes_home=str(tmp_path / ".hermes")
        ) as c:
            put = c.put("/api/profiles/custom/soul", json={"content": SOUL})

        assert put.status_code == 200, put.text
        assert (custom / "SOUL.md").read_text(encoding="utf-8") == SOUL


class TestIsolatedAggregateScope:
    """Alice/Bob matrix for the aggregate + list read surfaces (#91330 review,
    re-review 2026-08-31): an isolated server pinned to Alice must narrow every
    aggregate/list read to Alice, 403 explicit sibling selectors BEFORE any
    I/O, and never open Bob's ``state.db``."""

    @contextlib.contextmanager
    def _client(self, monkeypatch, *, isolated: bool, hermes_home: str):
        monkeypatch.setenv("HERMES_HOME", hermes_home)
        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "soul-test-token")
        from hermes_cli import web_server
        from hermes_constants import get_hermes_home

        prev = getattr(web_server.app.state, "isolated", None)
        had = hasattr(web_server.app.state, "isolated")
        prev_scope = getattr(web_server.app.state, "isolated_scope_dir", None)
        had_scope = hasattr(web_server.app.state, "isolated_scope_dir")
        web_server.app.state.isolated = isolated
        web_server.app.state.isolated_scope_dir = get_hermes_home().resolve()
        c = TestClient(web_server.app, raise_server_exceptions=False)
        c.headers["Authorization"] = "Bearer soul-test-token"
        try:
            with c:
                yield c
        finally:
            if had:
                web_server.app.state.isolated = prev
            else:
                delattr(web_server.app.state, "isolated")
            if had_scope:
                web_server.app.state.isolated_scope_dir = prev_scope
            else:
                delattr(web_server.app.state, "isolated_scope_dir")

    def _opened_db_paths(self, monkeypatch, tmp_path):
        """Record every state.db the routes open via a fake SessionDB."""
        import hermes_state

        opened = []

        class _FakeDB:
            def __init__(self, db_path, read_only=False):
                opened.append(str(db_path))

            def list_sessions_rich(self, **kwargs):
                return []

            def session_count(self, **kwargs):
                return 0

            def usage_totals(self):
                return {}

            def find_pr_url_messages(self, ids):
                return []

            def close(self):
                pass

        monkeypatch.setattr(hermes_state, "SessionDB", _FakeDB)
        return opened

    def _seed(self, tmp_path):
        profiles_root = tmp_path / ".hermes" / "profiles"
        for name in ("alice", "bob"):
            home = profiles_root / name
            home.mkdir(parents=True, exist_ok=True)
            (home / "state.db").write_bytes(b"\x00" * 8)
        return profiles_root

    def test_list_profiles_narrows_to_pinned(self, tmp_path, monkeypatch):
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        import hermes_cli.profiles as profiles_mod

        def fake_list_profiles():
            from types import SimpleNamespace

            return [
                SimpleNamespace(name="alice", path=str(alice)),
                SimpleNamespace(name="bob", path=str(profiles_root / "bob")),
                SimpleNamespace(name="default", path=str(tmp_path / ".hermes")),
            ]

        monkeypatch.setattr(profiles_mod, "list_profiles", fake_list_profiles)

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            r = c.get("/api/profiles")

        assert r.status_code == 200, r.text
        names = [p["name"] for p in r.json()["profiles"]]
        assert names == ["alice"], f"sibling profiles leaked: {names}"

    def test_sessions_all_clamped_to_pinned_db_only(self, tmp_path, monkeypatch):
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        opened = self._opened_db_paths(monkeypatch, tmp_path)

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            r = c.get("/api/profiles/sessions?profile=all")

        assert r.status_code == 200, r.text
        assert str(alice / "state.db") in opened
        assert not any("bob" in p for p in opened), f"Bob's DB opened: {opened}"

    def test_sessions_explicit_sibling_403_before_io(self, tmp_path, monkeypatch):
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        opened = self._opened_db_paths(monkeypatch, tmp_path)

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            r = c.get("/api/profiles/sessions?profile=bob")

        assert r.status_code == 403, r.text
        # The 403 must fire before any sibling I/O: Bob's state.db is never
        # opened (the only open recorded is the app's own test home, which is
        # a startup side effect, not a route target).
        assert not any("bob" in p for p in opened), f"Bob's DB opened: {opened}"

    def test_sidebar_pinned_only_and_sibling_403(self, tmp_path, monkeypatch):
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        opened = self._opened_db_paths(monkeypatch, tmp_path)

        try:
            with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
                ok = c.get("/api/profiles/sessions/sidebar")
                bad = c.get("/api/profiles/sessions/sidebar?recents_profile=bob")
        finally:
            # The sidebar response is cached per-request-args in a short-TTL
            # single-flight cache keyed ONLY on the request parameters (not on
            # HERMES_HOME / db contents), so a later test in the same process
            # calling the same endpoint shape would be served this test's
            # stale (alice-scoped) payload. Clear it so sibling tests see a
            # fresh scan under their own HERMES_HOME.
            from hermes_cli.web_routers import profiles as _profiles_mod

            try:
                _profiles_mod.get_profiles_sessions_sidebar.cache_clear()
            except AttributeError:
                pass

        assert ok.status_code == 200, ok.text
        assert bad.status_code == 403, bad.text

    def test_projects_tree_pinned_only(self, tmp_path, monkeypatch):
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        opened = self._opened_db_paths(monkeypatch, tmp_path)
        import tui_gateway.server as gateway_server

        monkeypatch.setattr(
            gateway_server,
            "_build_project_tree",
            lambda *a, **k: ({"projects": [], "scoped_session_ids": []}, None),
        )

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            r = c.get("/api/profiles/projects/tree")

        assert r.status_code == 200, r.text
        assert str(alice / "state.db") in opened
        assert not any("bob" in p for p in opened), f"Bob's DB opened: {opened}"

    def test_pull_requests_pinned_only(self, tmp_path, monkeypatch):
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        opened = self._opened_db_paths(monkeypatch, tmp_path)

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            r = c.post("/api/profiles/sessions/pull-requests", json={"ids": ["s1"]})

        assert r.status_code == 200, r.text
        assert str(alice / "state.db") in opened
        assert not any("bob" in p for p in opened), f"Bob's DB opened: {opened}"

    def test_sessions_query_clamped(self, tmp_path, monkeypatch):
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        opened = self._opened_db_paths(monkeypatch, tmp_path)

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            ok = c.get("/api/sessions?profile=all")
            bad = c.get("/api/sessions?profile=bob")

        assert ok.status_code == 200, ok.text
        assert bad.status_code == 403, bad.text
        assert not any("bob" in p for p in opened), f"Bob's DB opened: {opened}"

    def test_status_and_messaging_sibling_403(self, tmp_path, monkeypatch):
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            all_ok = c.get("/api/status?profile=all")
            sibling = c.get("/api/status?profile=bob")
            msg_sibling = c.get("/api/messaging/platforms?profile=bob")

        assert all_ok.status_code == 200, all_ok.text
        assert sibling.status_code == 403, sibling.text
        assert msg_sibling.status_code == 403, msg_sibling.text

    def test_status_omitted_and_current_pin_to_scoped_profile(self, tmp_path, monkeypatch):
        """An isolated server's /api/status with an omitted or ``current``
        profile must pin to the scoped profile, not fall through to the
        machine-level topology aggregation that enumerates sibling gateway
        state (#91381 review, consolidation owner)."""
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            omitted = c.get("/api/status")
            current = c.get("/api/status?profile=current")
            whitespace = c.get("/api/status?profile=%20%20")

        # All three resolve to the pinned profile (200) rather than erroring
        # or fanning out machine-wide; the sibling selector still 403s. The
        # whitespace form is the same class as omitted: the clamp would strip
        # it to "" and fall into the machine-wide branch, so the pin must
        # treat it as omitted too (#91381 review).
        assert omitted.status_code == 200, omitted.text
        assert current.status_code == 200, current.text
        assert whitespace.status_code == 200, whitespace.text

    def test_env_config_sibling_403_via_shared_scope(self, tmp_path, monkeypatch):
        """Sibling selectors on env/config/skills routes 403 through the
        shared ``_resolve_profile_dir`` seam — the policy owner, not a
        per-handler bolt-on (#91381 review, consolidation owner)."""
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        (profiles_root / "bob" / ".env").write_text("BOB_SECRET=1\n", encoding="utf-8")

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            env = c.get("/api/env?profile=bob")
            cfg = c.get("/api/config?profile=bob")
            update = c.put("/api/config?profile=bob", json={"config": {}})

        assert env.status_code == 403, env.text
        assert cfg.status_code == 403, cfg.text
        assert update.status_code == 403, update.text

    def test_cron_aggregation_pinned_only_and_sibling_403(self, tmp_path, monkeypatch):
        """The cron aggregation seam narrows ``profile=all`` to the pinned
        profile and rejects explicit siblings before any cron I/O."""
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        bob = profiles_root / "bob"

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            all_ok = c.get("/api/cron/jobs?profile=all")
            sibling = c.get("/api/cron/jobs?profile=bob")

        assert all_ok.status_code == 200, all_ok.text
        assert isinstance(all_ok.json(), list)
        # Bottleneck check: the aggregation seam must never enumerate Bob.
        assert sibling.status_code == 403, sibling.text

    def test_status_without_profile_pinned_not_machinewide(self, tmp_path, monkeypatch):
        """An isolated backend must NOT fall into the machine-level status
        topology aggregation when the profile param is omitted — it pins to
        its scoped profile instead (#91381 review, P1)."""
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        (alice / "gateway_state.json").write_text(
            '{"gateway_state": "running", "platforms": {}}', encoding="utf-8"
        )

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            bare = c.get("/api/status")
            explicit = c.get("/api/status?profile=alice")
            sibling = c.get("/api/status?profile=bob")

        assert bare.status_code == 200, bare.text
        assert explicit.status_code == 200, explicit.text
        # The sibling selector must fail before any sibling runtime read.
        assert sibling.status_code == 403, sibling.text

    def test_cron_ticker_pinned_only_when_isolated(self, monkeypatch, tmp_path):
        """The desktop cron ticker on an isolated backend ticks only the
        pinned profile — never sibling stores (#91381 review, P1)."""
        import threading

        from hermes_cli import web_server

        profiles_root = tmp_path / ".hermes" / "profiles"
        alice = profiles_root / "alice"
        alice.mkdir(parents=True, exist_ok=True)
        bob_sentinel = profiles_root / "bob"
        bob_sentinel.mkdir(parents=True, exist_ok=True)
        # The ticker resolves the pinned profile name against the profiles
        # root, so HERMES_HOME must point at the seeded home or the canonical
        # scope never matches and the ticker is disabled.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "soul-test-token")

        from cron import scheduler_provider
        from hermes_cli import profiles as profiles_mod

        multiplex_served = []

        def fake_profiles_to_serve(*a, **k):
            multiplex_served.append(list(profiles_mod._raw_profiles_to_serve(*a, **k)))
            return multiplex_served[-1]

        events = []
        real_resolve = scheduler_provider.resolve_cron_scheduler

        class _FakeProvider:
            name = "fake"

            def __init__(self, *a, **k):
                pass

            def start(self, stop_event, **kwargs):
                events.append(("start", kwargs.get("interval")))
                if kwargs.get("profile_homes"):
                    events.append(("homes", [str(h) for h in kwargs["profile_homes"]]))

        def fake_resolve(*a, **k):
            return _FakeProvider()

        monkeypatch.setattr(
            profiles_mod, "profiles_to_serve", fake_profiles_to_serve
        )
        monkeypatch.setattr(scheduler_provider, "resolve_cron_scheduler", fake_resolve)
        monkeypatch.setattr(
            scheduler_provider, "InProcessCronScheduler", _FakeProvider
        )

        prev = getattr(web_server.app.state, "isolated", None)
        had = hasattr(web_server.app.state, "isolated")
        prev_scope = getattr(web_server.app.state, "isolated_scope_dir", None)
        had_scope = hasattr(web_server.app.state, "isolated_scope_dir")
        web_server.app.state.isolated = True
        web_server.app.state.isolated_scope_dir = alice.resolve()
        try:
            stop = threading.Event()
            web_server._start_desktop_cron_ticker(stop)
        finally:
            if had:
                web_server.app.state.isolated = prev
            else:
                delattr(web_server.app.state, "isolated")
            if had_scope:
                web_server.app.state.isolated_scope_dir = prev_scope
            else:
                delattr(web_server.app.state, "isolated_scope_dir")

        assert len(multiplex_served) == 0, "isolated ticker must not enumerate siblings"
        homes = [h for e, h in events if e == "homes"]
        assert homes, f"ticker never started with homes: {events}"
        all_homes = [str(h) for lst in homes for h in lst[0]] if homes and isinstance(homes[0][0], list) else [h for lst in homes for h in lst]
        assert any("alice" in h for h in all_homes), f"alice missing from ticker homes: {all_homes}"
        assert not any("bob" in h for h in all_homes), f"bob leaked into ticker homes: {all_homes}"

    def test_single_session_sibling_403_via_db_seam(self, tmp_path, monkeypatch):
        """Single-session routes resolve through ``_cron_profile_home`` —
        an explicit sibling selector 403s there before any state.db open."""
        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        opened = self._opened_db_paths(monkeypatch, tmp_path)

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            r = c.get("/api/sessions/whatever-id?profile=bob")

        assert r.status_code == 403, r.text
        assert not any("bob" in p for p in opened), f"Bob's DB opened: {opened}"

    def test_status_no_query_isolated_uses_scoped_collector(self, tmp_path, monkeypatch):
        """Unit-level witness for the isolation invariant (#91381 re-review,
        P1): a plain no-query /api/status on an isolated server must NOT run
        the machine-wide topology collector at all — it reads only the pinned
        profile's home through the scoped collector. This test FAILS if
        sibling topology I/O (the machine-wide cached collector) is invoked
        for an isolated /api/status."""
        from hermes_cli import web_server

        machine_wide_called = []

        def _machine_wide_must_not_run():
            machine_wide_called.append(True)
            raise AssertionError(
                "machine-wide topology collector invoked on isolated /api/status"
            )

        def _scoped(name, home):
            return {
                "profiles": [name],
                "gateway_mode": "single",
                "gateways": [{"profile": name, "ports": [4000]}],
                "profile_platforms": {name: {"telegram": {"state": "connected"}}},
            }

        monkeypatch.setattr(
            web_server, "_collect_profile_gateway_topology_cached",
            _machine_wide_must_not_run,
        )
        monkeypatch.setattr(
            web_server, "_collect_single_profile_gateway_topology_cached", _scoped
        )

        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            r = c.get("/api/status")

        assert r.status_code == 200, r.text
        # The machine-wide collector must never run on an isolated plain
        # /api/status — that is the point of the scoped collector.
        assert not machine_wide_called, (
            "isolated /api/status must not enumerate sibling topology"
        )
        body = r.json()
        # Only the pinned profile's name/ports/platforms surface.
        assert body["profiles"] == ["alice"], f"leaked: {body['profiles']}"
        assert body["gateways"] == [{"profile": "alice", "ports": [4000]}]
        assert "bob" not in json.dumps(body), f"bob leaked into /api/status: {body}"
        leaked_platforms = [
            k for k in body.get("gateway_platforms", {}) if "bob" in str(k)
        ]
        assert not leaked_platforms, f"bob platforms leaked: {leaked_platforms}"
        assert "default" not in body["profiles"]

    def test_status_isolated_scoped_collector_cached(self, tmp_path, monkeypatch):
        """The isolated scoped topology collector is cached (TTL), so repeated
        desktop polls don't re-walk the pinned profile home on every request
        (#91381 re-review P2)."""
        from hermes_cli import web_server

        # Reset any scoped-cache entry a prior test may have left for this home.
        web_server._SCOPED_TOPOLOGY_CACHE["home"] = None
        web_server._SCOPED_TOPOLOGY_CACHE["data"] = None
        web_server._SCOPED_TOPOLOGY_CACHE["ts"] = 0.0

        calls = []

        def _scoped(name, home):
            calls.append(name)
            return {
                "profiles": [name],
                "gateway_mode": "single",
                "gateways": [{"profile": name, "ports": [4000]}],
                "profile_platforms": {},
            }

        monkeypatch.setattr(
            web_server, "_collect_single_profile_gateway_topology", _scoped
        )

        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"

        with self._client(monkeypatch, isolated=True, hermes_home=str(alice)) as c:
            for _ in range(3):
                r = c.get("/api/status")
                assert r.status_code == 200, r.text

        assert calls == ["alice"], f"scoped collector re-ran per poll: {calls}"

    def test_status_no_query_non_isolated_machine_wide(self, tmp_path, monkeypatch):
        """Control: with isolation off, plain /api/status keeps reporting the
        full machine-wide topology (the unified machine dashboard is
        intentionally cross-profile)."""
        from hermes_cli import web_server

        def _machine_wide_topology():
            return {
                "profiles": ["alice", "bob", "default"],
                "gateway_mode": "multiple",
                "gateways": [
                    {"profile": "alice", "ports": [4000]},
                    {"profile": "bob", "ports": [4001]},
                ],
                "profile_platforms": {},
            }

        profiles_root = self._seed(tmp_path)
        alice = profiles_root / "alice"
        monkeypatch.setattr(
            web_server, "_collect_profile_gateway_topology", _machine_wide_topology
        )

        with self._client(monkeypatch, isolated=False, hermes_home=str(alice)) as c:
            r = c.get("/api/status")

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["profiles"] == ["alice", "bob", "default"], body["profiles"]
        assert len(body["gateways"]) == 2
