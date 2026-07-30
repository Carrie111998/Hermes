from types import SimpleNamespace

import pytest

from agent.chat_completion_helpers import _dispatch_nonstreaming_api_request
from agent.turn_routing_runtime import RouteBudgetDispatchBlocked


def test_nonstreaming_dispatch_notifies_budget_at_exact_provider_boundary():
    events = []
    response = SimpleNamespace(id="provider-response-1")

    class _Completions:
        def create(self, **kwargs):
            events.append(("create", kwargs))
            return response

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_Completions()),
    )
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="xai",
        _turn_route_budget_submission_started=lambda: events.append(("started", None)),
        _turn_route_budget_submission_accepted=lambda result: events.append(
            ("accepted", result.id)
        ),
        _turn_route_budget_submission_failed=lambda error: events.append(
            ("failed", str(error))
        ),
    )

    result = _dispatch_nonstreaming_api_request(
        agent,
        {"model": "grok-4.5", "messages": []},
        make_client=lambda _reason: client,
    )

    assert result is response
    assert events == [
        ("started", None),
        ("create", {"model": "grok-4.5", "messages": []}),
        ("accepted", "provider-response-1"),
    ]


def test_nonstreaming_dispatch_reports_provider_exception_after_sdk_attempt():
    events = []
    error = RuntimeError("transport uncertain")

    class _Completions:
        def create(self, **_kwargs):
            events.append("create")
            raise error

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="xai",
        _turn_route_budget_submission_started=lambda: events.append("started"),
        _turn_route_budget_submission_accepted=lambda _result: events.append("accepted"),
        _turn_route_budget_submission_failed=lambda failure: events.append(
            ("failed", failure)
        ),
    )

    with pytest.raises(RuntimeError, match="transport uncertain"):
        _dispatch_nonstreaming_api_request(
            agent,
            {"model": "grok-4.5", "messages": []},
            make_client=lambda _reason: client,
        )

    assert events == ["started", "create", ("failed", error)]


def test_nonstreaming_dispatch_blocked_before_sdk_does_not_call_provider():
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: pytest.fail("SDK create must not run")
            )
        )
    )
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="xai",
        _turn_route_budget_submission_started=lambda: (_ for _ in ()).throw(
            RouteBudgetDispatchBlocked("released")
        ),
    )

    with pytest.raises(RouteBudgetDispatchBlocked):
        _dispatch_nonstreaming_api_request(
            agent,
            {"model": "grok-4.5", "messages": []},
            make_client=lambda _reason: client,
        )


def test_acceptance_callback_failure_is_not_reclassified_as_sdk_failure():
    events = []
    response = SimpleNamespace(id="provider-response-1")
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_kwargs: response)
        )
    )

    def _accepted(_response):
        events.append("accepted")
        raise RouteBudgetDispatchBlocked("accounting_failed")

    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="xai",
        _turn_route_budget_submission_started=lambda: events.append("started"),
        _turn_route_budget_submission_accepted=_accepted,
        _turn_route_budget_submission_failed=lambda _error: events.append("failed"),
    )

    with pytest.raises(RouteBudgetDispatchBlocked):
        _dispatch_nonstreaming_api_request(
            agent,
            {"model": "grok-4.5", "messages": []},
            make_client=lambda _reason: client,
        )

    assert events == ["started", "accepted"]
