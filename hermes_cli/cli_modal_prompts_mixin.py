"""Modal-prompt handlers (approval / clarify / sudo / secret) for the interactive CLI (god-file decomposition Wave 1).

This module hosts the modal-prompt methods lifted out of ``cli.py``'s
``HermesCLI`` class (shard s4, cluster c8). ``HermesCLI`` inherits
``CLIModalPromptsMixin`` so every ``self.<method>`` call resolves unchanged
via the MRO — behavior-neutral.

Import discipline (mirrors ``hermes_cli/cli_commands_mixin.py``, the accepted
Phase-4 decomposition):
  * Neutral, non-cyclic deps are imported at module top-level below.
  * cli.py-internal symbols (``CLI_CONFIG``/``_cprint``/``_DIM``/``_RST``)
    are imported LAZILY inside each method via ``from cli import ...`` — that
    resolves at call time when ``cli`` is fully loaded, so this module never
    imports ``cli`` at top level (no cycle).
"""

from __future__ import annotations

import queue
import shutil
import textwrap

from hermes_cli.callbacks import prompt_for_secret


class CLIModalPromptsMixin:
    def _persist_prompt_summary(self, icon: str, label: str, detail: str, outcome: str) -> None:
        """Print a one-line scrollback summary of a resolved modal prompt.

        Modal panels (approval / clarify) live in the prompt_toolkit layout and
        vanish on the next repaint, so the question and the decision leave no
        trace in the terminal scrollback. When display.persist_prompts is on
        (default), emit a dim single line after the prompt resolves so the
        decision survives in chat history.
        """
        from cli import CLI_CONFIG, _cprint, _DIM, _RST
        if not CLI_CONFIG.get("display", {}).get("persist_prompts", True):
            return
        detail = " ".join(detail.split())
        if len(detail) > 120:
            detail = detail[:119] + "…"
        outcome = " ".join(outcome.split())
        if len(outcome) > 120:
            outcome = outcome[:119] + "…"
        _cprint(f"\n{_DIM}{icon} {label}: {detail} → {outcome}{_RST}")

    def _clarify_callback(self, question, choices, multi_select=False):
        """
        Platform callback for the clarify tool. Called from the agent thread.

        Sets up the interactive selection UI (or freetext prompt for open-ended
        questions), then blocks until the user responds via the prompt_toolkit
        key bindings.  If no response arrives within the configured timeout the
        question is dismissed and the agent is told to decide on its own.

        When ``multi_select`` is True, shows checkboxes and the user can
        select multiple options with Space, confirming with Enter.
        """
        from cli import CLI_CONFIG, _cprint, _DIM, _RST
        import time as _time

        from tools.clarify_gateway import resolve_clarify_timeout

        # Canonical clarify timeout, shared with the gateway/TUI path. `<= 0`
        # means unlimited (never auto-skip mid-think) → a null deadline.
        timeout = resolve_clarify_timeout(CLI_CONFIG)
        response_queue = queue.Queue()
        is_open_ended = not choices
        # multi-select support: only active when multi_select is True and choices exist
        effective_multi = multi_select and not is_open_ended

        self._clarify_state = {
            "question": question,
            "choices": choices if not is_open_ended else [],
            "selected": 0,
            # multi-select support
            "multi_select": effective_multi,
            "selected_indices": set() if effective_multi else None,
            "response_queue": response_queue,
        }
        self._clarify_deadline = None if timeout <= 0 else _time.monotonic() + timeout
        # Open-ended questions skip straight to freetext input
        self._clarify_freetext = is_open_ended
        self._clarify_multi_base = None

        # Trigger an immediate prompt_toolkit repaint from this (non-main)
        # thread. Modal prompts must paint at once and must not be gated by the
        # _invalidate throttle / resize guard — see _paint_now / _invalidate (#41098).
        self._paint_now()

        # Poll for the user's response. The countdown in the hint line updates
        # on each repaint; refresh it once a second so the timer stays visible
        # while we wait. Selection changes (↑/↓) trigger instant repaints via
        # the key bindings.
        _last_countdown_refresh = _time.monotonic()
        while True:
            try:
                result = response_queue.get(timeout=1)
                self._clarify_deadline = None
                self._persist_prompt_summary("?", "Clarify", question, str(result))
                return result
            except queue.Empty:
                # None deadline = unlimited: never auto-skip, just keep polling.
                if self._clarify_deadline is not None:
                    remaining = self._clarify_deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                now = _time.monotonic()
                if now - _last_countdown_refresh >= 1.0:
                    _last_countdown_refresh = now
                    self._paint_now()

        # Timed out — tear down the UI and let the agent decide
        self._clarify_state = None
        self._clarify_freetext = False
        self._clarify_deadline = None
        self._clarify_multi_base = None
        self._paint_now()
        _cprint(f"\n{_DIM}(clarify timed out after {timeout}s — agent will decide){_RST}")
        return (
            "The user did not provide a response within the time limit. "
            "Use your best judgement to make the choice and proceed."
        )

    def _sudo_password_callback(self) -> str:
        """
        Prompt for sudo password through the prompt_toolkit UI.
        
        Called from the agent thread when a sudo command is encountered.
        Uses the same clarify-style mechanism: sets UI state, waits on a
        queue for the user's response via the Enter key binding.
        """
        from cli import _cprint, _DIM, _RST
        import time as _time

        timeout = 45
        response_queue = queue.Queue()

        self._capture_modal_input_snapshot()
        self._sudo_state = {
            "response_queue": response_queue,
        }
        self._sudo_deadline = _time.monotonic() + timeout

        # Modal prompt — paint immediately, bypassing the throttle/resize guard
        # so the prompt can't be dropped and time out unseen (#41098).
        self._paint_now()

        while True:
            try:
                result = response_queue.get(timeout=1)
                self._sudo_state = None
                self._sudo_deadline = 0
                self._restore_modal_input_snapshot()
                self._paint_now()
                if result:
                    _cprint(f"\n{_DIM}  ✓ Password received (cached for session){_RST}")
                else:
                    _cprint(f"\n{_DIM}  ⏭ Skipped{_RST}")
                return result
            except queue.Empty:
                remaining = self._sudo_deadline - _time.monotonic()
                if remaining <= 0:
                    break
                self._paint_now()

        self._sudo_state = None
        self._sudo_deadline = 0
        self._restore_modal_input_snapshot()
        self._paint_now()
        _cprint(f"\n{_DIM}  ⏱ Timeout — continuing without sudo{_RST}")
        return ""

    def _approval_callback(self, command: str, description: str,
                           *, allow_permanent: bool = True,
                           smart_denied: bool = False) -> str:
        """
        Prompt for dangerous command approval through the prompt_toolkit UI.

        Called from the agent thread. Shows a selection UI similar to clarify
        with choices: once / session / always / deny. Smart DENY owner
        overrides show only once / deny. When allow_permanent is False for
        another reason (for example tirith), only 'always' is hidden.
        Long commands also get a 'view' option so the full command can be
        expanded before deciding.

        Uses _approval_lock to serialize concurrent requests (e.g. from
        parallel delegation subtasks) so each prompt gets its own turn
        and the shared _approval_state / _approval_deadline aren't clobbered.
        """
        from cli import CLI_CONFIG, _cprint, _DIM, _RST
        import time as _time

        with self._approval_lock:
            timeout = int(CLI_CONFIG.get("approvals", {}).get("timeout", 300))
            response_queue = queue.Queue()

            self._approval_state = {
                "command": command,
                "description": description,
                "choices": self._approval_choices(
                    command,
                    allow_permanent=allow_permanent,
                    smart_denied=smart_denied,
                ),
                "selected": 0,
                "response_queue": response_queue,
            }
            self._approval_deadline = _time.monotonic() + timeout

            # Modal prompt — paint immediately, bypassing the throttle/resize
            # guard. A throttled paint here can be silently dropped (250ms
            # window collision or in-flight resize), leaving the panel unseen so
            # the command is denied on timeout without the user ever seeing it
            # (#41098). The countdown refreshes below paint the same way.
            self._paint_now()

            _last_countdown_refresh = _time.monotonic()
            while True:
                try:
                    result = response_queue.get(timeout=1)
                    self._approval_state = None
                    self._approval_deadline = 0
                    self._paint_now()
                    _outcome_labels = {
                        "once": "allowed once",
                        "session": "allowed for session",
                        "always": "added to allowlist",
                        "deny": "denied",
                    }
                    self._persist_prompt_summary(
                        "⚠", "Approval", command,
                        _outcome_labels.get(result, str(result)),
                    )
                    return result
                except queue.Empty:
                    remaining = self._approval_deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                    now = _time.monotonic()
                    if now - _last_countdown_refresh >= 1.0:
                        _last_countdown_refresh = now
                        self._paint_now()

            self._approval_state = None
            self._approval_deadline = 0
            self._paint_now()
            _cprint(f"\n{_DIM}  ⏱ Timeout — denying command{_RST}")
            self._persist_prompt_summary(
                "⚠", "Approval", command, "timed out (no response)",
            )
            return "timeout"

    def _approval_choices(self, command: str, *, allow_permanent: bool = True,
                          smart_denied: bool = False) -> list[str]:
        """Return approval choices for a dangerous command prompt."""
        if smart_denied:
            choices = ["once", "deny"]
        else:
            choices = ["once", "session", "always", "deny"] if allow_permanent else ["once", "session", "deny"]
        if len(command) > 70:
            choices.append("view")
        return choices

    def _computer_use_approval_callback(self, action: str, args: dict, summary: str) -> str:
        """Adapt the generic approval UI for the computer_use tool.

        The computer_use handler expects verdicts of the form
        `approve_once` | `approve_session` | `always_approve` | `deny`.
        The CLI's built-in approval UI returns `once` | `session` | `always`
        | `deny`. Translate between the two.
        """
        # Build a command-ish string so the existing UI renders something
        # meaningful. `summary` is already a one-line human description.
        verdict = self._approval_callback(
            command=f"computer_use: {summary}",
            description=f"Allow computer_use to perform `{action}`?",
        )
        return {
            "once": "approve_once",
            "session": "approve_session",
            "always": "always_approve",
            "deny": "deny",
            "timeout": "timeout",
        }.get(verdict, "deny")

    def _handle_approval_selection(self) -> None:
        """Process the currently selected dangerous-command approval choice."""
        state = self._approval_state
        if not state:
            return

        selected = state.get("selected", 0)
        choices = state.get("choices")
        if not isinstance(choices, list):
            choices = []
        if not (0 <= selected < len(choices)):
            return

        chosen = choices[selected]
        if chosen == "view":
            state["show_full"] = True
            state["choices"] = [choice for choice in choices if choice != "view"]
            if state["selected"] >= len(state["choices"]):
                state["selected"] = max(0, len(state["choices"]) - 1)
            self._invalidate()
            return

        state["response_queue"].put(chosen)
        self._approval_state = None
        self._invalidate()

    def _get_approval_display_fragments(self):
        """Render the dangerous-command approval panel for the prompt_toolkit UI.

        Layout priority: title + command + choices must always render, even if
        the terminal is short or the description is long. Description is placed
        at the bottom of the panel and gets truncated to fit the remaining row
        budget. This prevents HSplit from clipping approve/deny off-screen when
        tirith findings produce multi-paragraph descriptions or when the user
        runs in a compact terminal pane.
        """
        state = self._approval_state
        if not state:
            return []

        def _panel_box_width(title_text: str, content_lines: list[str], min_width: int = 46, max_width: int = 76) -> int:
            term_cols = shutil.get_terminal_size((100, 20)).columns
            longest = max([len(title_text)] + [len(line) for line in content_lines] + [min_width - 4])
            inner = min(max(longest + 4, min_width - 2), max_width - 2, max(24, term_cols - 6))
            return inner + 2

        def _wrap_panel_text(text: str, width: int, subsequent_indent: str = "") -> list[str]:
            wrapped = textwrap.wrap(
                text,
                width=max(8, width),
                replace_whitespace=False,
                drop_whitespace=False,
                subsequent_indent=subsequent_indent,
            )
            return wrapped or [""]

        def _append_panel_line(lines, border_style: str, content_style: str, text: str, box_width: int) -> None:
            inner_width = max(0, box_width - 2)
            lines.append((border_style, "│ "))
            lines.append((content_style, text.ljust(inner_width)))
            lines.append((border_style, " │\n"))

        def _append_blank_panel_line(lines, border_style: str, box_width: int) -> None:
            lines.append((border_style, "│" + (" " * box_width) + "│\n"))

        command = state["command"]
        description = state["description"]
        choices = state["choices"]
        selected = state.get("selected", 0)
        show_full = state.get("show_full", False)

        title = "⚠️  Dangerous Command"
        cmd_display = command
        choice_labels = {
            "once": "Allow once",
            "session": "Allow for this session",
            "always": "Add to permanent allowlist",
            "deny": "Deny",
            "view": "Show full command",
        }

        preview_lines = _wrap_panel_text(description, 60)
        preview_lines.extend(_wrap_panel_text(cmd_display, 60))
        for i, choice in enumerate(choices):
            prefix = '❯ ' if i == selected else '  '
            preview_lines.extend(_wrap_panel_text(
                f"{prefix}{choice_labels.get(choice, choice)}",
                60,
                subsequent_indent="  ",
            ))

        box_width = _panel_box_width(title, preview_lines)
        inner_text_width = max(8, box_width - 2)

        # Pre-wrap the mandatory content — command + choices must always render.
        cmd_wrapped = _wrap_panel_text(cmd_display, inner_text_width)
        if not show_full and "view" in choices and len(cmd_wrapped) > 4:
            cmd_wrapped = cmd_wrapped[:3] + _wrap_panel_text(
                "… (choose Show full command)",
                inner_text_width,
            )

        # (choice_index, wrapped_line) so we can re-apply selected styling below
        choice_wrapped: list[tuple[int, str]] = []
        for i, choice in enumerate(choices):
            label = choice_labels.get(choice, choice)
            # Show number prefix for quick selection (1-9 for items 1-9, 0 for 10th item)
            if i < 9:
                num_prefix = str(i + 1)
            elif i == 9:
                num_prefix = '0'
            else:
                num_prefix = ' '  # No number for items beyond 10th
            if i == selected:
                prefix = f'❯ {num_prefix}. '
            else:
                prefix = f'  {num_prefix}. '
            for wrapped in _wrap_panel_text(f"{prefix}{label}", inner_text_width, subsequent_indent="    "):
                choice_wrapped.append((i, wrapped))

        # Budget vertical space so HSplit never clips the command or choices.
        # Panel chrome (full layout with separators):
        #   top border + title + blank_after_title
        #   + blank_between_cmd_choices + bottom border = 5 rows.
        # In tight terminals we collapse to:
        #   top border + title + bottom border = 3 rows (no blanks).
        #
        # reserved_below: rows consumed below the approval panel by the
        # spinner/tool-progress line, status bar, input area, separators, and
        # prompt symbol. Measured at ~6 rows during live PTY approval prompts;
        # budget 6 so we don't overestimate the panel's room.
        term_rows = shutil.get_terminal_size((100, 24)).lines
        chrome_full = 5
        chrome_tight = 3
        reserved_below = 6

        available = max(0, term_rows - reserved_below)
        mandatory_full = chrome_full + len(cmd_wrapped) + len(choice_wrapped)

        # If the full-chrome panel doesn't fit, drop the separator blanks.
        # This keeps the command and every choice on-screen in compact terminals.
        use_compact_chrome = mandatory_full > available
        chrome_rows = chrome_tight if use_compact_chrome else chrome_full

        # If the command itself is too long to leave room for choices (e.g. user
        # hit "view" on a multi-hundred-character command), truncate it so the
        # approve/deny buttons still render. Keep at least 1 row of command.
        max_cmd_rows = max(1, available - chrome_rows - len(choice_wrapped))
        if len(cmd_wrapped) > max_cmd_rows:
            keep = max(1, max_cmd_rows - 1) if max_cmd_rows > 1 else 1
            cmd_wrapped = cmd_wrapped[:keep] + _wrap_panel_text(
                "… (command truncated — use /logs or /debug for full text)",
                inner_text_width,
            )

        # Allocate any remaining rows to description. The extra -1 in full mode
        # accounts for the blank separator between choices and description.
        mandatory_no_desc = chrome_rows + len(cmd_wrapped) + len(choice_wrapped)
        desc_sep_cost = 0 if use_compact_chrome else 1
        available_for_desc = available - mandatory_no_desc - desc_sep_cost
        # Even on huge terminals, cap description height so the panel stays compact.
        available_for_desc = max(0, min(available_for_desc, 10))

        desc_wrapped = _wrap_panel_text(description, inner_text_width) if description else []
        if available_for_desc < 1 or not desc_wrapped:
            desc_wrapped = []
        elif len(desc_wrapped) > available_for_desc:
            keep = max(1, available_for_desc - 1)
            desc_wrapped = desc_wrapped[:keep] + ["… (description truncated)"]

        # Render: title → command → choices → description (description last so
        # any remaining overflow clips from the bottom of the least-critical
        # content, never from the command or choices). Use compact chrome (no
        # blank separators) when the terminal is tight.
        lines = []
        lines.append(('class:approval-border', '╭' + ('─' * box_width) + '╮\n'))
        _append_panel_line(lines, 'class:approval-border', 'class:approval-title', title, box_width)
        if not use_compact_chrome:
            _append_blank_panel_line(lines, 'class:approval-border', box_width)

        for wrapped in cmd_wrapped:
            _append_panel_line(lines, 'class:approval-border', 'class:approval-cmd', wrapped, box_width)
        if not use_compact_chrome:
            _append_blank_panel_line(lines, 'class:approval-border', box_width)

        for i, wrapped in choice_wrapped:
            style = 'class:approval-selected' if i == selected else 'class:approval-choice'
            _append_panel_line(lines, 'class:approval-border', style, wrapped, box_width)

        if desc_wrapped:
            if not use_compact_chrome:
                _append_blank_panel_line(lines, 'class:approval-border', box_width)
            for wrapped in desc_wrapped:
                _append_panel_line(lines, 'class:approval-border', 'class:approval-desc', wrapped, box_width)

        lines.append(('class:approval-border', '╰' + ('─' * box_width) + '╯\n'))
        return lines

    def _secret_capture_callback(self, var_name: str, prompt: str, metadata=None) -> dict:
        return prompt_for_secret(self, var_name, prompt, metadata)

    def _capture_modal_input_snapshot(self) -> None:
        """Temporarily clear the input buffer and save the user's in-progress draft."""
        if self._modal_input_snapshot is not None or not getattr(self, "_app", None):
            return
        try:
            buf = self._app.current_buffer
            self._modal_input_snapshot = {
                "text": buf.text,
                "cursor_position": buf.cursor_position,
            }
            buf.reset()
        except Exception:
            self._modal_input_snapshot = None

    def _restore_modal_input_snapshot(self) -> None:
        """Restore any draft text that was present before a modal prompt opened."""
        snapshot = self._modal_input_snapshot
        self._modal_input_snapshot = None
        if not snapshot or not getattr(self, "_app", None):
            return
        try:
            buf = self._app.current_buffer
            buf.text = snapshot.get("text", "")
            buf.cursor_position = min(snapshot.get("cursor_position", 0), len(buf.text))
        except Exception:
            pass

    def _clear_active_overlays_for_interrupt(self) -> None:
        """Drain and clear every input-blocking overlay left by an interrupted agent.

        approval/clarify/sudo/secret prompts each block a worker thread on a
        ``response_queue.get()``.  When the agent is interrupted the worker
        thread is torn down, but the overlay's state dict stays set — leaving
        the CLI input gated (``read_only`` condition + keypress filter) with no
        thread servicing the prompt.  The result is a frozen terminal until the
        prompt's own timeout expires.  Push a terminal value onto each queue so
        any still-blocked thread unblocks cleanly, then nil the state out and
        restore the user's pre-modal draft (#14026).

        Safe default per prompt: approval -> "deny", clarify/sudo/secret ->
        cancel (None / empty).  Each step is wrapped so a dead queue can't
        prevent clearing the others.
        """
        if self._approval_state:
            try:
                self._approval_state["response_queue"].put("deny")
            except Exception:
                pass
            self._approval_state = None
        if self._clarify_state:
            try:
                self._clarify_state["response_queue"].put(
                    "The user cancelled. Use your best judgement to proceed."
                )
            except Exception:
                pass
            self._clarify_state = None
            self._clarify_freetext = False
            self._clarify_multi_base = None
        if self._sudo_state:
            try:
                self._sudo_state["response_queue"].put("")
            except Exception:
                pass
            self._sudo_state = None
            self._sudo_deadline = 0
            self._restore_modal_input_snapshot()
        if self._secret_state:
            try:
                self._cancel_secret_capture()
            except Exception:
                self._secret_state = None

    def _submit_secret_response(self, value: str) -> None:
        if not self._secret_state:
            return
        self._secret_state["response_queue"].put(value)
        self._secret_state = None
        self._secret_deadline = 0
        # Modal teardown — paint directly so the secret panel clears at once and
        # isn't held by the _invalidate throttle/resize guard (#41098).
        self._paint_now()

    def _cancel_secret_capture(self) -> None:
        self._submit_secret_response("")

    def _clear_secret_input_buffer(self) -> None:
        if getattr(self, "_app", None):
            try:
                self._app.current_buffer.reset()
            except Exception:
                pass
