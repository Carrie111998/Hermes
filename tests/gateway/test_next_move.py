import json

from gateway.next_move import inspect_next_move, is_complex_next_move, route_next_move


def marker(**fields):
    fields.setdefault("proposal", True)
    return "Suggested next move.\n<!-- hermes-next-move: " + json.dumps(fields) + " -->"


def test_simple_suggestion_stays_in_chat():
    created = []
    result = route_next_move(
        marker(title="Read the docs", steps=["Read the docs"]),
        session_id="s1",
        create_fn=lambda **kwargs: created.append(kwargs) or "unused",
    )
    assert result["created"] is False
    assert result["text"] == "Suggested next move."
    assert not created


def test_complex_suggestion_creates_one_task_and_reports_origin():
    calls = []

    def create_fn(**kwargs):
        calls.append(kwargs)
        return "t_123"

    result = route_next_move(
        marker(
            title="Run the migration",
            steps=["Back up", "Migrate", "Verify"],
            durable=True,
            assignee="builder",
        ),
        session_id="discord-session",
        create_fn=create_fn,
    )
    assert result["created"] is True
    assert "t_123" in result["text"]
    assert calls[0]["session_id"] == "discord-session"
    assert calls[0]["assignee"] == "builder"
    assert calls[0]["idempotency_key"].startswith("next-move:")


def test_user_confirmed_execution_is_preserved_in_task_body():
    calls = []
    route_next_move(
        marker(
            title="Publish report",
            steps=["Build", "Review", "Publish"],
            side_effecting=True,
            user_confirmed=True,
        ),
        session_id="s1",
        create_fn=lambda **kwargs: calls.append(kwargs) or "t_confirmed",
    )
    body = json.loads(calls[0]["body"])
    assert body["user_confirmed"] is True


def test_duplicate_proposals_use_same_idempotency_key():
    keys = []
    proposal = marker(title="Long plan", steps=["one", "two"], asynchronous=True)
    for _ in range(2):
        route_next_move(
            proposal,
            session_id="s1",
            create_fn=lambda **kwargs: keys.append(kwargs["idempotency_key"]) or "t_same",
        )
    assert keys[0] == keys[1]


def test_recursive_context_is_never_routed():
    calls = []
    result = route_next_move(
        marker(title="Plan", steps=["one", "two", "three"]),
        session_id="s1",
        create_fn=lambda **kwargs: calls.append(kwargs) or "t_bad",
        recursive=True,
    )
    assert result["created"] is False
    assert not calls


def test_invalid_or_unmarked_text_is_untouched():
    text = "Take a look at the next step."
    assert inspect_next_move(text) == (text, None)
    assert not is_complex_next_move(None)
