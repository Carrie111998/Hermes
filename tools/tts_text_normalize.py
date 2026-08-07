"""Utilities for preparing assistant text for speech synthesis.

The TTS provider should receive a spoken script, not raw chat Markdown.  This
module centralises the lightweight, deterministic cleanup used by explicit TTS
calls and gateway auto-TTS replies.

Non-ASCII characters are written as escapes on purpose so the file stays free of
invisible/look-alike glyphs.
"""

from __future__ import annotations

import html
import re

# Sentinel appended to former heading lines so smooth_whitespace_for_tts can
# fold a heading into the sentence that follows it ("Weather, it will be sunny")
# rather than leaving a bare "Weather." label that reads abruptly aloud.
_HEAD = "\x00"

_MD_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", flags=re.DOTALL)
_MD_UNDERSCORE_BOLD_RE = re.compile(r"__(.+?)__", flags=re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", flags=re.DOTALL)
_MD_UNDERSCORE_ITALIC_RE = re.compile(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", flags=re.DOTALL)
_MD_STRIKE_RE = re.compile(r"~~(.+?)~~", flags=re.DOTALL)
_MD_HEADING_LINE_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", flags=re.MULTILINE)
_MD_BLOCKQUOTE_RE = re.compile(r"^\s*>\s?", flags=re.MULTILINE)
_MD_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", flags=re.MULTILINE)
_MD_HR_RE = re.compile(r"^\s*[-*_]{3,}\s*$", flags=re.MULTILINE)
_MD_TABLE_PIPE_RE = re.compile(r"\s*\|\s*")
_URL_RE = re.compile(r"https?://\S+")

# Broad emoji / pictograph cleanup.  Voice providers vary a lot here; most read
# emojis as awkward labels, so keep the speech script calm and literal.
_EMOJI_RE = re.compile(
    "["
    "\U0001F1E6-\U0001F1FF"
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "☀-➿"
    "]+",
    flags=re.UNICODE,
)
_VARIATION_SELECTOR_RE = re.compile("[︎️]")


def strip_markdown_for_tts(text: str) -> str:
    """Strip Markdown/Telegram formatting while preserving readable words."""
    if not text:
        return ""

    text = html.unescape(str(text))
    text = _MD_CODE_BLOCK_RE.sub(" ", text)
    text = _MD_IMAGE_RE.sub(lambda m: f" {m.group(1)} " if m.group(1) else " ", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("", text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_UNDERSCORE_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_UNDERSCORE_ITALIC_RE.sub(r"\1", text)
    text = _MD_STRIKE_RE.sub(r"\1", text)
    # Mark headings (do not just delete the marker): the whitespace pass folds a
    # heading into the sentence after it so speech says "Weather, it will be
    # sunny" instead of a clipped "Weather." then a separate sentence.
    text = _MD_HEADING_LINE_RE.sub(lambda m: m.group(1).rstrip() + _HEAD, text)
    text = _MD_BLOCKQUOTE_RE.sub("", text)
    text = _MD_LIST_ITEM_RE.sub("", text)
    text = _MD_HR_RE.sub("", text)

    # Pipe tables are terrible read aloud.  Turn any leftover pipes into pauses
    # instead of letting a provider speak "vertical bar".
    text = _MD_TABLE_PIPE_RE.sub("; ", text)
    return text


_NUMBER_RE = r"[-+\u2212]?\d+(?:[.,]\d+)?"

_EN_SYMBOL_FORMS = {
    "range_to": "to",
    "degrees_celsius": "degrees Celsius",
    "degrees_fahrenheit": "degrees Fahrenheit",
    "degrees": "degrees",
    "km_per_hour": "kilometres per hour",
    "millimetres": "millimetres",
    "centimetres": "centimetres",
    "metres": "metres",
    "per": "per",
    "nzd": "New Zealand dollars",
    "aud": "Australian dollars",
    "usd": "US dollars",
    "eur": "euros",
    "gbp": "pounds",
    "dollars": "dollars",
    "percent": "percent",
    "and": "and",
    "to": "to",
    "about": "about",
}

_FR_SYMBOL_FORMS = {
    "range_to": "à",
    "degrees_celsius": "degrés Celsius",
    "degrees_fahrenheit": "degrés Fahrenheit",
    "degrees": "degrés",
    "km_per_hour": "kilomètres par heure",
    "millimetres": "millimètres",
    "centimetres": "centimètres",
    "metres": "mètres",
    "per": "par",
    "nzd": "dollars néo-zélandais",
    "aud": "dollars australiens",
    "usd": "dollars américains",
    "eur": "euros",
    "gbp": "livres sterling",
    "dollars": "dollars",
    "percent": "pour cent",
    "and": "et",
    "to": "à",
    "about": "environ",
}


def _symbol_forms_for_locale(locale: str | None) -> dict[str, str] | None:
    """Return spoken forms for *locale*.

    ``None`` preserves the historical English behavior. For a known
    non-English locale without a bundled table, leave semantic symbols intact
    so the selected voice can pronounce them instead of injecting English.
    """
    if locale is None:
        return _EN_SYMBOL_FORMS
    language = str(locale).strip().lower().replace("_", "-").split("-", 1)[0]
    if not language or language == "en":
        return _EN_SYMBOL_FORMS
    if language == "fr":
        return _FR_SYMBOL_FORMS
    return None


def _normalize_temperature_ranges(text: str, forms: dict[str, str]) -> str:
    # 11-17 degrees C -> a locale-appropriate spoken range.
    text = re.sub(
        rf"(?<!\w)({_NUMBER_RE})\s*[\u2013\u2014-]\s*({_NUMBER_RE})\s*°\s*C\b",
        lambda m: (
            f"{m.group(1).replace(chr(0x2212), '-')} {forms['range_to']} "
            f"{m.group(2).replace(chr(0x2212), '-')} {forms['degrees_celsius']}"
        ),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"(?<!\w)({_NUMBER_RE})\s*[\u2013\u2014-]\s*({_NUMBER_RE})\s*°\s*F\b",
        lambda m: (
            f"{m.group(1).replace(chr(0x2212), '-')} {forms['range_to']} "
            f"{m.group(2).replace(chr(0x2212), '-')} {forms['degrees_fahrenheit']}"
        ),
        text,
        flags=re.IGNORECASE,
    )
    return text


def normalize_symbols_for_tts(text: str, locale: str | None = None) -> str:
    """Expand common symbols/shorthand into words a TTS engine reads well."""
    if not text:
        return ""

    text = str(text)
    text = re.sub("[   ]", " ", text)  # non-breaking / thin spaces
    text = text.replace("\u2212", "-")  # minus sign
    text = text.replace("…", "...")  # ellipsis
    forms = _symbol_forms_for_locale(locale)

    # Do not inject English into known non-English languages for which Hermes
    # has no table. Their TTS provider gets the original semantic symbols.
    if forms is None:
        text = re.sub("[•◦▪▫]", " ", text)
        text = _VARIATION_SELECTOR_RE.sub("", text)
        return _EMOJI_RE.sub("", text)

    text = _normalize_temperature_ranges(text, forms)

    # Temperatures with a number.  Do this before generic degree handling.
    text = re.sub(
        rf"(?<!\w)({_NUMBER_RE})\s*°\s*C\b",
        lambda m: f"{m.group(1)} {forms['degrees_celsius']}",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"(?<!\w)({_NUMBER_RE})\s*°\s*F\b",
        lambda m: f"{m.group(1)} {forms['degrees_fahrenheit']}",
        text,
        flags=re.IGNORECASE,
    )
    # Bare units with no leading number ("measured in degrees C").
    text = re.sub(r"°\s*C\b", forms["degrees_celsius"], text, flags=re.IGNORECASE)
    text = re.sub(r"°\s*F\b", forms["degrees_fahrenheit"], text, flags=re.IGNORECASE)
    # Any remaining degree symbol (angles, stray cases).
    text = re.sub(
        rf"(?<!\w)({_NUMBER_RE})\s*°",
        lambda m: f"{m.group(1)} {forms['degrees']}",
        text,
    )
    text = text.replace("°", f" {forms['degrees']}")

    # Common weather/travel units.
    text = re.sub(r"(?<=\d)\s*km\s*/\s*h\b", f" {forms['km_per_hour']}", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*km/h\b", f" {forms['km_per_hour']}", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*mm\b", f" {forms['millimetres']}", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*cm\b", f" {forms['centimetres']}", text, flags=re.IGNORECASE)
    text = re.sub(r"(?<=\d)\s*m\b", f" {forms['metres']}", text, flags=re.IGNORECASE)

    # Numeric rates only ("5/month" -> "5 per month").  Requiring digit-then-letter
    # keeps "and/or", "N/A", "TCP/IP" and dates like "2026/06" intact.
    text = re.sub(r"(?<=\d)\s*/\s*(?=[A-Za-zÀ-ÖØ-öø-ÿ])", f" {forms['per']} ", text)

    # Money and percentages.  The integer part must END in a digit so a trailing
    # comma ("A$50, ...") is not swallowed into the spoken amount.
    amount = r"([\d,]*\d(?:\.\d+)?)"
    text = re.sub(rf"NZ\$\s*{amount}", lambda m: f"{m.group(1)} {forms['nzd']}", text, flags=re.IGNORECASE)
    text = re.sub(rf"A\$\s*{amount}", lambda m: f"{m.group(1)} {forms['aud']}", text, flags=re.IGNORECASE)
    text = re.sub(rf"US\$\s*{amount}", lambda m: f"{m.group(1)} {forms['usd']}", text, flags=re.IGNORECASE)
    text = re.sub(rf"€\s*{amount}", lambda m: f"{m.group(1)} {forms['eur']}", text)
    text = re.sub(rf"£\s*{amount}", lambda m: f"{m.group(1)} {forms['gbp']}", text)
    text = re.sub(rf"\$\s*{amount}", lambda m: f"{m.group(1)} {forms['dollars']}", text)
    text = re.sub(r"(?<=\d)\s*%", f" {forms['percent']}", text)

    # Operators and separators that commonly leak from formatted answers.
    text = text.replace("&", f" {forms['and']} ")
    text = re.sub("[•◦▪▫]", " ", text)  # bullet glyphs
    text = text.replace("→", f" {forms['to']} ")  # ->
    text = text.replace("⇒", f" {forms['to']} ")  # =>
    text = text.replace("≈", f" {forms['about']} ")  # almost equal
    text = text.replace("~", f" {forms['about']} ")

    text = _VARIATION_SELECTOR_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    return text


def smooth_whitespace_for_tts(text: str) -> str:
    """Collapse visual formatting into calm spoken paragraphs.

    A former heading line (marked with the _HEAD sentinel) folds into the next
    content line as a spoken lead-in: "Weather" + "It will be sunny" becomes
    "Weather, It will be sunny."  A heading with no content after it becomes its
    own short sentence.
    """
    if not text:
        return ""

    raw_lines = text.splitlines()
    add_sentence_pauses = sum(1 for raw_line in raw_lines if raw_line.replace(_HEAD, "").strip()) > 1
    lines: list[str] = []
    pending_heading: str | None = None

    def flush_pending() -> None:
        nonlocal pending_heading
        if pending_heading is not None:
            lines.append(pending_heading.rstrip(".:;,") + ".")
            pending_heading = None

    for raw_line in raw_lines:
        is_heading = raw_line.rstrip().endswith(_HEAD)
        line = raw_line.replace(_HEAD, "").strip()
        if not line:
            # Hold a pending heading across blank lines so it still folds into
            # the next real content line; otherwise just collapse the blank.
            if pending_heading is None and lines and lines[-1] != "":
                lines.append("")
            continue
        if is_heading:
            flush_pending()
            pending_heading = line.rstrip(".:;,")
            continue
        if pending_heading is not None:
            line = f"{pending_heading.rstrip('.:;,')}, {line}"
            pending_heading = None
        if add_sentence_pauses and line[-1] not in ".!?;:":
            line += "."
        lines.append(line)

    flush_pending()

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,.;:!?])([A-Za-z])", r"\1 \2", text)
    text = re.sub(r"\.{4,}", "...", text)
    return text.strip()


# Reasoning blocks: models with ``/reasoning show`` enabled emit
# ``<think>...</think>`` blocks in the final assistant message.  Users want to
# SEE reasoning, not hear it read aloud (#34213).
_THINK_BLOCK_RE = re.compile(r"<think[\s>].*?</think>", flags=re.DOTALL | re.IGNORECASE)
# An unterminated block (streaming cut-off) should still not be spoken.
_THINK_BLOCK_OPEN_RE = re.compile(r"<think[\s>].*\Z", flags=re.DOTALL | re.IGNORECASE)

# Turn-end file-mutation verifier footer appended by run_agent.py
# (``_format_file_mutation_failure_footer``).  It's a UI affordance — reading
# "warning file mutation verifier, 2 files were NOT modified..." aloud is
# noise (#40772).  The footer is a ``⚠️ File-mutation verifier:`` header line
# followed by indented ``•`` bullet lines; strip the whole block.
_VERIFIER_FOOTER_RE = re.compile(
    r"^\s*⚠️?\s*File-mutation verifier:.*(?:\n[ \t]+•.*)*",
    flags=re.MULTILINE,
)


def strip_nonspoken_blocks(text: str) -> str:
    """Remove blocks that must never reach a speech provider.

    Currently: ``<think>`` reasoning blocks and the end-of-turn
    file-mutation verifier footer.
    """
    if not text:
        return ""
    text = _THINK_BLOCK_RE.sub(" ", text)
    text = _THINK_BLOCK_OPEN_RE.sub(" ", text)
    text = _VERIFIER_FOOTER_RE.sub(" ", text)
    return text


def flatten_newlines_for_payload(text: str) -> str:
    """Collapse newlines into sentence breaks for single-line TTS payloads.

    Some OpenAI-compatible backends (e.g. Kokoro) truncate synthesis at the
    first newline (#9004).  The smoothing pass already terminates each line
    with punctuation, so newlines can safely become plain spaces.
    """
    if not text:
        return ""
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"(?<=[.!?;:,])\n", " ", text)
    text = text.replace("\n", ". ")
    text = re.sub(r"\.\s*\.", ".", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def prepare_spoken_text(
    text: str,
    max_chars: int | None = 4000,
    locale: str | None = None,
) -> str:
    """Return a TTS-friendly script from assistant text.

    Deterministic cleanup, not a semantic rewrite: it removes ``<think>``
    reasoning blocks and the file-mutation verifier footer, removes Markdown,
    expands common symbols such as a degree-Celsius sign to "degrees Celsius",
    turns visual line formatting into speakable sentence pauses, and flattens
    the result to a single line so newline-sensitive providers (Kokoro) speak
    the whole script.
    """
    spoken = strip_nonspoken_blocks(text)
    spoken = strip_markdown_for_tts(spoken)
    spoken = normalize_symbols_for_tts(spoken, locale=locale)
    spoken = smooth_whitespace_for_tts(spoken)
    spoken = flatten_newlines_for_payload(spoken)
    if max_chars is not None and max_chars > 0 and len(spoken) > max_chars:
        spoken = spoken[:max_chars].rstrip()
    return spoken
