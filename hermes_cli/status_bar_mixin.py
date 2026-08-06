"""Status-bar rendering methods for ``HermesCLI`` (god-file decomposition campaign).

This module hosts the status-bar cluster (``_build_status_bar_text``,
``_get_status_bar_fragments``, ``_get_status_bar_snapshot``, the width/trim
helpers, and the style mappers) lifted verbatim out of ``cli.py``'s
``HermesCLI`` class. ``HermesCLI`` inherits ``StatusBarMixin`` so every
``self.<method>`` call resolves unchanged via the MRO — behavior-neutral.

Import discipline (mirrors hermes_cli/cli_commands_mixin.py):
  * Neutral, non-cyclic deps are imported at module top-level below.
  * cli.py-internal symbols (``_reverse_alias_for_display``,
    ``format_duration_compact``, ``format_token_count_compact``) are imported
    LAZILY inside the methods via ``from cli import ...`` — that resolves at
    call time when ``cli`` is fully loaded, so this module never imports
    ``cli`` at top level (no cycle).
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime
from typing import Any, Dict, Optional

from hermes_cli.banner import _format_context_length


class StatusBarMixin:
    """Status-bar / TUI footer rendering methods for ``HermesCLI``."""

    def _status_bar_context_style(self, percent_used: Optional[int]) -> str:
        if percent_used is None:
            return "class:status-bar-dim"
        if percent_used >= 95:
            return "class:status-bar-critical"
        if percent_used > 80:
            return "class:status-bar-bad"
        if percent_used >= 50:
            return "class:status-bar-warn"
        return "class:status-bar-good"

    @staticmethod
    def _battery_status_style(category: str) -> str:
        """Map a battery colour category to a status-bar style class."""
        return {
            "good": "class:status-bar-good",
            "warn": "class:status-bar-warn",
            "bad": "class:status-bar-bad",
            "critical": "class:status-bar-critical",
        }.get(category, "class:status-bar-dim")

    def _handle_battery_command(self, cmd_original: str) -> None:
        """Toggle the status-bar battery read-out.

        ``/battery`` toggles, ``/battery on|off`` sets explicitly, and
        ``/battery status`` reports the current setting plus a live reading.
        The choice is persisted to ``display.battery`` so it survives restarts.
        """
        from cli import save_config_value

        parts = (cmd_original or "").split()
        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        try:
            from agent.battery import format_battery, read_battery
            reading = read_battery(use_cache=False)
        except Exception:
            reading = None

        if arg in ("status", "show"):
            state = "on" if self._battery_visible else "off"
            if reading is not None and reading.available:
                self._console_print(
                    f"  Battery indicator {state} — currently {format_battery(reading)}"
                )
            elif reading is not None:
                self._console_print(
                    f"  Battery indicator {state} — no battery detected on this machine"
                )
            else:
                self._console_print(f"  Battery indicator {state}")
            return

        if arg in ("on", "true", "yes"):
            target = True
        elif arg in ("off", "false", "no"):
            target = False
        elif arg in ("", "toggle"):
            target = not self._battery_visible
        else:
            self._console_print("  Usage: /battery [on|off|status]")
            return

        self._battery_visible = target
        save_config_value("display.battery", target)

        if target:
            if reading is not None and not reading.available:
                self._console_print(
                    "  Battery indicator on — no battery detected, so nothing will show here"
                )
            elif reading is not None and reading.available:
                self._console_print(
                    f"  Battery indicator on — {format_battery(reading)}"
                )
            else:
                self._console_print("  Battery indicator on")
        else:
            self._console_print("  Battery indicator off")

    @staticmethod
    def _compression_count_style(count: int) -> str:
        """Return a style class reflecting context compression pressure."""
        if count >= 10:
            return "class:status-bar-bad"
        if count >= 5:
            return "class:status-bar-warn"
        return "class:status-bar-dim"

    def _build_context_bar(self, percent_used: Optional[int], width: int = 10) -> str:
        safe_percent = max(0, min(100, percent_used or 0))
        filled = round((safe_percent / 100) * width)
        return f"[{('█' * filled) + ('░' * max(0, width - filled))}]"

    @staticmethod
    def _format_prompt_elapsed(prompt_start_time: Optional[float], prompt_duration: float, live: bool = False) -> str:
        """Format per-prompt elapsed time for the status bar.

        Always returns a string — shows 0s on fresh start before first turn.
        Keeps seconds visible at all scales so it increments smoothly:
            59s → 1m → 1m 1s → ... → 1m 59s → 2m → 2m 1s → ...
            59m 59s → 1h → 1h 0m 1s → ...
            23h 59m 59s → 1d → 1d 0h 1m → ...

        Emoji prefix: ⏱ when turn is live, ⏲ when frozen or fresh start.
        Uses width-1 (no variation selector) glyphs so the status bar stays
        aligned in monospace terminals.
        """
        if prompt_start_time is None and prompt_duration == 0.0:
            return "⏲ 0s"
        elapsed = time.time() - prompt_start_time if prompt_start_time is not None else prompt_duration
        elapsed = max(0.0, elapsed)

        days = int(elapsed // 86400)
        remaining = elapsed % 86400
        hours = int(remaining // 3600)
        remaining = remaining % 3600
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)

        if days > 0:
            time_str = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            time_str = f"{hours}h {minutes}m {seconds}s" if seconds else f"{hours}h {minutes}m"
        elif minutes > 0:
            time_str = f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
        else:
            time_str = f"{int(elapsed)}s"

        emoji = "⏱" if live else "⏲"
        return f"{emoji} {time_str}"

    @staticmethod
    def _format_idle_since(last_finished_at: Optional[float], turn_live: bool) -> str:
        """Format time since the last final agent response for the status bar.

        Returns an empty string while a turn is live (the per-prompt elapsed
        timer covers that case) or before the first turn has completed.
        Compact read-out: ``✓ 42s`` / ``✓ 3m`` / ``✓ 1h 12m``.
        """
        if turn_live or last_finished_at is None:
            return ""
        idle = max(0.0, time.time() - last_finished_at)
        from cli import format_duration_compact

        return f"✓ {format_duration_compact(idle)}"

    def _get_status_bar_snapshot(self) -> Dict[str, Any]:
        from cli import _reverse_alias_for_display, format_duration_compact

        # Prefer the agent's model name — it updates on fallback.
        # self.model reflects the originally configured model and never
        # changes mid-session, so the TUI would show a stale name after
        # _try_activate_fallback() switches provider/model.
        agent = getattr(self, "agent", None)
        model_name = (getattr(agent, "model", None) or self.model or "unknown")
        # Friendly display: prefer reverse-alias from config.yaml ``model_aliases:``
        # before slash/length truncation. This turns long Palantir RIDs like
        # ``ri.language-model-service..language-model.anthropic-claude-4-7-opus``
        # into the user's chosen short name (e.g. ``opus-4.7``) in the status bar.
        model_short = _reverse_alias_for_display(model_name)
        if model_short == model_name:
            model_short = model_name.split("/")[-1] if "/" in model_name else model_name
            # Strip Palantir RID prefixes via the shared display formatter so
            # this site and ``ModelSwitchResult`` confirmation can't drift.
            from hermes_cli.model_switch import format_model_for_display
            model_short = format_model_for_display(model_short)
        if model_short.endswith(".gguf"):
            model_short = model_short[:-5]
        if len(model_short) > 26:
            model_short = f"{model_short[:23]}..."

        elapsed_seconds = max(0.0, (datetime.now() - self.session_start).total_seconds())
        snapshot = {
            "model_name": model_name,
            "model_short": model_short,
            "duration": format_duration_compact(elapsed_seconds),
            "prompt_elapsed": self._format_prompt_elapsed(
                getattr(self, "_prompt_start_time", None),
                getattr(self, "_prompt_duration", 0.0),
                live=getattr(self, "_prompt_start_time", None) is not None,
            ),
            "idle_since": self._format_idle_since(
                getattr(self, "_last_turn_finished_at", None),
                turn_live=getattr(self, "_prompt_start_time", None) is not None,
            ),
            "context_tokens": 0,
            "context_length": None,
            "context_percent": None,
            "session_input_tokens": 0,
            "session_output_tokens": 0,
            "session_cache_read_tokens": 0,
            "session_cache_write_tokens": 0,
            "session_prompt_tokens": 0,
            "session_completion_tokens": 0,
            "session_total_tokens": 0,
            "session_api_calls": 0,
            "compressions": 0,
            "active_background_tasks": 0,
            "active_background_processes": 0,
            "active_background_subagents": 0,
            "battery_label": "",
            "battery_category": "dim",
            # Focus view badge (/focus). Persistent indicator so the reduced
            # output mode is never invisible. Display-only.
            "focus_label": "",
        }

        try:
            from hermes_cli.focus_view import focus_statusbar_segment

            snapshot["focus_label"] = focus_statusbar_segment(
                bool(getattr(self, "_focus_view_enabled", False))
            )
        except Exception:
            pass

        # Battery read-out (first status-bar element when enabled). Reads are
        # memoised for a few seconds inside agent.battery, so polling it on
        # every status-bar repaint is cheap.
        if getattr(self, "_battery_visible", False):
            try:
                from agent.battery import (
                    battery_category,
                    format_battery,
                    read_battery,
                )

                _batt = read_battery()
                snapshot["battery_label"] = format_battery(_batt)
                snapshot["battery_category"] = battery_category(_batt)
            except Exception:
                pass

        # Count live /background tasks. The dict entry is removed in the
        # task thread's finally block, so len() reflects truly-running tasks.
        # len() on a CPython dict is atomic; safe to read without a lock.
        try:
            bg_tasks = getattr(self, "_background_tasks", None)
            if bg_tasks:
                snapshot["active_background_tasks"] = len(bg_tasks)
        except Exception:
            pass

        # Count live background terminal processes (terminal tool background
        # sessions tracked by tools.process_registry). Cheap O(1) read.
        try:
            from tools.process_registry import process_registry
            snapshot["active_background_processes"] = process_registry.count_running()
        except Exception:
            pass

        # Count live background/async subagents (delegate_task batches and
        # background single delegations tracked by tools.async_delegation).
        # active_count() iterates an in-memory records dict under a lock —
        # cheap and only counts records still in the "running" state.
        try:
            from tools.async_delegation import active_count as _async_active_count
            snapshot["active_background_subagents"] = _async_active_count()
        except Exception:
            pass

        # Standing /goal state (Ralph loop). GoalManager is cached on self and
        # keeps its state in memory, so this is a cheap attribute read — no DB
        # hit per repaint. Only an *active* goal earns a segment; paused/done
        # goals stay out of the bar (matching the desktop's active-first row).
        snapshot["goal_active"] = False
        snapshot["goal_turns_used"] = 0
        snapshot["goal_max_turns"] = 0
        try:
            goal_mgr = self._get_goal_manager()
            if goal_mgr is not None and goal_mgr.is_active():
                goal_state = goal_mgr.state
                snapshot["goal_active"] = True
                snapshot["goal_turns_used"] = int(getattr(goal_state, "turns_used", 0) or 0)
                snapshot["goal_max_turns"] = int(getattr(goal_state, "max_turns", 0) or 0)
        except Exception:
            pass


        if not agent:
            return snapshot

        snapshot["session_input_tokens"] = getattr(agent, "session_input_tokens", 0) or 0
        snapshot["session_output_tokens"] = getattr(agent, "session_output_tokens", 0) or 0
        snapshot["session_cache_read_tokens"] = getattr(agent, "session_cache_read_tokens", 0) or 0
        snapshot["session_cache_write_tokens"] = getattr(agent, "session_cache_write_tokens", 0) or 0
        snapshot["session_prompt_tokens"] = getattr(agent, "session_prompt_tokens", 0) or 0
        snapshot["session_completion_tokens"] = getattr(agent, "session_completion_tokens", 0) or 0
        snapshot["session_total_tokens"] = getattr(agent, "session_total_tokens", 0) or 0
        snapshot["session_api_calls"] = getattr(agent, "session_api_calls", 0) or 0

        compressor = getattr(agent, "context_compressor", None)
        if compressor:
            # last_prompt_tokens is parked at the -1 sentinel right after a
            # compression, until the next real API call reports a prompt count
            # (awaiting_real_usage_after_compression). The status bar must not
            # render that sentinel verbatim — it produced "-1/200K" / "-1%".
            # Clamp it to 0 so the one transitional turn reads as empty context.
            context_tokens = getattr(compressor, "last_prompt_tokens", 0) or 0
            if context_tokens < 0:
                context_tokens = 0
            context_length = getattr(compressor, "context_length", 0) or 0
            if context_length < 0:
                context_length = 0
            snapshot["context_tokens"] = context_tokens
            snapshot["context_length"] = context_length or None
            snapshot["compressions"] = getattr(compressor, "compression_count", 0) or 0
            if context_length:
                snapshot["context_percent"] = max(0, min(100, round((context_tokens / context_length) * 100)))

        return snapshot

    @staticmethod
    def _status_bar_display_width(text: str) -> int:
        """Return terminal cell width for status-bar text.

        len() is not enough for prompt_toolkit layout decisions because some
        glyphs can render wider than one Python codepoint. Keeping the status
        bar within the real display width prevents it from wrapping onto a
        second line and leaving behind duplicate rows.
        """
        try:
            from prompt_toolkit.utils import get_cwidth
            return get_cwidth(text or "")
        except Exception:
            return len(text or "")

    @classmethod
    def _trim_status_bar_text(cls, text: str, max_width: int) -> str:
        """Trim status-bar text to a single terminal row."""
        if max_width <= 0:
            return ""
        try:
            from prompt_toolkit.utils import get_cwidth
        except Exception:
            get_cwidth = None

        if cls._status_bar_display_width(text) <= max_width:
            return text

        ellipsis = "..."
        ellipsis_width = cls._status_bar_display_width(ellipsis)
        if max_width <= ellipsis_width:
            return ellipsis[:max_width]

        out = []
        width = 0
        for ch in text:
            ch_width = get_cwidth(ch) if get_cwidth else len(ch)
            if width + ch_width + ellipsis_width > max_width:
                break
            out.append(ch)
            width += ch_width
        return "".join(out).rstrip() + ellipsis

    @staticmethod
    def _get_tui_terminal_width(default: tuple[int, int] = (80, 24)) -> int:
        """Return the live prompt_toolkit width, falling back to ``shutil``.

        The TUI layout can be narrower than ``shutil.get_terminal_size()`` reports,
        especially on Termux/mobile shells, so prefer prompt_toolkit's width whenever
        an app is active.
        """
        try:
            from prompt_toolkit.application import get_app
            return get_app().output.get_size().columns
        except Exception:
            return shutil.get_terminal_size(default).columns

    def _use_minimal_tui_chrome(self, width: Optional[int] = None) -> bool:
        """Hide low-value chrome on narrow/mobile terminals to preserve rows."""
        if width is None:
            width = self._get_tui_terminal_width()
        return width < 64

    @staticmethod
    def _scrollback_box_width(width: Optional[int] = None) -> int:
        """Return the full viewport width for printed scrollback box rules.

        Previously this clamped to ``max(32, min(width, 56))`` as a defense
        against terminal-emulator reflow on column-shrink (#25975, salvaging
        #24403).  That clamp made response/reasoning borders look stubby on
        any modern wide terminal.  We now trust the prompt_toolkit
        ``_output_screen_diff`` monkey-patch landed in #26137 (salvaging
        #25981) to keep chrome out of scrollback in the first place, and
        accept that an aggressive column-shrink may visually reflow already
        printed Panel borders — that's a cosmetic artifact of stamped
        scrollback history, not a live-render bug.

        A small floor (32 cols) is kept so the box still renders on tiny
        terminals without negative ``'─' * (w - 2)`` math.
        """
        if width is None:
            try:
                width = shutil.get_terminal_size((80, 24)).columns
            except Exception:
                width = 80
        return max(32, int(width or 80))

    def _tui_input_rule_height(self, position: str, width: Optional[int] = None) -> int:
        """Return the visible height for the top/bottom input separator rules."""
        if position not in {"top", "bottom"}:
            raise ValueError(f"Unknown input rule position: {position}")
        if getattr(self, "_status_bar_suppressed_after_resize", False):
            return 0
        if position == "top":
            return 1
        return 0 if self._use_minimal_tui_chrome(width=width) else 1

    def _agent_spacer_height(self, width: Optional[int] = None) -> int:
        """Return the spacer height shown above the status bar while the agent runs."""
        if not getattr(self, "_agent_running", False):
            return 0
        return 0 if self._use_minimal_tui_chrome(width=width) else 1

    def _spinner_widget_height(self, width: Optional[int] = None) -> int:
        """Return the visible height for the spinner/status text line above the status bar."""
        spinner_line = self._render_spinner_text()
        if not spinner_line:
            return 0
        if self._use_minimal_tui_chrome(width=width):
            return 0
        width = width or self._get_tui_terminal_width()
        if width and width > 10:
            import math
            text_width = self._status_bar_display_width(spinner_line)
            return max(1, math.ceil(text_width / width))
        return 1

    def _render_spinner_text(self) -> str:
        """Return the live spinner/status text exactly as rendered in the TUI."""
        txt = getattr(self, "_spinner_text", "")
        if not txt:
            return ""
        flow = self._spinner_token_flow()
        t0 = getattr(self, "_tool_start_time", 0) or 0
        if t0 > 0:
            elapsed = time.monotonic() - t0
            if elapsed >= 60:
                _m, _s = int(elapsed // 60), int(elapsed % 60)
                # Fixed-width timer to avoid status-line wrap jitter while
                # scrolling/repainting (e.g. 01m05s, 12m09s).
                elapsed_str = f"{_m:02d}m{_s:02d}s"
            else:
                # Keep width stable before the 60s rollover as well.
                elapsed_str = f"{elapsed:5.1f}s"
            if flow:
                return f"  {txt}  ({elapsed_str} · {flow})"
            return f"  {txt}  ({elapsed_str})"
        if flow:
            return f"  {txt}  ({flow})"
        return f"  {txt}"

    @staticmethod
    def _status_bar_goal_segment(snapshot: Dict[str, Any]) -> str:
        """Return the ``⊙ goal 3/20`` segment, or ``""`` when no goal is active.

        Active-goal-only by design: paused/done goals don't occupy status-bar
        real estate (they already print their own glyph lines in the thread).
        """
        if not snapshot.get("goal_active"):
            return ""
        used = snapshot.get("goal_turns_used") or 0
        max_turns = snapshot.get("goal_max_turns") or 0
        if max_turns:
            return f"⊙ goal {used}/{max_turns}"
        return "⊙ goal"

    def _build_status_bar_text(self, width: Optional[int] = None) -> str:
        """Return a compact one-line session status string for the TUI footer."""
        from cli import format_token_count_compact

        try:
            snapshot = self._get_status_bar_snapshot()
            if width is None:
                width = self._get_tui_terminal_width()
            percent = snapshot["context_percent"]
            percent_label = f"{percent}%" if percent is not None else "--"
            duration_label = snapshot["duration"]
            battery_label = snapshot.get("battery_label") or ""
            battery_prefix = f"{battery_label} │ " if battery_label else ""
            focus_label = snapshot.get("focus_label") or ""

            yolo_active = self._is_session_yolo_active()
            goal_segment = self._status_bar_goal_segment(snapshot)
            if width < 52:
                text = f"{battery_prefix}⚕ {snapshot['model_short']} · {duration_label}"
                if goal_segment:
                    text += f" · {goal_segment}"
                if focus_label:
                    text += f" · {focus_label}"
                if yolo_active:
                    text += " · ⚠ YOLO"
                return self._trim_status_bar_text(text, width)
            if width < 76:
                parts = [f"⚕ {snapshot['model_short']}", percent_label]
                if battery_label:
                    parts.insert(0, battery_label)
                compressions = snapshot.get("compressions", 0)
                if compressions:
                    parts.append(f"🗜️ {compressions}")
                bg_count = snapshot.get("active_background_tasks", 0)
                if bg_count:
                    parts.append(f"▶ {bg_count}")
                bg_proc_count = snapshot.get("active_background_processes", 0)
                if bg_proc_count:
                    parts.append(f"⚙ {bg_proc_count}")
                bg_subagent_count = snapshot.get("active_background_subagents", 0)
                if bg_subagent_count:
                    parts.append(f"⛓ {bg_subagent_count}")
                if goal_segment:
                    parts.append(goal_segment)
                parts.append(duration_label)
                if focus_label:
                    parts.append(focus_label)
                if yolo_active:
                    parts.append("⚠ YOLO")
                return self._trim_status_bar_text(" · ".join(parts), width)

            if snapshot["context_length"]:
                ctx_total = _format_context_length(snapshot["context_length"])
                ctx_used = format_token_count_compact(snapshot["context_tokens"])
                context_label = f"{ctx_used}/{ctx_total}"
            else:
                context_label = "ctx --"

            compressions = snapshot.get("compressions", 0)
            parts = [f"⚕ {snapshot['model_short']}", context_label, percent_label]
            if battery_label:
                parts.insert(0, battery_label)
            if compressions:
                parts.append(f"🗜️ {compressions}")
            bg_count = snapshot.get("active_background_tasks", 0)
            if bg_count:
                parts.append(f"▶ {bg_count}")
            bg_proc_count = snapshot.get("active_background_processes", 0)
            if bg_proc_count:
                parts.append(f"⚙ {bg_proc_count}")
            bg_subagent_count = snapshot.get("active_background_subagents", 0)
            if bg_subagent_count:
                parts.append(f"⛓ {bg_subagent_count}")
            if goal_segment:
                parts.append(goal_segment)
            parts.append(duration_label)
            prompt_elapsed = snapshot.get("prompt_elapsed")
            if prompt_elapsed:
                parts.append(prompt_elapsed)
            idle_since = snapshot.get("idle_since")
            if idle_since:
                parts.append(idle_since)
            if focus_label:
                parts.append(focus_label)
            if yolo_active:
                parts.append("⚠ YOLO")
            return self._trim_status_bar_text(" │ ".join(parts), width)
        except Exception:
            return f"⚕ {self.model if getattr(self, 'model', None) else 'Hermes'}"

    def _get_status_bar_fragments(self):
        from cli import format_token_count_compact

        if not self._status_bar_visible or getattr(self, '_model_picker_state', None):
            return []
        try:
            snapshot = self._get_status_bar_snapshot()
            # Use prompt_toolkit's own terminal width when running inside the
            # TUI — shutil.get_terminal_size() can return stale or fallback
            # values (especially on SSH) that differ from what prompt_toolkit
            # actually renders, causing the fragments to overflow to a second
            # line and produce duplicated status bar rows over long sessions.
            width = self._get_tui_terminal_width()
            duration_label = snapshot["duration"]
            yolo_active = self._is_session_yolo_active()
            goal_segment = self._status_bar_goal_segment(snapshot)
            battery_label = snapshot.get("battery_label") or ""
            battery_style = self._battery_status_style(snapshot.get("battery_category", "dim"))
            focus_label = snapshot.get("focus_label") or ""

            if width < 52:
                frags = [
                    ("class:status-bar", " ⚕ "),
                    ("class:status-bar-strong", snapshot["model_short"]),
                    ("class:status-bar-dim", " · "),
                    ("class:status-bar-dim", duration_label),
                ]
                if goal_segment:
                    frags.append(("class:status-bar-dim", " · "))
                    frags.append(("class:status-bar-strong", goal_segment))
                if focus_label:
                    frags.append(("class:status-bar-dim", " · "))
                    frags.append(("class:status-bar-strong", focus_label))
                if yolo_active:
                    frags.append(("class:status-bar-dim", " · "))
                    frags.append(("class:status-bar-yolo", "⚠ YOLO"))
                frags.append(("class:status-bar", " "))
            else:
                percent = snapshot["context_percent"]
                percent_label = f"{percent}%" if percent is not None else "--"
                if width < 76:
                    compressions = snapshot.get("compressions", 0)
                    bg_count = snapshot.get("active_background_tasks", 0)
                    bg_proc_count = snapshot.get("active_background_processes", 0)
                    bg_subagent_count = snapshot.get("active_background_subagents", 0)
                    frags = [
                        ("class:status-bar", " ⚕ "),
                        ("class:status-bar-strong", snapshot["model_short"]),
                        ("class:status-bar-dim", " · "),
                        (self._status_bar_context_style(percent), percent_label),
                    ]
                    if compressions:
                        frags.append(("class:status-bar-dim", " · "))
                        frags.append((self._compression_count_style(compressions), f"🗜️ {compressions}"))
                    if bg_count:
                        frags.append(("class:status-bar-dim", " · "))
                        frags.append(("class:status-bar-strong", f"▶ {bg_count}"))
                    if bg_proc_count:
                        frags.append(("class:status-bar-dim", " · "))
                        frags.append(("class:status-bar-strong", f"⚙ {bg_proc_count}"))
                    if bg_subagent_count:
                        frags.append(("class:status-bar-dim", " · "))
                        frags.append(("class:status-bar-strong", f"⛓ {bg_subagent_count}"))
                    if goal_segment:
                        frags.append(("class:status-bar-dim", " · "))
                        frags.append(("class:status-bar-strong", goal_segment))
                    frags.extend([
                        ("class:status-bar-dim", " · "),
                        ("class:status-bar-dim", duration_label),
                    ])
                    if focus_label:
                        frags.append(("class:status-bar-dim", " · "))
                        frags.append(("class:status-bar-strong", focus_label))
                    if yolo_active:
                        frags.append(("class:status-bar-dim", " · "))
                        frags.append(("class:status-bar-yolo", "⚠ YOLO"))
                    frags.append(("class:status-bar", " "))
                else:
                    if snapshot["context_length"]:
                        ctx_total = _format_context_length(snapshot["context_length"])
                        ctx_used = format_token_count_compact(snapshot["context_tokens"])
                        context_label = f"{ctx_used}/{ctx_total}"
                    else:
                        context_label = "ctx --"

                    bar_style = self._status_bar_context_style(percent)
                    compressions = snapshot.get("compressions", 0)
                    bg_count = snapshot.get("active_background_tasks", 0)
                    bg_proc_count = snapshot.get("active_background_processes", 0)
                    bg_subagent_count = snapshot.get("active_background_subagents", 0)
                    frags = [
                        ("class:status-bar", " ⚕ "),
                        ("class:status-bar-strong", snapshot["model_short"]),
                        ("class:status-bar-dim", " │ "),
                        ("class:status-bar-dim", context_label),
                        ("class:status-bar-dim", " │ "),
                        (bar_style, self._build_context_bar(percent)),
                        ("class:status-bar-dim", " "),
                        (bar_style, percent_label),
                    ]
                    if compressions:
                        frags.append(("class:status-bar-dim", " │ "))
                        frags.append((self._compression_count_style(compressions), f"🗜️ {compressions}"))
                    if bg_count:
                        frags.append(("class:status-bar-dim", " │ "))
                        frags.append(("class:status-bar-strong", f"▶ {bg_count}"))
                    if bg_proc_count:
                        frags.append(("class:status-bar-dim", " │ "))
                        frags.append(("class:status-bar-strong", f"⚙ {bg_proc_count}"))
                    if bg_subagent_count:
                        frags.append(("class:status-bar-dim", " │ "))
                        frags.append(("class:status-bar-strong", f"⛓ {bg_subagent_count}"))
                    if goal_segment:
                        frags.append(("class:status-bar-dim", " │ "))
                        frags.append(("class:status-bar-strong", goal_segment))
                    frags.extend([
                        ("class:status-bar-dim", " │ "),
                        ("class:status-bar-dim", duration_label),
                    ])
                    # Position 7: per-prompt elapsed timer (live or frozen)
                    prompt_elapsed = snapshot.get("prompt_elapsed")
                    if prompt_elapsed:
                        frags.append(("class:status-bar-dim", " │ "))
                        frags.append(("class:status-bar-dim", prompt_elapsed))
                    # Position 8: idle time since the last final agent response
                    idle_since = snapshot.get("idle_since")
                    if idle_since:
                        frags.append(("class:status-bar-dim", " │ "))
                        frags.append(("class:status-bar-dim", idle_since))
                    # Persistent focus-view badge — so the reduced-output mode
                    # is never invisible (mirrors the YOLO badge convention).
                    if focus_label:
                        frags.append(("class:status-bar-dim", " │ "))
                        frags.append(("class:status-bar-strong", focus_label))
                    if yolo_active:
                        frags.append(("class:status-bar-dim", " │ "))
                        frags.append(("class:status-bar-yolo", "⚠ YOLO"))
                    frags.append(("class:status-bar", " "))

            # Stash indicator (📌 N) — appended after all width tiers so the
            # user always knows a parked draft exists, even on narrow
            # terminals.  Placed before the battery prepend so it stays at the
            # right edge, and it is the first thing the width trim below drops
            # if the bar genuinely cannot fit.
            try:
                stash_indicator = self._prompt_stash.indicator()
            except Exception:
                stash_indicator = ""
            if stash_indicator:
                # Insert before the trailing pad fragment so the bar keeps its
                # one-cell right margin.
                if frags and frags[-1] == ("class:status-bar", " "):
                    frags[-1:-1] = [
                        ("class:status-bar-dim", " · "),
                        ("class:status-bar-strong", stash_indicator),
                    ]
                else:
                    frags.append(("class:status-bar-dim", " · "))
                    frags.append(("class:status-bar-strong", stash_indicator))

            # Battery is the first status-bar element when enabled: prepend it
            # ahead of the leading ⚕ marker in whichever width tier ran above.
            if battery_label:
                frags[0:0] = [
                    ("class:status-bar", " "),
                    (battery_style, battery_label),
                    ("class:status-bar-dim", " │"),
                ]

            total_width = sum(self._status_bar_display_width(text) for _, text in frags)
            if total_width > width:
                plain_text = "".join(text for _, text in frags)
                trimmed = self._trim_status_bar_text(plain_text, width)
                return [("class:status-bar", trimmed)]
            return frags
        except Exception:
            return [("class:status-bar", f" {self._build_status_bar_text()} ")]
