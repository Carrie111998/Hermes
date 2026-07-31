#!/usr/bin/env python3
"""factory_admission_hook.py — pont Hermes `pre_tool_call` -> gate d'admission.

Ce script est le point d'intégration runtime de la porte d'admission worktree
(HER-95). Il se branche via le mécanisme de shell-hooks générique déjà chargé
par `cli.py`, `hermes_cli/main.py` et `gateway/run.py`
(`agent.shell_hooks.register_from_config`) — aucun changement de core n'est
requis. Déclaration opt-in dans `config.yaml` (profile-aware) :

    hooks:
      pre_tool_call:
        - matcher: ".*"
          fail_closed: true
          command: >-
            python3 /ABS/scripts/factory_admission_hook.py
            --registry /ABS/registry --agent hermes-code-a --profile hermes-code-a
            --only-mutating --require-owned-git

    # Profil métier (ex. hermes-immo) : le refus de domaine est AUTOMATIQUE car
    # --profile/--domain-prefixes vivent dans la config du profil, pas dans un
    # flag que l'appelant doit se souvenir de passer.
    hooks:
      pre_tool_call:
        - matcher: ".*"
          fail_closed: true
          command: >-
            python3 /ABS/scripts/factory_admission_hook.py
            --registry /ABS/registry --agent hermes-immo
            --profile hermes-immo --domain-prefixes JYI,HER

Protocole (voir `agent/shell_hooks.py`) : la charge utile arrive en JSON sur
stdin ; pour bloquer un tool AVANT son exécution, on émet sur stdout
`{"decision": "block", "reason": "..."}` (traduit par `_parse_response` en
`{"action": "block", "message": "..."}`, la forme que
`get_pre_tool_call_block_message` remonte au dispatcher de tools).

Posture : LECTURE SEULE. Le hook n'écrit jamais d'owner et ne persiste jamais le
PID éphémère de ce subprocess (cf. blocker identité process) — il ne fait
qu'interroger `evaluate_admission_guard`. Fail-open advisory si l'infra est
absente/anormale (pas de registry, pas un dépôt git) ; fail-closed (block) sur
un conflit d'occupation avéré ou une violation de domaine métier.
"""

import argparse
import json
import os
import re
import shlex
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acp_adapter.edit_approval import extract_v4a_patch_paths  # noqa: E402
import factory_lane  # noqa: E402  (résolu via le sys.path ci-dessus)

# Classification explicite de la surface runtime connue. Sous le contrat Code
# strict (``--only-mutating --require-owned-git``), tout nom/action/payload qui
# n'est pas une observation positivement validée est mutateur par défaut. Les
# mutations ``unbounded`` n'ont pas de cible worktree prouvable et restent donc
# bloquées même après un claim valide.
_WORKTREE_MUTATION_TOOLS = frozenset({
    "terminal", "patch", "write_file", "str_replace_editor", "apply_patch",
    "edit_file", "create_file", "delete_file", "move_file",
})
_ALWAYS_UNBOUNDED_TOOLS = frozenset({
    "bfl_flux3_get_result", "bfl_flux3_image_to_video", "bfl_flux3_keyframes_to_video",
    "bfl_flux3_prompting_guide", "bfl_flux3_text_to_video", "bfl_flux3_video_continuation",
    "browser_back", "browser_cdp", "browser_click", "browser_console", "browser_dialog", "browser_navigate",
    "browser_press", "browser_scroll", "browser_type", "browser_vision", "close_terminal",
    "delegate_task", "discord", "discord_admin", "execute_code", "feishu_doc_read",
    "feishu_drive_add_comment", "feishu_drive_list_comment_replies",
    "feishu_drive_list_comments", "feishu_drive_reply_comment", "focus_pane",
    "ha_call_service", "ha_get_state", "ha_list_entities", "ha_list_services",
    "image_generate", "kanban_attach", "kanban_attach_url", "kanban_attachments",
    "kanban_block", "kanban_comment", "kanban_complete", "kanban_create",
    "kanban_heartbeat", "kanban_link", "kanban_list", "kanban_show", "kanban_unblock",
    "memory", "open_preview", "project_create", "project_switch", "react_to_message",
    "skill_manage",
    "spotify_albums", "spotify_devices", "spotify_library", "spotify_playback",
    "spotify_playlists", "spotify_queue", "spotify_search", "text_to_speech",
    "video_analyze", "video_generate", "vision_analyze", "x_search", "xai_video_edit",
    "xai_video_extend", "yb_query_group_info", "yb_query_group_members",
    "yb_search_sticker", "yb_send_dm", "yb_send_sticker",
})
_STRICT_OBSERVATION_TOOLS = frozenset({
    "browser_get_images", "browser_snapshot", "clarify", "project_list",
    "read_file", "read_terminal", "search_files", "session_search", "skill_view", "skills_list", "todo",
    "web_extract", "web_search",
})
_KNOWN_RUNTIME_TOOLS = frozenset(
    _WORKTREE_MUTATION_TOOLS | _ALWAYS_UNBOUNDED_TOOLS | _STRICT_OBSERVATION_TOOLS
    | {"computer_use", "cronjob", "process"}
)
_EXACT_WORKER_LIFECYCLE_TOOLS = frozenset({
    "kanban_show", "kanban_heartbeat", "kanban_complete", "kanban_block",
})
_EXACT_WORKER_LIFECYCLE_KEYS = {
    "kanban_show": frozenset(),
    "kanban_heartbeat": frozenset({"note"}),
    "kanban_complete": frozenset({"summary", "result", "metadata"}),
    "kanban_block": frozenset({"reason", "kind"}),
}
_KANBAN_METADATA_SIDE_EFFECT_KEYS = frozenset({
    "artifact", "artifacts", "_staged_artifacts", "attachment", "attachments",
    "created_card", "created_cards",
})

_PATH_AFFECTING_SHELL_COMMANDS = frozenset({
    "apply_patch", "cat", "chmod", "chown", "cp", "dd", "install", "ln",
    "mkdir", "mktemp", "mv", "perl", "python", "python3", "rm", "ruby",
    "sed", "sh", "tee", "touch", "truncate", "zsh",
})
_HARD_READONLY_SHELL_COMMANDS = frozenset({"echo", "false", "printf", "pwd", "true", ":"})
_SHELL_CONTROL_TOKENS = frozenset({"&&", "||", ";", "\n", "|", "&", "(", ")", "<", ">", ">>"})
_SHELL_PUNCTUATION_CHARS = frozenset(";&|()<>\n")
_MAX_REPARSED_SHELL_DEPTH = 4

# Literal builtins needed by normal Code read/edit/commit workflows. Never
# derive this from ``git help``, config aliases, or PATH.
_GIT_BUILTIN_SUBCOMMANDS = frozenset({
    "add", "am", "apply", "branch", "checkout", "cherry-pick", "commit", "diff",
    "diff-tree", "fetch", "format-patch", "log", "merge", "merge-base", "mv", "pull", "push",
    "rebase", "remote", "reset", "restore", "rev-parse", "revert", "rm", "show", "stash",
    "status", "switch", "tag", "worktree",
})


def _is_shell_control_token(token):
    """Recognize shlex-coalesced control runs such as ``;\n`` or ``&&\n``."""
    return (
        isinstance(token, str)
        and bool(token)
        and all(character in _SHELL_PUNCTUATION_CHARS for character in token)
    )


def _emit_block(reason):
    print(json.dumps({"decision": "block", "reason": reason}))


def _emit_allow():
    print(json.dumps({"decision": "allow"}))


_GIT_REV_PARSE_OPTIONS = frozenset({
    "--show-toplevel", "--show-prefix", "--show-cdup", "--show-current",
    "--show-superproject-working-tree", "--git-dir", "--absolute-git-dir",
    "--is-inside-work-tree", "--is-bare-repository", "--verify", "--quiet", "-q",
    "--short", "--symbolic", "--symbolic-full-name", "--abbrev-ref", "--revs-only",
    "--no-revs", "--flags", "--no-flags", "--default", "--end-of-options", "--",
})
_GH_PR_OPTIONS = frozenset({
    "--repo", "-R", "--json", "--jq", "-q", "--template", "-t", "--limit", "-L",
    "--state", "-s", "--author", "-A", "--assignee", "-a", "--base", "-B",
    "--head", "-H", "--label", "-l", "--search", "-S", "--draft", "--app",
    "--comments", "-c", "--name-only", "--files", "--commits",
})


def _consume_options_with_values(tokens, allowed, value_options):
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            option = token.split("=", 1)[0]
            if token not in allowed and option not in allowed:
                return False
            if option in value_options and "=" not in token:
                index += 1
                if index >= len(tokens) or tokens[index].startswith("-"):
                    return False
        index += 1
    return True


def _parse_git_global_options(args, base):
    """Parse Git globals before the subcommand and resolve every ``-C`` target.

    Git's global option surface includes config and path selectors that can
    redirect execution or helpers. Only the minimal proven-safe set is accepted;
    every other leading option is ambiguous and therefore rejected.
    """
    index = 0
    current_base = os.path.realpath(base)
    targets = []
    while index < len(args) and args[index].startswith("-"):
        token = args[index]
        if token == "-C":
            index += 1
            if index >= len(args):
                return None
            raw_target = args[index]
        elif token.startswith("-C"):
            raw_target = token[2:]
        elif token == "--no-pager":
            index += 1
            continue
        else:
            return None
        if not raw_target or _has_active_shell_expansion(raw_target):
            return None
        targets.append((raw_target, current_base))
        current_base = os.path.realpath(
            raw_target if os.path.isabs(raw_target) else os.path.join(current_base, raw_target)
        )
        index += 1
    if index >= len(args) or args[index].startswith("-"):
        return None
    subcommand = args[index]
    if subcommand not in _GIT_BUILTIN_SUBCOMMANDS:
        return None
    return subcommand, args[index + 1:], targets


def _process_is_instant_readonly(tool_input):
    """Allow only schema-valid, instantaneous process observations."""
    action = tool_input.get("action")
    if action == "list":
        return set(tool_input) <= {"action"}
    session_id = tool_input.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    if action == "poll":
        return set(tool_input) <= {"action", "session_id"}
    if action != "log" or not set(tool_input) <= {"action", "session_id", "offset", "limit"}:
        return False
    offset = tool_input.get("offset", 0)
    limit = tool_input.get("limit", 200)
    return (
        isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0
        and isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 2000
    )


def _is_int(value, *, minimum=None, maximum=None):
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


def _strict_observation_payload_is_valid(tool_name, tool_input):
    """Validate the positive observation grammar allowed by the strict Code gate."""
    keys = set(tool_input)
    if tool_name == "cronjob":
        return tool_input == {"action": "list"}
    if tool_name == "computer_use":
        action = tool_input.get("action")
        if action in {"list_apps", "list_windows"}:
            return keys == {"action"}
        if action != "capture" or not keys <= {
                "action", "mode", "app", "pid", "window_id", "max_elements"}:
            return False
        if "mode" in tool_input and tool_input["mode"] not in {"som", "vision", "ax"}:
            return False
        if "app" in tool_input and not isinstance(tool_input["app"], str):
            return False
        for key in ("pid", "window_id"):
            if key in tool_input and not _is_int(tool_input[key], minimum=0):
                return False
        return "max_elements" not in tool_input or _is_int(
            tool_input["max_elements"], minimum=1, maximum=1000,
        )
    if tool_name == "process":
        return _process_is_instant_readonly(tool_input)
    if tool_name in {"browser_get_images", "project_list", "todo"}:
        return not keys
    if tool_name == "skills_list":
        return keys <= {"category"} and (
            "category" not in tool_input or isinstance(tool_input["category"], str)
        )
    if tool_name == "browser_snapshot":
        return keys <= {"full"} and (
            "full" not in tool_input or isinstance(tool_input["full"], bool)
        )
    if tool_name == "clarify":
        choices = tool_input.get("choices")
        return (
            keys <= {"question", "choices"}
            and isinstance(tool_input.get("question"), str)
            and bool(tool_input["question"].strip())
            and (choices is None or (
                isinstance(choices, list) and len(choices) <= 4
                and all(isinstance(choice, str) and choice.strip() for choice in choices)
            ))
        )
    if tool_name == "read_file":
        return (
            keys <= {"path", "offset", "limit"}
            and isinstance(tool_input.get("path"), str) and bool(tool_input["path"].strip())
            and ("offset" not in tool_input or _is_int(tool_input["offset"], minimum=1))
            and ("limit" not in tool_input or _is_int(tool_input["limit"], minimum=1, maximum=2000))
        )
    if tool_name == "read_terminal":
        return (
            keys <= {"start_line", "count"}
            and ("start_line" not in tool_input
                 or _is_int(tool_input["start_line"], minimum=0))
            and ("count" not in tool_input or _is_int(tool_input["count"], minimum=1))
        )
    if tool_name == "search_files":
        return (
            keys <= {"pattern", "target", "path", "file_glob", "limit", "offset",
                     "output_mode", "context"}
            and isinstance(tool_input.get("pattern"), str)
            and ("target" not in tool_input or tool_input["target"] in {"content", "files"})
            and ("output_mode" not in tool_input or tool_input["output_mode"] in {
                "content", "files_only", "count"})
            and all(key not in tool_input or isinstance(tool_input[key], str)
                    for key in ("path", "file_glob"))
            and ("limit" not in tool_input or _is_int(tool_input["limit"], minimum=1))
            and ("offset" not in tool_input or _is_int(tool_input["offset"], minimum=0))
            and ("context" not in tool_input or _is_int(tool_input["context"], minimum=0))
        )
    if tool_name == "session_search":
        return (
            keys <= {"query", "session_id", "around_message_id", "window", "limit",
                     "profile", "role_filter", "sort"}
            and all(key not in tool_input or isinstance(tool_input[key], str)
                    for key in ("query", "session_id", "profile", "role_filter"))
            and ("sort" not in tool_input or tool_input["sort"] in {"newest", "oldest"})
            and ("around_message_id" not in tool_input
                 or _is_int(tool_input["around_message_id"], minimum=1))
            and ("window" not in tool_input or _is_int(tool_input["window"], minimum=1, maximum=20))
            and ("limit" not in tool_input or _is_int(tool_input["limit"], minimum=1, maximum=10))
        )
    if tool_name == "skill_view":
        return (
            keys <= {"name", "file_path"}
            and isinstance(tool_input.get("name"), str) and bool(tool_input["name"].strip())
            and ("file_path" not in tool_input or isinstance(tool_input["file_path"], str))
        )
    if tool_name == "web_search":
        return (
            keys <= {"query", "limit"}
            and isinstance(tool_input.get("query"), str) and bool(tool_input["query"].strip())
            and ("limit" not in tool_input or _is_int(tool_input["limit"], minimum=1, maximum=100))
        )
    if tool_name == "web_extract":
        urls = tool_input.get("urls")
        return (
            keys <= {"urls", "char_limit"}
            and isinstance(urls, list) and 1 <= len(urls) <= 5
            and all(isinstance(url, str) and url.strip() for url in urls)
            and ("char_limit" not in tool_input or _is_int(tool_input["char_limit"], minimum=2000))
        )
    return False


def _worktree_mutation_payload_has_target_contract(tool_name, tool_input):
    if tool_name == "terminal":
        return isinstance(tool_input.get("command"), str) and bool(
            tool_input["command"].strip()
        )
    if tool_name == "patch":
        mode = tool_input.get("mode")
        if mode == "patch":
            return isinstance(tool_input.get("patch"), str) and bool(tool_input["patch"].strip())
        return (
            mode == "replace"
            and isinstance(tool_input.get("path"), str) and bool(tool_input["path"].strip())
            and isinstance(tool_input.get("old_string"), str)
            and isinstance(tool_input.get("new_string"), str)
        )
    if tool_name == "apply_patch":
        changes = tool_input.get("changes")
        return (
            isinstance(changes, list) and bool(changes)
            and all(isinstance(change, dict) and any(
                isinstance(change.get(key), str) and change[key].strip()
                for key in ("path", "file_path", "target_path")
            ) for change in changes)
        )
    return any(
        isinstance(tool_input.get(key), str) and tool_input[key].strip()
        for key in ("path", "file_path", "target_path")
    )


def _strict_tool_classification(tool_name, tool_input):
    """Return observation, worktree_mutation, or fail-closed unbounded_mutation."""
    if tool_name in _WORKTREE_MUTATION_TOOLS:
        if _worktree_mutation_payload_has_target_contract(tool_name, tool_input):
            return "worktree_mutation"
        return "unbounded_mutation"
    if tool_name in _STRICT_OBSERVATION_TOOLS | {"computer_use", "cronjob", "process"}:
        if _strict_observation_payload_is_valid(tool_name, tool_input):
            return "observation"
    return "unbounded_mutation"


def _metadata_has_kanban_side_effect(value):
    if isinstance(value, dict):
        return any(
            str(key).lower() in _KANBAN_METADATA_SIDE_EFFECT_KEYS
            or _metadata_has_kanban_side_effect(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_metadata_has_kanban_side_effect(item) for item in value)
    return False


def _exact_worker_lifecycle_error(tool_name, tool_input, session):
    """Return why a strict Code lifecycle call is not the exact current run."""
    if tool_name not in _EXACT_WORKER_LIFECYCLE_TOOLS:
        return "Kanban tool is outside the exact-current worker lifecycle"
    if set(tool_input) - _EXACT_WORKER_LIFECYCLE_KEYS[tool_name]:
        return "exact-current worker lifecycle does not accept identity or routing overrides"
    if tool_name == "kanban_complete" and _metadata_has_kanban_side_effect(
        tool_input.get("metadata")
    ):
        return "exact-current completion metadata cannot request attachment side effects"
    task_id = os.environ.get("HERMES_KANBAN_TASK")
    raw_run_id = os.environ.get("HERMES_KANBAN_RUN_ID")
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE")
    if not task_id:
        return "exact-current worker task is missing"
    try:
        run_id = int(raw_run_id or "")
    except ValueError:
        return "exact-current worker run id is missing or invalid"
    if run_id <= 0:
        return "exact-current worker run id must be positive"
    expected_session = f"kanban-{task_id}-run-{run_id}"
    if session != expected_session:
        return "pre-tool session does not match the deterministic Kanban worker session"
    if not workspace or not os.path.isabs(workspace):
        return "exact-current worker workspace is missing or not absolute"
    if factory_lane._git_toplevel_or_none(workspace) != os.path.realpath(workspace):
        return "exact-current worker workspace is not the exact Git top-level"
    return None


def _terminal_is_readonly(command):
    """Positive, literal grammar for pre-claim discovery commands.

    This deliberately is not a mutability guess. Anything outside these exact
    command families remains mutation-capable and therefore requires ownership.
    """
    if not isinstance(command, str) or not command.strip() or "\n" in command or "\r" in command:
        return False
    if _has_active_shell_expansion(command) or re.search(r"[*?\[]", command):
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    if not tokens or any(_is_shell_control_token(token) for token in tokens):
        return False
    if tokens == ["pwd"]:
        return True
    if tokens in (["claude", "--version"], ["claude", "auth", "status"]):
        return True
    if tokens[:2] == ["gh", "pr"] and len(tokens) >= 3 and tokens[2] in {"list", "view"}:
        return _consume_options_with_values(
            tokens[3:], _GH_PR_OPTIONS,
            {"--repo", "-R", "--json", "--jq", "-q", "--template", "-t", "--limit", "-L",
             "--state", "-s", "--author", "-A", "--assignee", "-a", "--base", "-B",
             "--head", "-H", "--label", "-l", "--search", "-S", "--app"},
        )
    if not tokens or tokens[0] != "git":
        return False
    parsed = _parse_git_global_options(tokens[1:], os.getcwd())
    if parsed is None:
        return False
    subcommand, operands, _targets = parsed
    if subcommand == "status":
        # core.fsmonitor can point at a repository-controlled executable.
        return False
    if subcommand == "worktree":
        return bool(operands) and operands[0] == "list" and all(
            token in {"--porcelain", "-v", "--verbose", "-z"} for token in operands[1:]
        )
    if subcommand == "branch":
        return operands == ["--show-current"]
    if subcommand in {"diff", "log", "show"}:
        # These commands can invoke repository-controlled external diff,
        # textconv, driver, or pager helpers. They are never pre-claim reads.
        return False
    if subcommand == "rev-parse":
        return bool(operands) and all(
            not token.startswith("-") or token in _GIT_REV_PARSE_OPTIONS for token in operands
        )
    return False


def _has_active_shell_expansion(text):
    """Detect shell expansions that survive into execution, never quoted data.

    ``shlex`` deliberately removes quote context, so it cannot tell ``'$HOME'``
    (literal) from ``"$HOME"`` (expanded). This small scanner keeps that
    distinction and treats escaped ``$``/backticks as literal. It is a guard,
    not a shell interpreter: uncertainty remains a reason to refuse a mutable
    target rather than attempt expansion in the hook.
    """
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "`":
            return True
        if char != "$" or index + 1 >= len(text):
            index += 1
            continue
        next_char = text[index + 1]
        if next_char == "(":
            return True
        if next_char == "{" or next_char == "_" or next_char.isalnum() or next_char in "?*@$#!-":
            return True
        index += 1
    return False


def _scan_dollar_paren_end(command, start):
    """Return the offset after a balanced active ``$(...)`` substitution."""
    depth = 1
    quote = None
    index = start + 2
    while index < len(command):
        char = command[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < len(command):
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "\\" and index + 1 < len(command):
            index += 2
            continue
        if command.startswith("$(", index):
            depth += 1
            index += 2
            continue
        if char == ")":
            depth -= 1
            index += 1
            if depth == 0:
                return index
            continue
        index += 1
    return None


def _scan_backtick_end(command, start):
    """Return the offset after an active backtick substitution."""
    index = start + 1
    while index < len(command):
        if command[index] == "\\" and index + 1 < len(command):
            index += 2
            continue
        if command[index] == "`":
            return index + 1
        index += 1
    return None


def _mask_active_command_substitutions(command):
    """Replace active substitution bodies before tokenizing shell syntax.

    ``shlex`` otherwise splits ``$(date)`` on its parentheses, losing the fact
    that the resulting word is dynamic.  Single-quoted syntax stays untouched;
    unmatched substitutions remain unparseable and must be rejected by callers.
    """
    marker = "__HERMES_DYNAMIC_SUBSTITUTION__"
    result = []
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and index + 1 < len(command):
            result.append(command[index:index + 2])
            index += 2
            continue
        if quote == "'":
            result.append(char)
            if char == "'":
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            result.append(char)
            quote = char
            index += 1
            continue
        if command.startswith("$(", index):
            end = _scan_dollar_paren_end(command, index)
            if end is None:
                return None
            result.append(marker)
            index = end
            continue
        if char == "`":
            end = _scan_backtick_end(command, index)
            if end is None:
                return None
            result.append(marker)
            index = end
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _command_substitution_bodies(command):
    """Return every active substitution body, or ``None`` on unbalanced syntax.

    Mirrors the state machine of :func:`_mask_active_command_substitutions`
    exactly, so the same substitutions that get masked as dynamic values are
    the ones whose executable bodies are surfaced here.
    """
    bodies = []
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and index + 1 < len(command):
            index += 2
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if command.startswith("$(", index):
            end = _scan_dollar_paren_end(command, index)
            if end is None:
                return None
            bodies.append(command[index + 2:end - 1])
            index = end
            continue
        if char == "`":
            end = _scan_backtick_end(command, index)
            if end is None:
                return None
            bodies.append(command[index + 1:end - 1])
            index = end
            continue
        index += 1
    return bodies


def _substitution_bodies_are_readonly(command):
    """True only when every ``$(...)``/backtick body is a literal readonly call.

    The shell EXECUTES a substitution body before the outer command runs, so a
    readonly outer command (``echo``, ``printf``) is no protection at all:
    ``echo $($C)``, ``echo $(eval "$C")`` and ``printf $(sh -c "$C")`` all run
    arbitrary host mutations while producing an innocuous-looking value. Under
    the strict Code gate the only executable substitution bodies are the same
    positive literal discovery grammar as pre-claim reads; anything dynamic,
    variable-driven, re-parsed or outside that grammar fails closed.
    """
    bodies = _command_substitution_bodies(command)
    if bodies is None:
        return False
    for body in bodies:
        stripped = body.strip()
        if not stripped or not _terminal_is_readonly(stripped):
            return False
    return True


def _unquote_shell_token(token):
    """Return the one shell-quoted word a re-parsing wrapper will execute."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    return token


def _shell_segments(command):
    """Return shell command segments, or ``None`` when syntax is ambiguous."""
    masked_command = _mask_active_command_substitutions(command)
    if masked_command is None:
        return None
    try:
        lexer = shlex.shlex(masked_command, posix=False, punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None

    segments = []
    current = []
    for token in [*tokens, ";"]:
        if _is_shell_control_token(token):
            if current:
                segments.append(current)
            current = []
        else:
            current.append(token)
    return segments


def _reparsed_shell_programs(command, depth=0):
    """Return literal programs evaluated by ``sh -c``/``eval`` recursively.

    ``None`` means that a nested reparse is ambiguous or exceeds the fixed
    budget, so callers fail closed rather than silently omit a target.
    """
    if depth >= _MAX_REPARSED_SHELL_DEPTH:
        return None
    segments = _shell_segments(command)
    if segments is None:
        return None
    programs = []
    for segment in segments:
        command_name = os.path.basename(segment[0].strip("'\""))
        operands = segment[1:]
        if command_name in {"sh", "bash", "dash", "ksh", "zsh"}:
            script = None
            for index, operand in enumerate(operands):
                if operand == "-c":
                    if index + 1 >= len(operands):
                        return None
                    script = _unquote_shell_token(operands[index + 1])
                    break
            if script is None:
                continue
            programs.append(script)
        elif command_name == "eval":
            if not operands:
                return None
            programs.append(" ".join(_unquote_shell_token(operand) for operand in operands))
        else:
            continue
        nested = _reparsed_shell_programs(programs[-1], depth + 1)
        if nested is None:
            return None
        programs.extend(nested)
    return programs


def _git_global_targets(command, base):
    """Return all direct Git ``-C`` targets, or ``None`` on bad globals."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    targets = []
    segment = []
    for token in [*tokens, ";"]:
        if _is_shell_control_token(token):
            if segment:
                command_name = os.path.basename(segment[0].strip("'\""))
                hidden_git = any(
                    os.path.basename(value.strip("'\"")) == "git" for value in segment[1:]
                )
                if hidden_git:
                    # ``env git ...``, ``sudo git ...``, and similar wrappers
                    # make the effective global/subcommand grammar ambiguous.
                    return None
                if command_name == "git":
                    parsed = _parse_git_global_options(segment[1:], base)
                    if parsed is None:
                        return None
                    targets.extend(parsed[2])
            segment = []
        else:
            segment.append(token)
    return targets


def _reparsed_shell_target_is_dynamic(command_name, operands, depth):
    """Inspect code evaluated by ``sh -c``/``eval`` with the same path policy.

    The outer shell can safely single-quote a script, but that quote disappears
    before a re-parsing wrapper evaluates it. Treat only dynamic nested path
    effects as unsafe, preserving harmless ``printf $(date)`` scripts.
    """
    if command_name in {"sh", "bash", "dash", "ksh", "zsh"}:
        for index, operand in enumerate(operands):
            if operand == "-c" and index + 1 < len(operands):
                return _terminal_has_unresolved_dynamic_target(
                    _unquote_shell_token(operands[index + 1]), depth + 1,
                )
        return "-c" in operands
    if command_name == "eval":
        if not operands:
            return True
        return _terminal_has_unresolved_dynamic_target(
            " ".join(_unquote_shell_token(operand) for operand in operands), depth + 1,
        )
    return False


def _terminal_has_unresolved_dynamic_target(command, depth=0):
    """True when terminal inspection cannot resolve a potentially mutable path.

    Simple variable and command/backtick substitutions are blocked only when
    they can select a cwd (``cd``/``pushd``), a git ``-C`` target, a redirection
    target, or an operand of a command that may write. Commands outside a tiny
    hard read-only allowlist are conservatively treated as write-capable,
    preventing wrappers such as ``env`` or ``sudo`` from hiding the real
    command. Literal single-quoted dollars remain safe data.
    """
    if depth >= _MAX_REPARSED_SHELL_DEPTH:
        return True
    marker = "__HERMES_DYNAMIC_SUBSTITUTION__"
    masked_command = _mask_active_command_substitutions(command)
    if masked_command is None:
        return _has_active_shell_expansion(command)
    try:
        lexer = shlex.shlex(masked_command, posix=False, punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return _has_active_shell_expansion(command)

    segment = []
    redirection_target = False
    for token in [*tokens, ";"]:
        if redirection_target:
            if _is_shell_control_token(token):
                return True
            if marker in token or _has_active_shell_expansion(token):
                return True
            redirection_target = False
        if _is_shell_control_token(token):
            if segment:
                if marker in segment[0] or _has_active_shell_expansion(segment[0]):
                    # A dynamic command word can resolve to a write-capable
                    # command or wrapper, so it is never a safe readonly call.
                    return True
                command_name = os.path.basename(segment[0].strip("'\""))
                operands = segment[1:]
                if _reparsed_shell_target_is_dynamic(command_name, operands, depth):
                    return True
                dynamic_operand = any(
                    marker in value or _has_active_shell_expansion(value)
                    for value in operands
                )
                if dynamic_operand:
                    if command_name in {"cd", "pushd"}:
                        return True
                    if command_name == "git":
                        for index, value in enumerate(operands):
                            if value == "-C" and index + 1 < len(operands):
                                if _has_active_shell_expansion(operands[index + 1]):
                                    return True
                            if value.startswith("-C") and _has_active_shell_expansion(value[2:]):
                                return True
                    if (
                        command_name in _PATH_AFFECTING_SHELL_COMMANDS
                        or command_name not in _HARD_READONLY_SHELL_COMMANDS
                    ):
                        return True
            segment = []
            redirection_target = token in {"<", ">", ">>"}
        else:
            segment.append(token)
    return False


def _path_anchor(path, base):
    """Return an existing directory from which git can resolve a target path.

    File mutation tools commonly target a not-yet-created file.  Walk upward to
    the first existing ancestor instead of falling back to the gateway cwd.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    candidate = path if os.path.isabs(path) else os.path.join(base, path)
    candidate = os.path.realpath(candidate)
    while not os.path.exists(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return None
        candidate = parent
    if os.path.isfile(candidate):
        candidate = os.path.dirname(candidate)
    return candidate


def _target_directories(payload):
    """Yield every effective target; cwd is only the no-target fallback."""
    cwd = payload.get("cwd") or os.getcwd()
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}

    target_specs = []
    workdir = tool_input.get("workdir")
    if isinstance(workdir, str) and workdir.strip():
        target_specs.append((workdir, cwd))
        terminal_base = os.path.realpath(
            workdir if os.path.isabs(workdir) else os.path.join(cwd, workdir)
        )
    else:
        terminal_base = os.path.realpath(cwd)

    # File mutation tools use one of these names across Hermes adapters.
    for key in ("path", "file_path", "target_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            target_specs.append((value, cwd))

    # Codex-style apply_patch transports one or more paths under ``changes``.
    # Treat every declared path as a first-class mutation target.
    changes = tool_input.get("changes")
    if isinstance(changes, list):
        for change in changes:
            if not isinstance(change, dict):
                continue
            for key in ("path", "file_path", "target_path"):
                value = change.get(key)
                if isinstance(value, str) and value.strip():
                    target_specs.append((value, cwd))

    if payload.get("tool_name") == "patch" and tool_input.get("mode", "replace") == "patch":
        for patch_path in extract_v4a_patch_paths(tool_input.get("patch")):
            target_specs.append((patch_path, cwd))

    # A terminal command can address a foreign worktree without setting
    # ``workdir``. Inspect every literal path-shaped/operand token. Dynamic or
    # malformed commands are rejected before this function is reached.
    command = tool_input.get("command")
    if payload.get("tool_name") == "terminal" and isinstance(command, str):
        nested_programs = _reparsed_shell_programs(command)
        if nested_programs is None:
            return
        for shell_command in [command, *nested_programs]:
            git_targets = _git_global_targets(shell_command, terminal_base)
            if git_targets is None:
                raise ValueError("unknown or ambiguous Git global option")
            target_specs.extend(git_targets)
            try:
                lexer = shlex.shlex(shell_command, posix=True, punctuation_chars=";&|()<>\n")
                lexer.whitespace = " \t\r"
                lexer.whitespace_split = True
                lexer.commenters = ""
                tokens = list(lexer)
            except ValueError:
                continue
            for index, token in enumerate(tokens):
                candidate = token.split("=", 1)[-1] if "=" in token else token
                null_redirection = (
                    candidate == os.devnull
                    and index > 0
                    and tokens[index - 1] in {"<", ">", "<<", ">>"}
                )
                if null_redirection:
                    continue
                path_shaped = os.path.isabs(candidate) or candidate.startswith(("./", "../"))
                relative_argument = (
                    index > 0
                    and not _is_shell_control_token(token)
                    and not token.startswith("-")
                )
                if path_shaped or relative_argument:
                    target_specs.append((candidate, terminal_base))

    if not target_specs:
        target_specs.append((cwd, cwd))
    seen = set()
    for raw, base in target_specs:
        anchor = _path_anchor(raw, base)
        if anchor is not None and anchor not in seen:
            seen.add(anchor)
            yield anchor


def main(argv=None):
    parser = argparse.ArgumentParser(prog="factory_admission_hook")
    parser.add_argument("--registry", required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--domain-prefixes")
    # Restrict the gate to mutations, using a positive terminal discovery grammar.
    parser.add_argument("--only-mutating", action="store_true")
    # Code profiles require an exact, live, active owner for every mutation target.
    parser.add_argument("--require-owned-git", action="store_true")
    args = parser.parse_args(argv)

    # Invalid payloads intentionally produce no directive. A required shell-hook
    # bridge interprets that absence as a block; legacy advisory wiring stays
    # compatible and fail-open.
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    if payload.get("hook_event_name") != "pre_tool_call":
        return 0

    tool_name = payload.get("tool_name")
    raw_tool_input = payload.get("tool_input")
    tool_input_is_valid = isinstance(raw_tool_input, dict)
    tool_input = raw_tool_input if tool_input_is_valid else {}
    command = tool_input.get("command")
    exact_worker_lifecycle = False
    strict_code_gate = bool(args.only_mutating and args.require_owned_git)
    if args.only_mutating:
        if strict_code_gate:
            if not isinstance(tool_name, str) or not tool_name or not tool_input_is_valid:
                _emit_block("unknown tool or invalid payload is mutation-capable by default")
                return 0
            if tool_name == "terminal" and _terminal_is_readonly(command):
                _emit_allow()
                return 0
            if tool_name in _EXACT_WORKER_LIFECYCLE_TOOLS:
                lifecycle_error = _exact_worker_lifecycle_error(
                    tool_name, tool_input, payload.get("session_id") or "",
                )
                if lifecycle_error:
                    _emit_block(lifecycle_error)
                    return 0
                exact_worker_lifecycle = True
                classification = "worktree_mutation"
            else:
                classification = _strict_tool_classification(tool_name, tool_input)
            if classification == "observation":
                _emit_allow()
                return 0
            if classification == "unbounded_mutation":
                _emit_block(
                    "tool mutation has no verifiable worktree target under the strict Code gate"
                )
                return 0
        else:
            # Historical advisory hooks keep their former opt-in matcher
            # semantics. Fail-closed classification belongs only to HER-96's
            # strict Code contract above.
            legacy_mutating = _WORKTREE_MUTATION_TOOLS | {"execute_code", "process"}
            if tool_name not in legacy_mutating:
                _emit_allow()
                return 0
            if tool_name == "terminal" and _terminal_is_readonly(command):
                _emit_allow()
                return 0
            if tool_name == "process":
                if _process_is_instant_readonly(tool_input):
                    _emit_allow()
                else:
                    _emit_block(
                        "process action is not instantaneous read-only; target process ownership "
                        "is unavailable to the strict admission hook"
                    )
                return 0

    session = payload.get("session_id") or ""
    if tool_name == "terminal":
        if not isinstance(command, str) or not command.strip():
            _emit_block("terminal command is missing or invalid")
            return 0
        if (
            _terminal_has_unresolved_dynamic_target(command)
            or _reparsed_shell_programs(command) is None
        ):
            _emit_block("unresolved shell expansion can affect a worktree target")
            return 0
        if strict_code_gate and not _substitution_bodies_are_readonly(command):
            # A substitution body executes before the outer command, so a
            # readonly outer word never bounds it. Only the literal discovery
            # grammar is admissible inside ``$(...)``/backticks.
            _emit_block(
                "command substitution body is not a literal read-only command "
                "under the strict Code gate"
            )
            return 0

    try:
        root = factory_lane._readonly_registry_root(args.registry)
    except factory_lane.RegistryError as exc:
        _emit_block(str(exc))
        return 0
    except Exception:
        _emit_block("registry root cannot be inspected safely")
        return 0
    if root is None:
        if args.require_owned_git:
            _emit_block("worktree registry unavailable; explicit live owner required")
        else:
            _emit_allow()
        return 0

    try:
        targets = (
            [os.environ["HERMES_KANBAN_WORKSPACE"]]
            if exact_worker_lifecycle else list(_target_directories(payload))
        )
        if args.require_owned_git and not targets:
            _emit_block("no effective mutation target could be proven")
            return 0
        for target_dir in targets:
            worktree_real = factory_lane._git_toplevel_or_none(target_dir)
            if args.require_owned_git and worktree_real is None:
                _emit_block("owned Git worktree required for Code profile mutation")
                return 0
            if worktree_real is None:
                worktree_real = os.path.realpath(target_dir)

            if args.require_owned_git:
                with factory_lane._anchored_registry_root(root) as root_anchor:
                    match = factory_lane._find_claim_for_worktree(root_anchor, worktree_real)
                if match is None:
                    _emit_block("explicit live owner required for Code profile mutation")
                    return 0
                _key, owner = match
                expected_profile = args.profile or args.agent
                if not factory_lane._is_same_session(owner, args.agent, session):
                    _emit_block("explicit live owner belongs to another Code session")
                    return 0
                if owner.get("profile") != expected_profile:
                    _emit_block("explicit live owner belongs to another Code profile")
                    return 0
                if owner.get("state", "active") != "active":
                    _emit_block("bootstrap-pending owner cannot authorize mutation")
                    return 0
                if owner.get("pid") != os.getppid():
                    _emit_block("explicit owner PID does not match the calling Code process")
                    return 0
                if factory_lane.determine_process_state(owner) != "alive":
                    _emit_block("explicit owner process identity is not proven alive")
                    return 0

            allowed, reason = factory_lane.evaluate_admission_guard(
                root, worktree_real, args.agent, session,
                profile=args.profile, domain_prefixes=args.domain_prefixes,
            )
            if not allowed:
                _emit_block(reason or "worktree admission denied")
                return 0
    except factory_lane.RegistryError as exc:
        _emit_block(str(exc))
        return 0
    except Exception:
        if args.require_owned_git:
            _emit_block("admission gate could not prove exact ownership")
        return 0

    _emit_allow()
    return 0


if __name__ == "__main__":
    sys.exit(main())
