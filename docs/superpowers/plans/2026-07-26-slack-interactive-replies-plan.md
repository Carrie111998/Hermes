# Slack Interactive Replies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render validated Slack button directives as real Block Kit controls and safely relay one click into the original authenticated Slack conversation.

**Architecture:** A focused Slack-plugin helper owns directive parsing, opaque card records, and safe action-block construction. The Slack adapter and standalone sender both use that helper; the adapter alone consumes a click and creates a normal user-sourced `MessageEvent`, so policy remains with the receiving agent.

**Tech Stack:** Python 3.11+, Slack Bolt/Slack Web API, existing Hermes profile storage, pytest through `scripts/run_tests.sh`.

## Global Constraints

- Keep this a generic Hermes Slack transport feature; do not name Sorpio, HubSpot, AgentMail, or Brokerkit code in the runtime.
- Use `get_hermes_home()` for every persistent path and support the repository's cross-platform lock fallback pattern.
- A click is an ordinary non-internal user event; it must not call tools or bypass gateway authorization.
- Require an opaque, expiry-bound, channel-bound, message-bound, single-use record before accepting a click.
- Preserve Slack `text` fallbacks, existing Block Kit rejection fallback, and existing approval, clarification, and feedback controls.
- Support both `SlackAdapter.send` and plugin `_standalone_send`; malformed directives stay literal text.
- Use only `scripts/run_tests.sh` for tests.

---

## File Structure

- Create `plugins/platforms/slack/interactive_replies.py` — directive parser, opaque action-record store, and safe action-block builder.
- Modify `plugins/platforms/slack/adapter.py` — attach interactive actions to adapter sends, register the Slack callback, and relay accepted clicks as normal `MessageEvent` instances.
- Modify `plugins/platforms/slack/adapter.py::_standalone_send` — attach the same interactive actions for out-of-process Slack delivery and discard records if posting fails.
- Create `tests/gateway/test_slack_interactive_replies.py` — pure parser/store tests and adapter rendering/click-routing tests.
- Modify `tests/tools/test_send_message_slack.py` — standalone sender compatibility test.

### Task 1: Define the isolated interactive-reply contract

**Files:**
- Create: `plugins/platforms/slack/interactive_replies.py`
- Test: `tests/gateway/test_slack_interactive_replies.py`

**Interfaces:**
- Produces: `parse_interactive_reply(content: str) -> InteractiveReply | None`.
- Produces: `InteractiveReplyStore.create_card(channel_id: str, thread_ts: str | None, buttons: tuple[InteractiveButton, ...]) -> PreparedInteractiveReply`.
- Produces: `InteractiveReplyStore.bind_message(card_id: str, message_ts: str) -> bool`, `discard(card_id: str) -> None`, and `consume(button_token: str, channel_id: str, message_ts: str) -> ConsumedInteractiveAction | None`.
- Produces: `append_actions_block(blocks: list[dict], prepared: PreparedInteractiveReply) -> list[dict]`.

- [ ] **Step 1: Write the failing parser and store tests**

```python
def test_parse_valid_directive_strips_it_and_preserves_visible_reply():
    reply = parse_interactive_reply(
        "Approved lead.\n[[slack_buttons: Enroll:enroll, Skip:skip]]"
    )
    assert reply.visible_content == "Approved lead."
    assert [button.action_id for button in reply.buttons] == ["enroll", "skip"]

def test_consume_requires_bound_message_and_is_single_use(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    store = InteractiveReplyStore(ttl_seconds=60)
    prepared = store.create_card("C1", "T1", (InteractiveButton("Enroll", "enroll"),))
    assert store.consume(prepared.buttons[0].token, "C1", "M1") is None
    assert store.bind_message(prepared.card_id, "M1") is True
    assert store.consume(prepared.buttons[0].token, "C1", "M1").action_id == "enroll"
    assert store.consume(prepared.buttons[0].token, "C1", "M1") is None
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `scripts/run_tests.sh tests/gateway/test_slack_interactive_replies.py -q`

Expected: FAIL because the module and its contract do not exist.

- [ ] **Step 3: Write the minimal parser and store**

```python
@dataclass(frozen=True)
class InteractiveButton:
    label: str
    action_id: str

def parse_interactive_reply(content: str) -> InteractiveReply | None:
    """Return a validated reply only when the whole directive is well-formed."""

class InteractiveReplyStore:
    def consume(self, button_token: str, channel_id: str, message_ts: str) -> ConsumedInteractiveAction | None:
        """Atomically validate and consume one bound token, else return None."""
```

Use a random opaque value for each button, a card record with expected channel/thread/message and action mapping, atomic replacement for writes, and the repository's `fcntl`/`msvcrt` lock fallback. Clean expired cards while holding the lock. Reject blank labels, duplicate action IDs, invalid identifier characters, over-limit button counts, and malformed syntax by returning `None` so callers retain literal content.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `scripts/run_tests.sh tests/gateway/test_slack_interactive_replies.py -q`

Expected: PASS for parser validity, malformed literal fallback, expiry, binding, cross-channel rejection, and replay rejection.

- [ ] **Step 5: Commit the isolated contract**

```bash
git add plugins/platforms/slack/interactive_replies.py tests/gateway/test_slack_interactive_replies.py
git commit -m "feat: add Slack interactive reply store"
```

### Task 2: Render valid directives on every Slack send path

**Files:**
- Modify: `plugins/platforms/slack/adapter.py:SlackAdapter.send`
- Modify: `plugins/platforms/slack/adapter.py::_standalone_send`
- Modify: `tests/gateway/test_slack_interactive_replies.py`
- Modify: `tests/tools/test_send_message_slack.py`

**Interfaces:**
- Consumes: `parse_interactive_reply`, `InteractiveReplyStore.create_card`, `bind_message`, `discard`, and `append_actions_block` from Task 1.
- Produces: an outbound Slack payload with non-empty `text`, visible content without a valid directive, and one `hermes_interactive_reply` actions block.

- [ ] **Step 1: Write failing adapter and standalone-send tests**

```python
@pytest.mark.asyncio
async def test_adapter_send_posts_buttons_without_literal_directive():
    adapter, client = _make_adapter()
    await adapter.send("C1", "Lead ready\n[[slack_buttons: Enroll:enroll]]")
    posted = client.chat_postMessage.await_args.kwargs
    assert "[[slack_buttons:" not in posted["text"]
    assert posted["blocks"][-1]["type"] == "actions"
    assert posted["blocks"][-1]["elements"][0]["action_id"] == "hermes_interactive_reply"

def test_standalone_send_posts_the_same_action_block(monkeypatch, _standalone_send):
    result = asyncio.run(_standalone_send(pconfig, "C123", "x\n[[slack_buttons: Go:go]]"))
    assert result["success"] is True
    assert fake_session.calls[0][1]["blocks"][-1]["type"] == "actions"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `scripts/run_tests.sh tests/gateway/test_slack_interactive_replies.py tests/tools/test_send_message_slack.py -q`

Expected: FAIL because neither send path interprets the directive.

- [ ] **Step 3: Integrate the helper without changing ordinary messages**

```python
interactive = parse_interactive_reply(content)
if interactive is not None:
    visible_content = interactive.visible_content
    formatted_visible = self.format_message(visible_content)
    prepared = self._interactive_reply_store.create_card(chat_id, thread_ts, interactive.buttons)
    blocks = append_actions_block(self._maybe_blocks(visible_content) or [
        {"type": "section", "text": {"type": "mrkdwn", "text": formatted_visible}}
    ], prepared)
```

Use `interactive.visible_content` for formatting, truncation, block rendering, and accessibility fallback. Bind the card only after `chat_postMessage` returns its timestamp; discard it after any post or retry failure. In `_standalone_send`, use the same helper and bind/discard lifecycle around the direct Web API call. Preserve ordinary text and current rich/markdown block behavior when parsing returns `None`.

- [ ] **Step 4: Run the focused tests to verify they pass**

Run: `scripts/run_tests.sh tests/gateway/test_slack_interactive_replies.py tests/tools/test_send_message_slack.py -q`

Expected: PASS; both sends produce real buttons, retain `text`, and leave malformed directives literal.

- [ ] **Step 5: Commit outbound rendering**

```bash
git add plugins/platforms/slack/adapter.py tests/gateway/test_slack_interactive_replies.py tests/tools/test_send_message_slack.py
git commit -m "feat: render Slack interactive reply buttons"
```

### Task 3: Relay only a validated click into the normal gateway path

**Files:**
- Modify: `plugins/platforms/slack/adapter.py:SlackAdapter.connect`
- Modify: `plugins/platforms/slack/adapter.py:SlackAdapter`
- Modify: `tests/gateway/test_slack_interactive_replies.py`

**Interfaces:**
- Consumes: `InteractiveReplyStore.consume` from Task 1.
- Produces: `SlackAdapter._handle_interactive_reply_action(ack, body, action) -> None`.
- Produces: a regular `MessageEvent` with `text == "Slack button action: go"` and `internal is False`.

- [ ] **Step 1: Write failing callback tests**

```python
@pytest.mark.asyncio
async def test_valid_click_relays_as_user_event_in_original_thread(adapter):
    prepared = adapter._interactive_reply_store.create_card("C1", "T1", (InteractiveButton("Go", "go"),))
    adapter._interactive_reply_store.bind_message(prepared.card_id, "M1")
    await adapter._handle_interactive_reply_action(
        AsyncMock(), _click_body(channel="C1", message_ts="M1", thread_ts="T1", user="U1"),
        {"action_id": "hermes_interactive_reply", "value": prepared.buttons[0].token},
    )
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "Slack button action: go"
    assert event.source.user_id == "U1"
    assert event.source.thread_id == "T1"
    assert event.internal is False

@pytest.mark.asyncio
async def test_forged_or_replayed_click_never_reaches_handle_message(adapter):
    await adapter._handle_interactive_reply_action(AsyncMock(), _click_body(), {"value": "forged"})
    adapter.handle_message.assert_not_awaited()
```

- [ ] **Step 2: Run the focused callback tests to verify they fail**

Run: `scripts/run_tests.sh tests/gateway/test_slack_interactive_replies.py -q`

Expected: FAIL because no generic interactive action handler is registered or implemented.

- [ ] **Step 3: Register and implement the fail-closed callback**

```python
self._app.action("hermes_interactive_reply")(self._handle_interactive_reply_action)

async def _handle_interactive_reply_action(self, ack, body, action) -> None:
    await ack()
    accepted = self._interactive_reply_store.consume(str(action.get("value") or ""), channel_id, message_ts)
    if accepted is None:
        return
    await self.handle_message(MessageEvent(
        text=f"Slack button action: {accepted.action_id}",
        source=source,
        raw_message=body,
        message_id=message_ts,
        reply_to_message_id=thread_ts if thread_ts != message_ts else None,
        channel_prompt=channel_prompt,
        auto_skill=auto_skill,
        metadata=metadata,
    ))
```

Build `source`, prompt, skills, workspace scope, and thread metadata from the callback using the same adapter helpers used for normal messages. Update the clicked card to remove its action block after consumption; failure to update must not restore the record or emit another event. Never set `internal=True` and never resolve an action name directly from the Slack payload.

- [ ] **Step 4: Run callback and regression tests to verify they pass**

Run: `scripts/run_tests.sh tests/gateway/test_slack_interactive_replies.py tests/gateway/test_slack_approval_buttons.py tests/gateway/test_slack_clarify_buttons.py tests/gateway/test_slack_plugin_action_handlers.py -q`

Expected: PASS; valid clicks relay once, invalid clicks fail closed, and existing controls remain wired.

- [ ] **Step 5: Commit click routing**

```bash
git add plugins/platforms/slack/adapter.py tests/gateway/test_slack_interactive_replies.py
git commit -m "feat: relay validated Slack button clicks"
```

### Task 4: Verify the completed contribution

**Files:**
- Modify: `tests/gateway/test_slack_interactive_replies.py`
- Modify: `docs/superpowers/plans/2026-07-26-slack-interactive-replies-plan.md` by checking completed steps and recording exact verification evidence.

**Interfaces:**
- Consumes: all production code and tests from Tasks 1–3.
- Produces: an evidence-backed, review-ready branch with no uncommitted implementation changes.

- [x] **Step 1: Add a post-failure cleanup regression**

```python
@pytest.mark.asyncio
async def test_post_failure_discards_unbound_card(adapter, client):
    client.chat_postMessage.side_effect = RuntimeError("slack unavailable")
    result = await adapter.send("C1", "x\n[[slack_buttons: Go:go]]")
    assert result.success is False
    assert adapter._interactive_reply_store.pending_count() == 0
```

- [ ] **Step 2: Run the exact focused suites**

Run: `scripts/run_tests.sh tests/gateway/test_slack_interactive_replies.py tests/gateway/test_slack_block_kit_adapter.py tests/gateway/test_slack_approval_buttons.py tests/gateway/test_slack_clarify_buttons.py tests/gateway/test_slack_plugin_action_handlers.py tests/tools/test_send_message_slack.py -q`

Expected: PASS with no test deselection caused by the new feature.

Actual (2026-07-26): **BLOCKED.** The exact wrapper command discovered 6 files
and approximately 144 tests. It reported 150 passed and 1 failed. The new
`test_post_failure_discards_unbound_card` failed because
`InteractiveReplyStore` has no `pending_count()` method. Task 1's declared
store interface contains only `create_card`, `bind_message`, `discard`, and
`consume`. The repository suite was not run after this focused gate failed.

- [ ] **Step 3: Run the repository suite required by the contributor guide**

Run: `scripts/run_tests.sh`

Expected: PASS. If an unrelated existing failure appears, capture its exact output and stop for review rather than relabeling it as a feature pass.

- [ ] **Step 4: Inspect and commit verification evidence**

```bash
git diff origin/main...HEAD --check
git status --short
git add docs/superpowers/plans/2026-07-26-slack-interactive-replies-plan.md
git commit -m "docs: record Slack interactive reply verification"
```

Expected: the whitespace check passes and the worktree is clean after the commit.

## Plan Self-Review

- Spec coverage: Task 1 implements parsing, opaque records, profile-safe storage, expiry, binding, and replay prevention. Task 2 covers both outbound paths and fallback behavior. Task 3 covers normal authenticated click routing. Task 4 covers cleanup and the required suites.
- Placeholder scan: all implementation and test steps name concrete files, interfaces, commands, and assertions.
- Type consistency: every later use of `InteractiveReplyStore`, `PreparedInteractiveReply`, `InteractiveButton`, and `ConsumedInteractiveAction` is defined by Task 1; every click uses `consume(button_token, channel_id, message_ts)` from that contract.
