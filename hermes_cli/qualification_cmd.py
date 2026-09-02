"""Observation-only implementation of ``hermes qualification``."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import re
from collections.abc import Sequence


SCENARIOS = ("clean", "existing")


_QUALIFICATION_PROBE_SENTINEL = "__qualification_probe_value__"
_MCP_BOUNDARY_PROBE_SENTINEL = "__qualification_mcp_boundary__"
_MCP_BOUNDARY_PROBE_NAME = "__qualification_mcp_name__"
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Keep command-boundary recognition local and import-free.  ``main`` has a
# matching built-in command table, but importing it here would cross the
# pre-bootstrap boundary this probe is intended to protect.
_TOP_LEVEL_COMMANDS = frozenset({
    "acp",
    "approvals",
    "auth",
    "backup",
    "bundles",
    "checkpoints",
    "claw",
    "completion",
    "computer-use",
    "config",
    "console",
    "cron",
    "curator",
    "dashboard",
    "serve",
    "debug",
    "doctor",
    "dump",
    "egress",
    "fallback",
    "gateway",
    "hooks",
    "import",
    "import-agent",
    "insights",
    "gui",
    "desktop",
    "kanban",
    "login",
    "logout",
    "logs",
    "lsp",
    "mcp",
    "memory",
    "migrate",
    "moa",
    "journey",
    "memory-graph",
    "learning",
    "model",
    "monitoring",
    "pairing",
    "pause",
    "peer",
    "pets",
    "plugins",
    "portal",
    "profile",
    "project",
    "proxy",
    "prompt-size",
    "resume",
    "send",
    "sessions",
    "setup",
    "skin",
    "skills",
    "slack",
    "status",
    "sync",
    "tools",
    "uninstall",
    "update",
    "webhook",
    "whatsapp",
    "whatsapp-cloud",
    "worktree",
    "chat",
    "secrets",
    "security",
    "browser",
    "verify",
    "qualification",
    "help",
})


def _build_qualification_probe_parser() -> argparse.ArgumentParser:
    """Build a parser that mirrors global CLI option consumption.

    This parser is intentionally only a probe: it reuses the top-level
    parser's real option definitions, removes its normal subcommand parser,
    and records the first remaining positional command without parsing any
    command-specific arguments.
    """
    from hermes_cli._parser import build_top_level_parser

    parser, subparsers, _chat_parser = build_top_level_parser()
    parser._actions.remove(subparsers)
    parser.add_argument("_qualification_probe_command", nargs="?")
    parser.exit_on_error = False
    return parser


def _parse_qualification_probe(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
    *,
    strict: bool = False,
):
    """Parse probe arguments without exposing argparse's diagnostic output."""
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        if strict:
            return parser.parse_args(list(argv)), ()
        return parser.parse_known_args(list(argv))


def _strip_profile_probe_args(
    argv: Sequence[str], *, profile_selector_seen: bool = False
) -> list[str]:
    """Remove only the first exact pre-argparse profile selector."""
    (
        probe_argv,
        _malformed_profile_spans,
        _later_profile_selector_indexes,
        _qualification_after_pending_profile,
    ) = _scan_profile_probe_args(argv, profile_selector_seen=profile_selector_seen)
    return probe_argv


def _scan_profile_probe_args(
    argv: Sequence[str],
    *,
    profile_selector_seen: bool = False,
    reject_invalid_short_clusters: bool = True,
    protected_profile_indexes: Sequence[int] = (),
) -> tuple[list[str], tuple[tuple[int, int], ...], tuple[int, ...], bool]:
    """Strip the first profile selector and report malformed selector spans.

    The returned argv preserves malformed selectors so the probe does not
    silently treat their values as valid profile names.  After the first
    selector is consumed, later profile tokens remain in argv just as they do
    after the real pre-argparse scanner strips its first selector.  The spans
    let the caller remove only malformed pairs when determining the command
    boundary, after real option-value consumption has run.  The returned
    indexes identify residual profile tokens in probe argv.

    ``reject_invalid_short_clusters`` is disabled only while recovering from
    a malformed profile pair.  In that path, a rejected cluster can be the
    malformed profile value itself and the following positional token still
    needs to establish the qualification command boundary.

    ``protected_profile_indexes`` marks profile-looking tokens that were
    already identified as malformed values.  Recovery must keep those tokens
    opaque instead of reinterpreting them as a later profile selector.
    """
    from hermes_cli._parser import top_level_value_flag_sets

    value_flags, optional_value_flags = top_level_value_flag_sets()
    probe_argv: list[str] = []
    malformed_profile_spans: list[tuple[int, int]] = []
    later_profile_selector_indexes: list[int] = []
    mcp_boundary_parser: argparse.ArgumentParser | None = None
    mcp_parser: argparse.ArgumentParser | None = None
    mcp_action_choices = None
    mcp_add_parser: argparse.ArgumentParser | None = None
    mcp_context = "unknown"
    global_probe_parser: argparse.ArgumentParser | None = None
    context_value_pending: str | None = None
    context_value_exact = False
    profile_value_pending = False
    profile_after_pending_value = False
    preparse_optionlike_value_seen = False
    preparse_skip_next = False
    malformed_profile_value_pending = False
    malformed_profile_selector_seen = False
    protected_profile_index_set = set(protected_profile_indexes)
    positional_probe = argparse.ArgumentParser(add_help=False)
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            probe_argv.extend(argv[index:])
            break
        preparse_token_skipped = preparse_skip_next
        preparse_skip_next = False
        malformed_profile_value_now = malformed_profile_value_pending
        malformed_profile_value_pending = False
        if preparse_token_skipped:
            preparse_optionlike_value_seen = True
        profile_value_pending_now = profile_value_pending
        profile_value_pending = False
        pre_profile_context_pending = context_value_pending
        pre_profile_context_exact = context_value_exact
        pending_value = context_value_pending == "required" or (
            context_value_pending == "optional" and not token.startswith("-")
        )
        pending_value_exact = context_value_exact
        context_value_pending = None
        context_value_exact = False
        if token.startswith("-") and mcp_context == "mcp_add" and not pending_value:
            if mcp_boundary_parser is None:
                mcp_boundary_parser = _build_mcp_boundary_probe_parser()
                mcp_parser, mcp_action_choices, mcp_add_parser = _mcp_boundary_grammar(
                    mcp_boundary_parser
                )
            if _inside_mcp_add_args(
                argv,
                index,
                mcp_add_parser=mcp_add_parser,
            ):
                probe_argv.append(token)
                break
        if index in protected_profile_index_set and (
            token in {"--profile", "-p"} or token.startswith("--profile=")
        ):
            probe_argv.append(token)
            index += 1
            continue
        if (
            preparse_token_skipped
            and not profile_selector_seen
            and (token in {"--profile", "-p"} or token.startswith("--profile="))
            and index + 1 < len(argv)
            and (
                argv[index + 1] in {"--profile", "-p"}
                or argv[index + 1].startswith("--profile=")
            )
        ):
            # An exact required global value owns this option-looking token.
            # It is not a malformed profile selector; preserve the value slot
            # without exposing an unknown option to the probe parser, and
            # leave the next profile token for the real pre-argparse scanner.
            probe_argv.append(_QUALIFICATION_PROBE_SENTINEL)
            index += 1
            continue
        if malformed_profile_value_now and (
            token in {"--profile", "-p"} or token.startswith("--profile=")
        ):
            # This option-looking token is the value of the malformed
            # selector immediately before it, not a fresh selector whose
            # following token can be consumed as profile data.
            probe_argv.append(token)
            index += 1
            continue
        if malformed_profile_selector_seen and (
            token in {"--profile", "-p"} or token.startswith("--profile=")
        ):
            # The real pre-argparse scanner stops after its first malformed
            # selector.  Keep all later profile-looking tokens opaque rather
            # than reopening selector parsing and swallowing the command.
            probe_argv.append(token)
            index += 1
            continue
        if token == "--profile" or token == "-p":
            if not profile_selector_seen and index + 1 < len(argv):
                if _PROFILE_ID_RE.match(argv[index + 1]):
                    if preparse_token_skipped:
                        # An exact required-value flag consumes option-looking
                        # tokens in the real pre-parser.  In that case this
                        # profile pair is its value, not the profile selector
                        # that the later scanner would remove.
                        profile_value = argv[index + 1]
                        if (
                            profile_value in _TOP_LEVEL_COMMANDS
                            and profile_value != "qualification"
                        ):
                            # The consumed profile token's value is now the
                            # first real command.  Do not let the following
                            # command data be mistaken for a qualification
                            # command during parse-error recovery.
                            return (
                                probe_argv,
                                tuple(malformed_profile_spans),
                                tuple(later_profile_selector_indexes),
                                False,
                            )
                        # Preserve the consumed pair as one opaque profile
                        # token.  Keeping only the selector would let a later
                        # recovery pass pair it with the next token as though
                        # that token were its value.  The inline spelling is
                        # already handled as the same opaque pair below.
                        probe_argv.append(f"--profile={profile_value}")
                        profile_after_pending_value = (
                            profile_value == "qualification"
                            or profile_value not in _TOP_LEVEL_COMMANDS
                        )
                        index += 2
                        continue
                    profile_selector_seen = True
                    profile_after_pending_value = preparse_token_skipped or (
                        pending_value
                        and pending_value_exact
                        and not preparse_optionlike_value_seen
                    )
                    if (
                        pre_profile_context_pending is not None
                        and not preparse_token_skipped
                    ):
                        # The profile scanner removes this pair before the
                        # real parser runs.  Keep the option's pending value
                        # state so a command-looking value immediately after
                        # the pair is consumed by the option, rather than
                        # being mistaken for the top-level command boundary.
                        context_value_pending = pre_profile_context_pending
                        context_value_exact = pre_profile_context_exact
                    index += 2
                    continue
                malformed_profile_spans.append((index, index + 2))
                malformed_profile_value_pending = True
                malformed_profile_selector_seen = True
                context_value_pending = "required"
            elif profile_selector_seen:
                later_profile_selector_indexes.append(len(probe_argv))
                if (
                    index + 1 < len(argv)
                    and positional_probe._parse_optional(argv[index + 1]) is None
                ):
                    profile_value_pending = True
        elif not profile_selector_seen and token.startswith("--profile="):
            if preparse_token_skipped:
                # As above, an inline profile token can itself be the value
                # consumed by an exact required-value flag.
                profile_value = token.split("=", 1)[1]
                if (
                    profile_value in _TOP_LEVEL_COMMANDS
                    and profile_value != "qualification"
                ):
                    return (
                        probe_argv,
                        tuple(malformed_profile_spans),
                        tuple(later_profile_selector_indexes),
                        False,
                    )
                if profile_value == "qualification":
                    probe_argv.append(token)
                    profile_after_pending_value = True
                    index += 1
                    continue
                probe_argv.append(token)
                profile_after_pending_value = profile_value not in _TOP_LEVEL_COMMANDS
                index += 1
                continue
            profile_selector_seen = True
            if not _PROFILE_ID_RE.match(token.split("=", 1)[1]):
                malformed_profile_spans.append((index, index + 1))
                malformed_profile_selector_seen = True
            elif preparse_optionlike_value_seen:
                # An inline selector reached this point only after the real
                # pre-parser consumed an earlier option-looking value.  The
                # normal scanner stops at this selector; later profile-like
                # tokens are residual parser input, not new selector pairs.
                malformed_profile_selector_seen = True
            profile_after_pending_value = preparse_token_skipped or (
                pending_value
                and pending_value_exact
                and not preparse_optionlike_value_seen
            )
            if pre_profile_context_pending is not None and not preparse_token_skipped:
                context_value_pending = pre_profile_context_pending
                context_value_exact = pre_profile_context_exact
            index += 1
            continue
        elif profile_selector_seen and token.startswith("--profile="):
            later_profile_selector_indexes.append(len(probe_argv))
        next_is_profile_selector = (
            profile_selector_seen
            and index + 1 < len(argv)
            and (
                argv[index + 1] in {"--profile", "-p"}
                or argv[index + 1].startswith("--profile=")
            )
        )
        if (
            "=" not in token
            and token in value_flags
            and index + 1 < len(argv)
            and not next_is_profile_selector
            and positional_probe._parse_optional(argv[index + 1]) is None
        ):
            probe_argv.extend((token, argv[index + 1]))
            context_value_pending = None
            context_value_exact = False
            preparse_optionlike_value_seen = False
            index += 2
            continue
        if (
            "=" not in token
            and token in optional_value_flags
            and index + 1 < len(argv)
            and not argv[index + 1].startswith("-")
            and not next_is_profile_selector
        ):
            probe_argv.extend((token, argv[index + 1]))
            context_value_pending = None
            context_value_exact = False
            preparse_optionlike_value_seen = False
            index += 2
            continue
        if (
            "=" not in token
            and token in value_flags
            and index + 1 < len(argv)
            and positional_probe._parse_optional(argv[index + 1]) is not None
        ):
            # The real pre-argparse scanner consumes every following token
            # for an exact required-value flag, including option-looking
            # tokens.  Keep that provenance while this probe follows
            # argparse's command/value semantics, so a later profile pair
            # cannot turn the displaced option's value into a command.
            preparse_optionlike_value_seen = True
            if not preparse_token_skipped:
                preparse_skip_next = True
        if profile_after_pending_value and token.startswith("-"):
            # An option-looking token starts a new parser decision.  The
            # immediate post-profile command exception no longer applies;
            # any later positional token belongs to this option grammar.
            profile_after_pending_value = False
        probe_argv.append(token)
        if token.startswith("-") and mcp_context != "other":
            if global_probe_parser is None:
                global_probe_parser = _build_qualification_probe_parser()
            short_cluster = _probe_short_cluster(global_probe_parser, token)
            if (
                short_cluster is not None
                and not short_cluster[2]
                and not malformed_profile_value_now
                and not preparse_token_skipped
                and reject_invalid_short_clusters
            ):
                return (
                    probe_argv,
                    tuple(malformed_profile_spans),
                    tuple(later_profile_selector_indexes),
                    False,
                )
            if short_cluster is not None and short_cluster[2]:
                action, explicit_arg = short_cluster[:2]
            else:
                action, explicit_arg = _probe_option_action(global_probe_parser, token)
            if (
                action is not None
                and getattr(action, "nargs", None) != 0
                and explicit_arg is None
            ):
                context_value_pending = (
                    "optional" if getattr(action, "nargs", None) == "?" else "required"
                )
                context_value_exact = (
                    token in value_flags or token in optional_value_flags
                )
        elif not token.startswith("-") and profile_after_pending_value:
            profile_after_pending_value = False
            if token == "qualification":
                return (
                    probe_argv,
                    tuple(malformed_profile_spans),
                    tuple(later_profile_selector_indexes),
                    True,
                )
        if (
            not token.startswith("-")
            and not pending_value
            and not profile_value_pending_now
        ):
            if profile_after_pending_value:
                profile_after_pending_value = False
                if token == "qualification":
                    return (
                        probe_argv,
                        tuple(malformed_profile_spans),
                        tuple(later_profile_selector_indexes),
                        True,
                    )
            stop_after_token = False
            if mcp_context == "unknown":
                if mcp_boundary_parser is None:
                    mcp_boundary_parser = _build_mcp_boundary_probe_parser()
                    mcp_parser, mcp_action_choices, mcp_add_parser = (
                        _mcp_boundary_grammar(mcp_boundary_parser)
                    )
                top_subparsers = _find_subparsers_action(mcp_boundary_parser)
                if (
                    top_subparsers is not None
                    and top_subparsers.choices.get(token) is mcp_parser
                ):
                    mcp_context = "mcp"
                else:
                    mcp_context = "other"
                    stop_after_token = token == "qualification"
                    if token in _TOP_LEVEL_COMMANDS:
                        stop_after_token = True
            elif mcp_context == "mcp":
                if (
                    mcp_action_choices is not None
                    and mcp_action_choices.get(token) is mcp_add_parser
                ):
                    mcp_context = "mcp_add_name"
                else:
                    mcp_context = "other"
                    stop_after_token = True
            elif mcp_context == "mcp_add_name":
                mcp_context = "mcp_add"
            if stop_after_token:
                return (
                    probe_argv,
                    tuple(malformed_profile_spans),
                    tuple(later_profile_selector_indexes),
                    False,
                )
        index += 1
    return (
        probe_argv,
        tuple(malformed_profile_spans),
        tuple(later_profile_selector_indexes),
        False,
    )


def _inside_mcp_add_args(
    argv: Sequence[str],
    index: int,
    *,
    mcp_add_parser: argparse.ArgumentParser | None = None,
) -> bool:
    """Match a real MCP add passthrough option before its child argv."""
    if mcp_add_parser is not None:
        candidate = [
            _MCP_BOUNDARY_PROBE_NAME,
            argv[index],
            _MCP_BOUNDARY_PROBE_SENTINEL,
        ]
        try:
            parsed, unknown = _parse_qualification_probe(mcp_add_parser, candidate)
        except (SystemExit, argparse.ArgumentError, TypeError, ValueError):
            return False
        return (
            _MCP_BOUNDARY_PROBE_SENTINEL in getattr(parsed, "args", ())
            and _MCP_BOUNDARY_PROBE_SENTINEL not in unknown
        )
    parser = _build_mcp_boundary_probe_parser()
    _, _, mcp_add_parser = _mcp_boundary_grammar(parser)
    candidate = [
        _MCP_BOUNDARY_PROBE_NAME,
        argv[index],
        _MCP_BOUNDARY_PROBE_SENTINEL,
    ]
    try:
        parsed, unknown = _parse_qualification_probe(mcp_add_parser, candidate)
    except (SystemExit, argparse.ArgumentError, TypeError, ValueError):
        return False
    return (
        _MCP_BOUNDARY_PROBE_SENTINEL in getattr(parsed, "args", ())
        and _MCP_BOUNDARY_PROBE_SENTINEL not in unknown
    )


def _build_mcp_boundary_probe_parser() -> argparse.ArgumentParser:
    """Build a parser for recognizing the top-level MCP add grammar only."""
    from hermes_cli._parser import build_top_level_parser
    from hermes_cli.subcommands.mcp import build_mcp_parser

    parser, subparsers, _chat_parser = build_top_level_parser()
    build_mcp_parser(subparsers, cmd_mcp=lambda _args: None)
    parser.exit_on_error = False
    return parser


def _find_subparsers_action(parser: argparse.ArgumentParser):
    """Return the parser's subcommand action without knowing command names."""
    return next(
        (
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ),
        None,
    )


def _mcp_boundary_grammar(parser: argparse.ArgumentParser):
    """Extract MCP/add parser objects from the registered parser grammar."""
    top_subparsers = _find_subparsers_action(parser)
    if top_subparsers is None:
        raise ValueError("MCP boundary probe has no top-level subparsers")
    mcp_parser = next(
        (
            candidate
            for candidate in top_subparsers.choices.values()
            if _find_subparsers_action(candidate) is not None
        ),
        None,
    )
    if mcp_parser is None:
        raise ValueError("MCP boundary probe has no nested subparsers")
    mcp_subparsers = _find_subparsers_action(mcp_parser)
    mcp_add_parser = next(
        (
            candidate
            for candidate in mcp_subparsers.choices.values()
            if any(
                getattr(action, "nargs", None) == argparse.REMAINDER
                for action in candidate._actions
            )
        ),
        None,
    )
    if mcp_add_parser is None:
        raise ValueError("MCP boundary probe has no remainder parser")
    return mcp_parser, mcp_subparsers.choices, mcp_add_parser


def _probe_option_action(parser: argparse.ArgumentParser, token: str):
    """Resolve one option token across supported argparse result shapes."""
    short_cluster = _probe_short_cluster(parser, token)
    if short_cluster is not None:
        action, explicit_arg, accepted = short_cluster
        if not accepted:
            return None, None
        return action, explicit_arg
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        try:
            option = parser._parse_optional(token)
        except (SystemExit, TypeError, ValueError):
            return None, None
    if isinstance(option, list):
        if len(option) != 1:
            return None, None
        option = option[0]
    if not isinstance(option, tuple) or len(option) < 3:
        return None, None
    return option[0], option[2]


def _probe_short_cluster(
    parser: argparse.ArgumentParser,
    token: str,
) -> tuple[argparse.Action | None, str | None, bool] | None:
    """Resolve short-option clusters from the parser's option grammar.

    Argparse returns only the first flag for a cluster such as ``-Vm`` even
    though the trailing ``m`` is a value-taking option.  Walk the parser's
    registered one-character aliases so the scanner can preserve the same
    value boundary without maintaining a second flag list.
    """
    if not token.startswith("-") or token.startswith("--") or len(token) <= 2:
        return None
    if argparse.ArgumentParser(add_help=False)._parse_optional(token) is None:
        return None

    option_actions = getattr(parser, "_option_string_actions", {})
    body = token[1:]
    index = 0
    while index < len(body):
        action = option_actions.get(f"-{body[index]}")
        if action is None:
            return None, None, False
        index += 1
        if getattr(action, "nargs", None) != 0:
            return action, body[index:] or None, True
    return None, None, True


def _qualification_first_positional_token(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
    *,
    skip_profile_pairs: bool = False,
    allow_profile_pair_as_value: bool = True,
) -> str | None:
    """Return the first positional token under the parser's global grammar.

    ``skip_profile_pairs`` is used only while inspecting the raw argv for a
    malformed profile recovery carveout.  The real pre-parser treats every
    exact selector attempt as owning its following value, even when that
    value is option-looking, so later selector-looking tokens must not become
    the apparent first command in that compatibility scan.

    ``allow_profile_pair_as_value`` controls the corresponding recovery
    behavior after a malformed selector's residual data span.  In that
    suffix, profile-looking tokens are opaque parser input; they must not
    satisfy a pending global option and consume the following positional
    token as profile data.
    """
    positional_probe = argparse.ArgumentParser(add_help=False)
    pending_value: str | None = None
    index = 0
    while index < len(argv):
        token = argv[index]
        index += 1

        if token == "--":
            return argv[index] if index < len(argv) else None

        if skip_profile_pairs:
            if token in {"--profile", "-p"}:
                if index < len(argv) and argv[index] != "--":
                    index += 1
                continue
            if token.startswith("--profile="):
                continue

        if pending_value is not None:
            if (
                allow_profile_pair_as_value
                and token in {"--profile", "-p"}
                and index < len(argv)
                and positional_probe._parse_optional(argv[index]) is None
            ):
                # Profile selection is consumed by the real pre-argparse
                # scanner.  Preserve that pair even when a preceding global
                # value flag was malformed and accidentally reached it.
                pending_value = None
                index += 1
                continue
            if positional_probe._parse_optional(token) is None:
                pending_value = None
                continue
            pending_value = None

        if not token.startswith("-"):
            return token

        action, explicit_arg = _probe_option_action(parser, token)
        if (
            action is not None
            and getattr(action, "nargs", None) != 0
            and explicit_arg is None
        ):
            pending_value = (
                "optional" if getattr(action, "nargs", None) == "?" else "required"
            )
    return None


def _qualification_after_first_positional(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
) -> bool:
    """Classify qualification after parser-derived values and one command.

    Parse-error recovery cannot use argparse's positional result directly when
    an earlier option is ambiguous or malformed.  Track the same global value
    consumption ourselves: an option resolved by the real parser owns its
    following positional value, while an unresolved option is only a prefix
    error and does not own any value.  The first remaining positional token is
    therefore the top-level command boundary.
    """
    return _qualification_first_positional_token(parser, argv) == "qualification"


def _qualification_after_parse_error(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
) -> bool:
    """Fail closed for malformed prefixes while preserving option values.

    A parse error may be caused by qualification-only arguments being left
    unknown, or by a malformed leading global option.  The parser-derived
    positional scan distinguishes a literal command from a global option
    value such as ``--model qualification`` without retrying malformed
    prefixes or exposing argparse diagnostics.
    """
    return _qualification_after_first_positional(parser, argv)


def _qualification_after_later_profile_selector(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
    selector_indexes: Sequence[int],
) -> bool:
    """Find a command after the first profile override's residual pair.

    The real pre-argparse scanner removes only the first profile selector.
    Later selectors therefore remain as unknown options to argparse, but
    their values are still profile data for command-boundary purposes.  A
    residual pair is also a hard boundary: an option-looking profile token
    cannot satisfy a pending global value-taking option.  Once that boundary
    is crossed, parser-derived global options consume their own values and a
    literal ``qualification`` is a command only when it is not one of them.
    """
    selector_index_set = set(selector_indexes)
    positional_probe = argparse.ArgumentParser(add_help=False)
    pending_value: str | None = None
    index = 0
    while index < len(argv):
        if index in selector_index_set:
            # The residual profile option is unknown to the normal parser;
            # neither it nor its value can satisfy a pending global option.
            pending_value = None
            token = argv[index]
            index += 1
            if (
                token in {"--profile", "-p"}
                and index < len(argv)
                and argv[index] != "--"
                and positional_probe._parse_optional(argv[index]) is None
            ):
                index += 1
            continue

        token = argv[index]
        index += 1
        if token == "--":
            # ``--`` ends global-option parsing.  The first token after it is
            # therefore the command boundary; never send the marker through
            # argparse's private option resolver (which treats it as an
            # ambiguous abbreviation on some supported Python versions).
            return index < len(argv) and argv[index] == "qualification"
        if pending_value is not None:
            if positional_probe._parse_optional(token) is None:
                pending_value = None
                continue
            pending_value = None

        if not token.startswith("-"):
            if token == "qualification":
                return True
            # The first positional token is the real top-level command.  Its
            # arguments, including any later ``qualification`` token, belong
            # to that command and must not trigger this early guard.
            return False

        action, explicit_arg = _probe_option_action(parser, token)
        if (
            action is not None
            and getattr(action, "nargs", None) != 0
            and explicit_arg is None
        ):
            pending_value = (
                "optional" if getattr(action, "nargs", None) == "?" else "required"
            )
    return False


def _qualification_after_double_dash_boundary(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
    selector_indexes: Sequence[int] = (),
) -> bool | None:
    """Classify the first command across an option terminator.

    The normal parser may fail before it can expose its positional command
    (for example, when a required global value is missing).  Track only
    parser-derived global option values and residual profile data until the
    first positional token, then apply the same rule after ``--``.  This
    keeps malformed prefixes from turning nested command data into a
    qualification command.
    """
    try:
        marker_index = argv.index("--")
    except ValueError:
        return None

    selector_index_set = set(selector_indexes)
    positional_probe = argparse.ArgumentParser(add_help=False)
    pending_value: str | None = None
    index = 0
    while index < marker_index:
        if index in selector_index_set:
            pending_value = None
            token = argv[index]
            index += 1
            if (
                token in {"--profile", "-p"}
                and index < marker_index
                and argv[index] != "--"
                and positional_probe._parse_optional(argv[index]) is None
            ):
                index += 1
            continue

        token = argv[index]
        index += 1
        if token in {"--profile", "-p"} or token.startswith("--profile="):
            pending_value = None
            if (
                token in {"--profile", "-p"}
                and index < marker_index
                and argv[index] != "--"
                and positional_probe._parse_optional(argv[index]) is None
            ):
                index += 1
            continue

        if pending_value is not None:
            if positional_probe._parse_optional(token) is None:
                pending_value = None
                continue
            pending_value = None

        if not token.startswith("-"):
            return token == "qualification"

        action, explicit_arg = _probe_option_action(parser, token)
        if (
            action is not None
            and getattr(action, "nargs", None) != 0
            and explicit_arg is None
        ):
            pending_value = (
                "optional" if getattr(action, "nargs", None) == "?" else "required"
            )

    return marker_index + 1 < len(argv) and argv[marker_index + 1] == "qualification"


def _qualification_is_residual_profile_version_value(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
    malformed_profile_spans: Sequence[tuple[int, int]],
) -> bool | None:
    """Keep the established residual profile/version data interpretation."""
    if not malformed_profile_spans:
        return None
    for _start, end in malformed_profile_spans:
        # Only the first residual selector after the malformed span can
        # participate in this compatibility carveout.  Looking through later
        # selector/value pairs would let a subsequent ``profile ... version``
        # sequence retroactively reinterpret an earlier positional command.
        index = end
        if index + 2 >= len(argv):
            continue
        token = argv[index]
        if token not in {"--profile", "-p"} and not token.startswith("--profile="):
            continue
        if (
            _qualification_first_positional_token(
                parser,
                argv[:index],
                skip_profile_pairs=True,
            )
            is not None
        ):
            continue
        if argv[index + 1] != "qualification":
            continue
        action, explicit_arg = _probe_option_action(parser, argv[index + 2])
        if (
            action is not None
            and getattr(action, "dest", None) == "version"
            and explicit_arg is None
        ):
            return (
                _qualification_first_positional_token(
                    parser,
                    argv[index + 3 :],
                    allow_profile_pair_as_value=False,
                )
                == "qualification"
            )
    return None


def has_leading_global_option_before_qualification(argv: Sequence[str]) -> bool:
    """Recognize a prefixed qualification command without starting Hermes."""
    if not argv or argv[0] == "qualification":
        return False

    (
        probe_argv,
        malformed_profile_spans,
        later_profile_selector_indexes,
        qualification_after_pending_profile,
    ) = _scan_profile_probe_args(argv)
    # A malformed profile pair can cause the initial scanner to consume a
    # later literal qualification as the value of a reinterpreted selector.
    # Keep recovery alive in that case; it removes only the malformed span
    # and re-evaluates the remaining command boundary.
    if "qualification" not in probe_argv and not malformed_profile_spans:
        return False

    if qualification_after_pending_profile:
        return True

    parser = _build_qualification_probe_parser()
    after_double_dash = _qualification_after_double_dash_boundary(
        parser, probe_argv, later_profile_selector_indexes
    )
    if after_double_dash is not None:
        return after_double_dash
    residual_profile_version_result = _qualification_is_residual_profile_version_value(
        parser, argv, malformed_profile_spans
    )
    if residual_profile_version_result is not None:
        return residual_profile_version_result

    if malformed_profile_spans:
        positional_probe = argparse.ArgumentParser(add_help=False)
        use_probe_recovery = _QUALIFICATION_PROBE_SENTINEL in probe_argv
        recovery_source = list(probe_argv) if use_probe_recovery else list(argv)
        if use_probe_recovery:
            # The initial scan may replace an exact global option's
            # option-looking profile value with the probe sentinel, so the
            # malformed spans still use original argv indexes.  Align the
            # surviving selector tokens in order before recovery; inline
            # malformed selectors omitted by the scan need no removal.
            source_spans = []
            source_search_index = 0
            for start, end in malformed_profile_spans:
                try:
                    source_start = recovery_source.index(
                        argv[start], source_search_index
                    )
                except ValueError:
                    continue
                source_spans.append((source_start, source_start + end - start))
                source_search_index = source_start + 1
        else:
            source_spans = list(malformed_profile_spans)
        malformed_indexes = set()
        protected_profile_indexes = set()
        for start, _end in source_spans:
            if start + 1 < len(recovery_source):
                malformed_value = recovery_source[start + 1]
                if malformed_value in {"--profile", "-p"} or malformed_value.startswith(
                    "--profile="
                ):
                    protected_profile_indexes.add(start + 1)
        if source_spans:
            # The real pre-argparse scanner stops at its first malformed
            # selector.  Protect every later profile-looking token during
            # recovery as well, so re-scanning cannot reopen selector parsing
            # at an arbitrary depth and swallow the command as profile data.
            first_malformed_start = min(start for start, _end in source_spans)
            protected_profile_indexes.update(
                index
                for index, token in enumerate(recovery_source)
                if index > first_malformed_start
                and (token in {"--profile", "-p"} or token.startswith("--profile="))
            )
        for start, _end in source_spans:
            # A profile-looking malformed value is already owned by the
            # earlier selector.  Do not reinterpret that value as another
            # selector and remove its actual data during recovery.
            if start in protected_profile_indexes:
                continue
            malformed_indexes.add(start)
            if (
                _end > start + 1
                and start + 1 < len(recovery_source)
                and positional_probe._parse_optional(recovery_source[start + 1]) is None
            ):
                malformed_indexes.add(start + 1)
            pending_index = start - 1
            while pending_index >= 0 and pending_index not in malformed_indexes:
                action, explicit_arg = _probe_option_action(
                    parser, recovery_source[pending_index]
                )
                if (
                    action is not None
                    and getattr(action, "nargs", None) != 0
                    and explicit_arg is None
                ):
                    # Each option in this contiguous chain saw another
                    # option/profile token where its value should have been.
                    # None of the chain is an owner of the next positional
                    # command, so drop the complete chain with the malformed
                    # profile pair during recovery.
                    malformed_indexes.add(pending_index)
                    pending_index -= 1
                    continue
                break
        recovery_argv = []
        recovery_protected_profile_indexes = set()
        for index, token in enumerate(recovery_source):
            if index in malformed_indexes:
                continue
            if index in protected_profile_indexes:
                recovery_protected_profile_indexes.add(len(recovery_argv))
            recovery_argv.append(token)
        (
            command_probe_argv,
            _recovery_malformed_profile_spans,
            later_profile_selector_indexes,
            _recovery_qualification_after_pending_profile,
        ) = _scan_profile_probe_args(
            recovery_argv,
            profile_selector_seen=True,
            reject_invalid_short_clusters=False,
            protected_profile_indexes=tuple(recovery_protected_profile_indexes),
        )
        if "qualification" not in command_probe_argv:
            return False
        probe_argv = command_probe_argv

    if later_profile_selector_indexes:
        return _qualification_after_later_profile_selector(
            parser, probe_argv, later_profile_selector_indexes
        )

    try:
        parsed, _unknown = _parse_qualification_probe(parser, probe_argv)
    except (SystemExit, argparse.ArgumentError, TypeError, ValueError):
        return _qualification_after_parse_error(parser, probe_argv)
    return getattr(parsed, "_qualification_probe_command", None) == "qualification"


def build_qualification_cli_parser() -> argparse.ArgumentParser:
    """Build the standalone parser used before normal Hermes startup."""
    parser = argparse.ArgumentParser(
        prog="hermes qualification",
        allow_abbrev=False,
        add_help=False,
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        required=True,
        help="Qualification fixture scenario to describe",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        required=True,
        help="Emit the canonical JSON qualification report",
    )
    return parser


def run_qualification_command(args) -> int:
    """Emit the fixed public qualification report without touching state."""
    payload = {
        "schema_version": 1,
        "command": "qualification",
        "scenario": args.scenario,
        "network_accessed": False,
        "private_state_accessed": False,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def run_qualification_cli(argv: list[str]) -> int:
    """Parse and execute the public qualification command."""
    args = build_qualification_cli_parser().parse_args(argv)
    return run_qualification_command(args)


def reject_prefixed_qualification_cli() -> None:
    """Reject a prefixed qualification command with a parser usage error."""
    parser = build_qualification_cli_parser()
    parser.error("qualification must be the first command-line argument")
