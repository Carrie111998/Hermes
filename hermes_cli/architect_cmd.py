"""Shared handler for the /architect prompt-architect slash command.

The command intentionally does not add a model tool or mutate the system prompt.
It rewrites the user's rough request into a normal agent turn that instructs the
current agent to behave as a prompt architect. This keeps prompt caching stable
and works uniformly across CLI and gateway surfaces.
"""

from __future__ import annotations

import shlex
import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class ArchitectCommandResult:
    """Result returned by the shared /architect command handler."""

    text: str
    agent_seed: str | None = None


_USAGE = """Usage: /architect [--fast|--deep] [rough request]

Turn a rough request into an agent-ready execution prompt, or start an adaptive
prompt-building interview when no request is provided.

Modes:
  /architect                   Start an adaptive guided interview.
  /architect <request>         Ask targeted clarifying questions first.
  /architect --fast <request>  Generate the optimized prompt immediately using explicit assumptions.
  /architect --deep <request>  Ask deeper scope/source/risk questions before generating the prompt.

After producing the prompt, Hermes should offer to run or build from it only after you agree.
"""

_VALID_MODES = {"default", "fast", "deep"}


def _parse_args(args: str) -> tuple[str | None, str, str | None]:
    """Return (mode, request, error)."""

    raw = (args or "").strip()
    if not raw:
        return None, "", None

    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        return None, "", f"Could not parse /architect arguments: {exc}"

    mode = "default"
    request_tokens: list[str] = []
    options_done = False
    for token in tokens:
        if not options_done and token in {"--fast", "-f"}:
            mode = "fast"
            continue
        if not options_done and token in {"--deep", "-d"}:
            mode = "deep"
            continue
        if not options_done and token == "--":
            options_done = True
            continue
        if not options_done and token.startswith("--"):
            return None, "", f"Unknown /architect option: {token}\n\n{_USAGE.strip()}"
        options_done = True
        request_tokens.append(token)

    request = " ".join(request_tokens).strip()
    if mode not in _VALID_MODES:  # defensive guard for future edits
        return None, "", f"Unknown /architect mode: {mode}"
    return mode, request, None


def _guided_interview_seed() -> str:
    """Build the agent seed for bare /architect guided-interview mode."""

    return textwrap.dedent(
        """
        You are acting as a prompt architect for Hermes/OpenClaw/Codex-style tool-using agents.

        Mode: adaptive interview
        The user invoked /architect without a rough request. Start a concise adaptive
        prompt-building interview instead of producing the final prompt immediately.

        Your job:
        1. Classify the eventual request type from the user's answers: coding,
           business operations, research, automation, data analysis, writing,
           personal productivity, strategy, or another category.
        2. Ask only questions that materially improve the final execution prompt.
        3. Prefer multiple-choice questions where useful, with an "Other" or
           free-form answer option when the provided choices may not fit.
        4. Adapt follow-up questions to the user's prior answers; do not run a
           rigid checklist if enough detail is already available.
        5. Once enough detail exists, produce a ready-to-run agent prompt, not just
           prettier wording.
        6. Optimize for a tool-using agent: include objective, context, scope,
           data sources to inspect, assumptions, deliverables, constraints /
           exclusions, verification criteria, and recommended first steps.
        7. Distinguish verified/provided facts from assumptions.
        8. After the optimized prompt is ready, offer follow-up actions: "run it",
           "build it", "save it", or "turn it into a recurring workflow".
        9. Do not execute the generated prompt or take follow-up actions until the
           user explicitly approves.

        Output rules:
        - Begin with 3-5 concise numbered interview questions.
        - Use multiple-choice format for questions with clear common options.
        - Keep each multiple-choice option short and selectable by letter or number.
        - If asking questions, ask the questions only, then wait.
        - If generating the final prompt, return a markdown execution brief with
          sections: Objective, Context, Scope, Data Sources, Assumptions, Required
          Output, Verification Criteria, Constraints / Exclusions, Recommended
          First Steps.
        - End the final prompt response by saying the user can reply "run it",
          "build it", "save it", or "turn it into a recurring workflow".
        """
    ).strip()


def _seed_for(mode: str, request: str) -> str:
    """Build the agent seed for the selected /architect mode."""

    if mode == "fast":
        mode_instruction = """
        Mode: fast
        Do not ask clarifying questions first unless the request is impossible or unsafe without one.
        Generate the optimized prompt immediately using reasonable assumptions.
        Clearly label those assumptions so the user can correct them.
        """
    elif mode == "deep":
        mode_instruction = """
        Mode: deep
        Ask deeper clarifying questions before generating the final optimized prompt.
        Cover scope, stakeholders, constraints, risks, privacy exclusions, data sources,
        desired output format, verification criteria, and what “done” means.
        Ask only questions that materially improve the resulting prompt.
        """
    else:
        mode_instruction = """
        Mode: default
        ask 3-7 targeted clarifying questions before generating the final optimized prompt.
        Do not generate the final optimized prompt yet unless the user has already provided enough detail.
        Focus on the few missing requirements that materially affect execution.
        """

    return textwrap.dedent(
        f"""
        You are acting as a prompt architect for Hermes/OpenClaw/Codex-style agents.

        Rough user request:
        {request}

        {textwrap.dedent(mode_instruction).strip()}

        Your job:
        1. Classify the request type: coding, business operations, research, automation,
           data analysis, writing, personal productivity, strategy, or another category.
        2. Identify missing requirements and ambiguity.
        3. Produce or prepare an agent-ready execution prompt, not just prettier wording.
        4. Optimize for a tool-using agent: include objective, context, scope, data sources
           to inspect, assumptions, deliverables, constraints/exclusions, verification
           criteria, and recommended first steps.
        5. Distinguish verified/provided facts from assumptions.
        6. After the optimized prompt is ready, offer to run or build from it, but do not
           start execution until the user explicitly agrees.

        Output rules:
        - If asking questions, ask concise numbered questions only, then wait.
        - If generating now, return a markdown execution brief with sections:
          Objective, Context, Scope, Data Sources, Assumptions, Required Output,
          Verification Criteria, Constraints / Exclusions, Recommended First Steps.
        - End by saying the user can reply “run it” or “build it” if they want Hermes
          to execute from that prompt.
        """
    ).strip()


def handle_architect_command(args: str) -> ArchitectCommandResult:
    """Handle /architect arguments and return an agent seed or usage/error text."""

    mode, request, error = _parse_args(args)
    if error:
        return ArchitectCommandResult(text=error)
    if not mode:
        seed = _guided_interview_seed()
        ack = "Prompt architect interview mode: starting an adaptive prompt-building interview…"
        return ArchitectCommandResult(text=ack, agent_seed=seed)
    if not request:
        return ArchitectCommandResult(text=_USAGE.strip())

    seed = _seed_for(mode, request)
    if mode == "fast":
        ack = "Prompt architect fast mode: generating an optimized prompt with explicit assumptions…"
    elif mode == "deep":
        ack = "Prompt architect deep mode: preparing deeper scope/source/risk questions…"
    else:
        ack = "Prompt architect mode: I’ll ask targeted clarifying questions before writing the final prompt…"
    return ArchitectCommandResult(text=ack, agent_seed=seed)
