#!/usr/bin/env python3
"""``/learn`` — build the standards-guided prompt that turns whatever the user
described into a reusable skill.

``/learn`` is open-ended. The user can point it at anything they can describe:
a directory of code, an API doc URL, a workflow they just walked the agent
through in this conversation, or pasted notes. This module builds ONE prompt
that instructs the live agent to:

  1. Gather the sources the user named, using the tools it already has
     (``read_file`` / ``search_files`` for dirs, ``web_extract`` for URLs, the
     current conversation for "what I just did", the user's text for pasted
     material).
  2. Author a single ``SKILL.md`` via ``skill_manage`` that follows the Hermes
     skill-authoring standards (description <=60 chars, the modern section
     order, Hermes-tool framing, no invented commands).

There is no separate distillation engine and no model-tool footprint: the
agent does the work with its existing toolset, so this works identically on
local, Docker, and remote terminal backends. Every surface (CLI ``/learn``,
gateway ``/learn``, the dashboard "Learn a skill" panel) calls
:func:`build_learn_prompt` and feeds the result to the agent as a normal turn.
"""

from __future__ import annotations

import re

# The house-style rules, distilled from AGENTS.md "Skill authoring standards
# (HARDLINE)" and the hermes-agent-dev new-skill salvage reference. Embedded in
# the prompt so the agent authors skills the way a maintainer would by hand.
_AUTHORING_STANDARDS = """\
Follow the Hermes skill-authoring standards exactly. These are the same
HARDLINE rules a maintainer enforces in review:

Frontmatter:
- name: lowercase-hyphenated, <=64 chars, no spaces.
- description: ONE sentence, **<=60 characters**, ends with a period. State the
  capability, not the implementation. No marketing words (powerful,
  comprehensive, seamless, advanced, robust). Do NOT repeat the skill name. If
  the description contains a colon, wrap the whole value in double quotes.
  This is the most-violated rule and it is NOT cosmetic: the system-prompt
  skill index truncates the description to 60 chars and loads it every
  session, so anything past char 60 is silently cut and never routes. After
  you write the description, COUNT the characters; if it is over 60, cut it
  down before saving — do not ship a sentence and hope.
    Good (<=60): `Search arXiv papers by keyword, author, or ID.`
    Bad (123):   `A comprehensive skill that lets the agent search arXiv for
                  academic papers using keywords, authors, and categories.`
- version: 0.1.0
- author: always the literal value `Hermes`. NEVER fill it from the host
  environment — the OS/login username (e.g. the `user=` line in your
  environment hints), git config, or any identity you can probe must not be
  written. Skills get shared and published, so an environment-derived name is
  a privacy leak the user never opted into; the skill names itself as Hermes.
- platforms: declare `[macos]`, `[linux]`, and/or `[windows]` IF the skill
  uses OS-bound primitives (osascript/apt/systemctl => the matching OS; /proc,
  os.setsid, signal.SIGKILL => linux; fcntl/termios => POSIX). Prefer fixing it
  cross-platform first (tempfile.gettempdir(), pathlib.Path, psutil); gate only
  when the dependency is genuinely platform-bound. Omit the field for portable
  skills.
- metadata.hermes.tags: a few Capitalized, Relevant, Tags.

Body section order (omit a section only if it genuinely has no content):
1. "# <Human Title>" then a 2-3 sentence intro: what it does, what it does NOT
   do, and the key dependency stance (e.g. "stdlib only").
2. "## When to Use" — bullet list of concrete trigger phrases.
3. "## Prerequisites" — exact env vars, install steps, credentials.
4. "## How to Run" — the canonical invocation, framed through Hermes tools.
5. "## Quick Reference" — a flat command/endpoint list, no narration.
6. "## Procedure" — numbered steps with copy-paste-exact commands.
7. "## Pitfalls" — known limits, rate limits, things that look broken but aren't.
8. "## Verification" — a single command/check that proves the skill worked.

Hermes-tool framing (this is what makes it a skill, not shell docs):
- Frame running scripts as "invoke through the `terminal` tool".
- Reference Hermes tools by name in backticks: `terminal`, `read_file`,
  `write_file`, `search_files`, `patch`, `web_extract`, `web_search`,
  `vision_analyze`, `browser_navigate`, `delegate_task`, `image_generate`,
  `text_to_speech`, `cronjob`, `memory`, `skill_view`, `execute_code`.
- Do NOT name shell utilities the agent already has wrapped: say `read_file`
  not cat/head/tail, `search_files` not grep/rg/find/ls, `patch` not sed/awk,
  `web_extract` not curl-to-scrape, `write_file` not echo>file or heredocs.
- Third-party CLIs (ffmpeg, gh, an SDK) are fine inside a script file, but the
  prose still frames them as "invoke through the `terminal` tool". If the
  skill needs an MCP server, name it and document its setup in Prerequisites.

Quality bar:
- Prefer exact commands, endpoint URLs, function signatures, and config keys
  that appear VERBATIM in the source. NEVER invent flags, paths, or APIs — if
  you didn't see it in the source, don't write it.
- Keep it tight and scannable: ~100 lines for a simple skill, ~200 for a
  complex one. Don't re-paste the source docs.
- Don't write a router/index/hub skill that only points at other skills.
- Larger scripts/parsers belong in a `scripts/` file (add via
  `skill_manage` write_file), referenced from SKILL.md by relative path — not
  inlined for the agent to re-type every run. References go in `references/`,
  templates in `templates/`."""


# Guidance appended when the /learn request points at a `hermes record`
# recording (a .json under HERMES_HOME/recordings/, or the word "recording").
# The recording JSON schema is documented in hermes_cli/record.py:
# {version, started_at, url, steps: [{t, type, selector, text?, value?, url?}]}.
_RECORDING_GUIDANCE = """\
RECORDING SOURCE — the request references a `hermes record` browser workflow
recording. Handle it like this:

- Read the recording JSON with `read_file`. Its shape is
  {version, started_at, url, steps: [{t, type, selector, text?, value?, url?}]}
  where step types are: click (selector + text), input (selector + final
  value), enter (selector), navigate (url), and manual (free-text narration).
- Reconstruct the workflow in HUMAN terms first: what the user was doing and
  why, not a selector-by-selector transcript. Collapse noise (stray clicks,
  focus changes) into the meaningful steps.
- SECRET PLACEHOLDERS: any step value like `{SECRET:name}` is a masked
  password or credential — the real value was never recorded. Ask the user
  which env var or secret reference should supply each placeholder (e.g.
  `$MYAPP_PASSWORD` from .env or `hermes secrets`). NEVER ask them to paste
  the secret inline, and NEVER write a literal secret into the skill.
- Author the skill so its Procedure section REPLAYS the flow with the
  existing browser tools: `browser_navigate` to the recorded URLs,
  `browser_snapshot` to find elements, `browser_click` / `browser_type` for
  the recorded clicks and inputs (using the recorded selectors and text as
  hints for locating the right elements, since ref IDs change per session),
  and `browser_press` for Enter submissions. Secret placeholders become
  env/secret lookups at replay time.
- Keep the recorded selectors in the skill as location hints, but tell the
  agent to prefer the live `browser_snapshot` refs when replaying."""


def _references_recording(req: str) -> bool:
    """True when a /learn request points at a `hermes record` recording."""
    low = (req or "").lower()
    if "recording" in low:
        return True
    # A .json path under a recordings/ directory also counts, even without
    # the keyword (e.g. "/learn ~/.hermes/recordings/checkout-....json").
    return bool(re.search(r"recordings[/\\][^\s]+\.json", low))


def build_learn_prompt(user_request: str) -> str:
    """Build the agent prompt for an open-ended ``/learn`` request.

    Args:
        user_request: the free-text the user gave after ``/learn`` — a
            description of the workflow, paths, URLs, or "what I just did".

    Returns:
        A complete instruction the agent runs as a normal turn. The agent
        gathers the described sources with its existing tools and authors the
        skill via ``skill_manage``.
    """
    req = (user_request or "").strip()
    if not req:
        req = (
            "the workflow we just went through in this conversation — review "
            "the steps taken and distill them into a reusable skill"
        )

    recording_block = ""
    if _references_recording(req):
        recording_block = f"{_RECORDING_GUIDANCE}\n\n"

    return (
        "[/learn] The user wants you to learn a reusable skill from the "
        "request below, and save it.\n\n"
        f"THE REQUEST:\n{req}\n\n"
        "The request is open-ended and may mix two kinds of content, in any "
        "order: SOURCES to gather (directories, file paths, URLs, \"what we "
        "just did\", pasted notes) AND REQUIREMENTS that shape the skill "
        "(what to focus on, what to leave out, scope, naming, the angle to "
        "take). Treat EVERY part of the request as load-bearing. In "
        "particular, prose that comes after a path or link is NOT incidental "
        "— it is the user telling you what they want from that source. A "
        "request like `<url> focus on the auth flow, skip the deprecated "
        "endpoints` means: gather the URL AND honor \"focus on auth, skip "
        "deprecated\" as authoring requirements. Never fetch the first source "
        "and ignore the rest.\n\n"
        f"{recording_block}"
        "Do this:\n"
        "1. Gather every source the user named, using the tools you already "
        "have — `read_file`/`search_files` for local files or directories, "
        "`web_extract` for URLs, the current conversation history if they "
        "referred to something you just did, and the text they pasted as-is. "
        "If the request is ambiguous about scope, make a reasonable choice "
        "and note it; do not stall.\n"
        "1b. Apply every requirement, focus, and constraint in the request to "
        "the skill you author — these govern what the SKILL.md covers and "
        "emphasizes, not just which sources you read.\n"
        "2. Author ONE SKILL.md and save it with the `skill_manage` tool "
        "(action=\"create\"). Pick a sensible category. If the procedure needs "
        "a non-trivial script, add it under the skill's `scripts/` with "
        "`skill_manage` write_file and reference it by relative path.\n\n"
        f"{_AUTHORING_STANDARDS}\n\n"
        "When done, tell the user the skill name, its category, and a "
        "one-line summary of what it captured."
    )
