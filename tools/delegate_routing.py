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

WHAT IS WITHHELD is enumerated at MUTATING_TOOLS below, with the reasoning for
each -- including the ones considered and deliberately left available.

Two of those choices are worth stating up here because they have a visible cost.
`terminal` is withheld even though it is mostly used for reading, because
`echo > file` is an edit and leaving it would make the guarantee a slogan; the
price is that Hermes can no longer run `git status` to answer a question about
the working tree. And the first version of the list was a pattern guess that
missed `computer_use` -- "Universal desktop control" -- which could simply open an
editor and type. The list is enumerated from the registry now, not matched.

Reading and searching remain, which covers most of what an investigation needs.
Anything further is a reason to turn the switch off rather than to weaken it.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

CONFIG_KEY = "delegate_wave_route_repo_changes"

# The tools that can change a repository, or run something that can.
#
# Named explicitly rather than matched by pattern: a substring rule would quietly
# start withholding a future tool nobody considered, and quietly stop withholding
# one that gets renamed. The first version of this list WAS a pattern guess and it
# missed computer_use -- "Universal desktop control", which can simply open an
# editor and type. So this is enumerated from the registry, with the reasoning for
# each kept where the next person will look.
#
#   patch, write_file   direct edits
#   terminal            arbitrary shell; `echo > file` is an edit
#   execute_code        runs Python that can call any other tool -- an escape
#                       hatch around every other entry in this set
#   computer_use        drives the desktop; an editor is a GUI away
#   cronjob             schedules commands, so it is deferred shell access
#   browser_exec        "the `code` argument" -- arbitrary code driving a browser
#
# CONSIDERED AND DELIBERATELY LEFT AVAILABLE, so the next reader does not have to
# re-derive it:
#
#   browser_cdp     evaluates JS inside a page. It can cause a download, which
#                   lands in the browser's download directory rather than the
#                   working tree, and the person would see it. Withholding it
#                   would cost ordinary web work for a vector that cannot reach
#                   the repository unaided. One line to add if that judgement is
#                   ever shown wrong.
#   process         manages background processes STARTED by terminal. With
#                   terminal withheld there is nothing for it to have started.
#   skill_manage,   write to HERMES_HOME, not to the repository under discussion.
#   memory, kanban_*
#   delegate_task   children are AIAgents built through the same tool-assembly
#                   path, so they inherit this filter. Proved by regression
#                   rather than assumed -- see tests/test_delegate_routing.py.
MUTATING_TOOLS = frozenset({
    "patch",
    "write_file",
    "execute_code",
    "terminal",
    "computer_use",
    "cronjob",
    "browser_exec",
})

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


def filter_tools(tool_names: Iterable[str], config: Mapping[str, Any] | None = None) -> set[str]:
    """Remove the tools that could change a repository, when the switch is on.

    A no-op when it is off, which is the default -- this must not change what an
    existing install offers unless somebody asked for it.

    RAISES rather than degrades when the switch is on and something is wrong. See
    the caller: with this switch on, failing to filter is worse than failing to
    answer.
    """
    names = set(tool_names)
    if not routing_enabled(config):
        return names
    withheld = names & MUTATING_TOOLS
    if withheld:
        logger.info(
            "delegate-wave routing on: withholding %s", ", ".join(sorted(withheld))
        )
    return names - MUTATING_TOOLS


def status_label(config: Mapping[str, Any] | None = None) -> str:
    """What the person sees. Empty when off, so nothing is added to a normal session."""
    return "Delegate Wave ON" if routing_enabled(config) else ""
