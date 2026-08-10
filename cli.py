#!/usr/bin/env python3
"""
Hermes Agent CLI - Interactive Terminal Interface

A beautiful command-line interface for the Hermes Agent, inspired by Claude Code.
Features ASCII art branding, interactive REPL, toolset selection, and rich formatting.

Usage:
    python cli.py                          # Start interactive mode with all tools
    python cli.py --toolsets web,terminal  # Start with specific toolsets
    python cli.py --skills hermes-agent-dev,github-auth
    python cli.py --list-tools             # List available tools and exit
"""

# IMPORTANT: hermes_bootstrap must be the very first import â€” UTF-8 stdio
# on Windows.  No-op on POSIX.  See hermes_bootstrap.py for full rationale.
try:
    import hermes_bootstrap  # noqa: F401
except ModuleNotFoundError:
    # Graceful fallback when hermes_bootstrap isn't registered in the venv
    # yet â€” happens during partial ``hermes update`` where git-reset landed
    # new code but ``uv pip install -e .`` didn't finish.  Missing bootstrap
    # means UTF-8 stdio setup is skipped on Windows; POSIX is unaffected.
    pass

import logging
import copy
import os
import shutil
import sys
import json
import re
import concurrent.futures
import base64
import atexit
import errno
import tempfile
import time
import uuid
import textwrap
from collections import deque
from urllib.parse import unquote, urlparse
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Suppress startup messages for clean CLI experience
os.environ["HERMES_QUIET"] = "1"  # Our own modules

from hermes_cli.fallback_config import get_fallback_chain
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin
from hermes_cli.cli_commands_mixin import CLICommandsMixin
from hermes_cli.cli_billing_mixin import CLIBillingMixin
from agent.interrupt_compat import request_hard_interrupt

# prompt_toolkit for fixed input area TUI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style as PTStyle
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl, ConditionalContainer, WindowAlign
from prompt_toolkit.layout.processors import Processor, Transformation, PasswordProcessor, ConditionalProcessor
from prompt_toolkit.filters import Condition
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit import print_formatted_text as _pt_print
from prompt_toolkit.formatted_text import ANSI as _PT_ANSI
try:
    from prompt_toolkit.cursor_shapes import CursorShape
    _STEADY_CURSOR = CursorShape.BLOCK  # Non-blinking block cursor
except (ImportError, AttributeError):
    _STEADY_CURSOR = None

try:
    from hermes_cli.pt_input_extras import (
        install_cmd_backspace_alias,
        install_ctrl_enter_alias,
        install_ignored_terminal_sequences,
        install_shift_enter_alias,
    )
    install_shift_enter_alias()
    install_ctrl_enter_alias()
    install_cmd_backspace_alias()
    install_ignored_terminal_sequences()
    del install_shift_enter_alias, install_ctrl_enter_alias, install_cmd_backspace_alias, install_ignored_terminal_sequences
except Exception:
    pass
import threading
import queue

def CanonicalUsage(*args, **kwargs):
    from agent.usage_pricing import CanonicalUsage as _CanonicalUsage

    return _CanonicalUsage(*args, **kwargs)


def estimate_usage_cost(*args, **kwargs):
    from agent.usage_pricing import estimate_usage_cost as _estimate_usage_cost

    return _estimate_usage_cost(*args, **kwargs)


def format_duration_compact(*args, **kwargs):
    seconds = float(args[0] if args else kwargs.get("seconds", 0.0))
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 24:
        remaining_min = int(minutes % 60)
        return f"{int(hours)}h {remaining_min}m" if remaining_min else f"{int(hours)}h"
    days = hours / 24
    return f"{days:.1f}d"


# Cached reverse map of config.yaml ``model_aliases:`` so the TUI can show
# friendly names instead of full Palantir RIDs / long catalog IDs. Built
# lazily on first call; cache is process-lifetime (config is read once at
# session start, so further invalidation is unnecessary).
_REVERSE_ALIAS_CACHE: dict[str, str] | None = None


def _reverse_alias_for_display(model_name: str) -> str:
    """Return the shortest configured alias for ``model_name``, or ``model_name``.

    Looks up both ``model_aliases:`` (dict-based, full DirectAlias entries)
    and ``model.aliases:`` (string-based, set via ``hermes config set``)
    from config.yaml. Multiple aliases pointing at the same model â€” the
    shortest wins, so ``opus47`` beats ``palantir-claude47``.
    """
    global _REVERSE_ALIAS_CACHE
    if not model_name:
        return model_name
    if _REVERSE_ALIAS_CACHE is None:
        rmap: dict[str, str] = {}
        try:
            from hermes_cli.config import load_config
            cfg = load_config() or {}
            ma = cfg.get("model_aliases")
            if isinstance(ma, dict):
                for alias, entry in ma.items():
                    if isinstance(entry, dict):
                        m = str(entry.get("model", "") or "").strip()
                        if m and (m not in rmap or len(alias) < len(rmap[m])):
                            rmap[m] = alias
            mdl = cfg.get("model", {}) or {}
            if isinstance(mdl, dict):
                simple = mdl.get("aliases")
                if isinstance(simple, dict):
                    for alias, val in simple.items():
                        if isinstance(val, str) and val.strip():
                            v = val.strip()
                            m = v.split("/", 1)[1] if "/" in v else v
                            if m and (m not in rmap or len(alias) < len(rmap[m])):
                                rmap[m] = alias
        except Exception:
            pass
        _REVERSE_ALIAS_CACHE = rmap
    return _REVERSE_ALIAS_CACHE.get(model_name, model_name)


def format_token_count_compact(*args, **kwargs):
    value = int(args[0] if args else kwargs.get("value", 0))
    abs_value = abs(value)
    if abs_value < 1_000:
        return str(value)

    sign = "-" if value < 0 else ""
    units = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
    for threshold, suffix in units:
        if abs_value >= threshold:
            scaled = abs_value / threshold
            if scaled < 10:
                text = f"{scaled:.2f}"
            elif scaled < 100:
                text = f"{scaled:.1f}"
            else:
                text = f"{scaled:.0f}"
            if "." in text:
                text = text.rstrip("0").rstrip(".")
            return f"{sign}{text}{suffix}"

    return f"{value:,}"


def is_table_divider(*args, **kwargs):
    from agent.markdown_tables import is_table_divider as _is_table_divider

    return _is_table_divider(*args, **kwargs)


def looks_like_table_row(*args, **kwargs):
    from agent.markdown_tables import looks_like_table_row as _looks_like_table_row

    return _looks_like_table_row(*args, **kwargs)


def realign_markdown_tables(*args, **kwargs):
    from agent.markdown_tables import realign_markdown_tables as _realign_markdown_tables

    return _realign_markdown_tables(*args, **kwargs)
# NOTE: `from agent.account_usage import ...` is deliberately NOT at module
# top â€” it transitively pulls the OpenAI SDK chain (~230 ms cold) and is only
# needed when the user runs `/limits`. Lazy-imported inside the handler below.
from hermes_cli.banner import _format_context_length, format_banner_version_label

_COMMAND_SPINNER_FRAMES = ("â ‹", "â ™", "â ¹", "â ¸", "â ¼", "â ´", "â ¦", "â §", "â ‡", "â ")


# Load .env from ~/.hermes/.env first, then project root as dev fallback.
# User-managed env files should override stale shell exports on restart.
from hermes_constants import get_hermes_home, display_hermes_home
from hermes_cli.browser_connect import (
    DEFAULT_BROWSER_CDP_URL,
    is_browser_debug_ready,
    manual_chrome_debug_command,
    try_launch_chrome_debug,
)
from hermes_cli.env_loader import load_hermes_dotenv
from utils import base_url_host_matches, fast_safe_load

_hermes_home = get_hermes_home()
_project_env = Path(__file__).parent / '.env'
load_hermes_dotenv(hermes_home=_hermes_home, project_env=_project_env)


_REASONING_TAGS = (
    "REASONING_SCRATCHPAD",
    "think",
    "thinking",
    "reasoning",
    "thought",
)


def _strip_reasoning_tags(text: str) -> str:
    """Remove reasoning/thinking blocks from displayed text.

    Handles every case:
      * Closed pairs ``<tag>â€¦</tag>`` (case-insensitive, multi-line).
      * Unterminated open tags that run to end-of-text (e.g. truncated
        generations on NIM/MiniMax where the close tag is dropped).
      * Stray orphan close tags (``stuff</think>answer``) left behind by
        partial-content dumps.

    Covers the variants emitted by reasoning models today: ``<think>``,
    ``<thinking>``, ``<reasoning>``, ``<REASONING_SCRATCHPAD>``, and
    ``<thought>`` (Gemma 4).  Must stay in sync with
    ``run_agent.py::_strip_think_blocks`` and the stream consumer's
    ``_OPEN_THINK_TAGS`` / ``_CLOSE_THINK_TAGS`` tuples.

    Also strips tool-call XML blocks some open models leak into visible
    content (``<tool_call>``, ``<function_calls>``, Gemma-style
    ``<function name="â€¦">â€¦</function>``). Ported from
    openclaw/openclaw#67318.
    """
    cleaned = text
    for tag in _REASONING_TAGS:
        # Closed pair â€” case-insensitive so <THINK>â€¦</THINK> is handled too.
        cleaned = re.sub(
            rf"<{tag}>.*?</{tag}>\s*",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Unterminated open tag â€” strip from the tag to end of text.
        cleaned = re.sub(
            rf"<{tag}>.*$",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Stray orphan close tag left behind by partial dumps.
        cleaned = re.sub(
            rf"</{tag}>\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
    # Tool-call XML blocks (openclaw/openclaw#67318).
    for tc_tag in ("tool_call", "tool_calls", "tool_result",
                   "function_call", "function_calls"):
        cleaned = re.sub(
            rf"<{tc_tag}\b[^>]*>.*?</{tc_tag}>\s*",
            "",
            cleaned,
            flags=re.DOTALL | re.IGNORECASE,
        )
    # <function name="..."> â€” boundary + attribute gated to avoid prose FPs.
    cleaned = re.sub(
        r'(?:(?<=^)|(?<=[\n\r.!?:]))[ \t]*'
        r'<function\b[^>]*\bname\s*=[^>]*>'
        r'(?:(?:(?!</function>).)*)</function>\s*',
        '',
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Stray tool-call close tags.
    cleaned = re.sub(
        r'</(?:tool_call|tool_calls|tool_result|function_call|function_calls|function)>\s*',
        '',
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned.strip()


def _assistant_content_as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return str(content)


def _assistant_copy_text(content: Any) -> str:
    return _strip_reasoning_tags(_assistant_content_as_text(content))


# =============================================================================
# Configuration Loading
# =============================================================================

def _load_prefill_messages(file_path: str) -> List[Dict[str, Any]]:
    """Load ephemeral prefill messages from a JSON file.
    
    The file should contain a JSON array of {role, content} dicts, e.g.:
        [{"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Hello!"}]
    
    Relative paths are resolved from ~/.hermes/.
    Returns an empty list if the path is empty or the file doesn't exist.
    """
    if not file_path:
        return []
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = _hermes_home / path
    if not path.exists():
        logger.warning("Prefill messages file not found: %s", path)
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            logger.warning("Prefill messages file must contain a JSON array: %s", path)
            return []
        return data
    except Exception as e:
        logger.warning("Failed to load prefill messages from %s: %s", path, e)
        return []


def _resolve_prefill_messages_file(config: Dict[str, Any]) -> str:
    """Resolve the prefill file path from env/config.

    ``prefill_messages_file`` at the top level is the canonical config key.
    ``agent.prefill_messages_file`` remains a legacy fallback for older CLI and
    godmode-generated configs.
    """
    env_path = os.getenv("HERMES_PREFILL_MESSAGES_FILE", "").strip()
    if env_path:
        return env_path
    top_level = str(config.get("prefill_messages_file", "") or "").strip()
    if top_level:
        return top_level
    agent_cfg = config.get("agent", {})
    if isinstance(agent_cfg, dict):
        return str(agent_cfg.get("prefill_messages_file", "") or "").strip()
    return ""


def _parse_reasoning_config(effort) -> dict | None:
    """Parse a reasoning effort level into an OpenRouter reasoning config dict.

    Accepts the raw config value (string or YAML boolean â€” ``false``/``off``
    parse as thinking disabled, see parse_reasoning_effort).
    """
    from hermes_constants import parse_reasoning_effort
    result = parse_reasoning_effort(effort)
    if effort and str(effort).strip() and result is None:
        logger.warning("Unknown reasoning_effort '%s', using default (medium)", effort)
    return result


def _parse_service_tier_config(raw: str) -> str | None:
    """Parse a persisted service-tier preferenÛµã‹h‘éì¶»§q«^uÌ¥‘•¹Ñ¥…±±ä¥¸Ñ¡”QU$…¹1$4(€€€€€€€€Œ€¡½Á¥±½ĞÉ½Õ¹´äÉ•Ù¥•Ü½¸€ŒÄäàÌÔ¤¸ÍÕÁ•É€½İ¥¹€½İ¥¹‘½İÍ€4(€€€€€€€€Œ½¹™¥ÌÍ¥±•¹Ñ±ä™…±°‰…¬Ñ¼Ñ¡”‘•™…Õ±Ğ¡•É”Í¥¹”ÁÉ½µÁÑ}Ñ½½±­¥Ğ4(€€€€€€€€Œ¡…Ì¹¼ÍÕÁ•Èµ½‘¥™¥•ÈƒŠP±½œ„İ…É¹¥¹œÍ¼ÕÍ•ÉÌ¹½Ñ¥”Ñ¡”4(€€€€€€€€ŒQU$½1$ÍÁ±¥Ğ¥¹ÍÑ•…½˜„Í¥±•¹Ğµ¥Íµ…Ñ €¡É½Õ¹´ÄÄ¤¸4(€€€€€€€}É…İ}­•äè½‰©•Ğ€ô€‰ÑÉ°­ˆˆ4(€€€€€€€ÑÉäè4(€€€€€€€€€€€™É½´¡•Éµ•Í}±¤¹½¹™¥œ¥µÁ½ÉĞ±½…‘}½¹™¥œ4(€€€€€€€€€€€™É½´¡•Éµ•Í}±¤¹Ù½¥”¥µÁ½ÉĞ€ 4(€€€€€€€€€€€€€€€¹½Éµ…±¥é•}Ù½¥•}É•½É‘}­•å}™½É}ÁÉ½µÁÑ}Ñ½½±­¥Ğ°4(€€€€€€€€€€€€€€€Ù½¥•}É•½É‘}­•å}™É½µ}½¹™¥œ°4(€€€€€€€€€€€€¤4(€€€€€€€€€€€}É…İ}­•ä€ôÙ½¥•}É•½É‘}­•å}™É½µ}½¹™¥œ¡±½…‘}½¹™¥œ ¤¤4(€€€€€€€€€€€}Ù½¥•}­•ä€ô¹½Éµ…±¥é•}Ù½¥•}É•½É‘}­•å}™½É}ÁÉ½µÁÑ}Ñ½½±­¥Ğ¡}É…İ}­•ä¤4(€€€€€€€€€€€¥˜€ 4(€€€€€€€€€€€€€€€¥Í¥¹ÍÑ…¹”¡}É…İ}­•ä°ÍÑÈ¤4(€€€€€€€€€€€€€€€…¹}É…İ}­•ä¹ÍÑÉ¥À ¤¹±½İ•È ¤¹ÍÁ±¥Ğ ˆ¬ˆ°€Ä¥lÁt¹ÍÑÉ¥À ¤¥¸ì‰ÍÕÁ•Èˆ°€‰İ¥¸ˆ°€‰İ¥¹‘½İÌ‰ô4(€€€€€€€€€€€€€€€…¹}Ù½¥•}­•ä€ôô€‰Œµˆˆ4(€€€€€€€€€€€€¤è4(€€€€€€€€€€€€€€€±½•È¹İ…É¹¥¹œ 4(€€€€€€€€€€€€€€€€€€€€‰Ù½¥”¹É•½É‘}­•ä€•ÈÕÍ•Ì„QU$µ½¹±äµ½‘¥™¥•È€¡ÍÕÁ•È½İ¥¸¤ì€ˆ4(€€€€€€€€€€€€€€€€€€€€‰1$™•±°‰…¬Ñ¼ÑÉ°­¸UÍ”ÑÉ°¬ñ­•äø½È…±Ğ¬ñ­•äø™½È€ˆ4(€€€€€€€€€€€€€€€€€€€€‰É½ÍÌµÉÕ¹Ñ¥µ”Á…É¥Ñä¸ˆ°4(€€€€€€€€€€€€€€€€€€€}É…İ}­•ä°4(€€€€€€€€€€€€€€€€¤4(€€€€€€€•á•ÁĞá•ÁÑ¥½¸è4(€€€€€€€€€€€}Ù½¥•}­•ä€ô€‰Œµˆˆ4(4(€€€€€€€€Œ…¡”Ñ¡”U$±…‰•°¡•É”ƒŠPÍ…µ”}É…İ}­•å€Ñ¡…Ğ‘É¥Ù•ÌÑ¡”4(€€€€€€€€ŒÁÉ½µÁÑ}Ñ½½±­¥Ğ‰¥¹‘¥¹œ‰•±½Ü¸Ù•ÉäÍÑ…ÑÕÌ€¼Á±…•¡½±‘•È€¼4(€€€€€€€€ŒÉ•½É‘¥¹œµ¡¥¹ĞÉ•¹‘•ÈÉ•…‘ÌÑ¡¥Ì…¡•Ù…±Õ”Í¼‘¥ÍÁ±…ä…¸4(€€€€€€€€Œ¹•Ù•È‘É¥™Ğ™É½´Ñ¡”±¥Ù”­•å‰¥¹‘¥¹œ•Ù•¸¥˜Ñ¡”ÕÍ•È•‘¥ÑÌ4(€€€€€€€€ŒÙ½¥”¹É•½É‘}­•äµ¥µÍ•ÍÍ¥½¸€¡½Á¥±½ĞÉ½Õ¹´ÄÌ½¸€ŒÄäàÌÔ¤¸4(€€€€€€€Í•±˜¹Í•Ñ}Ù½¥•}É•½É‘}­•å}…¡”¡}É…İ}­•ä¤4(4(€€€€€€€­ˆ¹…‘¡}Ù½¥•}­•ä¤4(€€€€€€€‘•˜¡…¹‘±•}Ù½¥•}É•½É¡•Ù•¹Ğ¤è4(€€€€€€€€€€€€ˆˆ‰Q½±”Ù½¥”É•½É‘¥¹œİ¡•¸Ù½¥”µ½‘”¥Ì…Ñ¥Ù”¸4(4(€€€€€€€€€€€%5A=IQ9PèQ¡¥Ì¡…¹‘±•ÈÉÕ¹Ì¥¸ÁÉ½µÁÑ}Ñ½½±­¥ĞÌ•Ù•¹Ğµ±½½ÀÑ¡É•…¸4(€€€€€€€€€€€¹ä‰±½­¥¹œ…±°¡•É”€¡±½­Ì°Í¹İ…¥Ğ°‘¥Í¬$½<¤™É••é•ÌÑ¡”4(€€€€€€€€€€€•¹Ñ¥É”U$¸€±°¡•…Ùäİ½É¬¥Ì‘¥ÍÁ…Ñ¡•Ñ¼‘…•µ½¸Ñ¡É•…‘Ì¸4(€€€€€€€€€€€€ˆˆˆ4(€€€€€€€€€€€¥˜¹½Ğ±¥}É•˜¹}Ù½¥•}µ½‘”è4(€€€€€€€€€€€€€€€É•ÑÕÉ¸4(€€€€€€€€€€€€Œ±İ…åÌ…±±½ÜMQ=AA%9„É•½É‘¥¹œ€¡•Ù•¸İ¡•¸…•¹Ğ¥ÌÉÕ¹¹¥¹œ¤4(€€€€€€€€€€€¥˜±¥}É•˜¹}Ù½¥•}É•½É‘¥¹œè4(€€€€€€€€€€€€€€€€Œ5…¹Õ…°ÍÑ½ÀÙ¥„ÁÕÍ µÑ¼µÑ…±¬­•äèÍÑ½À½¹Ñ¥¹Õ½ÕÌµ½‘”4(€€€€€€€€€€€€€€€İ¥Ñ ±¥}É•˜¹}Ù½¥•}±½¬è4(€€€€€€€€€€€€€€€€€€€±¥}É•˜¹}Ù½¥•}½¹Ñ¥¹Õ½ÕÌ€ô…±Í”4(€€€€€€€€€€€€€€€€Œ±…œ±•…É¥¹œ¥Ì¡…¹‘±•…Ñ½µ¥…±±ä¥¹Í¥‘”}Ù½¥•}ÍÑ½Á}…¹‘}ÑÉ…¹ÍÉ¥‰”4(€€€€€€€€€€€€€€€•Ù•¹Ğ¹…ÁÀ¹¥¹Ù…±¥‘…Ñ” ¤4(€€€€€€€€€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•… 4(€€€€€€€€€€€€€€€€€€€Ñ…É•Ğõ±¥}É•˜¹}Ù½¥•}ÍÑ½Á}…¹‘}ÑÉ…¹ÍÉ¥‰”°4(€€€€€€€€€€€€€€€€€€€‘…•µ½¸õQÉÕ”°4(€€€€€€€€€€€€€€€€¤¹ÍÑ…ÉĞ ¤4(€€€€€€€€€€€•±Í”è4(€€€€€€€€€€€€€€€€Œ±±½Ü‘¥Í…Éµ¥¹œ½¹Ñ¥¹Õ½ÕÌµ½‘”•Ù•¸İ¡•¸Ñ¡”…•¹Ğ¥Ì4(€€€€€€€€€€€€€€€€ŒÉÕ¹¹¥¹œ½ÈÑÉ…¹ÍÉ¥‰¥¹œƒŠP½Ñ¡•Éİ¥Í”Ñ¡”ÕÍ•È¥ÌÍÑÕ¬¥¸4(€€€€€€€€€€€€€€€€Œ…¸…ÕÑ¼µÉ•ÍÑ…ÉĞ±½½ÀÕ¹Ñ¥°€½Ù½¥”½™˜€ ŒØÜÔĞÔ¤¸4(€€€€€€€€€€€€€€€¥˜±¥}É•˜¹}…•¹Ñ}ÉÕ¹¹¥¹œ½È±¥}É•˜¹}Ù½¥•}ÁÉ½•ÍÍ¥¹œè4(€€€€€€€€€€€€€€€€€€€İ¥Ñ ±¥}É•˜¹}Ù½¥•}±½¬è4(€€€€€€€€€€€€€€€€€€€€€€€±¥}É•˜¹}Ù½¥•}½¹Ñ¥¹Õ½ÕÌ€ô…±Í”4(€€€€€€€€€€€€€€€€€€€•Ù•¹Ğ¹…ÁÀ¹¥¹Ù…±¥‘…Ñ” ¤4(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸4(€€€€€€€€€€€€€€€€ŒÕ…Éè‘½¸ĞMQIPÉ•½É‘¥¹œ‘ÕÉ¥¹œ¥¹Ñ•É…Ñ¥Ù”ÁÉ½µÁÑÌ4(€€€€€€€€€€€€€€€¥˜±¥}É•˜¹}±…É¥™å}ÍÑ…Ñ”½È±¥}É•˜¹}ÍÕ‘½}ÍÑ…Ñ”½È±¥}É•˜¹}…ÁÁÉ½Ù…±}ÍÑ…Ñ”½È±¥}É•˜¹}Í±…Í¡}½¹™¥Éµ}ÍÑ…Ñ”è4(€€€€€€€€€€€€€€€€€€€É•ÑÕÉ¸4(4(€€€€€€€€€€€€€€€€Œ%¹Ñ•ÉÉÕÁĞQQL¥˜Á±…å¥¹œ°Í¼ÕÍ•È…¸ÍÑ…ÉĞÑ…±­¥¹œ¸4(€€€€€€€€€€€€€€€€ŒÍÑ½Á}Á±…å‰…¬ ¤¥Ì™…ÍĞ€¡©ÕÍĞÑ•Éµ¥¹…Ñ•Ì„ÍÕ‰ÁÉ½•ÍÌ¤ì4(€€€€€€€€€€€€€€€€ŒÑ¡”ÍÑ½À•Ù•¹Ğ‘É…¥¹ÌÑ¡”ÍÑÉ•…µ¥¹œÁ¥Á•±¥¹”¥˜½¹”¥Ì±¥Ù”¸4(€€€€€€€€€€€€€€€¥˜¹½Ğ±¥}É•˜¹}Ù½¥•}ÑÑÍ}‘½¹”¹¥Í}Í•Ğ ¤è4(€€€€€€€€€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€€€€€€€€€±½•È¹¥¹™¼ ‰QQLUPèÉ•½É­•ä¡…¹‘±•ÈÕÑÑ¥¹œQQLˆ¤4(€€€€€€€€€€€€€€€€€€€€€€€™É½´Ñ½½±Ì¹ÑÑÍ}ÍÑÉ•…µ¥¹œ¥µÁ½ÉĞµ…É­}ÍÁ••¡}¥¹Ñ•ÉÉÕÁÑ•4(€€€€€€€€€€€€€€€€€€€€€€€µ…É­}ÍÁ••¡}¥¹Ñ•ÉÉÕÁÑ• ¤4(€€€€€€€€€€€€€€€€€€€€€€€¥˜±¥}É•˜¹}Ù½¥•}ÑÑÍ}ÍÑ½À¥Ì¹½Ğ9½¹”è4(€€€€€€€€€€€€€€€€€€€€€€€€€€€±¥}É•˜¹}Ù½¥•}ÑÑÍ}ÍÑ½À¹Í•Ğ ¤4(€€€€€€€€€€€€€€€€€€€€€€€™É½´Ñ½½±Ì¹Ù½¥•}µ½‘”¥µÁ½ÉĞÍÑ½Á}Á±…å‰…¬4(€€€€€€€€€€€€€€€€€€€€€€€ÍÑ½Á}Á±…å‰…¬ ¤4(€€€€€€€€€€€€€€€€€€€€€€€±¥}É•˜¹}Ù½¥•}ÑÑÍ}‘½¹”¹Í•Ğ ¤4(€€€€€€€€€€€€€€€€€€€•á•ÁĞá•ÁÑ¥½¸è4(€€€€€€€€€€€€€€€€€€€€€€€Á…ÍÌ4(4(€€€€€€€€€€€€€€€İ¥Ñ ±¥}É•˜¹}Ù½¥•}±½¬è4(€€€€€€€€€€€€€€€€€€€±¥}É•˜¹}Ù½¥•}½¹Ñ¥¹Õ½ÕÌ€ôQÉÕ”4(4(€€€€€€€€€€€€€€€€Œ¥ÍÁ…Ñ Ñ¼„‘…•µ½¸Ñ¡É•…Í¼Á±…å}‰••À¡Í¹İ…¥Ğ¤°4(€€€€€€€€€€€€€€€€ŒÕ‘¥½I•½É‘•È¹ÍÑ…ÉĞ¡±½¬…ÅÕ¥É”¤°…¹½¹™¥œ$½<4(€€€€€€€€€€€€€€€€Œ¹•Ù•È‰±½¬Ñ¡”ÁÉ½µÁÑ}Ñ½½±­¥Ğ•Ù•¹Ğ±½½À¸4(€€€€€€€€€€€€€€€‘•˜}ÍÑ…ÉÑ}É•½É‘¥¹œ ¤è4(€€€€€€€€€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€€€€€€€€€±¥}É•˜¹}Ù½¥•}ÍÑ…ÉÑ}É•½É‘¥¹œ ¤4(€€€€€€€€€€€€€€€€€€€€€€€¥˜¡…Í…ÑÑÈ¡±¥}É•˜°€}…ÁÀœ¤…¹±¥}É•˜¹}…ÁÀè4(€€€€€€€€€€€€€€€€€€€€€€€€€€€±¥}É•˜¹}…ÁÀ¹¥¹Ù…±¥‘…Ñ” ¤4(€€€€€€€€€€€€€€€€€€€•á•ÁĞá•ÁÑ¥½¸…Ì”è4(€€€€€€€€€€€€€€€€€€€€€€€}ÁÉ¥¹Ğ¡˜‰q¹í}%5õY½¥”É•½É‘¥¹œ™…¥±•èí•õí}IMQôˆ¤4(4(€€€€€€€€€€€€€€€Ñ¡É•…‘¥¹œ¹Q¡É•…¡Ñ…É•Ğõ}ÍÑ…ÉÑ}É•½É‘¥¹œ°‘…•µ½¸õQÉÕ”¤¹ÍÑ…ÉĞ ¤4(€€€€€€€€€€€€€€€•Ù•¹Ğ¹…ÁÀ¹¥¹Ù…±¥‘…Ñ” ¤4(€€€€€€€™É½´ÁÉ½µÁÑ}Ñ½½±­¥Ğ¹­•åÌ¥µÁ½ÉĞ-•åÌ4(4(€€€€€€€­ˆ¹…‘¡-•åÌ¹	É…­•Ñ•‘A…ÍÑ”°•…•ÈõQÉÕ”¤4(€€€€€€€‘•˜¡…¹‘±•}Á…ÍÑ”¡•Ù•¹Ğ¤è4(€€€€€€€€€€€€ˆˆ‰!…¹‘±”Ñ•Éµ¥¹…°Á…ÍÑ”ƒŠP‘•Ñ•Ğ±¥Á‰½…É¥µ…•Ì¸4(4(€€€€€€€€€€€]¡•¸Ñ¡”Ñ•Éµ¥¹…°ÍÕÁÁ½ÉÑÌ‰É…­•Ñ•Á…ÍÑ”°ÑÉ°­X€¼µ­X4(€€€€€€€€€€€ÑÉ¥•ÉÌÑ¡¥Ìİ¥Ñ Ñ¡”Á…ÍÑ•Ñ•áĞ¸]”½¹±ä…ÕÑ¼µ…ÑÑ… „4(€€€€€€€€€€€±¥Á‰½…É¥µ…”™½È¥µ…”µ½¹±ä½•µÁÑäÁ…ÍÑ”•ÍÑÕÉ•ÌÍ¼Ñ•áĞ4(€€€€€€€€€€€Á…ÍÑ•Ì…¹‘¥Ñ…Ñ¥½¸‘¼¹½Ğ…¥‘•¹Ñ…±±ä…ÑÑ… ÍÑ…±”¥µ…•Ì¸4(4(€€€€€€€€€€€1…É”Á…ÍÑ•Ì€ Ô¬±¥¹•Ì¤…É”½±±…ÁÍ•Ñ¼„™¥±”É•™•É•¹”4(€€€€€€€€€€€Á±…•¡½±‘•Èİ¡¥±”ÁÉ•Í•ÉÙ¥¹œ…¹ä•á¥ÍÑ¥¹œÕÍ•ÈÑ•áĞ¥¸Ñ¡”4(€€€€€€€€€€€‰Õ™™•È¸4(€€€€€€€€€€€€ˆˆˆ4(€€€€€€€€€€€€Œ¥…¹½ÍÑ¥Œ…¹…Éäèµ•…ÍÕÉ”¡½Ü±½¹œÑ¡”Á…ÍÑ”¡…¹‘±•È‰±½­Ì4(€€€€€€€€€€€€ŒÑ¡”ÁÉ½µÁÑ}Ñ½½±­¥Ğ•Ù•¹Ğ±½½À¸%˜Ñ¡¥Ì•á••‘ÌøÔÀÁµÌİ”±½œ4(€€€€€€€€€€€€Œ¥ĞÍ¼É•ÕÉÉ¥¹œ€‰1$™É••é•Ì½¸Á…ÍÑ”ˆÉ•Á½ÉÑÌ€¡¥ÍÍÕ”€ŒÄØÈØÌ°4(€€€€€€€€€€€€Œµ…=LQ…¡½”€ÈØ€¬¥Q•É´È½¡½ÍÑÑä¤…ÉÉ¥Ù”İ¥Ñ ‘…Ñ„…ÑÑ…¡•¸4(€€€€€€€€€€€}Á…ÍÑ•}¡…¹‘±•É}ÍÑ…ÉĞ€ôÑ¥µ”¹Á•É™}½Õ¹Ñ•È ¤4(€€€€€€€€€€€}Á…ÍÑ•}É…İ}Í¥é”€ô±•¸¡•Ù•¹Ğ¹‘…Ñ„½È€ˆˆ¤4(€€€€€€€€€€€Á…ÍÑ•‘}Ñ•áĞ€ô•Ù•¹Ğ¹‘…Ñ„½È€ˆˆ4(€€€€€€€€€€€€Œ9½Éµ…±¥Í”±¥¹”•¹‘¥¹ÌƒŠP]¥¹‘½İÌqÉq¸…¹½±5…ŒqÈ‰½Ñ ‰•½µ”q¸4(€€€€€€€€€€€€ŒÍ¼Ñ¡”€Ôµ±¥¹”½±±…ÁÍ”Ñ¡É•Í¡½±…¹‘¥ÍÁ±…ä…É”½¹Í¥ÍÑ•¹Ğ¸4(€€€€€€€€€€€Á…ÍÑ•‘}Ñ•áĞ€ôÁ…ÍÑ•‘}Ñ•áĞ¹É•Á±…” qÉq¸œ°€q¸œ¤¹É•Á±…” qÈœ°€q¸œ¤4(€€€€€€€€€€€Á…ÍÑ•‘}Ñ•áĞ€ô}ÍÑÉ¥Á}±•…­•‘}‰É…­•Ñ•‘}Á…ÍÑ•}İÉ…ÁÁ•ÉÌ¡Á…ÍÑ•‘}Ñ•áĞ¤4(€€€€€€€€€€€Á…ÍÑ•‘}Ñ•áĞ°}¡…‘}µ½ÕÍ•}É•Á½ÉÑÌ€ô}ÍÑÉ¥Á}±•…­•‘}Ñ•Éµ¥¹…±}É•ÍÁ½¹Í•Í}İ¥Ñ¡}µ•Ñ„¡Á…ÍÑ•‘}Ñ•áĞ¤4(€€€€€€€€€€€¥˜}¡…‘}µ½ÕÍ•}É•Á½ÉÑÌè4(€€€€€€€€€€€€€€€Í•±˜¹}É•½Ù•É}Ñ•Éµ¥¹…±}¥¹ÁÕÑ}µ½‘•Ì¡É•…Í½¸ô‰µ½ÕÍ”É•Á½ÉÑÌ±•…­•¥¹Ñ¼‰É…­•Ñ•Á…ÍÑ”Á…å±½…ˆ¤4(€€€€€€€€€€€¥˜}Í¡½Õ±‘}…ÕÑ½}…ÑÑ…¡}±¥Á‰½…É‘}¥µ…•}½¹}Á…ÍÑ”¡Á…ÍÑ•‘}Ñ•áĞ¤…¹Í•±˜¹}ÑÉå}…ÑÑ…¡}±¥Á‰½…É‘}¥µ…” ¤è4(€€€€€€€€€€€€€€€•Ù•¹Ğ¹…ÁÀ¹¥¹Ù…±¥‘…Ñ” ¤4(€€€€€€€€€€€¥˜Á…ÍÑ•‘}Ñ•áĞè4(€€€€€€€€€€€€€€€€ŒM…¹¥Ñ¥é”ÍÕÉÉ½…Ñ”¡…É…Ñ•ÉÌ€¡”¹œ¸™É½´]½É½½½±”½ÌÁ…ÍÑ”¤‰•™½É”İÉ¥Ñ¥¹œ4(€€€€€€€€€€€€€€€™É½´ÉÕ¹}…•¹Ğ¥µÁ½ÉĞ}Í…¹¥Ñ¥é•}ÍÕÉÉ½…Ñ•Ì4(€€€€€€€€€€€€€€€Á…ÍÑ•‘}Ñ•áĞ€ô}Í…¹¥Ñ¥é•}ÍÕÉÉ½…Ñ•Ì¡Á…ÍÑ•‘}Ñ•áĞ¤4(€€€€€€€€€€€€€€€±¥¹•}½Õ¹Ğ€ôÁ…ÍÑ•‘}Ñ•áĞ¹½Õ¹Ğ q¸œ¤4(€€€€€€€€€€€€€€€‰Õ˜€ô•Ù•¹Ğ¹ÕÉÉ•¹Ñ}‰Õ™™•È4(€€€€€€€€€€€€€€€Ñ¡É•Í¡½±€ôÍ•±˜¹½¹™¥œ¹•Ğ ‰Á…ÍÑ•}½±±…ÁÍ•}Ñ¡É•Í¡½±ˆ°€Ô¤4(€€€€€€€€€€€€€€€¡…É}Ñ¡É•Í¡½±€ôÍ•±˜¹½¹™¥œ¹•Ğ ‰Á…ÍÑ•}½±±…ÁÍ•}¡…É}Ñ¡É•Í¡½±ˆ°€ÈÀÀÀ¤4(€€€€€€€€€€€€€€€±¥¹•Í}¡¥Ğ€ôÑ¡É•Í¡½±€ø€À…¹±¥¹•}½Õ¹Ğ€øôÑ¡É•Í¡½±4(€€€€€€€€€€€€€€€¡…ÉÍ}¡¥Ğ€ô¡…É}Ñ¡É•Í¡½±€ø€À…¹±•¸¡Á…ÍÑ•‘}Ñ•áĞ¤€øô¡…É}Ñ¡É•Í¡½±4(€€€€€€€€€€€€€€€¥˜€¡±¥¹•Í}¡¥Ğ½È¡…ÉÍ}¡¥Ğ¤…¹¹½Ğ‰Õ˜¹Ñ•áĞ¹ÍÑÉ¥À ¤¹ÍÑ…ÉÑÍİ¥Ñ  œ¼œ¤è4(€€€€€€€€€€€€€€€€€€€}Á…ÍÑ•}½Õ¹Ñ•ÉlÁt€¬ô€Ä4(€€€€€€€€€€€€€€€€€€€Á…ÍÑ•}‘¥È€ô}¡•Éµ•Í}¡½µ”€¼€‰Á…ÍÑ•Ìˆ4(€€€€€€€€€€€€€€€€€€€Á…ÍÑ•}‘¥È¹µ­‘¥È¡Á…É•¹ÑÌõQÉÕ”°•á¥ÍÑ}½¬õQÉÕ”¤4(€€€€€€€€€€€€€€€€€€€Á…ÍÑ•}™¥±”€ôÁ…ÍÑ•}‘¥È€¼˜‰Á…ÍÑ•}í}Á…ÍÑ•}½Õ¹Ñ•ÉlÁuõ}í‘…Ñ•Ñ¥µ”¹¹½Ü ¤¹ÍÑÉ™Ñ¥µ” œ• •4•Lœ¥ô¹ÑáĞˆ4(€€€€€€€€€€€€€€€€€€€Á…ÍÑ•}™¥±”¹İÉ¥Ñ•}Ñ•áĞ¡Á…ÍÑ•‘}Ñ•áĞ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤4(€€€€€€€€€€€€€€€€€€€±½•È¹¥¹™¼ ‰½±±…ÁÍ•Á…ÍÑ”€Œ•è€•±¥¹•Ì°€•¡…ÉÌ€´ø€•Ìˆ°}Á…ÍÑ•}½Õ¹Ñ•ÉlÁt°±¥¹•}½Õ¹Ğ€¬€Ä°±•¸¡Á…ÍÑ•‘}Ñ•áĞ¤°Á…ÍÑ•}™¥±”¤4(€€€€€€€€€€€€€€€€€€€Á±…•¡½±‘•È€ô˜‰mA…ÍÑ•Ñ•áĞ€í}Á…ÍÑ•}½Õ¹Ñ•ÉlÁuôèí±¥¹•}½Õ¹Ğ€¬€Åô±¥¹•ÌqÔÈÄäÈíÁ…ÍÑ•}™¥±•õtˆ4(€€€€€€€€€€€€€€€€€€€ÁÉ•™¥à€ô€ˆˆ4(€€€€€€€€€€€€€€€€€€€¥˜‰Õ˜¹ÕÉÍ½É}Á½Í¥Ñ¥½¸€ø€À…¹‰Õ˜¹Ñ•áÑm‰Õ˜¹ÕÉÍ½É}Á½Í¥Ñ¥½¸€´€Åt€„ô€q¸œè4(€€€€€€€€€€€€€€€€€€€€€€€ÁÉ•™¥à€ô€‰q¸ˆ4(€€€€€€€€€€€€€€€€€€€}Á…ÍÑ•}©ÕÍÑ}½±±…ÁÍ•‘lÁt€ôQÉÕ”4(€€€€€€€€€€€€€€€€€€€‰Õ˜¹¥¹Í•ÉÑ}Ñ•áĞ¡ÁÉ•™¥à€¬Á±…•¡½±‘•È¤4(€€€€€€€€€€€€€€€•±Í”è4(€€€€€€€€€€€€€€€€€€€‰Õ˜¹¥¹Í•ÉÑ}Ñ•áĞ¡Á…ÍÑ•‘}Ñ•áĞ¤4(€€€€€€€€€€€}Á…ÍÑ•}¡…¹‘±•É}•±…ÁÍ•‘}µÌ€ô€¡Ñ¥µ”¹Á•É™}½Õ¹Ñ•È ¤€´}Á…ÍÑ•}¡…¹‘±•É}ÍÑ…ÉĞ¤€¨€ÄÀÀÀ¸À4(€€€€€€€€€€€¥˜}Á…ÍÑ•}¡…¹‘±•É}•±…ÁÍ•‘}µÌ€ø€ÔÀÀ¸Àè4(€€€€€€€€€€€€€€€±½•È¹İ…É¹¥¹œ 4(€€€€€€€€€€€€€€€€€€€€‰M±½Ü‰É…­•Ñ•µÁ…ÍÑ”¡…¹‘±•Èè€”¸Å™µÌÑ¼ÁÉ½•ÍÌ€•‰åÑ•Ì€ˆ4(€€€€€€€€€€€€€€€€€€€€ˆ •±¥¹•Ì¤½¸€•Ì¸%˜Ñ¡”¥¹ÁÕĞ‰•½µ•ÌÕ¹É•ÍÁ½¹Í¥Ù”…™Ñ•È€ˆ4(€€€€€€€€€€€€€€€€€€€€‰Ñ¡¥Ì°…ÑÑ… Ñ¡¥Ì±½œ±¥¹”Ñ¼Ñ¡”‰ÕœÉ•Á½ÉĞ¸ˆ°4(€€€€€€€€€€€€€€€€€€€}Á…ÍÑ•}¡…¹‘±•É}•±…ÁÍ•‘}µÌ°4(€€€€€€€€€€€€€€€€€€€}Á…ÍÑ•}É…İ}Í¥é”°4(€€€€€€€€€€€€€€€€€€€Á…ÍÑ•‘}Ñ•áĞ¹½Õ¹Ğ q¸œ¤€¬€Ä¥˜Á…ÍÑ•‘}Ñ•áĞ•±Í”€À°4(€€€€€€€€€€€€€€€€€€€ÍåÌ¹Á±…Ñ™½É´°4(€€€€€€€€€€€€€€€€¤4(4(€€€€€€€­ˆ¹…‘ ŒµØœ¤4(€€€€€€€‘•˜¡…¹‘±•}ÑÉ±}Ø¡•Ù•¹Ğ¤è4(€€€€€€€€€€€€ˆˆ‰…±±‰…¬¥µ…”Á…ÍÑ”™½ÈÑ•Éµ¥¹…±Ìİ¥Ñ¡½ÕĞ‰É…­•Ñ•Á…ÍÑ”¸4(4(€€€€€€€€€€€=¸1¥¹ÕàÑ•Éµ¥¹…±Ì€¡9=5Q•Éµ¥¹…°°-½¹Í½±”°•ÑŒ¸¤°ÑÉ°­X4(€€€€€€€€€€€Í•¹‘ÌÉ…Ü‰åÑ”€ÁàÄØ¥¹ÍÑ•…½˜ÑÉ¥•É¥¹œ„Á…ÍÑ”¸€Q¡¥Ì4(€€€€€€€€€€€‰¥¹‘¥¹œ…Ñ¡•ÌÑ¡…Ğ…¹¡•­ÌÑ¡”±¥Á‰½…É™½È¥µ…•Ì¸4(€€€€€€€€€€€=¸Ñ•Éµ¥¹…±ÌÑ¡…Ğ<¥¹Ñ•É•ÁĞÑÉ°­X™½ÈÁ…ÍÑ”€¡µ…=L4(€€€€€€€€€€€Q•Éµ¥¹…°°¥Q•É´È°YM½‘”°]¥¹‘½İÌQ•Éµ¥¹…°¤°Ñ¡”‰É…­•Ñ•4(€€€€€€€€€€€Á…ÍÑ”¡…¹‘±•È™¥É•Ì¥¹ÍÑ•……¹Ñ¡¥Ì‰¥¹‘¥¹œ¹•Ù•ÈÑÉ¥•ÉÌ¸4(€€€€€€€€€€€€ˆˆˆ4(€€€€€€€€€€€¥˜Í•±˜¹}ÑÉå}…ÑÑ…¡}±¥Á‰½…É‘}¥µ…” ¤è4(€€€€€€€€€€€€€€€•Ù•¹Ğ¹…ÁÀ¹¥¹Ù…±¥‘…Ñ” ¤4(4(€€€€€€€­ˆ¹…‘ •Í…Á”œ°€Øœ¤4(€€€€€€€‘•˜¡…¹‘±•}…±Ñ}Ø¡•Ù•¹Ğ¤è4(€€€€€€€€€€€€ˆˆ‰±Ğ­XƒŠPÁ…ÍÑ”¥µ…”™É½´±¥Á‰½…É¸4(4(€€€€€€€€€€€±Ğ­•ä½µ‰½ÌÁ…ÍÌÑ¡É½Õ …±°Ñ•Éµ¥¹…°•µÕ±…Ñ½ÉÌ€¡Í•¹Ğ…Ì4(€€€€€€€€€€€M€¬­•ä¤°Õ¹±¥­”ÑÉ°­Xİ¡¥ Ñ•Éµ¥¹…±Ì¥¹Ñ•É•ÁĞ™½ÈÑ•áĞ4(€€€€€€€€€€€Á…ÍÑ”¸€Q¡¥Ì¥ÌÑ¡”É•±¥…‰±”İ…äÑ¼…ÑÑ… ±¥Á‰½…É¥µ…•Ì4(€€€€€€€€€€€½¸]M0È°YM½‘”°…¹…¹äÑ•Éµ¥¹…°½Ù•ÈMM İ¡•É”ÑÉ°­X4(€€€€€€€€€€€…¸ĞÉ•… Ñ¡”…ÁÁ±¥…Ñ¥½¸™½È¥µ…”µ½¹±ä±¥Á‰½…É¸4(€€€€€€€€€€€€ˆˆˆ4(€€€€€€€€€€€¥˜Í•±˜¹}ÑÉå}…ÑÑ…¡}±¥Á‰½…É‘}¥µ…” ¤è4(€€€€€€€€€€€€€€€•Ù•¹Ğ¹…ÁÀ¹¥¹Ù…±¥‘…Ñ” ¤4(€€€€€€€€€€€•±Í”è4(€€€€€€€€€€€€€€€€Œ9¼¥µ…”™½Õ¹ƒŠPÍ¡½Ü„¡¥¹Ğ4(€€€€€€€€€€€€€€€Á…ÍÌ€€ŒÍ¥±•¹Ğİ¡•¸¹¼¥µ…”€¡…Ù½¥¹½¥Í”½¸…¥‘•¹Ñ…°ÁÉ•ÍÌ¤4(4(€€€€€€€€Œå¹…µ¥ŒÁÉ½µÁĞèÍ¡½İÌ!•Éµ•ÌÍåµ‰½°İ¡•¸…•¹Ğ¥Ìİ½É­¥¹œ°4(€€€€€€€€Œ½È…¹Íİ•ÈÁÉ½µÁĞİ¡•¸±…É¥™ä™É••Ñ•áĞµ½‘”¥Ì…Ñ¥Ù”¸4(€€€€€€€±¥}É•˜€ôÍ•±˜4(4(€€€€€€€‘•˜•Ñ}ÁÉ½µÁĞ ¤è4(€€€€€€€€€€€É•ÑÕÉ¸±¥}É•˜¹}•Ñ}ÑÕ¥}ÁÉ½µÁÑ}™É…µ•¹ÑÌ ¤4(4(€€€€€€€€ŒÉ•…Ñ”Ñ¡”¥¹ÁÕĞ…É•„İ¥Ñ µÕ±Ñ¥±¥¹”€¡±Ğ­¹Ñ•È¤°…ÕÑ½½µÁ±•Ñ”°…¹Á…ÍÑ”¡…¹‘±¥¹œ4(€€€€€€€™É½´ÁÉ½µÁÑ}Ñ½½±­¥Ğ¹…ÕÑ½}ÍÕ•ÍĞ¥µÁ½ÉĞÕÑ½MÕ•ÍÑÉ½µ!¥ÍÑ½Éä4(€€€€€€€™É½´ÁÉ½µÁÑ}Ñ½½±­¥Ğ¹½µÁ±•Ñ¥½¸¥µÁ½ÉĞQ¡É•…‘•‘½µÁ±•Ñ•È4(4(4(€€€€€€€}½µÁ±•Ñ•È€ôM±…Í¡½µµ…¹‘½µÁ±•Ñ•È 4(€€€€€€€€€€€Í­¥±±}½µµ…¹‘Í}ÁÉ½Ù¥‘•Èõ±…µ‰‘„è•Ñ}Í­¥±±}½µµ…¹‘Ì ¤°4(€€€€€€€€€€€½µµ…¹‘}™¥±Ñ•Èõ±¥}É•˜¹}½µµ…¹‘}…Ù…¥±…‰±”°4(€€€€€€€€€€€Í­¥±±}‰Õ¹‘±•Í}ÁÉ½Ù¥‘•Èõ±…µ‰‘„è•Ñ}Í­¥±±}‰Õ¹‘±•Ì ¤°4(€€€€€€€€¤4(€€€€€€€¥¹ÁÕÑ}…É•„€ôQ•áÑÉ•„ 4(€€€€€€€€€€€¡•¥¡Ğõ¥µ•¹Í¥½¸¡µ¥¸ôÄ°µ…àôà°ÁÉ•™•ÉÉ•ôÄ¤°4(€€€€€€€€€€€ÁÉ½µÁĞõ•Ñ}ÁÉ½µÁĞ°4(€€€€€€€€€€€ÍÑå±”ô±…ÍÌé¥¹ÁÕĞµ…É•„œ°4(€€€€€€€€€€€µÕ±Ñ¥±¥¹”õQÉÕ”°4(€€€€€€€€€€€İÉ…Á}±¥¹•ÌõQÉÕ”°4(€€€€€€€€€€€É•…‘}½¹±äõ½¹‘¥Ñ¥½¸¡±…µ‰‘„è‰½½°¡±¥}É•˜¹}½µµ…¹‘}‰±½­Í}¥¹ÁÕĞ¤¤°4(€€€€€€€€€€€¡¥ÍÑ½Éäõ¥±•!¥ÍÑ½Éä¡ÍÑÈ¡Í•±˜¹}¡¥ÍÑ½Éå}™¥±”¤¤°4(€€€€€€€€€€€€Œ½µÁ±•Ñ•}İ¡¥±•}ÑåÁ¥¹œ™¥É•ÌÑ¡”½µÁ±•Ñ•È½¸•Ù•Éä­•åÍÑÉ½­”¸Q¡”4(€€€€€€€€€€€€Œ½µÁ±•Ñ•È‘½•Ì‰±½­¥¹œİ½É¬ƒŠP™Õééä µ™¥±”¥¹‘•á¥¹œÍ¡•±±Ì½ÕĞÑ¼4(€€€€€€€€€€€€ŒÉœ½™€¡ÕÀÑ¼„€ÉÌÑ¥µ•½ÕĞ¤…¹Á…Ñ ½µÁ±•Ñ¥½¸¡¥ÑÌ½Ì¹±¥ÍÑ‘¥È½ÍÑ…Ğ4(€€€€€€€€€€€€ŒƒŠPÍ¼ÉÕ¹¹¥¹œ¥Ğ¥¹±¥¹”İ½Õ±ÍÑ…±°Ñ¡”É•¹‘•È±½½À½¸•… ­•ä€¡Ù•Éä4(€€€€€€€€€€€€Œ¹½Ñ¥•…‰±”½¸]M0È½Í±½Ü™¥±•ÍåÍÑ•µÌ¤¸Q¡É•…‘•‘½µÁ±•Ñ•Èµ½Ù•Ì¥Ğ½™˜4(€€€€€€€€€€€€ŒÑ¡”U$•Ù•¹Ğ±½½À°­••Á¥¹œÑåÁ¥¹œÉ•ÍÁ½¹Í¥Ù”¸4(€€€€€€€€€€€½µÁ±•Ñ•ÈõQ¡É•…‘•‘½µÁ±•Ñ•È¡}½µÁ±•Ñ•È¤°4(€€€€€€€€€€€½µÁ±•Ñ•}İ¡¥±•}ÑåÁ¥¹œõQÉÕ”°4(€€€€€€€€€€€…ÕÑ½}ÍÕ•ÍĞõM±…Í¡½µµ…¹‘ÕÑ½MÕ•ÍĞ 4(€€€€€€€€€€€€€€€¡¥ÍÑ½Éå}ÍÕ•ÍĞõÕÑ½MÕ•ÍÑÉ½µ!¥ÍÑ½Éä ¤°4(€€€€€€€€€€€€€€€½µÁ±•Ñ•Èõ}½µÁ±•Ñ•È°4(€€€€€€€€€€€€¤°4(€€€€€€€€¤4(€€€€€€€€Œ-••ÀÁÉ½µÁÑ}Ñ½½±­¥Ğ½¸¥ÑÌÍ¥µÁ±”Ñ•µÁ™¥±”Á…Ñ ¸M•ÑÑ¥¹œ4(€€€€€€€€Œ‰Õ™™•È¹Ñ•µÁ™¥±”€ô€‰ÁÉ½µÁĞ¹µˆÑÉ¥•ÉÌ¥ÑÌ½µÁ±•àµÑ•µÁ™¥±”‰É…¹ °4(€€€€€€€€Œİ¡¥ ÑÉ¥•ÌÑ¼µ­‘¥È ¤Ñ¡”µ­‘Ñ•µÀ ¤‘¥É•Ñ½Éä……¥¸…¹É…¥Í•Ì4(€€€€€€€€Œa%MP¸Q¡”ÍÕ™™¥à­••ÁÌµ…É­‘½İ¸¡¥¡±¥¡Ñ¥¹œİ¥Ñ¡½ÕĞÑ¡…Ğ‰Õœ¸4(€€€€€€€¥¹ÁÕÑ}…É•„¹‰Õ™™•È¹Ñ•µÁ™¥±•}ÍÕ™™¥à€ô€œ¹µœ4(4(€€€€€€€€Œå¹…µ¥Œ¡•¥¡Ğè…½Õ¹ÑÌ™½È‰½Ñ •áÁ±¥¥Ğ¹•İ±¥¹•Ì9Ù¥ÍÕ…°4(€€€€€€€€ŒİÉ…ÁÁ¥¹œ½˜±½¹œ±¥¹•ÌÍ¼Ñ¡”¥¹ÁÕĞ…É•„…±İ…åÌ™¥ÑÌ¥ÑÌ½¹Ñ•¹Ğ¸4(€€€€€€€‘•˜}¥¹ÁÕÑ}¡•¥¡Ğ ¤è4(€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€™É½´ÁÉ½µÁÑ}Ñ½½±­¥Ğ¹…ÁÁ±¥…Ñ¥½¸¥µÁ½ÉĞ•Ñ}…ÁÀ4(4(€€€€€€€€€€€€€€€‘½Œ€ô¥¹ÁÕÑ}…É•„¹‰Õ™™•È¹‘½Õµ•¹Ğ4(€€€€€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€€€€€Ñ•Éµ¥¹…±}½±Õµ¹Ì€ô•Ñ}…ÁÀ ¤¹½ÕÑÁÕĞ¹•Ñ}Í¥é” ¤¹½±Õµ¹Ì4(€€€€€€€€€€€€€€€•á•ÁĞá•ÁÑ¥½¸è4(€€€€€€€€€€€€€€€€€€€Ñ•Éµ¥¹…±}½±Õµ¹Ì€ôÍ¡ÕÑ¥°¹•Ñ}Ñ•Éµ¥¹…±}Í¥é”  àÀ°€ÈĞ¤¤¹½±Õµ¹Ì4(€€€€€€€€€€€€€€€É•ÑÕÉ¸}•ÍÑ¥µ…Ñ•}ÑÕ¥}¥¹ÁÕÑ}¡•¥¡Ğ 4(€€€€€€€€€€€€€€€€€€€‘½Œ¹±¥¹•Ì°4(€€€€€€€€€€€€€€€€€€€Í•±˜¹}•Ñ}ÑÕ¥}ÁÉ½µÁÑ}Ñ•áĞ ¤°4(€€€€€€€€€€€€€€€€€€€Ñ•Éµ¥¹…±}½±Õµ¹Ì°4(€€€€€€€€€€€€€€€€¤4(€€€€€€€€€€€•á•ÁĞá•ÁÑ¥½¸è4(€€€€€€€€€€€€€€€É•ÑÕÉ¸€Ä4(4(€€€€€€€¥¹ÁÕÑ}…É•„¹İ¥¹‘½Ü¹¡•¥¡Ğ€ô}¥¹ÁÕÑ}¡•¥¡Ğ4(4(€€€€€€€€ŒA…ÍÑ”½±±…ÁÍ¥¹œè‘•Ñ•Ğ±…É”Á…ÍÑ•Ì…¹Í…Ù”Ñ¼Ñ•µÀ™¥±”4(€€€€€€€}Á…ÍÑ•}½Õ¹Ñ•È€ôlÁt4(€€€€€€€}ÁÉ•Ù}Ñ•áÑ}±•¸€ôlÁt4(€€€€€€€}ÁÉ•Ù}¹•İ±¥¹•}½Õ¹Ğ€ôlÁt4(€€€€€€€}Á…ÍÑ•}©ÕÍÑ}½±±…ÁÍ•€ôm…±Í•t4(€€€€€€€Í•±˜¹}Í­¥Á}Á…ÍÑ•}½±±…ÁÍ”€ô…±Í”4(4(€€€€€€€‘•˜}½¹}Ñ•áÑ}¡…¹•¡‰Õ˜¤è4(€€€€€€€€€€€€ˆˆ‰•Ñ•Ğ±…É”Á…ÍÑ•Ì…¹½±±…ÁÍ”Ñ¡•´Ñ¼„™¥±”É•™•É•¹”¸4(4(€€€€€€€€€€€]¡•¸‰É…­•Ñ•Á…ÍÑ”¥Ì…Ù…¥±…‰±”°¡…¹‘±•}Á…ÍÑ”½±±…ÁÍ•Ì4(€€€€€€€€€€€±…É”Á…ÍÑ•Ì‘¥É•Ñ±ä¸€Q¡¥Ì¡…¹‘±•È¥Ì„™…±±‰…¬™½È4(€€€€€€€€€€€Ñ•Éµ¥¹…±Ìİ¥Ñ¡½ÕĞ‰É…­•Ñ•Á…ÍÑ”ÍÕÁÁ½ÉĞ¸4(4(€€€€€€€€€€€Qİ¼¡•ÕÉ¥ÍÑ¥Ì€¡•¥Ñ¡•ÈÑÉ¥•ÉÌ½±±…ÁÍ”¤è4(€€€€€€€€€€€€Ä¸5…¹ä¡…É…Ñ•ÉÌ…‘‘•…Ğ½¹”€¡¡…ÉÍ}…‘‘•€ø€Ä¤ƒŠPİ½É­Ì4(€€€€€€€€€€€€€€İ¡•¸Ñ¡”Ñ•Éµ¥¹…°‘•±¥Ù•ÉÌÑ¡”Á…ÍÑ”¥¸½¹”•Ù•¹Ğµ±½½ÀÑ¥¬¸4(€€€€€€€€€€€€È¸9•İ±¥¹”½Õ¹Ğ©ÕµÁ•‰ä€Ğ¬¥¸„Í¥¹±”Ñ•áĞµ¡…¹”•Ù•¹ĞƒŠP4(€€€€€€€€€€€€€€…Ñ¡•ÌÑ•Éµ¥¹…±ÌÑ¡…Ğ™••¡…É…Ñ•ÉÌ¥¹‘¥Ù¥‘Õ…±±ä‰ÕĞ4(€€€€€€€€€€€€€€ÍÑ¥±°‰…Ñ ¹•İ±¥¹•Ì¸€±Ğ­¹Ñ•È½¹±ä…‘‘Ì€Ä¹•İ±¥¹”Á•È4(€€€€€€€€€€€€€€•Ù•¹ĞÍ¼¥Ğ¹•Ù•ÈÑÉ¥•ÉÌÑ¡¥Ì¸4(€€€€€€€€€€€€ˆˆˆ4(€€€€€€€€€€€Ñ•áĞ€ô}ÍÑÉ¥Á}±•…­•‘}‰É…­•Ñ•‘}Á…ÍÑ•}İÉ…ÁÁ•ÉÌ¡‰Õ˜¹Ñ•áĞ¤4(€€€€€€€€€€€Ñ•áĞ°}¡…‘}µ½ÕÍ•}É•Á½ÉÑÌ€ô}ÍÑÉ¥Á}±•…­•‘}Ñ•Éµ¥¹…±