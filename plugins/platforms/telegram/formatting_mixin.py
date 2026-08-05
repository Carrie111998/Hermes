"""FormattingMixin.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the mechanical mixin lift
that produced ``TelegramAuthorizationMixin`` (PR #75742). This mixin
holds the markdown-to-MarkdownV2 conversion pipeline (``format_message`` and its nested helpers) plus the ``_escape_mdv2`` / ``_wrap_markdown_tables`` primitives it depends on. The module-level ``_escape_mdv2`` and ``_wrap_markdown_tables`` helpers are re-exported by the adapter module so existing imports (e.g. ``tests/gateway/test_telegram_format.py``) keep resolving.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
``self.*`` calls resolve unchanged via the MRO, and ``FormattingMixin`` precedes
``BasePlatformAdapter`` in the bases so resolution order is what it was
when these methods sat on the class.

Two details keep the lift observationally identical:

* ``logger`` is bound by explicit name rather than ``__name__``, so records
  emitted from these methods keep the logger name
  ``"plugins.platforms.telegram.adapter"``.
* ``Message`` (where used) is imported under the same ``ImportError`` guard
  the adapter uses, falling back to ``Any``.
"""

import logging
import re

from gateway.platforms.helpers import convert_table_to_bullets as _wrap_markdown_tables


logger = logging.getLogger("plugins.platforms.telegram.adapter")


# Matches every character that MarkdownV2 requires to be backslash-escaped
# when it appears outside a code span or fenced code block.
_MDV2_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')



def _escape_mdv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters with a preceding backslash."""
    return _MDV2_ESCAPE_RE.sub(r'\\\1', text)


class FormattingMixin:
    """Markdown formatting cluster lifted verbatim from ``TelegramAdapter``."""


    def format_message(self, content: str) -> str:
        """
        Convert standard markdown to Telegram MarkdownV2 format.

        Protected regions (code blocks, inline code) are extracted first so
        their contents are never modified.  Standard markdown constructs
        (headers, bold, italic, links) are translated to MarkdownV2 syntax,
        and all remaining special characters are escaped.
        """
        if not content:
            return content

        placeholders: dict = {}
        counter = [0]

        def _ph(value: str) -> str:
            """Stash *value* behind a placeholder token that survives escaping."""
            key = f"\x00PH{counter[0]}\x00"
            counter[0] += 1
            placeholders[key] = value
            return key

        text = content

        # 0) Rewrite GFM-style pipe tables into Telegram-friendly row groups
        #    before the normal MarkdownV2 conversions run.
        text = _wrap_markdown_tables(text)

        # 1) Protect fenced code blocks (``` ... ```)
        #    Per MarkdownV2 spec, \ and ` inside pre/code must be escaped.
        def _protect_fenced(m):
            raw = m.group(0)
            # Split off opening ``` (with optional language) and closing ```
            open_end = raw.index('\n') + 1 if '\n' in raw[3:] else 3
            opening = raw[:open_end]
            body_and_close = raw[open_end:]
            body = body_and_close[:-3]
            body = body.replace('\\', '\\\\').replace('`', '\\`')
            return _ph(opening + body + '```')

        text = re.sub(
            r'(```(?:[^\n]*\n)?[\s\S]*?```)',
            _protect_fenced,
            text,
        )

        # 2) Protect inline code (`...`)
        #    Escape \ inside inline code per MarkdownV2 spec.
        text = re.sub(
            r'(`[^`]+`)',
            lambda m: _ph(m.group(0).replace('\\', '\\\\')),
            text,
        )

        # 3) Convert markdown links – escape the display text; inside the URL
        #    only ')' and '\' need escaping per the MarkdownV2 spec.
        def _convert_link(m):
            display = _escape_mdv2(m.group(1))
            url = m.group(2).replace('\\', '\\\\').replace(')', '\\)')
            return _ph(f'[{display}]({url})')

        text = re.sub(r'\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)', _convert_link, text)

        # 4) Convert markdown headers (## Title) → bold *Title*
        def _convert_header(m):
            inner = m.group(1).strip()
            # Strip redundant bold markers that may appear inside a header
            inner = re.sub(r'\*\*(.+?)\*\*', r'\1', inner)
            return _ph(f'*{_escape_mdv2(inner)}*')

        text = re.sub(
            r'^#{1,6}\s+(.+)$', _convert_header, text, flags=re.MULTILINE
        )

        # 5) Convert bold: **text** → *text* (MarkdownV2 bold)
        text = re.sub(
            r'\*\*(.+?)\*\*',
            lambda m: _ph(f'*{_escape_mdv2(m.group(1))}*'),
            text,
        )

        # 6) Convert italic: *text* (single asterisk) → _text_ (MarkdownV2 italic)
        #    [^*\n]+ prevents matching across newlines (which would corrupt
        #    bullet lists using * markers and multi-line content).
        text = re.sub(
            r'\*([^*\n]+)\*',
            lambda m: _ph(f'_{_escape_mdv2(m.group(1))}_'),
            text,
        )

        # 7) Convert strikethrough: ~~text~~ → ~text~ (MarkdownV2)
        text = re.sub(
            r'~~(.+?)~~',
            lambda m: _ph(f'~{_escape_mdv2(m.group(1))}~'),
            text,
        )

        # 8) Convert spoiler: ||text|| → ||text|| (protect from | escaping)
        text = re.sub(
            r'\|\|(.+?)\|\|',
            lambda m: _ph(f'||{_escape_mdv2(m.group(1))}||'),
            text,
        )

        # 9) Convert blockquotes: > at line start → protect > from escaping
        #    Handle both regular blockquotes (> text) and expandable blockquotes
        #    (Telegram MarkdownV2: **> for expandable start, || to end the quote)
        def _convert_blockquote(m):
            prefix = m.group(1)  # >, >>, >>>, **>, or **>> etc.
            content = m.group(2)
            # Check if content ends with || (expandable blockquote end marker)
            # In this case, preserve the trailing || unescaped for Telegram
            if prefix.startswith('**') and content.endswith('||'):
                return _ph(f'{prefix} {_escape_mdv2(content[:-2])}||')
            return _ph(f'{prefix} {_escape_mdv2(content)}')

        text = re.sub(
            r'^((?:\*\*)?>{1,3}) (.+)$',
            _convert_blockquote,
            text,
            flags=re.MULTILINE,
        )

        # 10) Escape remaining special characters in plain text
        text = _escape_mdv2(text)

        # 11) Restore placeholders in reverse insertion order so that
        #    nested references (a placeholder inside another) resolve correctly.
        for key in reversed(list(placeholders.keys())):
            text = text.replace(key, placeholders[key])

        # 12) Safety net: escape unescaped ( ) { } that slipped through
        #     placeholder processing.  Split the text into code/non-code
        #     segments so we never touch content inside ``` or ` spans.
        _code_split = re.split(r'(```[\s\S]*?```|`[^`]+`)', text)
        _safe_parts = []
        for _idx, _seg in enumerate(_code_split):
            if _idx % 2 == 1:
                # Inside code span/block — leave untouched
                _safe_parts.append(_seg)
            else:
                # Outside code — escape bare ( ) { }
                def _esc_bare(m, _seg=_seg):
                    s = m.start()
                    ch = m.group(0)
                    # Already escaped
                    if s > 0 and _seg[s - 1] == '\\':
                        return ch
                    # ( that opens a MarkdownV2 link [text](url)
                    if ch == '(' and s > 0 and _seg[s - 1] == ']':
                        return ch
                    # ) that closes a link URL
                    if ch == ')':
                        before = _seg[:s]
                        if '](http' in before or '](' in before:
                            # Check depth
                            depth = 0
                            for j in range(s - 1, max(s - 2000, -1), -1):
                                if _seg[j] == '(':
                                    depth -= 1
                                    if depth < 0:
                                        if j > 0 and _seg[j - 1] == ']':
                                            return ch
                                        break
                                elif _seg[j] == ')':
                                    depth += 1
                    return '\\' + ch
                _safe_parts.append(re.sub(r'[(){}]', _esc_bare, _seg))
        text = ''.join(_safe_parts)

        return text
