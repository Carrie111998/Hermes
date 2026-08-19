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

WHAT IS CHECKED (``emits`` only)
--------------------------------
The cards already carry latent structure: UPPERCASE tokens are mailbox message
types, lowercase tokens are bus event names. Against that:

1. COMPLETENESS — a message type a card claims to emit, which the translator
   maps, must have its event name(s) written in that card.
2. NO PHANTOM — an event name written in a card must be producible from a
   message that card claims, or be a known non-mailbox producer.

``listens`` is deliberately out of scope: its truth is the wake table in
``jobflow_dispatch/contracts.py``, a different invariant, and those entries carry
observation counts that are prose by nature.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Dict, List, Set

import pytest

from events.schema import EventType

_REPO = Path(__file__).resolve().parents[2]
_TRANSLATOR = _REPO / "events" / "subscribers" / "mailbox_translator.py"
_WATCHER = _REPO / "events" / "producers" / "mailbox_watcher.py"

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
