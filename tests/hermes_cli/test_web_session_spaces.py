import pytest

from hermes_cli.web_models import SessionRename
from hermes_cli.web_routers import sessions as routes
from hermes_state import SessionDB


@pytest.mark.asyncio
async def test_dashboard_space_routes_share_assignment_with_session_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session("desktop-session", "desktop", cwd=str(tmp_path))
    db.close()

    monkeypatch.setattr(
        routes,
        "_open_session_db_for_profile",
        lambda _profile=None, read_only=False: SessionDB(db_path, read_only=read_only),
    )

    created = await routes.create_session_space_endpoint(
        {"name": "Health", "platform": "buzz", "chat_id": "channel-42"}
    )
    space = created["space"]

    assigned = await routes.rename_session_endpoint(
        "desktop-session",
        SessionRename(space_id=space["id"]),
    )
    assert assigned["space_id"] == space["id"]

    listed = await routes.list_session_spaces_endpoint()
    assert listed["spaces"][0]["name"] == "Health"

    verify = SessionDB(db_path)
    try:
        row = verify.get_session("desktop-session")
        assert row["cwd"] == str(tmp_path)
        assert row["space_id"] == space["id"]
    finally:
        verify.close()
