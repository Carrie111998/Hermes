"""Rendering bridge — routes TUI content through Python-side renderers.

When agent.rich_output exists, its functions are used. When it doesn't,
everything returns None and the TUI falls back to its own markdown.tsx.
"""

from __future__ import annotations

from typing import Any


def render_message(text: str, cols: int = 80) -> str | None:
    try:
        # Use dynamic import to satisfy ty unresolved-import
        format_response: Any = __import__(
            "agent.rich_output", fromlist=["format_response"]
        ).format_response
    except (ImportError, AttributeError):
        return None

    try:
        return format_response(text, cols=cols)
    except TypeError:
        return format_response(text)
    except Exception:
        return None


def render_diff(text: str, cols: int = 80) -> str | None:
    try:
        # Use dynamic import to satisfy ty unresolved-import
        _rd: Any = __import__(
            "agent.rich_output", fromlist=["render_diff"]
        ).render_diff
    except (ImportError, AttributeError):
        return None

    try:
        return _rd(text, cols=cols)
    except TypeError:
        return _rd(text)
    except Exception:
        return None


def make_stream_renderer(cols: int = 80):
    try:
        # Use dynamic import to satisfy ty unresolved-import
        StreamingRenderer: Any = __import__(
            "agent.rich_output", fromlist=["StreamingRenderer"]
        ).StreamingRenderer
    except (ImportError, AttributeError):
        return None

    try:
        return StreamingRenderer(cols=cols)
    except TypeError:
        return StreamingRenderer()
    except Exception:
        return None
