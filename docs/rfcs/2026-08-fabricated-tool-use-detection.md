# Fabricated tool-use detection in `run_conversation()`

**Status: Implemented** (branch `feat/fabricated-tool-use-detection`).

## Problem

Under certain conditions (observed live: a search backend that's registered as "available" but
unreachable, with no keyless fallback), a completed turn (`finish_reason: "stop"` or equivalent,
normal token usage, no error) can return text that either:

1. Leaks a raw tool-call-shaped JSON fragment as prose — e.g.
   `{"arguments": {"query": "local LLM inference"}, "name": "web_search"}` — with no real
   `tool_calls` anywhere in the response, or
2. Fully fabricates a plausible-looking tool result with no structural marker at all — e.g.
   `"I'll perform a quick web search. Here's the result from my search: ... The current gold
   price is USD $... per troy ounce."` with placeholder-shaped content (`example.com`, `20XX`,
   `$...`) presented as fact, again with no real `tool_calls`.

Both are silent: no error, no warning, `status: completed`, plausible token usage. The only way
to tell the reply apart from a genuine answer is to read the response's structured `output`/
`tool_calls` field directly — something no caller does by default.

This is not a new failure class for this codebase. `agent/codex_responses_adapter.py:60-68,
1585-1611` already detects and recovers from one specific instance of it — a literal Harmony
`to=functions.<name>` leak token, only on the Codex/xAI/GitHub Responses transport, only when
`finish_reason` becomes `"incomplete"` after that adapter's own processing. Its own comment names
the exact risk: *"the parent sees a confident-looking summary with no audit trail (empty
tool_trace) and no tools actually ran."* A second, sibling mechanism — the "dropped tool call"
retry in `agent/conversation_loop.py:8102-8134` — recovers when `finish_reason == "tool_calls"`
but `tool_calls` came back empty. Neither covers a genuinely `stop`-style completion, which is
this bug's actual shape; the dropped-toolcall code's own surrounding comment says so explicitly
(`conversation_loop.py:8036`): *"finish_reason='stop' text finishes never enter this guard."*

## Scope

**In scope:** detecting, for *any* transport/provider and *any* tool, a completed turn whose
text either (a) contains a tool-call-shaped JSON fragment, or (b) affirmatively claims a
completed tool action with a result, while the turn's real `tool_calls` are empty — and
recovering via the same retry mechanism already proven for the dropped-toolcall case.

**Explicitly out of scope:**
- Replacing or modifying the existing Codex-specific leak-recovery path
  (`codex_responses_adapter.py:1585-1611`) or the existing dropped-toolcall path
  (`conversation_loop.py:8102-8134`). Both keep working exactly as they do today; this is a new,
  third sibling check, not a refactor of the other two.
- Detecting a hallucination that reads as ordinary, ungrounded prose with **no** structural JSON
  marker and **no** self-claiming language at all (e.g. a wrong fact stated as flatly as a
  correct one, no "I searched" framing). This would require classifying whether a given message
  "needed" a tool at all — the same fuzzier, higher-false-positive-risk problem an earlier,
  narrower fix in a downstream consumer of this framework explicitly deferred. A semantic
  verifier pass could close this gap in a future iteration; not attempted here.
- Fixing whatever root cause makes the model behave this way in the first place (resource
  contention, cold model load, a misconfigured backend). This treats the symptom at the
  detect-and-retry layer, matching how every existing sibling mechanism in this codebase already
  does the same.

## Design

### Where it lives

A new `elif` branch alongside the existing dropped-toolcall check, inside `run_conversation()`
in `agent/conversation_loop.py` (~line 8157). Both checks read from the same normalized,
provider-agnostic `assistant_message`/`finish_reason` produced once per turn by
`_transport.normalize_response(response)` (`conversation_loop.py:6637-6639`) — the one call site
all four transports (`anthropic`, `codex`, `chat_completions`, `bedrock`) funnel through
(`agent/transports/{name}.py`, each implementing the shared `NormalizedResponse` contract at
`agent/transports/types.py:89-109`). Placing the check here, rather than per-transport, means one
implementation covers every provider this framework supports, not just whichever one a future bug
happens to surface on.

Ordering: this new check is chained as an `elif` after the existing dropped-toolcall condition
(not an independent `if`), so if both conditions happen to be true for the same turn, the
existing, more specific check fires first and this one is skipped — no double-fire, no new
ambiguity in an already-working path.

### Detection: two independent signals

Both signals are gated on `not assistant_message.tool_calls` (falsy-uniform, matching how the
existing check at line 8041 already treats `None` vs `[]`) — a real tool call in this turn always
wins over either signal, exactly like the existing checks.

**1. Structural — JSON tool-call shape (`_FABRICATED_TOOL_USE_JSON_RE`).** Two independent
regexes, `"name"\s*:\s*"[^"]*"` and `"arguments"\s*:`, both required to match somewhere in the
text (order-independent, no single regex spanning both keys). This is tool-agnostic and
wording-agnostic by construction — it does not need to know what the leaked JSON's surrounding
prose says, only that the payload shape is present. Accepted false-positive class: legitimate
prose that quotes a tool-call-shaped JSON example (e.g. explaining an API schema) will also match
— the retry cost of a wrong-but-bounded re-prompt is judged cheaper than the complexity of
disambiguating intent here. (This mirrors a structural fix already shipped and verified in a
downstream consumer of this framework, ported here so every consumer benefits, not just that one.)

**2. Self-claim language (`_FABRICATED_TOOL_USE_PATTERNS`).** A small, curated, evidence-seeded
list of regexes for text that affirmatively claims a *completed* tool action with a *result* —
seeded from the one real captured case: *"here's the result from my search"*-style framing.
Deliberately narrow, matching only affirmative completed-action claims, not proposals ("let me
check that") or honest disclaimers ("I don't have a way to look that up right now") — the latter
is the opposite of the bug and must never be flagged. Documented in the code, the same way
`TOOL_CALL_LEAK_SIGNATURES`-style lists already are in this codebase's own conventions, as a
starting set that will need to grow from future real incidents — this is the tradeoff accepted
going in: the JSON check generalizes cleanly from shape and needs no maintenance; the language
check is inherently reactive and will need new entries as new wordings are observed live, same as
its prior-art equivalent already has needed twice.

### Refinement found during planning: a real earlier tool call must suppress this check

A genuine post-tool-call summary turn (`finish_reason="stop"`, no `tool_calls` in *that specific*
turn, since it's just delivering the final text) can legitimately contain the same self-claim
language the detector looks for — e.g. *"the search returned $2,650/oz"* while accurately
summarizing a real tool result from an earlier round in the same turn. The existing
dropped-toolcall check avoids this by construction (it's gated on `finish_reason == "tool_calls"`,
which a normal summary never has); this new check needs its own guard.

Fix: a new agent attribute, `agent._landed_real_tool_call_this_turn`, set `True` at the same point
`agent._dropped_toolcall_retries` already gets reset after a successful tool round
(`conversation_loop.py:~7234`), and reset to `False` alongside the other counters at genuine turn
end (`~8079`). The new check additionally requires
`not getattr(agent, "_landed_real_tool_call_this_turn", False)`.

### Second refinement, found during code review: the two retry budgets could compound

Because this check does not gate on `finish_reason` the way the dropped-toolcall check does, an
overlap case is possible: a stall with `finish_reason == "tool_calls"` and empty `tool_calls`,
whose narration text ALSO happens to match the fabrication pattern (a plausible variant of the
same provider-contract violation the dropped-toolcall check exists for). Without a fix, once the
dropped-toolcall check's own 3-retry budget is exhausted, control would fall through to this new
check, which — not gating on `finish_reason` — would grant 3 *more* retries, silently doubling the
effective bound to 6 for that one overlap case, contradicting the "bounded to 3" invariant stated
above.

Fix: the check additionally requires `finish_reason != "tool_calls"` — that value is squarely the
dropped-toolcall check's own territory (it's the condition that check exists for), so once its
budget is exhausted for that finish_reason, the turn now correctly falls through to genuine turn
end instead of getting a second budget from this check. This does not narrow the check's actual
target: `finish_reason="stop"` (and every other non-`"tool_calls"` value) — the gap this whole
design exists to close — is completely unaffected by the exclusion.

Accepted tradeoff: once any real tool call has landed anywhere in a turn's multi-round loop, later
text in that same turn is never checked again for fabrication, even if it's a distinct, unrelated
claim. Favors false-negative risk over false-positive risk here — consistent with this whole
design's stated posture — and a model that has already demonstrated it can call tools correctly
this turn is less likely (though not guaranteed) to then also fabricate something unrelated.

### Response: reuse existing recovery mechanics exactly

Same `continue`-based pattern as the dropped-toolcall block: flag the bad assistant turn
(`_fabricated_tool_use_nudge`), append it plus a new synthetic user nudge message
(`_FABRICATED_TOOL_USE_NUDGE_CONTENT`) to `messages`, set `agent._session_messages = messages`,
`final_response = None`, `continue` the outer loop. Capped at 3 attempts via a new counter,
`agent._fabricated_tool_use_retries` (matching the existing precedent's cap). On a successful
retry, both scaffolding messages are stripped from final persisted history via the existing
`_is_ephemeral_scaffolding` mechanism (`run_agent.py`), unchanged from how the dropped-toolcall
pair already does this. No new retry infrastructure is introduced.

### Third refinement, found in independent post-merge review: the two new counters/flag must also reset at turn START, not just turn end

`agent._dropped_toolcall_retries`, `agent._fabricated_tool_use_retries`, and `agent._landed_real_tool_call_this_turn`
were originally reset only at `run_conversation()`'s own "genuine turn end" fallthrough. That point is skipped by
any of the function's ~35 early-return paths — concretely, a user interrupt during the empty-response retry
backoff returns immediately without ever reaching it. A real tool call landing in one turn, followed by that
same turn exiting early via such a path, left `_landed_real_tool_call_this_turn` stuck `True` and silently
suppressed fabricated-tool-use detection for the *next*, unrelated turn on the same long-lived agent object —
defeating the exact bug class this whole design exists to catch.

Fix: also reset all three in `build_turn_context()` (`agent/turn_context.py`), the prologue every
`run_conversation()` call unconditionally passes through at its very start, alongside the dozen other
per-turn retry counters it already resets there. The turn-end reset stays in place too (harmless, idempotent) —
this is a second, more reliable reset point, not a replacement for the first.

## Testing

New file `tests/run_agent/test_fabricated_tool_use_recovery.py`, mirroring
`tests/run_agent/test_dropped_tool_call_recovery.py`'s exact fixture and mocking style. The
`loop_agent` fixture (mocked-`AIAgent` construction, same shape as `test_dropped_tool_call_recovery.py`'s
own) was found in post-merge review to already be duplicated verbatim across several files in this
directory — moved to `tests/run_agent/conftest.py` (which already exists as the designated home
for shared fixtures in this directory) rather than adding an Nth copy here.

Cases:

1. **True positive — JSON leak.** The real captured leak text
   (`{"arguments": {"query": "local LLM inference"}, "name": "web_search"}`), `tool_calls=None`,
   `finish_reason="stop"`. Asserts a retry fires (`call_count == 2`).
2. **True positive — self-claim fabrication.** The real captured fake-search-result text.
   Same assertion.
3. **True positive — a different tool.** A synthetic case using the JSON-shape signal with
   `"name": "terminal"` instead of `"web_search"`, proving the structural check is genuinely
   tool-agnostic, not accidentally coupled to search.
4. **True negative — clean correct reply.** An ordinary `finish_reason="stop"` answer with no
   JSON shape and no self-claim language. Mirrors the existing
   `test_clean_stop_text_turn_is_unaffected` almost exactly; must not trigger a retry
   (`call_count == 1`).
5. **True negative — honest disclaimer.** Text that explicitly says the model *can't* look
   something up right now. Must not trigger — this is the specific false-positive risk this
   design accepts responsibility for guarding against, made an explicit regression test rather
   than an assumption.
6. **True negative — real tool call with similar-sounding summary text.** A turn with real,
   populated `tool_calls` whose accompanying text happens to say "the search returned..." while
   summarizing a genuine result. Must not trigger, mirroring the existing
   `test_does_not_flag_a_real_function_call_even_if_leak_like_text_is_also_present`-equivalent
   guarantee already proven at the detection-shape level in the downstream consumer.
7. **Bounded retries.** Repeated fabricated responses cap at `call_count <= 4` (1 initial + 3
   retries), mirroring `test_persistent_dropped_tool_calls_are_bounded`.
8. **Ephemeral scaffolding.** The nudge pair does not survive into `result["messages"]` after a
   successful retry, mirroring `test_nudge_pair_is_ephemeral_scaffolding`.
9. **Flag does not leak across turns.** Added during implementation once the
   `_landed_real_tool_call_this_turn` refinement above was written: a real tool call in one
   `run_conversation()` call must not permanently suppress detection in a later, separate call on
   the same long-lived agent — proves the flag resets at genuine turn end, not just within a turn.
10. **The two retry budgets don't compound.** Added after code review caught the gap described in
    "Second refinement" above: a stall that is simultaneously `finish_reason == "tool_calls"` (with
    empty `tool_calls`) and fabrication-shaped must cap at 4 total calls (the dropped-toolcall
    check's own budget), not 7 (both budgets stacked).

`scripts/run_tests.sh tests/run_agent/` must stay green throughout, alongside the full existing
`test_dropped_tool_call_recovery.py` suite (unmodified, still passing — this is additive, not a
replacement).
