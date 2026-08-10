"""Regression tests for GET /api/skills resolving recent_session_ids into
recent_sessions (see tools/skill_usage.py::bump_use and #82802).
"""
import pytest


def _write_skill(skills_dir, name, description="test skill"):
    d = skills_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


@pytest.fixture
def client(monkeypatch, tmp_path):
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")

    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    home = get_hermes_home()
    (home / "skills").mkdir(parents=True, exist_ok=True)
    _write_skill(home / "skills", "my-skill")

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db")
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


class TestSkillsRecentSessions:
    def test_resolves_recent_session_ids_against_state_db(self, client):
        from tools import skill_usage
        import hermes_state

        db = hermes_state.SessionDB(hermes_state.DEFAULT_DB_PATH)
        db.create_session("session-a", "cli", model="gpt-5")
        db.set_session_title("session-a", "Debugging the parser")
        db.close()

        skill_usage.bump_use("my-skill", session_id="session-a")

        resp = client.get("/api/skills")
        assert resp.status_code == 200
        skills = {s["name"]: s for s in resp.json()}
        recent = skills["my-skill"]["recent_sessions"]
        assert recent == [
            {
                "session_id": "session-a",
                "title": "Debugging the parser",
                "source": "cli",
                "model": "gpt-5",
                "started_at": recent[0]["started_at"],
            }
        ]
        assert recent[0]["started_at"] is not None

    def test_omits_stale_session_ids_not_in_state_db(self, client):
        from tools import skill_usage

        skill_usage.bump_use("my-skill", session_id="deleted-session")

        resp = client.get("/api/skills")
        skills = {s["name"]: s for s in resp.json()}
        assert skills["my-skill"]["recent_sessions"] == []

    def test_skill_never_used_has_empty_recent_sessions(self, client):
        resp = client.get("/api/skills")
        skills = {s["name"]: s for s in resp.json()}
        assert skills["my-skill"]["recent_sessions"] == []

    def test_survives_session_db_open_failure(self, client, monkeypatch):
        """A corrupt/irreparable state.db must degrade recent_sessions to []
        rather than 500ing the whole endpoint (previously resilient to
        session-db failures — regression from the recent_sessions feature).
        """
        from tools import skill_usage

        skill_usage.bump_use("my-skill", session_id="session-a")

        import hermes_cli.web_routers.skills as skills_router

        monkeypatch.setattr(
            skills_router,
            "_open_session_db_for_profile",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("corrupt state.db")),
        )

        resp = client.get("/api/skills")
        assert resp.status_code == 200
        skills = {s["name"]: s for s in resp.json()}
        assert skills["my-skill"]["recent_sessions"] == []
