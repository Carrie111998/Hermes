from types import SimpleNamespace

from hermes_cli import profiles as profiles_mod
from hermes_cli.web_routers import profiles as profiles_router
from hermes_state import SessionDB


def _seed(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=path)
    try:
        for row in rows:
            db.create_session(row["id"], source=row.get("source", "desktop"))
            db.append_message(row["id"], role="user", content=row["id"])
            if row.get("pinned"):
                db.set_session_pinned(row["id"], True)
            if row.get("archived"):
                db.set_session_archived(row["id"], True)
    finally:
        db.close()


def test_sidebar_reports_sibling_pins_without_mixing_profile_rows(tmp_path, monkeypatch):
    default_home = tmp_path / "default"
    work_home = tmp_path / "work"
    _seed(default_home / "state.db", [{"id": "same-id", "pinned": True}])
    _seed(
        work_home / "state.db",
        [
            {"id": "same-id", "pinned": True},
            {"id": "work-ordinary"},
            {"id": "work-archived-pin", "pinned": True, "archived": True},
            {"id": "work-cron-pin", "pinned": True, "source": "cron"},
        ],
    )
    monkeypatch.setattr(
        profiles_mod,
        "list_profiles",
        lambda: [
            SimpleNamespace(name="default", path=default_home),
            SimpleNamespace(name="work", path=work_home),
        ],
    )
    monkeypatch.setattr(profiles_router, "_strip_session_list_rows", lambda rows: rows)

    result = profiles_router.get_profiles_sessions_sidebar(
        recents_profile="default",
        recents_limit=1,
        recents_exclude="cron",
        cron_limit=1,
        messaging_limit=1,
        messaging_exclude="cron,desktop",
    )

    assert [(row["id"], row["profile"]) for row in result["recents"]["sessions"]] == [
        ("same-id", "default")
    ]
    assert result["recents"]["hidden_pinned_count"] == 1
