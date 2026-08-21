from hermes_state import SessionDB


def test_channel_binding_assigns_existing_and_future_sessions(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("before", "discord", chat_id="channel-1")
        space = db.create_session_space(
            "Infrastructure",
            platform="discord",
            chat_id="channel-1",
        )

        assert db.get_session("before")["space_id"] == space["id"]

        db.create_session("after", "discord", chat_id="channel-1")
        db.create_session("other", "discord", chat_id="channel-2")

        assert db.get_session("after")["space_id"] == space["id"]
        assert db.get_session("other")["space_id"] is None
    finally:
        db.close()


def test_space_assignment_is_cwd_independent_and_inherited_by_children(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        space = db.create_session_space("Health")
        db.create_session("parent", "desktop", cwd="C:/shared/repo")
        assert db.set_session_space("parent", space["id"])

        db.create_session("child", "desktop", parent_session_id="parent")

        parent = db.get_session("parent")
        child = db.get_session("child")
        assert parent["cwd"] == child["cwd"] == "C:/shared/repo"
        assert parent["space_id"] == child["space_id"] == space["id"]
    finally:
        db.close()


def test_space_binding_is_unique_and_delete_unassigns(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        first = db.create_session_space("One", platform="telegram", chat_id="42")
        db.create_session("bound", "telegram", chat_id="42")

        try:
            db.create_session_space("Two", platform="telegram", chat_id="42")
        except ValueError as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("duplicate channel binding was accepted")

        assert db.delete_session_space(first["id"])
        assert db.get_session("bound")["space_id"] is None
    finally:
        db.close()


def test_gateway_peer_refresh_assigns_recovered_row(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        space = db.create_session_space("Investing", platform="slack", chat_id="C123")
        db.create_session("recovered", "slack")

        db.record_gateway_session_peer(
            "recovered",
            source="slack",
            session_key="agent:main:slack:channel:C123",
            chat_id="C123",
            chat_type="channel",
        )

        assert db.get_session("recovered")["space_id"] == space["id"]
    finally:
        db.close()


def test_reset_child_does_not_inherit_space_without_channel_binding(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        space = db.create_session_space("Health")
        db.create_session("parent", "desktop")
        db.set_session_space("parent", space["id"])

        db.create_session(
            "fresh",
            "desktop",
            parent_session_id="parent",
            model_config={"_reset_from": "parent"},
        )

        assert db.get_session("fresh")["space_id"] is None
    finally:
        db.close()
