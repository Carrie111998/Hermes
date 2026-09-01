"""The JobFlow modules must not carry the operator's personal data.

Three separate leaks of the same class landed in tracked source and were only
found after publication to a public fork, where the parent repository serves
the objects and retraction is impossible:

* ``graphs/_profile.py`` hardcoded a ``FALLBACK_PROFILE`` constant holding a
  real name, city, citizenship, employers, degrees and compensation floor.
* ``graphs/_prompts.py`` and ``graphs/jobflow.py`` repeated the compensation
  bands and geography -- the latter inside Pydantic ``Field(description=...)``
  strings, which serialise into the structured-output schema and therefore
  reached the model on every scoring call.
* ``jobflow_quality/matcher_filter.py`` held the walk-away figure as a live
  constant, plus a citizenship statement and the real employer names from the
  operator's application pipeline.

These tests make that class of leak fail the build instead of reaching a remote.
The values now live beside the master resume in the CV Handler knowledge base
and are read at runtime.

TWO TIERS, because source and tests have genuinely different risks:

* **Source modules** must carry no compensation figure, address, email or phone
  at all. Nothing about the candidate belongs in them.
* **Test files** legitimately need salary strings to exercise salary parsing --
  ``tests/jobflow_quality/test_matcher_filter.py`` has six and every one is
  correct. Forbidding those would delete real coverage, so tests are instead
  held to the reserved-fictional convention: contact details must be
  unmistakably fake (``example.invalid`` domains, ``555-01xx`` numbers).

Everything here matches SHAPES, never a blocklist of the actual values -- a
blocklist would have to contain the data it forbids, making the guard the leak.
Patterns are context-anchored for the same reason: an unanchored five-digit ZIP
pattern matched the slice limits ``[:12000]`` and ``[:10000]`` in jobflow.py,
and the tempting fix -- deleting the check -- would have silently removed real
coverage. A shape guard over source code needs anchoring or it trains people to
remove it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

# Source modules that compose, carry, or filter on candidate data.
_GUARDED_SOURCE = (
    "graphs/_profile.py",
    "graphs/_prompts.py",
    "graphs/jobflow.py",
    "jobflow_quality/matcher_filter.py",
    "jobflow_quality/qc.py",
)

# Test files whose fixtures stand in for a real person.
_GUARDED_TESTS = (
    "tests/jobflow_quality/test_qc.py",
    "tests/jobflow_quality/test_readiness.py",
    "tests/jobflow_quality/test_semantic_qc.py",
)

_FORBIDDEN_IN_SOURCE = {
    # "$260K", and the underscore form a literal constant takes: 180_000.
    "a compensation figure": re.compile(r"\$\s?\d{2,3}\s?[Kk]\b|\b\d{3}_\d{3}\b"),
    # Context-anchored: a bare five-digit run is far too common in source to be
    # evidence of anything (slice limits, token budgets, ports).
    "a US ZIP code": re.compile(r"\b\d{5}-\d{4}\b|,\s*[A-Z]{2}\s+\d{5}\b"),
    "an email address": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "a phone number": re.compile(r"\+?\d{1,2}[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"),
}

# Profile-card field labels, followed by a value. Generic labels, not values.
_FORBIDDEN_LABELS = (
    "target comp:",
    "preferred industries:",
    "remote preference:",
    "target seniority:",
    "us citizen",
)

_EMAIL = re.compile(r"[\w.+-]+@([\w.-]+\.[\w]+)")
_PHONE = re.compile(r"\+?1?[\s.-]*\(?(\d{3})\)?[\s.-]*(\d{3})[\s.-](\d{4})")

# RFC 2606 / RFC 6761 reserved domains, and the 555-0100..555-0199 range
# reserved for fiction.
_FICTIONAL_DOMAINS = ("example.com", "example.org", "example.net", "example.edu",
                      "example.invalid", "example.test", "example.localhost")


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", _GUARDED_SOURCE)
def test_source_carries_no_personal_value_shapes(rel: str) -> None:
    """No compensation figures, addresses, emails or phones in tracked source."""

    text = _read(rel)
    offenders = [
        f"{label} (matched {match.group(0)!r} at offset {match.start()})"
        for label, pattern in _FORBIDDEN_IN_SOURCE.items()
        if (match := pattern.search(text))
    ]
    assert not offenders, (
        f"{rel} contains personal data that would be published on push: "
        + "; ".join(offenders)
        + ". Put it in local state under the CV Handler knowledge base instead; "
        "it reaches the code at runtime."
    )


@pytest.mark.parametrize("rel", _GUARDED_SOURCE)
def test_source_does_not_inline_profile_card_fields(rel: str) -> None:
    """The profile card's field labels must not reappear in tracked source."""

    lowered = _read(rel).lower()
    offenders = [
        label
        for label in _FORBIDDEN_LABELS
        if re.search(re.escape(label) + r"[^\n]*[A-Za-z0-9$]", lowered)
    ]
    assert not offenders, (
        f"{rel} appears to inline profile-card fields {offenders}. "
        "Those belong in local state, not in tracked source."
    )


@pytest.mark.parametrize("rel", _GUARDED_TESTS)
def test_fixture_contact_details_are_unmistakably_fictional(rel: str) -> None:
    """Test fixtures may carry contact details only in the reserved ranges.

    Salary strings are deliberately NOT checked here -- test_matcher_filter.py
    needs them to exercise the parser, and forbidding them would delete real
    coverage. The risk in a fixture is a real identity, not a real number.
    """

    text = _read(rel)

    bad_domains = sorted(
        {d for d in _EMAIL.findall(text) if d.lower() not in _FICTIONAL_DOMAINS}
    )
    assert not bad_domains, (
        f"{rel} uses non-fictional email domains {bad_domains}. Fixtures must "
        f"use a reserved domain ({', '.join(_FICTIONAL_DOMAINS[:3])}, ...)."
    )

    bad_phones = sorted(
        {
            m.group(0).strip()
            for m in _PHONE.finditer(text)
            # 555-0100..555-0199 is the range reserved for fictional use.
            if not (m.group(2) == "555" or (m.group(1) == "555" and m.group(2) == "010"))
        }
    )
    assert not bad_phones, (
        f"{rel} contains phone numbers outside the reserved fictional range: "
        f"{bad_phones}. Use +1 (555) 010-xxxx."
    )


def test_placeholder_profile_is_not_a_plausible_profile() -> None:
    """The no-card placeholder must read as missing, not as a real candidate."""

    from graphs._profile import PLACEHOLDER_PROFILE

    assert "NO CANDIDATE PROFILE LOADED" in PLACEHOLDER_PROFILE
    # A stand-in that looks real is worse than none: it scores silently.
    for label, pattern in _FORBIDDEN_IN_SOURCE.items():
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


def test_compensation_floor_absent_config_disables_filtering(tmp_path, monkeypatch) -> None:
    """Unreadable criteria must fail OPEN -- exclude nothing, never a default."""

    from jobflow_quality import matcher_filter

    monkeypatch.setattr(matcher_filter, "_CRITERIA_PATH", tmp_path / "absent.json")
    assert matcher_filter._load_compensation_floor_usd() is None


@pytest.mark.parametrize(
    "payload",
    ['{"compensation_floor_usd": "180000"}', '{"compensation_floor_usd": 0}',
     '{"compensation_floor_usd": true}', "{}", "not json"],
)
def test_compensation_floor_rejects_unusable_config(tmp_path, monkeypatch, payload) -> None:
    """A malformed floor disables filtering rather than excluding wrongly."""

    from jobflow_quality import matcher_filter

    path = tmp_path / "matcher-criteria.json"
    path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(matcher_filter, "_CRITERIA_PATH", path)
    assert matcher_filter._load_compensation_floor_usd() is None


def test_compensation_floor_loads_a_valid_config(tmp_path, monkeypatch) -> None:
    """A well-formed config supplies the floor, proving the seam works."""

    from jobflow_quality import matcher_filter

    path = tmp_path / "matcher-criteria.json"
    path.write_text('{"compensation_floor_usd": 123456}', encoding="utf-8")
    monkeypatch.setattr(matcher_filter, "_CRITERIA_PATH", path)
    assert matcher_filter._load_compensation_floor_usd() == 123456
