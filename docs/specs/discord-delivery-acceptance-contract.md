# Discord delivery acceptance — review contract (packet-provable)

Companion to `docs/specs/discord-delivery-acceptance.md`. That document says
what the code must do; this one says how a reviewer proves it, using only the
commit packet: **the diff, the test files, and the recorded test/static-check
output**. No criterion below requires a live Discord connection, a running
gateway, a deployment, or CI.

Base commit: `3f497e2b4f92ef83f45a98c02f7cb47c12ee069e`.
Spec sections referenced as §N are sections of the normative spec.

## How to read a criterion

Each criterion has an id (`C-nn`), the requirement, and the **proof** — the
exact artifact in the packet that discharges it. A criterion is met only when
the named proof exists and shows the stated result. "Test named X" means a
test function whose failure would be caused by the requirement being violated
(the reviewer should confirm the assertion, not just the name).

---

## Group 1 — Scope and containment

- **C-01** The implementation commit changes only
  `plugins/platforms/discord/adapter.py`, files under `tests/` that exercise
  the Discord adapter, and files under `docs/specs/`.
  *Proof:* `git show --stat <impl sha>`.
- **C-02** No file under `gateway/`, no other platform adapter, and no
  packaging/config/CI file is modified.
  *Proof:* same stat listing.
- **C-03** The two spec documents were committed alone, before the
  implementation, and are unmodified by the implementation commit.
  *Proof:* `git log --oneline` order plus `git show --stat` of both commits.

## Group 2 — Strict id validation (§2)

- **C-04** A single predicate defines validity and is the only gate in front
  of every `int()` used for a channel/thread id on the delivery path. Grepping
  the changed functions shows no `int(chat_id)` / `int(thread_id)` that is not
  preceded by the predicate.
  *Proof:* diff of `send()` / `edit_message()` / the new helper; grep output.
- **C-05** The predicate rejects, and the delivery path fails without posting,
  for every row A1–A12 of matrix §6a, and accepts A13–A14.
  *Proof:* a parametrised test covering all 14 rows, passing.
- **C-06** Rejection of a non-name-shaped invalid id produces
  `SendResult(success=False)` and no `channel.send` call.
  *Proof:* assertion on `success is False` and `channel.send.await_count == 0`
  in the A-row test.
- **C-07** `metadata["thread_id"]` is validated by the same predicate as
  `chat_id` (not a second, weaker copy).
  *Proof:* test asserting an invalid `thread_id` fails without posting, plus
  the diff showing one shared call site.

## Group 3 — Name resolution (§3)

- **C-08** Resolution is exact and case-sensitive; rows B1–B4 of §6b hold.
  *Proof:* tests for `#name` hit, bare `name` hit, case miss, absent miss.
- **C-09** Ambiguity fails loudly and never picks a winner; rows B5 and B9
  hold, and the diagnostic contains the competing channel ids.
  *Proof:* tests asserting `success is False`, `channel.send` never awaited,
  and both ids present in the captured error record.
- **C-10** A name is never passed to `int()` (row B7). The proof must be
  positive, not incidental: the test installs a client whose
  `get_channel`/`fetch_channel` raise if called with anything that is not an
  `int`, and asserts the name path never reaches them.
  *Proof:* named test with that guard.
- **C-11** The candidate set is exactly the one in §3.2 (guild
  `text_channels`, `forums` when present, `threads`), and objects lacking
  `name` are skipped without raising.
  *Proof:* diff of the resolver plus a test with a mixed-candidate guild
  including a nameless object.
- **C-12** A client without `guilds` (row B8) and the degenerate `"#"` input
  (row B6) both fail loudly with no post.
  *Proof:* two tests.
- **C-13** A resolved-by-name channel object is used directly for the post —
  no second lookup, no `int()` round trip.
  *Proof:* test asserting the object posted to is the same object the guild
  scan returned (`is` identity).

## Group 4 — Loud failure and secret-free diagnostics (§4)

- **C-14** Rows C1–C4 of §6c each yield `SendResult(success=False)`.
  *Proof:* four tests.
- **C-15** Each of those failures emits at least one `ERROR`-level record from
  the adapter logger; none of them is reported only at `DEBUG`/`TRACE`.
  *Proof:* tests capturing the adapter logger and asserting on record levels.
- **C-16** For every row in C1–C4 (row C5), neither the captured log records
  nor `SendResult.error` contains `http://`, `https://`, the bot token value,
  or the message content body. The test drives content and an exception text
  that both embed a URL and a token-shaped string, so a regression is caught
  rather than assumed.
  *Proof:* named secret-free test.
- **C-17** New error-level delivery diagnostics do not pass `exc_info=True`.
  *Proof:* diff inspection of the added `logger.error` calls.
- **C-18** A partial multi-chunk failure (row C4) preserves the ids of chunks
  that landed in `raw_response["message_ids"]`.
  *Proof:* test asserting the exact list.

## Group 5 — Received-message acceptance (§5)

- **C-19** The success path performs a read-back per posted chunk and requires
  an id match (row D1); a passing single-chunk success test asserts
  `fetch_message` was awaited with the posted id.
  *Proof:* named test.
- **C-20** Rows D2–D5 (absence, mismatch, read exception, no `fetch_message`)
  each produce `success=False`.
  *Proof:* four tests.
- **C-21** In every D2–D5 case, `SendResult.message_id` still carries the
  posted id and `raw_response["message_ids"]` lists every posted id —
  repair evidence is preserved, not discarded.
  *Proof:* assertions inside the same four tests.
- **C-22** In every D2–D5 case the adapter performs no additional
  `channel.send` — the side effect is never retried by the adapter.
  *Proof:* `channel.send.await_count` assertions in the same four tests.
- **C-23** `raw_response["delivery_acceptance"]` carries a machine-readable
  reason, the unverified id, and the verified ids.
  *Proof:* assertion on the dict shape in at least one D test.
- **C-24** Multi-chunk acceptance (row D6): all posted chunks are verified in
  order, the call fails on the first unverified chunk, and verification stops
  there.
  *Proof:* test asserting failure plus the exact `fetch_message` call sequence.
- **C-25** Thread targeting (row D8): the read-back is performed on the
  resolved thread object, not the parent channel.
  *Proof:* test with distinct parent/thread mocks asserting which one received
  `fetch_message`.
- **C-26** Row D7: driving the same acceptance failure through
  `_send_with_retry` leaves the total `channel.send` await count equal to the
  count from the single `send()` call — the base-class retry/plain-text
  fallback does not duplicate a message that already landed.
  *Proof:* named test asserting the await count before/after and that the
  returned result is the original acceptance failure.
- **C-27** `retryable` is `False` on acceptance failures.
  *Proof:* assertion in a D test.

## Group 6 — Non-regression (§7)

- **C-28** The pre-existing reply-reference retry behavior
  (`test_send_retries_without_reference_when_reply_target_is_deleted`) still
  passes, updated only to model the read-back that §5 now requires.
  *Proof:* recorded run of `tests/gateway/test_discord_send.py`.
- **C-29** Every other Discord test touched by this change is updated only to
  supply a read-back-capable channel mock; no assertion about pre-existing
  behavior is weakened or deleted.
  *Proof:* diff of the touched test files.
- **C-30** Forum sends, edits, and attachment senders keep their current
  semantics (they are out of scope per §1).
  *Proof:* diff shows no acceptance logic added to `_send_to_forum`,
  `_forum_post_file`, `_edit_overflow_split`, `_send_file_attachment`; the
  existing forum/edit tests still pass in the recorded run.

## Group 7 — Evidence discipline

- **C-31** The packet records the RED run: the new tests were executed against
  the pre-fix adapter and failed for the specified reason, before the
  implementation commit.
  *Proof:* the recorded pre-fix output in the session summary (assertion text
  quoted, not paraphrased).
- **C-32** The packet records the GREEN run of the same focused command plus
  the adjacent Discord suites, with pass/fail counts.
  *Proof:* recorded output.
- **C-33** The packet records which static checks were run
  (`python -m py_compile` / AST parse of each changed file) and their result.
  *Proof:* recorded output.
- **C-34** Any test that could NOT be executed in this environment (missing
  runner or plugin) is named explicitly, with the reason, rather than being
  silently omitted or reported as passing.
  *Proof:* an explicit "not executed" list in the summary.
- **C-35** No criterion in this contract is discharged by a claim of live
  Discord behavior, a deployment, or a CI run. A reviewer who has only the
  diff and the recorded output can decide every criterion above.
  *Proof:* self-evident from the criteria; violation is a contract defect.
