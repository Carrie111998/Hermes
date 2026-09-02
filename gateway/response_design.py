"""Stable response-design guidance for mobile gateway platforms.

This module only builds deterministic prompt text.  It never rewrites a model's
completed response, because semantic post-processing can corrupt code, quotes,
identifiers, and other literal content.
"""

from __future__ import annotations

_SUPPORTED_PLATFORMS = {
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
}

_STRUCTURED_MOBILE_PROMPT = """## Mobile response design ({platform})

Format substantive answers for quick scanning on a phone using platform-supported Markdown.
- Do not inflate a short answer of one to three sentences with a heading or template.
- For longer answers, use a short heading, a concise conclusion, clearly separated sections, and a final next step when useful.
- Put a blank line between independent semantic blocks so paragraphs, lists, and headings do not merge visually.
- Use bullet points for facts and numbered steps for ordered actions.
- Use the user's language for headings and labels; translate Problem / Risk / My advice naturally instead of forcing English labels.
- When you identify a problem, separate it as Problem / Risk / My advice, and keep the advice concrete.
- Use at most one semantic emoji per section heading or status item; do not decorate every line.
- Preserve commands, paths, identifiers, and values exactly as supplied. Put them in inline code or fenced code blocks.
- Prefer mobile-friendly cards or lists over wide tables. Omit empty sections.
- Do not split one answer into multiple chat messages merely for visual styling; transport chunking is handled separately.
"""


def build_response_design_prompt(platform_key: str, mode: str = "structured") -> str:
    """Return deterministic mobile response guidance for a platform.

    Unsupported platforms intentionally receive no block in the first release.
    """
    normalized_mode = str(mode or "off").strip().lower()
    platform_name = _SUPPORTED_PLATFORMS.get(str(platform_key).strip().lower())
    if normalized_mode != "structured" or platform_name is None:
        return ""
    return _STRUCTURED_MOBILE_PROMPT.format(platform=platform_name).strip()
