import sqlite3

from hermes_cli.agents_os_memory import (
    create_memory_candidate,
    create_memory_object,
    ensure_memory_schema,
    get_memory_object,
    record_memory_feedback,
    search_memory,
)


def _object(conn, **overrides):
    values = dict(
        kind="fact", title="Deploy convention", body_text="Use the local verification lane",
        scope="profile", profile_id="doni", producer_runtime="hermes", producer_agent="doni",
        provenance={"session_id": "s1", "write_origin": "assistant_tool", "confidence": 0.9},
    )
    values.update(overrides)
    return create_memory_object(conn, **values)


def test_schema_and_create_are_idempotent_with_provenance(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite")
    ensure_memory_schema(conn)
    ensure_memory_schema(conn)
    first = _object(conn)
    second = _object(conn)
    assert first["id"] == second["id"]
    assert conn.execute("SELECT COUNT(*) FROM memory_objects").fetchone()[0] == 1
    loaded = get_memory_object(conn, first["id"])
    assert loaded["producer_runtime"] == "hermes"
    assert loaded["producer_agent"] == "doni"
    assert loaded["session_id"] == "s1"
    assert loaded["metadata"] == {"confidence": 0.9}
    conn.close()


def test_scope_filters_do_not_merge_profiles(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite")
    _object(conn, title="Doni private", body_text="needle private", scope="private")
    _object(conn, title="Claude private", body_text="needle private", scope="private", profile_id="claude", producer_runtime="claude", producer_agent="claude")
    _object(conn, title="Shared", body_text="needle shared", scope="shared")
    _object(conn, title="Project", body_text="needle project", scope="project", project_id="p1")
    assert [r["title"] for r in search_memory(conn, "needle", profile_id="doni", scopes=["private"])] == ["Doni private"]
    assert {r["title"] for r in search_memory(conn, "needle", profile_id="doni", scopes=["private", "shared"])} == {"Doni private", "Shared"}
    assert [r["title"] for r in search_memory(conn, "needle", profile_id="doni", scopes=["project"], project_id="p1")] == ["Project"]
    conn.close()


def test_search_and_result_feedback_candidate(tmp_path):
    conn = sqlite3.connect(tmp_path / "memory.sqlite")
    obj = _object(conn, title="SQLite registry", body_text="Hybrid retrieval with provenance")
    results = search_memory(conn, "retrieval", profile_id="doni", scopes=["profile"])
    assert [r["id"] for r in results] == [obj["id"]]
    candidate = create_memory_candidate(
        conn, result_text="Promote this verified result", profile_id="doni",
        producer_runtime="codex", producer_agent="codex", task_id="task-1", run_id="run-1",
    )
    duplicate = create_memory_candidate(
        conn, result_text="Promote this verified result", profile_id="doni",
        producer_runtime="codex", producer_agent="codex", task_id="task-1", run_id="run-1",
    )
    assert duplicate["id"] == candidate["id"]
    accepted = record_memory_feedback(
        conn, candidate["id"], state="accepted", feedback="verified", object_id=obj["id"]
    )
    assert accepted["state"] == "accepted"
    assert accepted["feedback"] == "verified"
    assert accepted["object_id"] == obj["id"]
    conn.close()
