"""Dangerous command approval -- detection, prompting, and per-session state.

This package is the single source of truth for the dangerous command system:
- Pattern detection (DANGEROUS_PATTERNS, detect_dangerous_command)
- Per-session approval state (thread-safe, keyed by session_key)
- Approval prompting (CLI interactive + gateway async)
- Smart approval via auxiliary LLM (auto-approve low-risk commands)
- Permanent allowlist persistence (config.yaml)

The package was split into focused submodules (context, hardline, shell_parser,
session, gate); it re-exports every name so existing imports from
``tools.approval`` continue to work unchanged.
"""

from tools.approval import context  # noqa: F401
from tools.approval import gate  # noqa: F401
from tools.approval import hardline  # noqa: F401
from tools.approval import session  # noqa: F401
from tools.approval import shell_parser  # noqa: F401

from tools.approval.context import (
    logger, _YOLO_MODE_FROZEN, _approval_session_key, _approval_turn_id, _approval_tool_call_id, _hermes_interactive_ctx, set_hermes_interactive_context, reset_hermes_interactive_context,
    _is_interactive_cli, _fire_approval_hook, _prepare_smart_approval_observer, _observe_smart_approval_verdict, set_current_session_key, reset_current_session_key, set_current_observability_context, reset_current_observability_context,
    get_current_session_key, _get_session_platform, _is_cron_approval_context, _is_gateway_approval_context, _SSH_SENSITIVE_PATH, _HERMES_ENV_PATH, _HERMES_CONFIG_PATH, _PROJECT_ENV_PATH,
    _PROJECT_CONFIG_PATH, _SHELL_RC_FILES, _CREDENTIAL_FILES, _MACOS_PRIVATE_SYSTEM_PATH, _SYSTEM_CONFIG_PATH, _SENSITIVE_WRITE_TARGET, _USER_SENSITIVE_WRITE_TARGET, _PROJECT_SENSITIVE_WRITE_TARGET,
    _COMMAND_TAIL, _WRITE_TARGET_BOUNDARY, _CMDPOS,
)  # noqa: F401


from tools.approval.hardline import (
    logger, _hardline_rm_path, _HARDLINE_SYSTEM_DIRS, _RM_FLAG_PREFIX, HARDLINE_PATTERNS, _RE_FLAGS, HARDLINE_PATTERNS_COMPILED, _SUDO_STDIN_RE,
    _check_sudo_stdin_guard, detect_hardline_command, _match_user_deny_rule, _user_deny_block_result, _save_blocked_payload, _hardline_block_result, _sudo_stdin_block_result, DANGEROUS_PATTERNS,
    DANGEROUS_PATTERNS_COMPILED, _legacy_pattern_key, _PATTERN_KEY_ALIASES, _REMOVED_PATTERN_KEY_ALIASES, _approval_key_aliases, _normalize_command_for_detection,
)  # noqa: F401


from tools.approval.shell_parser import (
    _PATH_TOKEN_STOP, _PATH_TAIL, _home_prefix_fold_regex, _fold_home_prefixes, _rewrite_resolved_user_home, _rewrite_resolved_hermes_home, _PARAM_REPLACEMENT_RE, _PARAM_DEFAULT_RE,
    _SIMPLE_SHELL_LITERAL_RE, _ENV_ASSIGNMENT_RE, _COMMAND_WRAPPER_WORDS, _SUDO_OPTIONS_WITH_ARG, _INTERPRETER_EXEC_FLAGS, _INTERPRETER_WITH_ARG, _READ_TOOL_EXEC_FLAGS, _READ_TOOL_LONG_OPTIONS_WITH_ARG,
    _READ_TOOL_SHORT_OPTIONS_WITH_ARG, _SHELL_PUNCTUATION, _MAX_DETECTION_COMMAND_CHARS, _MAX_SEPARATOR_FREE_COMMAND_CHARS, _MAX_DETECTION_SEGMENTS, _PARSER_LIMIT_DESCRIPTION, _MALFORMED_EXEC_DESCRIPTION, _command_parser_limit_exceeded,
    _shell_tokens_with_spans, _GREP_OPTIONS_WITH_ARG, _GREP_SHORT_OPTIONS_WITH_ARG, _quoted_grep_pattern_spans, _grep_safe_detection_variant, _interpreter_family, _shell_segment_tokens, _iter_top_level_shell_segments,
    _split_option, _interpreter_exec_flag, _BASH_OPTIONS_WITH_ARG, _BASH_SHORT_OPTION_LETTERS, _bash_exec_payload, _read_tool_exec_flag, _execution_flag_findings, _skip_shell_whitespace,
    _scan_dollar_paren_end, _scan_backtick_end, _read_shell_word, _strip_optional_shell_quotes, _is_simple_shell_literal, _literal_command_substitution_output, _replace_simple_command_substitutions, _replace_simple_shell_expansions,
    _strip_shell_word_syntax, _deobfuscate_shell_word_for_detection, _iter_shell_command_starts, _mark_command_starts, _mask_quoted_newlines, _iter_shell_command_word_spans, _command_detection_variants, _is_verification_artifact_cleanup,
    detect_dangerous_command,
)  # noqa: F401


from tools.approval.session import (
    logger, _lock, _pending, _session_approved, _session_yolo, _permanent_approved, _HumanWaitState, _human_wait_lock,
    _human_wait_states, _HUMAN_WAIT_MAX_SESSIONS, HUMAN_WAIT_MARGIN_S, human_wait_ceiling, _clamped_window_seconds, _human_wait_state, human_wait_window, human_wait_seconds,
    _denial_tally, _DENIAL_TALLY_MAX_SESSIONS, _get_denial_breaker_threshold, _record_denial, _reset_denials, _denial_breaker_addendum, _ApprovalEntry, _gateway_queues,
    _gateway_notify_cbs, register_gateway_notify, unregister_gateway_notify, resolve_gateway_approval, has_blocking_approval, submit_pending, approve_session, _release_permission_mode_dependents,
    enable_session_yolo, disable_session_yolo, clear_session, is_session_yolo_enabled, is_current_session_yolo_enabled, is_approved, approve_permanent, load_permanent,
    _ALLOWLIST_SHELL_OPERATOR_RE, _has_allowlist_shell_operator, _command_matches_permanent_allowlist, load_permanent_allowlist, save_permanent_allowlist,
)  # noqa: F401


from tools.approval.gate import (
    logger, prompt_dangerous_approval, _prompt_dangerous_approval_inner, _normalize_approval_mode, _get_approval_config, _get_approval_mode, is_approval_bypass_active_for_session, is_approval_bypass_active,
    _get_approval_timeout, _get_cron_approval_mode, _strip_shell_comments, _strip_line_comment, _get_smart_policy, _smart_approve, _run_approval_gate, _should_skip_container_guards,
    check_dangerous_command, request_tool_approval, _format_tirith_description, _await_gateway_decision, check_all_command_guards, check_execute_code_guard, request_elicitation_consent,
)  # noqa: F401

from tools.approval.session import load_permanent_allowlist  # noqa: F401

# Load permanent allowlist from config on module import (preserves the
# original flat module's import-time side effect).
load_permanent_allowlist()
