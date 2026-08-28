#!/usr/bin/env python3
"""Whether repository changes go through delegate-wave, or Hermes makes them itself.

TWO SEPARATE QUESTIONS, AND CONFLATING THEM IS WHY THIS DID NOT EXIST BEFORE.

    is the repo registered with delegate-wave?   CAPABILITY -- where it may work
    is this switch on?                           POLICY -- whether to use it now

Registration is delegate-wave's business and it enforces its own. This module is
only the second question, and it is deliberately a switch rather than a
classifier: a "delegate the big ones" rule would have Hermes estimate effort
before it understands the task, get it wrong regularly, and fail silently -- half
an investigation, three edited files, and the walk-away property gone. A switch
has an answer; "is this task big?" does not.

WHY REMOVING TOOLS RATHER THAN INSTRUCTING.

Guidance alone is a suggestion a model may reason its way past, and the failure
mode is invisible until somebody reads a transcript. When the switch is on, the
tools that can mutate a repository are not offered at all -- the model cannot
call what it cannot see -- and the accompanying note explains the one route that
remains. That is what "mechanically disallowed" has to mean to be worth saying.

WHAT IS WITHHELD is whatever declares repo_access="write" at registration, plus
anything that declares nothing at all. There is no list of names here to fall out
of date; see the reasoning below _ALLOWED_WHEN_ROUTING.

One choice is worth stating up here because it has a visible cost. `terminal`
declares "write" even though it is mostly used for reading, because `echo > file`
is an edit and leaving it available would make the guarantee a slogan; the price
is that Hermes can no longer run `git status` to answer a question about the
working tree. The other cost is structural: an unclassified tool disappears while
the switch is on, which is the price of never silently offering a mutating one.

Reading and searching remain, which covers most of what an investigation needs.
Anything further is a reason to turn the switch off rather than to weaken it.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

CONFIG_KEY = "delegate_wave_route_repo_changes"

# HOW A TOOL IS JUDGED: BY WHAT IT DECLARES, NOT BY ITS NAME.
#
# This used to be MUTATING_TOOLS -- a frozenset of seven names. Enumerating beat
# pattern-matching (the first version was a pattern guess and it missed
# computer_use, "Universal desktop control", which can simply open an editor and
# type), but a name list has a failure mode that no amount of care fixes: it is
# correct only for the tools that existed when somebody last read it. The eighth
# mutating tool -- added upstream next month, or by a plugin this afternoon --
# is offered, silently, because nobody thought to add its name here.
#
# Every registered tool now declares repo_access at registration:
#
#   "write"            calling it can change the working tree from THIS
#                      process, or run something that can
#   "delegated_write"  it causes repository changes, but through the sanctioned
#                      supervised route rather than by editing anything here
#   "read"             reads repository contents, cannot change them
#   "none"             does not touch the repository at all
#
# "delegated_write" exists because calling delegate-wave's session_start "none"
# was a comfortable lie: it plainly causes a repository to change. What makes it
# allowable while the switch is on is not that it is harmless, it is that it is
# THE ROUTE THE SWITCH EXISTS TO FORCE. Saying that out loud keeps the vocabulary
# honest, and leaves room for a future tool that mutates directly to be
# classified "write" even though it lives on the same server.
#
# and this module asks the registry rather than consulting a list. A new
# mutating tool is withheld the day it appears, without being named anywhere.
#
# UNDECLARED MEANS WITHHELD. That is the load-bearing half.
#
# A tool with no repo_access -- a new built-in whose author did not classify it,
# a plugin, an MCP server's tool -- is treated as "write" and withheld while the
# switch is on. The alternative is a default of "harmless", which converts every
# oversight into a silent hole in the guarantee. Erring towards withholding costs
# a tool the model could have used and is visible immediately; erring the other
# way costs the guarantee and is invisible until a transcript shows Hermes
# editing a repository it was configured never to touch.
#
# The cost is real and deliberate: with the switch on, an unclassified tool
# disappears. tests/test_delegate_routing.py asserts that every registered
# built-in declares one, so that cost lands on whoever adds a tool, at the time
# they add it, rather than on the guarantee.
#
# MCP TOOLS declare through their server's config (repo_access on the server
# entry), because Hermes cannot know what a third-party tool does. Undeclared MCP
# servers are withheld under the same rule -- including, deliberately,
# delegate-wave's own: the server that is the sanctioned route still has to say
# so out loud rather than be special-cased by name.
_ALLOWED_WHEN_ROUTING = frozenset({"read", "none", "delegated_write"})
_VALID_ACCESS = frozenset({"write", "delegated_write", "read", "none"})


def _declared_access(name, registry_obj=None):
    """What one tool declared, normalised, or None when it declared nothing."""
    try:
        from tools.registry import repo_access_of
    except Exception:  # pragma: no cover - import cycle guard
        logger.exception("delegate routing: registry unavailable")
        return None
    value = repo_access_of(name, registry_obj)
    if value is None:
        return None
    if not isinstance(value, str):
        # A non-string declaration is a mistake, not a permission. Treated as
        # undeclared, which means withheld.
        logger.warning(
            "delegate routing: tool %r declared a non-string repo_access %r; "
            "treating as undeclared (withheld)", name, value,
        )
        return None
    normalised = value.strip().lower()
    if normalised not in _VALID_ACCESS:
        logger.warning(
            "delegate routing: tool %r declared unknown repo_access %r; "
            "treating as undeclared (withheld)", name, value,
        )
        return None
    return normalised


GUIDANCE = (
    "DELEGATE-WAVE ROUTING IS ON.\n"
    "\n"
    "Changes to this repository are made by delegate-wave, not by you. The tools that "
    "edit files or run commands are withheld for this reason, so attempting them is not "
    "an option you have.\n"
    "\n"
    "You remain the owner of the conversation. For a request that would modify the "
    "repository: understand what the person wants, then start the work with the "
    "delegate_wave tool `session_start`, describing the intent in your own words. Report "
    "back when it reports back. You may still read and search files freely to understand "
    "the request before delegating.\n"
    "\n"
    "If the person asks a question that changes nothing, answer it directly as usual."
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def routing_enabled(config: Mapping[str, Any] | None = None) -> bool:
    """Is the switch on? Defaults to OFF, so an existing install behaves as it did."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        except Exception:
            logger.debug("delegate routing: config unreadable, treating as off", exc_info=True)
            return False
    if not isinstance(config, Mapping):
        return False
    section = config.get("delegate_wave")
    if isinstance(section, Mapping) and "route_repo_changes" in section:
        return _as_bool(section.get("route_repo_changes"))
    return _as_bool(config.get(CONFIG_KEY))


def filter_tools(tool_names: Iterable[str], config: Mapping[str, Any] | None = None,
                 registry_obj: Any = None) -> set[str]:
    """Keep only the tools that declared they cannot change the repository.

    A no-op when the switch is off, which is the default -- this must not change
    what an existing install offers unless somebody asked for it.

    RAISES rather than degrades when the switch is on and something is wrong. See
    the caller: with this switch on, failing to filter is worse than failing to
    answer.
    """
    names = set(tool_names)
    if not routing_enabled(config):
        return names

    kept, withheld, undeclared = set(), set(), set()
    for name in names:
        access = _declared_access(name, registry_obj)
        if access is None:
            undeclared.add(name)
        elif access in _ALLOWED_WHEN_ROUTING:
            kept.add(name)
        else:
            withheld.add(name)

    if withheld:
        logger.info(
            "delegate-wave routing on: withholding %s (declared write)",
            ", ".join(sorted(withheld)),
        )
    if undeclared:
        # Named individually because this is the case a person has to act on:
        # either the tool should be classified, or the switch is costing them
        # something they wanted.
        logger.warning(
            "delegate-wave routing on: withholding %s (no repo_access declared; "
            "undeclared is treated as write)", ", ".join(sorted(undeclared)),
        )
    return kept


def status_label(config: Mapping[str, Any] | None = None) -> str:
    """What the person sees. Empty when off, so nothing is added to a normal session."""
    return "Delegate Wave ON" if routing_enabled(config) else ""
