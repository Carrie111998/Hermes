"""Behavior tests for deterministic same-gateway Discussion policy."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_rooms


ROOM_ID = "room-1"
GATEWAY_ID = "gateway-a"
LOCAL_PROFILES = ("research", "build", "review", "ops", "qa", "docs")
MEMBERS = [
    {
        "member_id": f"member-{profile}",
        "profile": profile,
        "handle": profile,
        "display_name": profile.title(),
    }
    for profile in LOCAL_PROFILES[:3]
]
MEMBER_IDS = tuple(member["member_id"] for member in MEMBERS)


def _refs(
    overrides: dict[str, str | None] | None = None,
) -> dict[str, str | None]:
    refs: dict[str, str | None] = {member_id: None for member_id in MEMBER_IDS}
    if overrides:
        refs.update(overrides)
    return refs


def _attachment(
    kind: str,
    name: str,
    mime: str,
    *,
    size: int = 128,
    refs: dict[str, str | None] | None = None,
) -> dict:
    return {
        "kind": kind,
        "name": name,
        "size": size,
        "mime": mime,
        "refs": refs if refs is not None else _refs(),
    }


def _attachment_manifest() -> list[dict]:
    return [
        _attachment(
            "image",
            "diagram.png",
            "image/png",
            size=2048,
            refs=_refs({
                "member-research": "stage:image:research",
                "member-build": "stage:image:build",
            }),
        ),
        _attachment(
            "pdf",
            "release.pdf",
            "application/pdf",
            size=4096,
            refs=_refs({
                "member-research": "stage:pdf:research",
                "member-build": "stage:pdf:build",
            }),
        ),
        _attachment(
            "file",
            "notes.txt",
            "text/plain",
            size=512,
            refs=_refs({
                "member-research": "stage:file:research",
                "member-build": "stage:file:build",
                "member-review": "stage:file:review",
            }),
        ),
    ]


@pytest.fixture
def room_db(tmp_path: Path) -> tuple[Path, dict]:
    db = tmp_path / "state.db"
    room = hosted_rooms.create_room(
        db,
        room_id=ROOM_ID,
        name="Release",
        members=MEMBERS,
        authority_gateway_id=GATEWAY_ID,
        now=1,
    )
    return db, room


def _events(db: Path) -> list[dict]:
    return hosted_rooms.read_events(
        db,
        room_id=ROOM_ID,
        since_seq=0,
        limit=hosted_rooms.MAX_LOG_LIMIT,
    )["events"]


def _append_user(
    db: Path,
    *,
    event_id: str,
    text: str,
    thread_id: str = "thread-1",
    attachments: list[dict] | None = None,
) -> dict:
    payload: dict[str, object] = {"text": text, "thread_id": thread_id}
    if attachments is not None:
        payload["attachments"] = attachments
    return hosted_rooms.append_event(
        db,
        room_id=ROOM_ID,
        event_id=event_id,
        kind="message.user",
        actor={"kind": "user", "id": "local-user"},
        payload=payload,
        now=time.time(),
    )


def _append_publication(
    db: Path,
    plan: discussion.PublicationPlan,
) -> list[dict]:
    return [
        hosted_rooms.append_event(
            db,
            **event.append_kwargs(ROOM_ID),
            now=time.time(),
        )
        for event in plan.events
    ]


def _next_task(room: dict, db: Path) -> discussion.DiscussionTaskPlan:
    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "task", decision
    assert decision.task is not None
    return decision.task


def _settle_next(
    room: dict,
    db: Path,
    *,
    text: str,
) -> discussion.DiscussionTaskPlan:
    task = _next_task(room, db)
    publication = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="settled",
        result={"text": text},
        local_profiles=LOCAL_PROFILES,
    )
    _append_publication(db, publication)
    return task


def test_deterministic_task_fits_existing_driver_and_reconstructs_after_restart(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    user = _append_user(db, event_id="user-1", text="Check the release.")

    first = _next_task(room, db)
    repeated = _next_task(room, db)
    assert first == repeated
    assert first.identity.thread_id == "thread-1"
    assert first.payload == {
        "target_profile": "research",
        "prompt": first.payload["prompt"],
        "source_event_seq": user["seq"],
    }
    assert set(first.payload) == {"target_profile", "prompt", "source_event_seq"}

    admitted = driver.admit_task(
        db,
        first.identity,
        payload=first.payload,
        clock=time.time,
    )
    stored = driver.get_task(db, first.identity)
    reconstructed = discussion.reconstruct_task_plan(
        room,
        _events(db),
        stored,
        local_profiles=LOCAL_PROFILES,
    )
    assert admitted["status"] == "queued"
    assert reconstructed == first

    reopened_events = _events(db)
    assert (
        discussion.reconstruct_task_plan(
            room,
            reopened_events,
            driver.get_task(db, first.identity),
            local_profiles=LOCAL_PROFILES,
        )
        == first
    )


@pytest.mark.parametrize(
    ("text", "expected_profile"),
    [
        ("@build please inspect this", "build"),
        ("@all inspect this", "research"),
        ("@everyone inspect this", "research"),
        ("inspect this", "research"),
        ("@unknown inspect this", "research"),
    ],
)
def test_mentions_select_handles_or_everyone(
    room_db: tuple[Path, dict],
    text: str,
    expected_profile: str,
):
    db, room = room_db
    _append_user(db, event_id="user-1", text=text)

    assert _next_task(room, db).member.profile == expected_profile


def test_member_mention_joins_the_next_round_not_the_current_round(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="@research lead this")

    first = _settle_next(room, db, text="@build can add the implementation detail.")
    second = _next_task(room, db)

    assert first.member.profile == "research"
    assert first.round_index == 0
    assert second.member.profile == "build"
    assert second.round_index == 1


@pytest.mark.parametrize("value", ["", "pass", "pass.", "(pass)", " ( PASS ). "])
def test_pass_detection(value: str):
    assert discussion.is_pass_text(value)


def test_real_text_is_not_a_pass():
    assert not discussion.is_pass_text("I found the issue.")


def test_full_pass_round_settles_without_member_messages(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Any concerns?")

    for _member in MEMBERS:
        _settle_next(room, db, text="(pass)")

    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "settled"
    assert decision.reason == "silent_round"
    assert [event["kind"] for event in _events(db)].count("message.member") == 0


def test_failed_members_advance_the_round_as_silence(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Any concerns?")

    for expected in ("research", "build", "review"):
        task = _next_task(room, db)
        assert task.member.profile == expected
        publication = discussion.plan_publication(
            room,
            _events(db),
            task,
            status="failed",
            result={"error": f"{expected} unavailable"},
            local_profiles=LOCAL_PROFILES,
        )
        assert publication.terminal_kind == "turn.failed"
        assert len(publication.events) == 1
        _append_publication(db, publication)

    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "settled"
    assert decision.reason == "silent_round"


def test_publication_is_idempotent_and_changed_result_conflicts(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Report.")
    task = _next_task(room, db)
    publication = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="settled",
        result={"text": "Ready."},
        local_profiles=LOCAL_PROFILES,
    )

    first = _append_publication(db, publication)
    repeated = _append_publication(db, publication)
    assert [event["seq"] for event in first] == [event["seq"] for event in repeated]
    assert all(event["idempotent"] for event in repeated)

    changed = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="settled",
        result={"text": "Different."},
        local_profiles=LOCAL_PROFILES,
    )
    with pytest.raises(hosted_rooms.EventConflictError):
        _append_publication(db, changed)


def test_partial_publication_replays_same_effects_before_policy_advances(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Report.")
    task = _next_task(room, db)
    publication = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="settled",
        result={"text": "Ready."},
        local_profiles=LOCAL_PROFILES,
    )

    message_effect = publication.events[0]
    hosted_rooms.append_event(
        db,
        **message_effect.append_kwargs(ROOM_ID),
        now=time.time(),
    )
    assert _next_task(room, db).identity == task.identity

    replayed = discussion.plan_publication(
        room,
        _events(db),
        task,
        status="settled",
        result={"text": "Ready."},
        local_profiles=LOCAL_PROFILES,
    )
    _append_publication(db, replayed)
    assert _next_task(room, db).member.profile == "build"


def test_watermark_excludes_a_members_old_input_and_own_reply(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Old request.")
    first = _settle_next(room, db, text="Old answer.")
    watermark = discussion.derive_member_watermarks(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )[("thread-1", first.member.member_id)]
    assert watermark == max(
        event["seq"]
        for event in _events(db)
        if event["kind"] == "message.member"
        and event["payload"]["task_id"] == first.identity.task_id
    )

    latest = _append_user(db, event_id="user-2", text="New request.")
    next_task = _next_task(room, db)
    assert next_task.member.profile == "research"
    assert next_task.payload["source_event_seq"] == latest["seq"]
    assert "New request." in next_task.payload["prompt"]
    assert "Old request." not in next_task.payload["prompt"]
    assert "Old answer." not in next_task.payload["prompt"]


def test_newer_same_thread_user_event_cancels_a_late_result(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="First request.")
    stale = _next_task(room, db)
    latest = _append_user(db, event_id="user-2", text="Second request.")

    publication = discussion.plan_publication(
        room,
        _events(db),
        stale,
        status="settled",
        result={"text": "Late stale answer."},
        local_profiles=LOCAL_PROFILES,
    )
    assert publication.terminal_kind == "turn.cancelled"
    assert [event.kind for event in publication.events] == ["turn.cancelled"]
    assert publication.events[0].payload["reason"] == "superseded_by_newer_user_event"
    _append_publication(db, publication)

    current = _next_task(room, db)
    assert current.payload["source_event_seq"] == latest["seq"]
    assert "Second request." in current.payload["prompt"]


def test_cross_thread_newer_user_does_not_discard_completed_old_reply(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="First request.", thread_id="thread-1")
    old = _next_task(room, db)
    _append_user(db, event_id="user-2", text="Other topic.", thread_id="thread-2")

    publication = discussion.plan_publication(
        room,
        _events(db),
        old,
        status="settled",
        result={"text": "Completed first topic."},
        local_profiles=LOCAL_PROFILES,
    )
    assert [event.kind for event in publication.events] == [
        "message.member",
        "turn.settled",
    ]


def test_three_round_bound(room_db: tuple[Path, dict]):
    db, room = room_db
    room["members"] = MEMBERS[:2]
    _append_user(db, event_id="user-1", text="Discuss.")

    for index in range(6):
        _settle_next(room, db, text=f"Reply {index}.")

    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "bounded"
    assert decision.reason == "max_rounds"


def test_ten_message_bound(tmp_path: Path):
    db = tmp_path / "state.db"
    members = [
        {
            "member_id": f"member-{profile}",
            "profile": profile,
            "handle": profile,
        }
        for profile in LOCAL_PROFILES
    ]
    room = hosted_rooms.create_room(
        db,
        room_id=ROOM_ID,
        name="Large",
        members=members,
        authority_gateway_id=GATEWAY_ID,
        now=1,
    )
    _append_user(db, event_id="user-1", text="Discuss.")

    for index in range(discussion.MAX_DISCUSSION_MESSAGES):
        _settle_next(room, db, text=f"Reply {index}.")

    decision = discussion.plan_next_task(
        room,
        _events(db),
        local_profiles=LOCAL_PROFILES,
    )
    assert decision.status == "bounded"
    assert decision.reason == "max_messages"


def test_prompt_delta_is_bounded_to_24_message_lines(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    for index in range(30):
        _append_user(
            db,
            event_id=f"user-{index}",
            text=f"Message {index}.",
        )

    task = _next_task(room, db)
    assert task.payload["prompt"].count("User (user):") == 24
    assert "Message 5." not in task.payload["prompt"]
    assert "Message 6." in task.payload["prompt"]
    assert "Message 29." in task.payload["prompt"]


def test_valid_image_pdf_and_file_manifest_is_normalized():
    payload = discussion.validate_user_payload(
        {
            "text": "Review the attached material.",
            "thread_id": "thread-1",
            "attachments": _attachment_manifest(),
        },
        member_ids=MEMBER_IDS,
    )

    assert [attachment["kind"] for attachment in payload["attachments"]] == [
        "image",
        "pdf",
        "file",
    ]
    assert payload["attachments"][0] == {
        "kind": "image",
        "name": "diagram.png",
        "size": 2048,
        "mime": "image/png",
        "refs": _refs({
            "member-research": "stage:image:research",
            "member-build": "stage:image:build",
        }),
    }


def test_prompts_include_only_the_current_members_refs_and_queued_media_note(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(
        db,
        event_id="user-attachments",
        text="Review the upload.",
        attachments=_attachment_manifest(),
    )

    research = _next_task(room, db)
    research_prompt = research.payload["prompt"]
    assert research.member.member_id == "member-research"
    assert "User (user): Review the upload." in research_prompt
    assert "User (user): Review the upload. diagram.png" not in research_prompt
    assert "stage:image:research" in research_prompt
    assert "stage:pdf:research" in research_prompt
    assert "stage:file:research" in research_prompt
    assert "stage:image:build" not in research_prompt
    assert "Queued image/PDF attachments are staged separately" in research_prompt
    assert 'Queued image "diagram.png"' in research_prompt
    assert 'Queued PDF "release.pdf"' in research_prompt
    assert 'Staged file "notes.txt"' in research_prompt

    publication = discussion.plan_publication(
        room,
        _events(db),
        research,
        status="settled",
        result={"text": "(pass)"},
        local_profiles=LOCAL_PROFILES,
    )
    _append_publication(db, publication)
    build = _next_task(room, db)
    build_prompt = build.payload["prompt"]
    assert build.member.member_id == "member-build"
    assert "stage:image:build" in build_prompt
    assert "stage:pdf:build" in build_prompt
    assert "stage:file:build" in build_prompt
    assert "stage:image:research" not in build_prompt
    assert build.identity.task_id != research.identity.task_id


def test_attachment_manifest_changes_the_deterministic_task_id(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(
        db,
        event_id="user-attachments",
        text="Review the upload.",
        attachments=_attachment_manifest(),
    )
    with_attachments = _next_task(room, db)
    without_events = [
        {
            **event,
            "payload": {
                "text": event["payload"]["text"],
                "thread_id": event["payload"]["thread_id"],
            },
        }
        if event["kind"] == "message.user"
        else event
        for event in _events(db)
    ]
    without_attachments = discussion.plan_next_task(
        room,
        without_events,
        local_profiles=LOCAL_PROFILES,
    ).task

    assert without_attachments is not None
    assert with_attachments.identity.turn_id == without_attachments.identity.turn_id
    assert with_attachments.identity.task_id != without_attachments.identity.task_id
    assert with_attachments.payload["prompt"] != without_attachments.payload["prompt"]


def test_attachment_task_reconstructs_after_driver_reopen(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(
        db,
        event_id="user-attachments",
        text="Review the upload.",
        attachments=_attachment_manifest(),
    )
    task = _next_task(room, db)
    driver.admit_task(db, task.identity, payload=task.payload, clock=time.time)

    reconstructed = discussion.reconstruct_task_plan(
        room,
        _events(db),
        driver.get_task(db, task.identity),
        local_profiles=LOCAL_PROFILES,
    )
    assert reconstructed == task
    assert "stage:image:research" in reconstructed.payload["prompt"]


def test_attachment_refs_require_every_member_and_reject_unknown_members():
    missing = _refs()
    missing.pop("member-review")
    unknown = _refs()
    unknown["member-remote"] = "stage:file:remote"

    with pytest.raises(
        discussion.DiscussionValidationError, match="missing member ids"
    ):
        discussion.validate_user_payload(
            {
                "text": "Review.",
                "thread_id": "thread-1",
                "attachments": [
                    _attachment("file", "notes.txt", "text/plain", refs=missing)
                ],
            },
            member_ids=MEMBER_IDS,
        )
    with pytest.raises(
        discussion.DiscussionValidationError, match="unknown member ids"
    ):
        discussion.validate_user_payload(
            {
                "text": "Review.",
                "thread_id": "thread-1",
                "attachments": [
                    _attachment("file", "notes.txt", "text/plain", refs=unknown)
                ],
            },
            member_ids=MEMBER_IDS,
        )


@pytest.mark.parametrize("field", ["data", "data_url", "base64", "path"])
def test_attachment_manifest_rejects_raw_data_and_path_fields(field: str):
    attachment = _attachment("file", "notes.txt", "text/plain")
    attachment[field] = "/tmp/raw" if field == "path" else "AAAA"

    with pytest.raises(discussion.DiscussionValidationError, match="unknown fields"):
        discussion.validate_user_payload(
            {
                "text": "Review.",
                "thread_id": "thread-1",
                "attachments": [attachment],
            },
            member_ids=MEMBER_IDS,
        )


def test_attachment_manifest_requires_every_exact_metadata_field():
    attachment = _attachment("file", "notes.txt", "text/plain")
    attachment.pop("mime")

    with pytest.raises(discussion.DiscussionValidationError, match="missing fields"):
        discussion.validate_user_payload(
            {
                "text": "Review.",
                "thread_id": "thread-1",
                "attachments": [attachment],
            },
            member_ids=MEMBER_IDS,
        )


@pytest.mark.parametrize(
    "unsafe_ref",
    [
        "data:image/png;base64,AAAA",
        "base64:AAAA",
        "file:secret",
        "path:secret",
        "/tmp/secret",
        "../secret",
        "folder/secret",
        "C:\\secret",
    ],
)
def test_attachment_manifest_rejects_unsafe_refs(unsafe_ref: str):
    refs = _refs({"member-research": unsafe_ref})

    with pytest.raises(discussion.DiscussionValidationError, match="safe opaque ref"):
        discussion.validate_user_payload(
            {
                "text": "Review.",
                "thread_id": "thread-1",
                "attachments": [
                    _attachment("file", "notes.txt", "text/plain", refs=refs)
                ],
            },
            member_ids=MEMBER_IDS,
        )


@pytest.mark.parametrize(
    ("attachment", "match"),
    [
        (
            _attachment("file", "x" * 256, "text/plain"),
            "bounded basename",
        ),
        (
            _attachment("file", "../secret", "text/plain"),
            "bounded basename",
        ),
        (
            _attachment(
                "file",
                "notes.txt",
                "text/plain",
                size=discussion.MAX_ATTACHMENT_SIZE_BYTES + 1,
            ),
            "size must be between",
        ),
        (
            _attachment("image", "image.bin", "application/octet-stream"),
            "image kind requires image mime",
        ),
        (
            _attachment("pdf", "release.pdf", "application/octet-stream"),
            "pdf kind requires application/pdf",
        ),
        (
            _attachment(
                "file",
                "notes.txt",
                "text/plain",
                refs=_refs({"member-research": "r" * 257}),
            ),
            "safe opaque ref",
        ),
    ],
)
def test_attachment_metadata_is_bounded(attachment: dict, match: str):
    with pytest.raises(discussion.DiscussionValidationError, match=match):
        discussion.validate_user_payload(
            {
                "text": "Review.",
                "thread_id": "thread-1",
                "attachments": [attachment],
            },
            member_ids=MEMBER_IDS,
        )


def test_attachment_count_is_bounded():
    attachments = [
        _attachment("file", f"notes-{index}.txt", "text/plain")
        for index in range(discussion.MAX_ATTACHMENTS + 1)
    ]

    with pytest.raises(discussion.DiscussionValidationError, match="at most 8"):
        discussion.validate_user_payload(
            {
                "text": "Review.",
                "thread_id": "thread-1",
                "attachments": attachments,
            },
            member_ids=MEMBER_IDS,
        )


def test_attachment_manifest_total_metadata_is_bounded():
    maximum_ref = "r" * discussion.MAX_ATTACHMENT_REF_CHARS
    member_ids = tuple(f"member-{profile}" for profile in LOCAL_PROFILES)
    refs: dict[str, str | None] = {member_id: maximum_ref for member_id in member_ids}
    attachments = [
        _attachment(
            "file",
            "\U0001f9ea" * discussion.MAX_ATTACHMENT_NAME_CHARS,
            "application/octet-stream",
            refs=refs,
        )
        for _index in range(discussion.MAX_ATTACHMENTS)
    ]

    with pytest.raises(
        discussion.DiscussionValidationError, match="manifest is too large"
    ):
        discussion.validate_user_payload(
            {
                "text": "Review.",
                "thread_id": "thread-1",
                "attachments": attachments,
            },
            member_ids=member_ids,
        )


@pytest.mark.parametrize(
    ("members", "match"),
    [
        (MEMBERS[:1], "between 2 and 6"),
        (MEMBERS + MEMBERS + MEMBERS[:1], "between 2 and 6"),
        (
            [MEMBERS[0], {**MEMBERS[1], "profile": "research"}],
            "profiles must be unique",
        ),
        ([MEMBERS[0], {**MEMBERS[1], "handle": "RESEARCH"}], "handles must be unique"),
        (
            [MEMBERS[0], {**MEMBERS[1], "member_id": "MEMBER-RESEARCH"}],
            "ids must be unique",
        ),
        ([MEMBERS[0], {**MEMBERS[1], "route": {"mode": "ssh"}}], "cross-gateway"),
        ([MEMBERS[0], {**MEMBERS[1], "connectionId": "remote"}], "cross-gateway"),
        ([MEMBERS[0], {**MEMBERS[1], "profile": "missing"}], "not local"),
    ],
)
def test_malformed_or_remote_roster_is_rejected(members: list[dict], match: str):
    with pytest.raises(discussion.DiscussionValidationError, match=match):
        discussion.validate_roster(members, local_profiles=LOCAL_PROFILES)


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "hello"},
        {"text": "hello", "thread_id": "thread-1", "images": []},
        {"text": "", "thread_id": "thread-1"},
        {"text": "hello", "thread_id": "../escape"},
        {"text": ["hello"], "thread_id": "thread-1"},
    ],
)
def test_user_payload_is_exact_and_text_only(payload: dict):
    with pytest.raises(discussion.DiscussionValidationError):
        discussion.validate_user_payload(payload)


def test_malformed_log_and_task_reconstruction_fail_closed(
    room_db: tuple[Path, dict],
):
    db, room = room_db
    _append_user(db, event_id="user-1", text="Report.")
    _append_user(db, event_id="user-2", text="Report again.")
    task = _next_task(room, db)
    events = _events(db)

    with pytest.raises(discussion.DiscussionValidationError, match="sequence order"):
        discussion.plan_next_task(
            room,
            list(reversed(events)),
            local_profiles=LOCAL_PROFILES,
        )

    malformed = {
        "identity": driver.TaskIdentity(
            room_id=task.identity.room_id,
            task_id="dtask:wrong",
            thread_id=task.identity.thread_id,
            turn_id=task.identity.turn_id,
        ),
        "payload": dict(task.payload),
    }
    with pytest.raises(
        discussion.DiscussionReconstructionError,
        match="deterministic reconstruction",
    ):
        discussion.reconstruct_task_plan(
            room,
            events,
            malformed,
            local_profiles=LOCAL_PROFILES,
        )
