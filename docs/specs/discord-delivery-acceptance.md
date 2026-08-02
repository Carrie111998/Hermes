# Discord delivery acceptance — normative spec

Status: normative for `plugins/platforms/discord/adapter.py` (the outbound
delivery path: `DiscordAdapter.send`, plus the target-resolution helpers it
shares with `DiscordAdapter.edit_message`).

Base commit this spec was written against: `3f497e2b4f92ef83f45a98c02f7cb47c12ee069e`.

Terminology in this document follows RFC 2119: MUST / MUST NOT / SHOULD / MAY.

---

## 0. Problem statement (observed on the base commit)

`DiscordAdapter.send()` resolves its target with a bare `int()`:

```python
channel = self._client.get_channel(int(thread_id))     # adapter.py:2983
channel = self._client.get_channel(int(chat_id))       # adapter.py:2990
```

Three consequences, all reachable from configuration a user can legitimately
write today (`DISCORD_HOME_CHANNEL=#general`, a channel name in a routing
table, an id pasted with stray characters):

1. **No id validation.** `int()` accepts values that are not Discord
   snowflakes: `"+123"`, `"1_2"`, `" 123 "`, `"١٢٣"` (non-ASCII decimal
   digits). These produce an int that addresses a *different* channel or no
   channel at all, and the failure surfaces — if at all — as a generic
   "not found".
2. **No name resolution.** A channel *name* (`"#general"`, `"general"`) is
   passed straight into `int()`, which raises `ValueError`. The value lands in
   the catch-all handler and is reported as an opaque send failure rather than
   as "that name does not resolve".
3. **No delivery acceptance.** `channel.send()` returning without raising is
   treated as proof of delivery. A post that Discord accepted but that is not
   subsequently readable in the target channel/thread is indistinguishable
   from a clean send, and a multi-chunk send that fails midway discards the
   ids of the chunks that *did* land, so nothing downstream can repair the
   partial state.

This spec closes all three. It is a delivery-path spec: it defines behavior
that is provable from unit tests against a mocked Discord client. It makes no
claim about live Discord behavior (see §8).

---

## 1. Scope

**In scope** — `plugins/platforms/discord/adapter.py`:

- `DiscordAdapter.send()` — target resolution, post, delivery acceptance.
- `DiscordAdapter.edit_message()` — target resolution only (§2, §3). Edits are
  not posts and are NOT subject to §5 acceptance.
- New module-level helpers for id validation, name resolution and diagnostic
  sanitisation.
- The adapter-local `_send_with_retry` override that enforces §5.6.

**Out of scope** (explicitly, so the boundary is finite and reviewable):

- `_send_to_forum()` / `_forum_post_file()`. Forum posts create a thread and a
  starter message atomically, and the returned `message_id` legitimately falls
  back to the *thread* id when the API response carries no starter message
  (`adapter.py:3127`). "Read back the message id" is therefore not
  well-defined for that path. Forum sends keep their current semantics.
- `_edit_overflow_split()` continuation sends (they are edits' recovery path,
  not the delivery path).
- File/voice/image attachment senders (`_send_file_attachment`, voice, etc.).
- `gateway/platforms/base.py` and every other platform adapter. No file
  outside the Discord adapter, its tests, and `docs/specs/` changes.

---

## 2. Snowflake identifiers — strict validation

A **valid Discord id** is defined here, exhaustively, as:

- a `str` which, after exactly one `.strip()`, matches the regular expression
  `^[1-9][0-9]{0,19}$` (ASCII decimal digits only, no leading zero, no sign,
  no separators, no internal whitespace, at most 20 digits); or
- an `int` (excluding `bool`) that is `> 0`.

Everything else is invalid. Rationale for each restriction:

| Restriction | Why |
| --- | --- |
| ASCII digits only | `int()` accepts other Unicode decimal digits (`"١٢٣"` → 123); a target id must be byte-exact. |
| no `+`/`-` sign | `int("+123")` succeeds and silently normalises. |
| no `_` separators | `int("1_2")` succeeds (PEP 515) and yields 12. |
| no surrounding-whitespace acceptance beyond one strip; no internal whitespace | `int(" 1 2 ")` raises, but `int("\n123\t")` succeeds; one explicit strip is the documented normalisation, nothing more. |
| no leading zero, and `"0"` invalid | Snowflakes are positive and canonical; `"0123"` is a typo, not an address. |
| ≤ 20 digits | A snowflake is a 64-bit unsigned integer (max 20 decimal digits). |

**Rules**

- 2.1 The delivery path MUST validate a candidate id with this predicate
  BEFORE any `int()` call, and MUST NOT call `int()` on a value that fails it.
- 2.2 An invalid value that is *name-shaped* (§3) MUST be routed to name
  resolution.
- 2.3 An invalid value that is neither a valid id nor name-shaped MUST fail
  loudly (§4) with no post attempted.

## 3. Channel/thread name resolution — deterministic and exact

A **name-shaped** target is a non-empty string that is not a valid id (§2) and
whose value, after one `.strip()` and after removing at most one leading `#`,
is a non-empty string containing no whitespace-only content.

**Rules**

- 3.1 Resolution MUST be by **exact, case-sensitive** equality against the
  candidate's `name` attribute. No prefix, substring, fuzzy, or
  case-insensitive matching. Discord lowercases text-channel names on
  creation; a user who writes `#General` where only `general` exists gets a
  loud miss, never a guess.
- 3.2 The candidate set is deterministic and consists of, for every guild in
  `client.guilds`, in guild order then per-collection order: `text_channels`,
  `forums` (when the attribute exists), `threads`. Candidates without a
  `name` attribute are skipped. No other object (category, voice channel,
  DM) participates.
- 3.3 Exactly one match → that channel object is the resolved target. The
  resolved object MUST be used directly; the delivery path MUST NOT round-trip
  it through `int()` or a second lookup.
- 3.4 Zero matches → loud failure (§4). MUST NOT fall back to a partial match,
  to the first guild's default channel, or to `int()`.
- 3.5 Two or more matches → **ambiguous** → loud failure (§4). MUST NOT pick
  one. The diagnostic MUST include the matching channel ids so an operator can
  disambiguate by id.
- 3.6 A client with no `guilds` attribute, or an empty guild list, yields zero
  matches → 3.4.
- 3.7 Name resolution MUST NOT be attempted for a value that is a valid id
  (§2); ids never fall through to a name scan.

## 4. Loud failure — no silent or debug-only degradation

- 4.1 Every failure defined in §2, §3, §5 and every exception raised by the
  Discord API during target resolution or posting (explicitly including HTTP
  404 "Unknown Channel" / "Unknown Message", and 403 Forbidden) MUST produce
  `SendResult(success=False)` with a non-empty `error`.
- 4.2 Each such failure MUST emit exactly one `logger.error(...)` record from
  the delivery path. `logger.debug` / `logger.trace`-only reporting of a
  delivery failure is forbidden. (Non-failure fallbacks that recover — e.g.
  dropping a stale reply reference — keep their existing lower level.)
- 4.3 No path may return `success=True` for a post that raised, that was not
  attempted, or that failed acceptance (§5).
- 4.4 **Secret-free diagnostics.** Log records and `SendResult.error` strings
  produced by the code added under this spec MUST NOT contain: the bot token
  or any credential, any URL (`http://` / `https://`), or the message content
  body. They MAY contain: channel/thread/message ids, the requested target
  string, exception type names, HTTP status and Discord error codes, and
  counts. Provider exception text MUST be passed through a sanitiser that
  replaces URL-shaped substrings and bounds the length. New error-level
  delivery diagnostics MUST NOT pass `exc_info=True` (a rendered traceback is
  an unbounded, unsanitised channel).

## 5. Received-message acceptance

"Posted" means `channel.send(...)` returned a message object. "Accepted"
means the posted id was subsequently read back from the *same* target
channel/thread object and matched. Only accepted posts are successes.

- 5.1 After each successful `channel.send()` in `send()`, the adapter MUST
  read the posted message back from the resolved target via
  `channel.fetch_message(<posted id>)` and MUST require that the returned
  object's `id` equals the posted id (string comparison).
- 5.2 Acceptance failure is any of: the read returns `None`; the read returns
  an object whose `id` differs from the posted id; the read raises any
  exception; the resolved target exposes no callable `fetch_message`. Each of
  these MUST make the whole `send()` call return `success=False`.
- 5.3 On acceptance failure the result MUST preserve repair evidence:
  `SendResult.message_id` is the first posted id (unchanged from the success
  shape) and `raw_response["message_ids"]` lists every id posted by this call,
  in send order. `raw_response` MUST also carry a machine-readable
  `delivery_acceptance` record containing the failure reason, the unverified
  id, and the verified ids.
- 5.4 The adapter MUST NOT re-post, re-send, delete, or otherwise mutate the
  channel in response to an acceptance failure. The side effect stands; the
  result reports it as unverified.
- 5.5 For a multi-chunk send, every posted chunk MUST be verified. The call
  fails on the first chunk that fails acceptance; verification of later chunks
  is not attempted, and the already-posted ids are still reported per 5.3.
- 5.6 A caller that routes through the base-class recovery wrapper
  (`_send_with_retry`) MUST NOT cause a second post after an acceptance
  failure. The base wrapper's plain-text fallback and retry loop both call
  `send()` again, which would duplicate a message that is already in the
  channel; the adapter MUST short-circuit those re-entrant calls and return
  the original acceptance failure without posting.
- 5.7 Acceptance applies to the thread target when `metadata["thread_id"]` is
  present: the read-back MUST use the resolved thread object, not the parent
  channel.
- 5.8 Bookkeeping that records what is *in the channel*
  (`_last_self_message_id`, `_nonconversational_messages`) reflects what was
  posted and is unaffected by acceptance outcome — the messages are really
  there.

## 6. Failure-mode matrix (finite enumeration of §2–§5 "every/all")

Every "every"/"all" above is discharged by this table. `send()` is called with
a mocked client; "no post" means `channel.send` await count is 0.

### 6a. Id validation (applies to `chat_id` and to `metadata["thread_id"]`)

| # | Input | Required outcome |
| --- | --- | --- |
| A1 | `""` / `"   "` | fail loud, no `int()`, no post |
| A2 | `"0"` | fail loud, no post |
| A3 | `"-123"` | fail loud, no post |
| A4 | `"+123"` | fail loud, no post |
| A5 | `"1_2"` | fail loud, no post |
| A6 | `"١٢٣"` (non-ASCII digits) | fail loud, no post |
| A7 | `"12.0"` | fail loud, no post |
| A8 | `"0123"` (leading zero) | fail loud, no post |
| A9 | `"1 2"` (internal space) | fail loud, no post |
| A10 | `None` | fail loud, no post |
| A11 | `True` (bool) | fail loud, no post |
| A12 | 21+ digits | fail loud, no post |
| A13 | `" 123 "` | valid → id 123 (single strip) |
| A14 | `123` (int) | valid → id 123 |

### 6b. Name resolution

| # | Input / world | Required outcome |
| --- | --- | --- |
| B1 | `"#general"`, one exact match | resolved to that object, post proceeds |
| B2 | `"general"`, one exact match | resolved, post proceeds |
| B3 | `"#General"`, only `general` exists | fail loud (case-sensitive), no post |
| B4 | `"nope"`, no match | fail loud, no post |
| B5 | `"general"` in two guilds | fail loud (ambiguous), ids in diagnostic, no post |
| B6 | `"#"` | fail loud, no post |
| B7 | any name | `int()` never called with a name; `get_channel`/`fetch_channel` never called with a non-id |
| B8 | client without `guilds` | fail loud, no post |
| B9 | `"general"` matching one text channel and one thread | fail loud (ambiguous) |

### 6c. API failure

| # | Condition | Required outcome |
| --- | --- | --- |
| C1 | `fetch_channel` raises 404 Unknown Channel | `success=False`, one error-level record, no `success=True` |
| C2 | `get_channel` → `None` and `fetch_channel` → `None` | `success=False`, error-level record |
| C3 | `channel.send` raises 403 Forbidden | `success=False`, error-level record |
| C4 | `channel.send` raises on chunk 2 of 3 | `success=False`, `raw_response["message_ids"] == [<chunk1 id>]` |
| C5 | any of C1–C4 | log records and `error` contain no URL, no token, no message body |

### 6d. Delivery acceptance

| # | Condition | Required outcome |
| --- | --- | --- |
| D1 | read-back returns matching id | `success=True` (unchanged shape) |
| D2 | read-back returns `None` | `success=False`, `message_id` preserved, no second post |
| D3 | read-back returns a different id | `success=False`, `message_id` preserved, no second post |
| D4 | read-back raises | `success=False`, sanitised error-level record, no second post |
| D5 | target has no `fetch_message` | `success=False`, no second post |
| D6 | 3 chunks, chunk 2 unverified | `success=False`, all posted ids in `raw_response`, chunk 3 not verified |
| D7 | acceptance failure under `_send_with_retry` | total `channel.send` await count is unchanged after the wrapper returns |
| D8 | `metadata["thread_id"]` set | read-back is performed on the thread object |

## 7. Non-goals / preserved behavior

- The reply-reference retry (`error code: 10008` / `50035`) keeps its current
  behavior, including its `logger.warning` level — it is a recovery, not a
  delivery failure.
- Chunking, formatting, forum detection, `_record_discord_response` ledger
  writes and their offloading to a thread are unchanged.
- `SendResult.retryable` stays `False` for acceptance failures: the post
  landed, so a transport-level retry is exactly the wrong response.
- `SendResult.error_kind` is left unset for acceptance failures — the existing
  vocabulary (`SEND_ERROR_KINDS`) has no member for "posted but unverified",
  and inventing one would change a shared cross-adapter contract.

## 8. Verification boundary

Everything in this spec is provable from unit tests against a mocked Discord
client, static syntax checks, and the diff. Nothing here asserts behavior
against live Discord, a running gateway, or CI. Any claim of live acceptance
is out of this spec's scope and MUST NOT be inferred from a passing packet.

## 9. Known consequence (flagged, not silently absorbed)

Making read-back mandatory adds one `fetch_message` API call per posted chunk
and converts a class of previously-silent conditions into visible failures. In
particular, a bot lacking permission to read the target's history will now
report every send as failed (§5.2). §5.6 keeps that from turning into
duplicate user-visible messages, but the visible-failure change is real and
intended: a post that cannot be read back has not been shown to be delivered.
