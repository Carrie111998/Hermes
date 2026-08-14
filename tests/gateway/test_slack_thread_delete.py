from unittest.mock import AsyncMock

import pytest

from gateway.slack_thread_delete import SlackThreadDeleteService, SlackThreadInventory


class FakeSlackClient:
    def __init__(self, messages, *, user_id="U_OWNER", team_id="T_ONE", failures=None):
        self.messages = list(messages)
        self.user_id = user_id
        self.team_id = team_id
        self.failures = failures or {}
        self.calls = []

    async def auth_test(self):
        self.calls.append(("auth",))
        return {"ok": True, "user_id": self.user_id, "team_id": self.team_id}

    async def conversations_replies(self, **kwargs):
        self.calls.append(("replies", kwargs.get("cursor", "")))
        error = self.failures.get(("inventory", kwargs.get("ts")))
        if error:
            return {"ok": False, "error": error}
        return {"ok": True, "messages": self.messages, "response_metadata": {}}

    async def files_delete(self, *, file):
        self.calls.append(("file", file))
        error = self.failures.get(("file", file))
        return {"ok": False, "error": error} if error else {"ok": True}

    async def chat_delete(self, *, channel, ts):
        self.calls.append(("message", ts))
        error = self.failures.get(("message", ts))
        return {"ok": False, "error": error} if error else {"ok": True}


@pytest.mark.asyncio
async def test_delete_orders_files_replies_local_trigger_and_root():
    client = FakeSlackClient([
        {"ts": "1.0", "files": [{"id": "F_ROOT"}]},
        {"ts": "2.0", "files": [{"id": "F_REPLY"}]},
        {"ts": "3.0", "files": []},
    ])
    calls = client.calls

    async def local_scrub():
        calls.append(("local",))
        return []

    async def quiesce():
        calls.append(("quiesce",))

    reports = []
    service = SlackThreadDeleteService(client, report_failure=reports.append)
    result = await service.execute(
        channel_id="C_ONE",
        thread_ts="1.0",
        trigger_ts="3.0",
        invoker_user_id="U_OWNER",
        workspace_id="T_ONE",
        quiesce=quiesce,
        local_scrub=local_scrub,
    )

    assert result.success
    assert reports == []
    assert calls == [
        ("auth",),
        ("quiesce",),
        ("replies", None),
        ("file", "F_ROOT"),
        ("file", "F_REPLY"),
        ("message", "2.0"),
        ("local",),
        ("message", "3.0"),
        ("message", "1.0"),
    ]


@pytest.mark.asyncio
async def test_not_found_states_are_idempotent_success():
    client = FakeSlackClient(
        [
            {"ts": "1.0", "files": [{"id": "F1"}]},
            {"ts": "2.0", "files": []},
        ],
        failures={
            ("file", "F1"): "file_deleted",
            ("message", "2.0"): "message_not_found",
            ("message", "1.0"): "message_not_found",
        },
    )

    async def local_scrub():
        return []

    result = await SlackThreadDeleteService(client).execute(
        channel_id="C_ONE",
        thread_ts="1.0",
        trigger_ts="2.0",
        invoker_user_id="U_OWNER",
        workspace_id="T_ONE",
        local_scrub=local_scrub,
    )
    assert result.success


@pytest.mark.asyncio
async def test_owner_or_workspace_mismatch_fails_before_inventory():
    client = FakeSlackClient([], user_id="U_OTHER")
    reports = []
    result = await SlackThreadDeleteService(client, report_failure=reports.append).execute(
        channel_id="C_ONE",
        thread_ts="1.0",
        trigger_ts="2.0",
        invoker_user_id="U_OWNER",
        workspace_id="T_ONE",
        local_scrub=lambda: None,
    )

    assert not result.success
    assert client.calls == [("auth",)]
    assert reports == ["workspace=T_ONE channel=C_ONE thread=1.0 auth=owner_mismatch"]


@pytest.mark.asyncio
async def test_partial_failure_preserves_trigger_and_root_and_reports_ids_only():
    client = FakeSlackClient(
        [
            {"ts": "1.0", "files": []},
            {"ts": "2.0", "files": []},
            {"ts": "3.0", "files": []},
        ],
        failures={("message", "2.0"): "cant_delete_message"},
    )
    reports = []

    async def local_scrub():
        return ["session:S1:unlink_failed"]

    result = await SlackThreadDeleteService(client, report_failure=reports.append).execute(
        channel_id="C_ONE",
        thread_ts="1.0",
        trigger_ts="3.0",
        invoker_user_id="U_OWNER",
        workspace_id="T_ONE",
        local_scrub=local_scrub,
    )

    assert not result.success
    assert ("message", "3.0") not in client.calls
    assert ("message", "1.0") not in client.calls
    assert reports == [
        "workspace=T_ONE channel=C_ONE thread=1.0 "
        "messages=2.0:cant_delete_message local=session:S1:unlink_failed"
    ]


@pytest.mark.asyncio
async def test_missing_slack_thread_still_scrubs_local_state():
    client = FakeSlackClient(
        [], failures={("inventory", "1.0"): "thread_not_found"}
    )
    calls = client.calls

    async def local_scrub():
        calls.append(("local",))
        return []

    result = await SlackThreadDeleteService(client).execute(
        channel_id="C_ONE",
        thread_ts="1.0",
        trigger_ts="2.0",
        invoker_user_id="U_OWNER",
        workspace_id="T_ONE",
        local_scrub=local_scrub,
    )

    assert result.success
    assert ("local",) in calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("malformed", "expected"),
    [
        (None, "empty_response"),
        ([], "invalid_response"),
        ({}, "invalid_response"),
        ({"ok": None}, "invalid_response"),
        ({"ok": "true"}, "invalid_response"),
        ({"error": "ratelimited"}, "invalid_response"),
    ],
)
async def test_malformed_response_is_rejected_by_shared_validator(malformed, expected):
    """Every Slack call (inventory, files_delete, chat_delete, and the
    failure-report transport) validates responses through the one ``_error``
    helper: a response that is missing, not a mapping, or whose ``ok`` is
    missing or non-boolean is rejected — never treated as success or as a
    retryable transient. Pinned against the helper directly, then through
    one representative call site (inventory), where the rejection fails the
    deletion closed before any scrubbing."""
    assert SlackThreadDeleteService._error(malformed) == expected

    client = FakeSlackClient([])
    client.conversations_replies = AsyncMock(return_value=malformed)
    local_scrub = AsyncMock(return_value=[])

    result = await SlackThreadDeleteService(client).execute(
        channel_id="C_ONE", thread_ts="1.0", trigger_ts="2.0",
        invoker_user_id="U_OWNER", workspace_id="T_ONE",
        local_scrub=local_scrub,
    )

    assert not result.success
    assert result.failures == [f"inventory:{expected}"]
    local_scrub.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_thread_with_parent_repeated_per_page_deletes_successfully():
    """conversations.replies can repeat the parent on later pages; that must
    not brick deletion of threads longer than one 100-message page."""
    page1 = [{"ts": "1.0", "files": [{"id": "F1"}]}] + [
        {"ts": f"2.{i}", "files": []} for i in range(99)
    ]
    page2 = [{"ts": "1.0", "files": []}] + [
        {"ts": f"3.{i}", "files": []} for i in range(100)
    ]
    page3 = [{"ts": "1.0", "files": []}, {"ts": "4.0", "files": []}]
    client = FakeSlackClient([])
    client.conversations_replies = AsyncMock(side_effect=[
        {"ok": True, "messages": page1, "response_metadata": {"next_cursor": "c2"}},
        {"ok": True, "messages": page2, "response_metadata": {"next_cursor": "c3"}},
        {"ok": True, "messages": page3, "response_metadata": {}},
    ])

    result = await SlackThreadDeleteService(client).execute(
        channel_id="C_ONE", thread_ts="1.0", trigger_ts="4.0",
        invoker_user_id="U_OWNER", workspace_id="T_ONE",
        local_scrub=AsyncMock(return_value=[]),
    )

    assert result.success
    assert ("file", "F1") in client.calls
    deleted = [call[1] for call in client.calls if call[0] == "message"]
    assert len(deleted) == 99 + 100 + 2  # replies + trigger + root
    assert deleted[-2:] == ["4.0", "1.0"]  # trigger, then root last


def test_failure_report_is_bounded_and_preserves_category_counts():
    failures = [f"files:F{i}:delete_failed" for i in range(20_000)]
    failures += ["local:session:S1:delete_failed", "root:message_not_found"]

    report = SlackThreadDeleteService._format_report(
        workspace_id="T_ONE", channel_id="C_ONE", thread_ts="1.0",
        failures=failures,
    )

    assert len(report.encode("utf-8")) <= 3500
    assert "files=F0:delete_failed,omitted_19999" in report
    assert "local=session:S1:delete_failed" in report
    assert "root=message_not_found" in report
    assert report.endswith("report=truncated")


@pytest.mark.asyncio
async def test_quiesce_failure_stops_before_inventory_or_deletion():
    client = FakeSlackClient([{"ts": "1.0", "files": []}])
    reports = []

    async def quiesce():
        raise RuntimeError("route changed")

    async def unused_local_scrub():
        return []

    result = await SlackThreadDeleteService(
        client, report_failure=reports.append
    ).execute(
        channel_id="C_ONE",
        thread_ts="1.0",
        trigger_ts="2.0",
        invoker_user_id="U_OWNER",
        workspace_id="T_ONE",
        quiesce=quiesce,
        local_scrub=unused_local_scrub,
    )

    assert not result.success
    assert client.calls == [("auth",)]
    assert reports == [
        "workspace=T_ONE channel=C_ONE thread=1.0 local=quiesce:RuntimeError"
    ]


@pytest.mark.asyncio
async def test_inventory_cursor_cycle_fails_bounded_and_reports():
    class CursorClient(FakeSlackClient):
        async def conversations_replies(self, **kwargs):
            self.calls.append(("replies", kwargs.get("cursor")))
            return {
                "ok": True,
                "messages": [{"ts": "1.0" if kwargs.get("cursor") is None else "1.1", "files": []}],
                "response_metadata": {"next_cursor": "repeat"},
            }

    client = CursorClient([])
    reports = []

    async def local_scrub():
        raise AssertionError("must not scrub after ambiguous inventory")

    result = await SlackThreadDeleteService(
        client, report_failure=reports.append
    ).execute(
        channel_id="C_ONE", thread_ts="1.0", trigger_ts="2.0",
        invoker_user_id="U_OWNER", workspace_id="T_ONE",
        local_scrub=local_scrub,
    )

    assert not result.success
    assert len([call for call in client.calls if call[0] == "replies"]) == 2
    assert reports == [
        "workspace=T_ONE channel=C_ONE thread=1.0 inventory=cursor_cycle"
    ]


@pytest.mark.asyncio
async def test_failure_report_delivery_failure_is_exposed_to_runner():
    client = FakeSlackClient([], user_id="U_OTHER")

    async def broken_report(_text):
        raise RuntimeError("unavailable")

    async def unused_local_scrub():
        return []

    service = SlackThreadDeleteService(client, report_failure=broken_report)
    result = await service.execute(
        channel_id="C_ONE", thread_ts="1.0", trigger_ts="2.0",
        invoker_user_id="U_OWNER", workspace_id="T_ONE",
        local_scrub=unused_local_scrub,
    )

    assert not result.success
    assert service.report_delivery_failed


@pytest.mark.asyncio
async def test_transient_auth_is_retried_with_bounded_attempts(monkeypatch):
    class RetryAuthClient(FakeSlackClient):
        def __init__(self):
            super().__init__([{"ts": "1.0", "files": []}])
            self.auth_attempts = 0

        async def auth_test(self):
            self.auth_attempts += 1
            self.calls.append(("auth",))
            if self.auth_attempts < 3:
                return {"ok": False, "error": "ratelimited"}
            return {"ok": True, "user_id": self.user_id, "team_id": self.team_id}

    async def no_sleep(_seconds):
        return None

    async def local_scrub():
        return []

    monkeypatch.setattr("gateway.slack_thread_delete.asyncio.sleep", no_sleep)
    client = RetryAuthClient()
    result = await SlackThreadDeleteService(client, max_attempts=3).execute(
        channel_id="C_ONE", thread_ts="1.0", trigger_ts="2.0",
        invoker_user_id="U_OWNER", workspace_id="T_ONE",
        local_scrub=local_scrub,
    )

    assert result.success
    assert client.auth_attempts == 3


@pytest.mark.asyncio
async def test_inventory_retains_only_identifiers():
    client = FakeSlackClient([
        {
            "ts": "1.0",
            "text": "sensitive root",
            "blocks": [{"type": "section", "text": {"text": "secret"}}],
            "files": [{"id": "F1", "name": "private.pdf"}],
        },
        {"ts": "2.0", "text": "sensitive reply", "files": []},
    ])

    inventory, error = await SlackThreadDeleteService(client)._inventory("C", "1.0")

    assert error == ""
    assert inventory == SlackThreadInventory(
        root_ts="1.0", reply_ts=["2.0"], file_ids=["F1"]
    )
    assert not hasattr(inventory, "messages")


@pytest.mark.asyncio
async def test_inventory_identifier_count_is_bounded():
    client = FakeSlackClient([
        {"ts": "1.0", "files": [{"id": "F1"}]},
        {"ts": "2.0", "files": [{"id": "F2"}]},
    ])

    inventory, error = await SlackThreadDeleteService(
        client, max_inventory_identifiers=2
    )._inventory("C", "1.0")

    assert inventory == SlackThreadInventory()
    assert error == "identifier_limit"


@pytest.mark.asyncio
async def test_retry_after_is_capped(monkeypatch):
    sleeps = []

    class SlackRateLimitError(RuntimeError):
        def __init__(self, response):
            super().__init__("rate limited")
            self.response = response

    class RateLimitedClient(FakeSlackClient):
        async def files_delete(self, *, file):
            response = type("Response", (dict,), {"headers": {"Retry-After": "9999"}})(
                error="ratelimited"
            )
            raise SlackRateLimitError(response)

    async def record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("gateway.slack_thread_delete.asyncio.sleep", record_sleep)
    service = SlackThreadDeleteService(
        RateLimitedClient([]), max_attempts=2, max_retry_after_seconds=5
    )

    error = await service._call(
        lambda: service.client.files_delete(file="F1"), not_found=set()
    )

    assert error == "ratelimited"
    assert sleeps == [5]


def test_malformed_slack_error_is_replaced_with_safe_code():
    error = SlackThreadDeleteService._error({
        "ok": False,
        "error": "bad\nworkspace=T_OTHER token=xoxp-secret",
    })

    assert error == "invalid_error_code"
