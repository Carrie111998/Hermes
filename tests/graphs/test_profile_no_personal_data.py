"""The JobFlow graph modules must not carry the operator's personal data.

Until 2026-09-01 ``graphs/_profile.py`` hardcoded a ``FALLBACK_PROFILE`` constant
holding a real name, city, citizenship, employers, degrees and compensation
floor, and ``graphs/_prompts.py`` repeated the compensation bands and geography
as prompt literals. Both files are tracked, so all of it was published to a
public fork -- and because that fork's parent repository serves the objects, the
exposure could not be retracted afterwards. These tests exist so the same class
of leak fails the build instead of reaching a remote.

The personal values now live in ``profile-card.md`` in the CV Handler knowledge
base, outside the repository, and reach the model at runtime through
``{profile_summary}``. Behaviour is preserved; the data path changed.

Scope note: this guards the graphs package specifically, because that is where
the leak happened and where prompt authors are most tempted to inline a concrete
example. It is not a repository-wide secret scanner.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_GRAPHS_DIR = Path(__file__).resolve().parents[2] / "graphs"

# Files that compose or carry model-facing profile text. jobflow.py is here
# because Pydantic ``Field(description=...)`` strings are serialised into the
# structured-output schema and therefore reach the model on every scoring call
# -- a comp figure there is a live prompt leak, not merely tracked source.
_GUARDED_FILES = ("_profile.py", "_prompts.py", "jobflow.py")

# Shapes, not a blocklist of one person's details -- a blocklist would itself
# have to contain the data it forbids. Each pattern describes a CLASS of value
# that belongs in local state rather than in tracked source.
_FORBIDDEN_SHAPES = {
    "a compensation figure": re.compile(r"\$\s?\d{2,3}\s?[Kk]\b"),
    "a full compensation range": re.compile(r"\$\s?\d{2,3}\s?[Kk]\s?[-–]\s?\$?\d{2,3}\s?[Kk]"),
    # Context-anchored: a bare five-digit run is far too common in source to be
    # evidence of anything (slice limits, token budgets, ports). Require ZIP+4,
    # or a "City, ST 12345" shape.
    "a US ZIP code": re.compile(r"\b\d{5}-\d{4}\b|,\s*[A-Z]{2}\s+\d{5}\b"),
    "an email address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "a phone number": re.compile(r"\+?\d{1,2}[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"),
}

# Substrings that would indicate the profile card has been inlined again. These
# are generic profile-card FIELD LABELS, not personal values.
_FORBIDDEN_LABELS = (
    "target comp:",
    "preferred industries:",
    "remote preference:",
    "target seniority:",
    "us citizen",
)


def _read(name: str) -> str:
    return (_GRAPHS_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _GUARDED_FILES)
def test_no_personal_value_shapes(name: str) -> None:
    """No compensation figures, addresses, emails or phone numbers in source."""

    text = _read(name)
    offenders = [
        f"{label} (matched {match.group(0)!r} at offset {match.start()})"
        for label, pattern in _FORBIDDEN_SHAPES.items()
        if (match := pattern.search(text))
    ]
    assert not offenders, (
        f"graphs/{name} contains personal data that would be published on push: "
        + "; ".join(offenders)
        + ". Put it in profile-card.md in the CV Handler knowledge base instead; "
        "it reaches the model at runtime via {profile_summary}."
    )


@pytest.mark.parametrize("name", _GUARDED_FILES)
def test_no_inlined_profile_card_fields(name: str) -> None:
    """The profile card's field labels must not reappear in tracked source."""

    lowered = _read(name).lower()
    # The modules legitimately NAME the card and its fields in prose when
    # explaining where data belongs; only a label followed by a value is a leak.
    offenders = [
        label
        for label in _FORBIDDEN_LABELS
        if re.search(re.escape(label) + r"[^\n]*[A-Za-z0-9$]", lowered)
    ]
    assert not offenders, (
        f"graphs/{name} appears to inline profile-card fields {offenders}. "
        "Those belong in profile-card.md, not in tracked source."
    )


def test_placeholder_profile_is_not_a_plausible_profile() -> None:
    """The no-card placeholder must read as missing, not as a real candidate."""

    from graphs._profile import PLACEHOLDER_PROFILE

    assert "NO CANDIDATE PROFILE LOADED" in PLACEHOLDER_PROFILE
    # A stand-in that looks real is worse than none: it scores silently.
    for label, pattern in _FORBIDDEN_SHAPES.items():
        assert not pattern.search(PLACEHOLDER_PROFILE), (
            f"PLACEHOLDER_PROFILE contains {label}; it must be obviously empty "
            "so a run grounded on it looks wrong at a glance."
        )


def test_loader_reads_card_from_local_state_not_source(tmp_path, monkeypatch) -> None:
    """Profile content must come from disk, so source carries none of it."""

    from graphs import _profile

    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "profile-card.md").write_text(
        "explanatory header\n---\nTarget comp: $999K\n", encoding="utf-8"
    )
    monkeypatch.setattr(_profile, "_PROFILE_CARD_PATH", kb / "profile-card.md")
    monkeypatch.setattr(_profile, "_RESUME_PATH", kb / "absent-resume.md")
    _profile.load_profile_summary.cache_clear()
    try:
        summary = _profile.load_profile_summary()
    finally:
        _profile.load_profile_summary.cache_clear()

    assert "Target comp: $999K" in summary
    # The header above the card's `---` rule is scaffolding, not profile content.
    assert "explanatory header" not in summary


def test_card_body_containing_a_rule_is_not_truncated(tmp_path, monkeypatch) -> None:
    """Split on the FIRST rule: a rule inside the body must not eat the card."""

    from graphs import _profile

    card = tmp_path / "profile-card.md"
    card.write_text(
        "explanatory header\n---\nfirst field\n---\nlast field\n", encoding="utf-8"
    )
    monkeypatch.setattr(_profile, "_PROFILE_CARD_PATH", card)
    monkeypatch.setattr(_profile, "_RESUME_PATH", tmp_path / "absent-resume.md")
    _profile.load_profile_summary.cache_clear()
    try:
        summary = _profile.load_profile_summary()
    finally:
        _profile.load_profile_summary.cache_clear()

    assert "first field" in summary, "rpartition semantics would drop this"
    assert "last field" in summary
    assert "explanatory header" not in summary


def test_missing_both_sources_yields_the_placeholder(tmp_path, monkeypatch) -> None:
    """Both files absent must surface the gap, never a stale hardcoded profile."""

    from graphs import _profile

    monkeypatch.setattr(_profile, "_PROFILE_CARD_PATH", tmp_path / "absent-card.md")
    monkeypatch.setattr(_profile, "_RESUME_PATH", tmp_path / "absent-resume.md")
    _profile.load_profile_summary.cache_clear()
    try:
        assert _profile.load_profile_summary() == _profile.PLACEHOLDER_PROFILE
    finally:
        _profile.load_profile_summary.cache_clear()
