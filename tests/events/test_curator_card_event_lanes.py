"""Keep the curator's agent cards honest about which bus events they produce.

``~/.hermes/profiles/curator/workspace/heartbeat_bootstrap.py`` holds ``AGENTS``,
one card per JobFlow agent, and each card's ``emits`` list is rendered verbatim
into that agent's HEARTBEAT.md (``:609``). The card text describes behaviour that
lives in THIS repo — the mirror set in ``events/producers/mailbox_watcher.py`` and
the ``_translate`` chain in ``events/subscribers/mailbox_translator.py`` — and
nothing links the two. An edit here silently invalidates prose there.

It has happened twice, both times in the same direction (agent-src moved, the
card did not):

* ``a4e6ade172`` made ``SUBMIT_RESULT`` the producer of ``application_submitted``
  and deleted the ``SUBMIT_REQUEST`` branch, which handed ``application_ready``
  to ``DRY_RUN_COMPLETE``. The applier card kept crediting ``SUBMIT_CONFIRM``.
* A 2026-08-19 audit of all eleven cards then found the same omission in four
  more (scout, sentinel, tracker, notifier) plus a stale ``telegram_topic``.

That is the same drift class ``EventType``'s own docstring records for the
``EVENT_TYPE_EMOJI`` dict — four occurrences before it was made unrepresentable.
This one cannot be made unrepresentable: the cards are prose in another repo. So
it stays a check, in the spirit of that docstring's note about the routing table.

WHY THE TEST LIVES HERE AND NOT IN ``~/.hermes``
------------------------------------------------
Both incidents began with an agent-src commit. A check on the ~/.hermes side —
a pre-commit hook, or that repo's own suite — only fires when someone edits the
card, which is the moment the card is already being corrected. Failing in the
suite the translator's author is running is the whole point.

WHAT IS CHECKED
---------------
``emits``. The cards already carry latent structure: UPPERCASE tokens are
mailbox message types, lowercase tokens are bus event names. Against that:

1. COMPLETENESS — a message type a card claims to emit, which the translator
   maps, must have its event name(s) written in that card.
2. NO PHANTOM — an event name written in a card must be producible from a
   message that card claims, or be a known non-mailbox producer.

``telegram_topic``. Truth is ``AGENT_TOPIC_MAP`` in ``events/routing_policy.py``
— imported, not AST-walked, because it is a plain dict literal rather than the
if/elif chain that forced the walk above. The rule is CONTAINMENT, not equality,
and the difference is the whole design:

    AGENT_TOPIC_MAP governs ``EventType.AGENT_ITERATION`` ALONE. It is read in
    one ``elif`` at ``routing_policy.py:493-495``; it is not the topic for an
    agent's domain events.

So a card's ``telegram_topic`` string legitimately carries two things at once —
the agent-iteration topic from the map, PLUS per-event domain routes. Two cards
do exactly that today and are correct: the applier's adds "application_blocked
is ACT-tier and routes to action_required", the notifier's adds "digest_generated
is TRACE-tier and routes to scribe_daily". An equality assertion would fail both.
Containment catches the defect that actually happened (curator reading "(n/a)"
while the map carries an explicit entry) without touching them. It deliberately
does NOT police the extra routes: their truth is the per-EventType spec table,
a different invariant.

``listens``. Truth is ``ROUTES`` in ``jobflow_dispatch/contracts.py``:
the ``(message type, destination) -> activity IDs`` table that decides which
agent a mailbox write actually activates. Four destinations appear there
(matcher, tailor, applier, researcher — the last has no card), so the rule is
small, and it runs ONE WAY ONLY: every route into an agent must be named in that
agent's ``listens``; a ``listens`` entry with no route is NOT a violation.

That asymmetry is deliberate. ``listens`` describes what an agent reads, and an
agent legitimately reads mailbox messages that wake nobody — the tracker's
inbound ``PIPELINE_UPDATE`` is drained on its own cron tick, not by a wake. Only
the forward direction is a drift claim: a wake target added or renamed here
leaves the card describing an agent that no longer stirs for it.

The prose is why this is containment and not a parse. Those entries carry
observation counts ("PIPELINE_UPDATE (~1770 observed: matcher 1486...)"), which
no structured read survives; asking only whether the token appears sidesteps them
entirely.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

from events.routing_policy import AGENT_TOPIC_MAP, JOBFLOW
from events.schema import EventType

_REPO = Path(__file__).resolve().parents[2]
_TRANSLATOR = _REPO / "events" / "subscribers" / "mailbox_translator.py"
_WATCHER = _REPO / "events" / "producers" / "mailbox_watcher.py"
_CONTRACTS = _REPO / "jobflow_dispatch" / "contracts.py"

# The consumer lives in the sibling repo. Resolved from $HOME rather than a
# relative path because agent-src is checked out at several paths (the shared
# tree plus every worktree) while ~/.hermes is a fixed location.
_CARDS = (
    Path.home() / ".hermes" / "profiles" / "curator" / "workspace"
    / "heartbeat_bootstrap.py"
)

# Event types with a producer outside the mailbox lane. A card may legitimately
# name these without naming a message type that yields them. Both verified
# 2026-08-19 by enumerating every ``EventType.<NAME>`` reference in the repo —
# do the same before adding a third, because an unverified entry here is a hole
# in rule 2, not a convenience.
NON_MAILBOX_PRODUCERS: Dict[str, str] = {
    "stage_transition": (
        "pipeline_state/manager.py emits it on the canonical state write, so the "
        "tracker card names it without owning a PIPELINE_UPDATE lane (that "
        "message is inbound to the tracker, written by matcher/applier/operator)"
    ),
}
# Only events a translator branch can ALSO produce need an entry here. One that
# no branch produces at all is out of rule 2's scope by construction (see
# ``violations``), which already covers digest_generated (in-gateway
# DigestComposer) and the never-emitted devflow.* schema types. Adding them here
# would be dead config that reads as load-bearing.

# Message types whose branch is gated on message CONTENT rather than on the type
# alone, so the events are possible but not implied. Naming them stays optional
# under rule 1; rule 2 still validates them when a card does.
#
# NOTIFICATION only yields interview_signal/offer_signal when the body matches
# the translator's patterns. Without this, devflow — which emits NOTIFICATION as
# its standup summary — would be required to name two interview/offer events it
# will realistically never produce.
CONDITIONAL_LANES: Set[str] = {"NOTIFICATION"}

# Agents whose card deliberately does NOT name its AGENT_TOPIC_MAP topic.
#
# ``main`` (Jaum) reads "all (Jaum can post to any topic via send_message tool)".
# Its map entry is jobflow_firehose, and that entry is real — its agent_iteration
# events do land there — but the card's field is describing the send_message
# tool's reach, not the iteration lane, and rewriting it to satisfy this check
# would make the card less true. Exempted explicitly rather than by weakening the
# rule, in the same spirit as CONDITIONAL_LANES and NON_MAILBOX_PRODUCERS: a hole
# that is named is a hole that can be reviewed.
TOPIC_EXEMPT_AGENTS: Dict[str, str] = {
    "main": (
        "card documents send_message's any-topic reach rather than the "
        "agent_iteration lane; AGENT_TOPIC_MAP['main'] is jobflow_firehose"
    ),
}

# Positive controls for the AST walk. A restructured ``_translate`` that yields
# an empty map would otherwise satisfy every assertion below vacuously.
_ANCHOR_MESSAGE_TYPES = {"SUBMIT_RESULT", "PIPELINE_UPDATE", "SCORE_RESULT"}
_MIN_TRANSLATED_MESSAGE_TYPES = 10

_UPPERCASE_TOKEN = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")

_ALL_TYPE_STRINGS = {event.type_string for event in EventType}


# ----------------------------------------------------------------- extraction

def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _assigned_value(tree: ast.Module, name: str) -> Any:
    """literal_eval a module-level assignment, annotated or not."""
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign):
            target, value = node.targets[0], node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == name and value is not None:
            return ast.literal_eval(value)
    raise AssertionError(f"{name} not found as a module-level assignment")


def _methods(tree: ast.Module) -> Dict[str, ast.FunctionDef]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


def _event_names(node: ast.AST) -> Set[str]:
    """Every ``EventType.<NAME>`` referenced anywhere under ``node``."""
    found = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "EventType"
        ):
            found.add(child.attr)
    return found


def _self_calls(node: ast.AST) -> Set[str]:
    """Every ``self._helper(...)`` invoked under ``node``."""
    found = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            found.add(func.attr)
    return found


def _message_type_constant(test: ast.AST) -> str | None:
    """Return X from a ``message_type == "X"`` comparison, else None."""
    if not isinstance(test, ast.Compare):
        return None
    if not (isinstance(test.left, ast.Name) and test.left.id == "message_type"):
        return None
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return None
    right = test.comparators[0]
    if isinstance(right, ast.Constant) and isinstance(right.value, str):
        return right.value
    return None


def translator_map() -> Dict[str, Set[str]]:
    """message type -> set of event ``type_string`` the translator yields for it.

    Follows one level of ``self._helper(...)`` indirection. That is not an
    optimisation: ``SUBMIT_RESULT`` and ``FOLLOWUP_ALERT`` hold NO ``EventType``
    reference in their own branch — they delegate to ``_submit_result_emissions``
    and ``_followup_emissions``. A walk that stopped at the branch body would map
    both to the empty set, which silently disables rule 1 for them AND makes rule
    2 report ``application_submitted`` on the applier card as a phantom.
    """
    tree = _module(_TRANSLATOR)
    methods = _methods(tree)
    translate = methods.get("_translate")
    assert translate is not None, f"_translate not found in {_TRANSLATOR}"

    by_member_name = {event.name: event.type_string for event in EventType}
    mapping: Dict[str, Set[str]] = {}

    for node in ast.walk(translate):
        if not isinstance(node, ast.If):
            continue
        message_type = _message_type_constant(node.test)
        if message_type is None:
            continue

        # The branch body only — never node.orelse, which is the next elif.
        members: Set[str] = set()
        for statement in node.body:
            members |= _event_names(statement)
            for helper in _self_calls(statement):
                target = methods.get(helper)
                if target is not None:
                    members |= _event_names(target)

        resolved = {by_member_name[m] for m in members if m in by_member_name}
        mapping.setdefault(message_type, set()).update(resolved)

    return mapping


def mirrored_message_types() -> Set[str]:
    """The gate: a type absent here never reaches the bus, so its branch is dead."""
    return set(_assigned_value(_module(_WATCHER), "MIRRORED_MESSAGE_TYPES"))


def wake_table() -> Dict[str, Set[str]]:
    """agent -> message types whose arrival activates that agent.

    ``ROUTES`` is keyed by a ``(message_type, destination)``
    TUPLE, which ``_assigned_value``'s literal_eval handles directly — no AST
    walk of a control-flow chain is needed here either.
    """
    raw = _assigned_value(_module(_CONTRACTS), "ROUTES")
    by_agent: Dict[str, Set[str]] = {}
    for (message_type, destination) in raw:
        by_agent.setdefault(destination, set()).add(message_type)
    return by_agent


def agent_cards() -> Dict[str, Dict[str, Any]]:
    return _assigned_value(_module(_CARDS), "AGENTS")


# -------------------------------------------------------------------- checker

def _emits_text(card: Dict[str, Any]) -> str:
    return " ".join(str(item) for item in card.get("emits", []) or [])


def _mentions(name: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


def violations(
    cards: Dict[str, Dict[str, Any]],
    mapping: Dict[str, Set[str]],
) -> List[str]:
    """Return one human-readable line per drifted card entry.

    Pure so the arm tests below can drive it with synthetic cards — a guard that
    has never been observed failing is not a guard.
    """
    found: List[str] = []

    # Rule 2 only judges events the mailbox lane can produce SOMEWHERE. Widening
    # it to every EventType produced two false positives on correct cards:
    #   * 'mailbox_message' is an EventType, and cards legitimately write
    #     "mailbox_message(scout->tracker)" to mean the raw mirrored message —
    #     which the WATCHER emits, not the translator.
    #   * devflow's card names 'devflow.run_started' precisely to record that it
    #     has NO producer and has never been emitted. Rule 2 read the disclaimer
    #     as a claim.
    # Neither is a mis-credit. Scoping to lane events keeps the rule pointed at
    # what it exists for, and still catches the case that matters: an event whose
    # branch was deleted stays in scope as long as any other branch yields it,
    # so a card left crediting the removed message is flagged.
    lane_events: Set[str] = set()
    for produced in mapping.values():
        lane_events |= produced

    for agent in sorted(cards):
        text = _emits_text(cards[agent])
        claimed = set(_UPPERCASE_TOKEN.findall(text))

        producible: Set[str] = set()
        for message_type in claimed:
            producible |= mapping.get(message_type, set())

        # 1. completeness
        for message_type in sorted(claimed):
            if message_type in CONDITIONAL_LANES:
                continue
            for event in sorted(mapping.get(message_type, set())):
                if not _mentions(event, text):
                    found.append(
                        f"{agent}: card claims to emit {message_type}, which the "
                        f"translator turns into '{event}', but the card's emits "
                        f"entry never names '{event}'. Add it in "
                        f"{_CARDS} (the sibling repo)."
                    )

        # 2. no phantom
        for event in sorted(lane_events):
            if not _mentions(event, text):
                continue
            if event in producible or event in NON_MAILBOX_PRODUCERS:
                continue
            found.append(
                f"{agent}: card names bus event '{event}', but no message type it "
                f"claims produces it and it is not in NON_MAILBOX_PRODUCERS. "
                f"Either its translator branch was removed, or the card is "
                f"crediting the wrong producer. Check {_CARDS}."
            )

    return found


def _topic_text(card: Dict[str, Any]) -> str:
    return str(card.get("telegram_topic") or "")


def topic_violations(
    cards: Dict[str, Dict[str, Any]],
    topic_map: Dict[str, str],
) -> List[str]:
    """One line per card whose ``telegram_topic`` omits its AGENT_ITERATION lane.

    Containment, not equality — see the module docstring. Pure for the same
    reason ``violations`` is: the arm tests below drive it in both directions.

    An agent absent from the map is not unrouted; ``routing_policy`` falls back
    to ``AGENT_TOPIC_MAP.get(agent, JOBFLOW)``, so the fallback is what its card
    must name, and that path is armed too.
    """
    found: List[str] = []

    for agent in sorted(cards):
        if agent in TOPIC_EXEMPT_AGENTS:
            continue
        expected = topic_map.get(agent, JOBFLOW)
        text = _topic_text(cards[agent])
        if _mentions(expected, text):
            continue
        found.append(
            f"{agent}: AGENT_ITERATION for this agent routes to '{expected}' "
            f"(events/routing_policy.py AGENT_TOPIC_MAP"
            + ("" if agent in topic_map else ", via the JOBFLOW fallback for "
               "agents with no entry")
            + f"), but the card's telegram_topic reads {text!r} and never names "
            f"it. Fix the card in {_CARDS} (the sibling repo), or — if the field "
            f"is deliberately describing something else — add {agent!r} to "
            f"TOPIC_EXEMPT_AGENTS here with the reason."
        )

    return found


def _listens_text(card: Dict[str, Any]) -> str:
    return " ".join(str(item) for item in card.get("listens", []) or [])


def wake_violations(
    cards: Dict[str, Dict[str, Any]],
    wake: Dict[str, Set[str]],
) -> List[str]:
    """One line per agent whose ``listens`` omits a message type that wakes it.

    One-directional by design — see the module docstring. Pure, like the other
    two checkers, so the arm tests can drive it.
    """
    found: List[str] = []

    for agent in sorted(cards):
        text = _listens_text(cards[agent])
        for message_type in sorted(wake.get(agent, set())):
            if _mentions(message_type, text):
                continue
            found.append(
                f"{agent}: ROUTES in {_CONTRACTS} activates this "
                f"agent on {message_type}, but the card's listens "
                f"entry never names it. Either the wake target is new and the "
                f"card in {_CARDS} has not caught up, or the agent no longer "
                f"reads it and the wake entry is stale."
            )

    return found


# ---------------------------------------------------------------- skip policy

_hermes_root_present = (Path.home() / ".hermes").is_dir()
_cards_present = _CARDS.is_file()

_requires_cards = pytest.mark.skipif(
    not _cards_present,
    reason=f"curator cards not present at {_CARDS} (agent-src checked out standalone)",
)


def test_cards_exist_whenever_the_hermes_tree_does():
    """Distinguish "not on this box" from "the file moved".

    Without this, renaming or relocating heartbeat_bootstrap.py turns every
    check below into a silent skip — the guard disables itself and stays green.
    """
    if not _hermes_root_present:
        pytest.skip("no ~/.hermes on this machine")
    assert _cards_present, (
        f"~/.hermes exists but the curator cards are not at {_CARDS}. If the file "
        f"moved, update _CARDS here; otherwise this guard is silently disabled."
    )


# ----------------------------------------------------------- positive controls

def test_translator_map_is_not_vacuous():
    mapping = translator_map()
    assert len(mapping) >= _MIN_TRANSLATED_MESSAGE_TYPES, (
        f"only {len(mapping)} translated message types found in {_TRANSLATOR}; "
        f"the if/elif chain was probably restructured and this walk no longer "
        f"reads it. Fix the walk — do not lower the floor."
    )
    missing = sorted(_ANCHOR_MESSAGE_TYPES - set(mapping))
    assert not missing, f"anchor message types missing from the walk: {missing}"


def test_helper_indirection_is_followed():
    """SUBMIT_RESULT delegates; a walk that missed it would fail open."""
    mapping = translator_map()
    assert mapping["SUBMIT_RESULT"] == {"application_submitted", "application_failed"}
    assert mapping["FOLLOWUP_ALERT"] == {"followup_due"}


def test_no_translator_branch_is_dead_code():
    """A branch for an unmirrored type can never fire.

    This is how SUBMIT_RESULT sat unreachable: the type was missing from the
    mirror set, so a translator branch for it would have been dead code.
    """
    unreachable = sorted(set(translator_map()) - mirrored_message_types())
    assert not unreachable, (
        f"{_TRANSLATOR} has branches for message types absent from "
        f"MIRRORED_MESSAGE_TYPES in {_WATCHER}, so they can never fire: "
        f"{unreachable}"
    )


@_requires_cards
def test_all_agent_cards_are_present():
    cards = agent_cards()
    assert len(cards) >= 11, f"expected at least 11 agent cards, found {sorted(cards)}"


# ------------------------------------------------------------- the live check

@_requires_cards
def test_agent_cards_name_the_events_they_produce():
    found = violations(agent_cards(), translator_map())
    assert not found, "curator agent cards have drifted:\n  " + "\n  ".join(found)


@_requires_cards
def test_agent_cards_name_their_agent_iteration_topic():
    found = topic_violations(agent_cards(), AGENT_TOPIC_MAP)
    assert not found, (
        "curator agent cards have drifted from AGENT_TOPIC_MAP:\n  "
        + "\n  ".join(found)
    )



@_requires_cards
def test_agent_cards_name_the_messages_that_wake_them():
    found = wake_violations(agent_cards(), wake_table())
    assert not found, (
        "curator agent cards have drifted from the wake table:\n  "
        + "\n  ".join(found)
    )



# ------------------------------------------------------------------ arm tests

def _fake_mapping() -> Dict[str, Set[str]]:
    return {
        "SCOUT_DISCOVERY": {"job_discovered"},
        "SUBMIT_RESULT": {"application_submitted", "application_failed"},
        "NOTIFICATION": {"interview_signal", "offer_signal"},
    }


def test_checker_catches_a_missing_event_name():
    """The exact shape of the two real incidents."""
    cards = {"scout": {"emits": ["SCOUT_DISCOVERY", "mailbox_message(scout->tracker)"]}}
    found = violations(cards, _fake_mapping())
    assert len(found) == 1
    assert "job_discovered" in found[0]


def test_checker_passes_when_the_event_is_named():
    cards = {"scout": {"emits": ["SCOUT_DISCOVERY (-> job_discovered, one per job)"]}}
    assert violations(cards, _fake_mapping()) == []


def test_checker_catches_a_phantom_event():
    """A card still crediting an event whose branch was deleted."""
    cards = {"main": {"emits": ["SUBMIT_CONFIRM (-> application_submitted)"]}}
    found = violations(cards, _fake_mapping())
    assert len(found) == 1
    assert "application_submitted" in found[0]


def test_conditional_lane_is_not_required_to_name_its_events():
    """devflow emits NOTIFICATION without ever meaning interview/offer."""
    cards = {"devflow": {"emits": ["NOTIFICATION(devflow->main) - the standup"]}}
    assert violations(cards, _fake_mapping()) == []


def test_conditional_lane_is_still_validated_when_named():
    """Opting out of rule 1 must not opt out of rule 2."""
    cards = {"notifier": {"emits": ["NOTIFICATION (-> interview_signal)"]}}
    assert violations(cards, _fake_mapping()) == []

    bogus = {"notifier": {"emits": ["NOTIFICATION (-> job_discovered)"]}}
    found = violations(bogus, _fake_mapping())
    assert len(found) == 1
    assert "job_discovered" in found[0]


def test_rule_two_ignores_events_no_translator_branch_produces():
    """Both false positives the first live run exposed, pinned.

    'mailbox_message' is a real EventType the WATCHER emits, and cards write it
    to mean the raw mirrored message. 'devflow.run_started' is named by devflow's
    card specifically to record that nothing produces it. Neither is a
    mis-credit, and flagging them would have made this guard cry wolf on two
    correct cards the day it landed.
    """
    cards = {
        "scout": {"emits": ["SCOUT_DISCOVERY (-> job_discovered)",
                            "mailbox_message(scout->tracker)"]},
        "devflow": {"emits": ["NOTIFICATION(devflow->main). The devflow.run_started "
                              "schema type has no producer and has never been emitted."]},
    }
    assert violations(cards, _fake_mapping()) == []


def test_rule_two_still_fires_when_another_branch_yields_the_event():
    """The scoping above must not blunt the case it exists for.

    A deleted branch leaves its event produced elsewhere, so a card still
    crediting the removed message stays in scope and is flagged.
    """
    cards = {"main": {"emits": ["SUBMIT_CONFIRM (-> application_submitted)"]}}
    found = violations(cards, _fake_mapping())
    assert len(found) == 1 and "application_submitted" in found[0]


def test_non_mailbox_producers_are_exempt_from_rule_two():
    cards = {"tracker": {"emits": ["STATUS_RESPONSE", "stage_transition"]}}
    assert violations(cards, _fake_mapping()) == []


def test_every_non_mailbox_producer_is_a_real_event_type():
    """An entry misspelt here would silently widen the rule-2 exemption."""
    unknown = sorted(set(NON_MAILBOX_PRODUCERS) - _ALL_TYPE_STRINGS)
    assert not unknown, f"NON_MAILBOX_PRODUCERS names non-existent events: {unknown}"


def test_every_conditional_lane_is_a_real_message_type():
    """Likewise: a misspelt lane would silently disable rule 1 for nothing."""
    unknown = sorted(CONDITIONAL_LANES - mirrored_message_types())
    assert not unknown, f"CONDITIONAL_LANES names unmirrored message types: {unknown}"


# ------------------------------------------------------ arm tests: topic rule

_FAKE_TOPIC_MAP = {"curator": "agents_memory", "applier": "jobflow_firehose"}


def test_topic_checker_catches_the_curator_defect():
    """The exact 2026-08-19 defect: an explicit map entry, a card saying (n/a)."""
    cards = {"curator": {"telegram_topic": "(n/a)"}}
    found = topic_violations(cards, _FAKE_TOPIC_MAP)
    assert len(found) == 1
    assert "agents_memory" in found[0]


def test_topic_checker_passes_when_the_topic_is_named():
    cards = {"curator": {"telegram_topic": "agents_memory (AGENT_TOPIC_MAP)"}}
    assert topic_violations(cards, _FAKE_TOPIC_MAP) == []


def test_topic_checker_catches_the_wrong_topic():
    """The stale-doc shape: a plausible sibling topic instead of the real one.

    GBrain's agent-iteration page carried exactly this, naming curator_digest
    for curator. Word-boundary matching is what makes such a near-miss fail
    rather than pass on a prefix: 'critic' is not satisfied by 'critic_proposals'.
    """
    cards = {"curator": {"telegram_topic": "curator_digest (AGENT_TOPIC_MAP)"}}
    found = topic_violations(cards, _FAKE_TOPIC_MAP)
    assert len(found) == 1 and "agents_memory" in found[0]


def test_topic_checker_allows_extra_per_event_routes():
    """Pins the two correct cards an equality assertion would have failed.

    AGENT_TOPIC_MAP is the AGENT_ITERATION lane only, so a card that also
    documents a domain event's route is more informative, not wrong.
    """
    cards = {
        "applier": {"telegram_topic": (
            "jobflow_firehose (routing_policy.AGENT_TOPIC_MAP); "
            "application_blocked is ACT-tier and routes to action_required"
        )},
    }
    assert topic_violations(cards, _FAKE_TOPIC_MAP) == []


def test_topic_checker_uses_the_jobflow_fallback_for_unmapped_agents():
    """``AGENT_TOPIC_MAP.get(agent, JOBFLOW)`` — armed in both directions."""
    absent = {"newcomer": {"telegram_topic": "agents_memory"}}
    found = topic_violations(absent, _FAKE_TOPIC_MAP)
    assert len(found) == 1 and JOBFLOW in found[0]

    correct = {"newcomer": {"telegram_topic": f"{JOBFLOW} (no entry: falls back)"}}
    assert topic_violations(correct, _FAKE_TOPIC_MAP) == []


def test_topic_exempt_agents_are_skipped():
    """Without the exemption main's honest 'all (...)' card would flag."""
    cards = {"main": {"telegram_topic": "all (Jaum can post to any topic)"}}
    assert topic_violations(cards, _FAKE_TOPIC_MAP) == []

    # The exemption is keyed on the agent, not on that wording: the same text
    # under another agent is still a violation.
    other = {"applier": {"telegram_topic": "all (Jaum can post to any topic)"}}
    assert len(topic_violations(other, _FAKE_TOPIC_MAP)) == 1


def test_topic_checker_treats_a_missing_field_as_a_violation():
    """A card that drops telegram_topic entirely must not pass by absence."""
    assert len(topic_violations({"curator": {}}, _FAKE_TOPIC_MAP)) == 1


@_requires_cards
def test_topic_exempt_agents_are_real_cards():
    """A renamed agent would leave a silent hole in the rule."""
    unknown = sorted(set(TOPIC_EXEMPT_AGENTS) - set(agent_cards()))
    assert not unknown, (
        f"TOPIC_EXEMPT_AGENTS names agents with no card: {unknown}. Drop them — "
        f"a stale exemption exempts nothing and hides that the rule shrank."
    )


def test_agent_topic_map_is_not_vacuous():
    """Guard the guard's own truth source.

    'curator' is the anchor because its entry is the one the defect denied
    existed, and it is the only card whose topic is not the JOBFLOW default —
    an emptied or defaulted map would leave the live check above trivially
    satisfiable for the other ten.
    """
    assert len(AGENT_TOPIC_MAP) >= 15, (
        f"AGENT_TOPIC_MAP has only {len(AGENT_TOPIC_MAP)} entries; it was "
        f"probably restructured. Fix this import — do not lower the floor."
    )
    assert AGENT_TOPIC_MAP.get("curator") == "agents_memory"
    assert any(topic != JOBFLOW for topic in AGENT_TOPIC_MAP.values())


# ------------------------------------------------------- arm tests: wake rule

_FAKE_WAKE = {
    "applier": {"SUBMIT_REQUEST", "SUBMIT_CONFIRM", "QUESTION_ANSWER"},
    "matcher": {"SCORE_REQUEST"},
}


def test_wake_checker_catches_a_missing_wake_message():
    cards = {"applier": {"listens": ["SUBMIT_REQUEST", "QUESTION_ANSWER"]}}
    found = wake_violations(cards, _FAKE_WAKE)
    assert len(found) == 1 and "SUBMIT_CONFIRM" in found[0]


def test_wake_checker_passes_through_the_prose_observation_counts():
    """The reason this is containment: the real entries are not parseable.

    Pinned in the shape the live cards actually use, so a future tightening to a
    structured read fails here rather than on someone's next edit.
    """
    cards = {"matcher": {"listens": [
        "SCORE_REQUEST (~1770 observed: matcher 1486, sentinel 284) - the scoring ask",
    ]}}
    assert wake_violations(cards, _FAKE_WAKE) == []


def test_wake_checker_ignores_reads_that_wake_nobody():
    """One-directional: a listens entry with no wake target is not drift.

    The tracker reads inbound PIPELINE_UPDATE on its own cron tick. Flagging
    that would demand a wake entry for a message deliberately not given one.
    """
    cards = {"tracker": {"listens": ["PIPELINE_UPDATE (inbound, drained on tick)"]}}
    assert wake_violations(cards, _FAKE_WAKE) == []


def test_wake_checker_treats_a_missing_field_as_a_violation():
    assert len(wake_violations({"matcher": {}}, _FAKE_WAKE)) == 1


def test_wake_table_is_not_vacuous():
    """Positive control for the extraction, matching the translator walk's."""
    wake = wake_table()
    assert wake.get("applier", set()) >= {"SUBMIT_REQUEST", "SUBMIT_CONFIRM"}
    assert wake.get("matcher") == {"SCORE_REQUEST"}
