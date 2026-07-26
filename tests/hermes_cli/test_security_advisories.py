"""Tests for hermes_cli.security_advisories.

The advisory module is the user-facing detection / remediation surface
for supply-chain attacks (e.g. the Mini Shai-Hulud worm of May 2026 that
poisoned mistralai 2.4.6 on PyPI). These tests exercise the public API in
isolation — no real package metadata, no real config, no real cache.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterator

import pytest

import hermes_cli.security_advisories as adv


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_advisory() -> adv.Advisory:
    """A self-contained Advisory used across tests."""
    return adv.Advisory(
        id="test-advisory-2026-99",
        title="Test advisory",
        summary="Pretend this package has been compromised.",
        url="https://example.com/advisory",
        compromised=(
            ("fake-malicious-pkg", frozenset({"6.6.6"})),
        ),
        remediation=(
            "pip uninstall -y fake-malicious-pkg",
            "Rotate any credentials that may have been exposed.",
        ),
        published="2026-01-01",
        severity="critical",
    )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HERMES_HOME so banner cache and config writes are sandboxed."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "cache").mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def patched_version(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str]]:
    """Override _installed_version with a controllable lookup table."""
    table: dict[str, str] = {}
    monkeypatch.setattr(adv, "_installed_version", lambda pkg: table.get(pkg))
    yield table


# ---------------------------------------------------------------------------
# detect_compromised
# ---------------------------------------------------------------------------


class TestDetectCompromised:
    def test_no_match_returns_empty_list(self, fake_advisory, patched_version):
        # No matching package installed.
        hits = adv.detect_compromised(advisories=[fake_advisory])
        assert hits == []

    def test_exact_version_match(self, fake_advisory, patched_version):
        patched_version["fake-malicious-pkg"] = "6.6.6"
        hits = adv.detect_compromised(advisories=[fake_advisory])
        assert len(hits) == 1
        assert hits[0].advisory.id == fake_advisory.id
        assert hits[0].package == "fake-malicious-pkg"
        assert hits[0].installed_version == "6.6.6"

    def test_safe_version_does_not_match(self, fake_advisory, patched_version):
        # Package is installed but the version is not in the compromised set.
        patched_version["fake-malicious-pkg"] = "6.6.5"
        hits = adv.detect_compromised(advisories=[fake_advisory])
        assert hits == []

    def test_empty_compromised_set_matches_any_version(
        self, patched_version
    ):
        # An advisory with an empty version set is a "any version is suspect"
        # wildcard — used when an entire maintainer namespace is owned.
        wildcard = adv.Advisory(
            id="wildcard",
            title="Whole namespace owned",
            summary="x",
            url="x",
            compromised=(("evil-namespace", frozenset()),),
            remediation=("uninstall it",),
        )
        patched_version["evil-namespace"] = "0.0.1"
        hits = adv.detect_compromised(advisories=[wildcard])
        assert len(hits) == 1
        assert hits[0].installed_version == "0.0.1"


# ---------------------------------------------------------------------------
# Acknowledgement persistence
# ---------------------------------------------------------------------------


class TestAck:
    def test_get_acked_ids_empty_when_no_config(self, monkeypatch):
        # load_config raises → returns empty set, doesn't crash.
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert adv.get_acked_ids() == set()

    def test_filter_unacked_strips_dismissed(self, fake_advisory, monkeypatch):
        hit = adv.AdvisoryHit(
            advisory=fake_advisory,
            package="fake-malicious-pkg",
            installed_version="6.6.6",
        )
        monkeypatch.setattr(adv, "get_acked_ids", lambda: {fake_advisory.id})
        assert adv.filter_unacked([hit]) == []

    def test_filter_unacked_passes_through_unknown(
        self, fake_advisory, monkeypatch
    ):
        hit = adv.AdvisoryHit(
            advisory=fake_advisory,
            package="fake-malicious-pkg",
            installed_version="6.6.6",
        )
        monkeypatch.setattr(adv, "get_acked_ids", lambda: set())
        assert adv.filter_unacked([hit]) == [hit]

    def test_ack_advisory_persists_id(self, isolated_home, monkeypatch):
        # Stub the config layer end-to-end with a tiny in-memory store so we
        # don't depend on the full hermes_cli.config bootstrap.
        store: dict = {"security": {}}
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: store
        )
        monkeypatch.setattr(
            "hermes_cli.config.save_config",
            lambda cfg: store.update(cfg) or None,
        )
        assert adv.ack_advisory("test-advisory-2026-99") is True
        assert "test-advisory-2026-99" in store["security"]["acked_advisories"]
        # Idempotent.
        adv.ack_advisory("test-advisory-2026-99")
        assert (
            store["security"]["acked_advisories"].count("test-advisory-2026-99")
            == 1
        )

    def test_ack_advisory_rejects_blank(self, isolated_home):
        assert adv.ack_advisory("") is False
        assert adv.ack_advisory("   ") is False


# ---------------------------------------------------------------------------
# Banner cache rate limiting
# ---------------------------------------------------------------------------


class TestBannerCache:
    def test_first_call_returns_due_hits(
        self, fake_advisory, isolated_home, monkeypatch
    ):
        monkeypatch.setattr(adv, "get_acked_ids", lambda: set())
        hit = adv.AdvisoryHit(
            advisory=fake_advisory,
            package="fake-malicious-pkg",
            installed_version="6.6.6",
        )
        due = adv.hits_due_for_banner([hit])
        assert due == [hit]

    def test_second_call_within_window_suppresses(
        self, fake_advisory, isolated_home, monkeypatch
    ):
        monkeypatch.setattr(adv, "get_acked_ids", lambda: set())
        hit = adv.AdvisoryHit(
            advisory=fake_advisory,
            package="fake-malicious-pkg",
            installed_version="6.6.6",
        )
        adv.hits_due_for_banner([hit])
        # Same banner inside repeat window → suppressed.
        again = adv.hits_due_for_banner([hit])
        assert again == []

    def test_call_after_window_re_banners(
        self, fake_advisory, isolated_home, monkeypatch
    ):
        monkeypatch.setattr(adv, "get_acked_ids", lambda: set())
        hit = adv.AdvisoryHit(
            advisory=fake_advisory,
            package="fake-malicious-pkg",
            installed_version="6.6.6",
        )
        adv.hits_due_for_banner([hit])
        # Backdate the cache so it looks like the banner was shown more
        # than 24h ago — should re-banner.
        cache_path = adv._banner_cache_path()
        assert cache_path is not None
        old_lines = cache_path.read_text(encoding="utf-8").splitlines()
        backdated = []
        for line in old_lines:
            parts = line.split(None, 1)
            if len(parts) == 2:
                backdated.append(f"{parts[0]} {time.time() - 48 * 3600}")
        cache_path.write_text("\n".join(backdated) + "\n", encoding="utf-8")
        again = adv.hits_due_for_banner([hit])
        assert again == [hit]

    def test_acked_hits_never_banner(
        self, fake_advisory, isolated_home, monkeypatch
    ):
        monkeypatch.setattr(adv, "get_acked_ids", lambda: {fake_advisory.id})
        hit = adv.AdvisoryHit(
            advisory=fake_advisory,
            package="fake-malicious-pkg",
            installed_version="6.6.6",
        )
        assert adv.hits_due_for_banner([hit]) == []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRendering:
    def test_short_banner_lines_includes_id_and_version(self, fake_advisory):
        hit = adv.AdvisoryHit(
            advisory=fake_advisory,
            package="fake-malicious-pkg",
            installed_version="6.6.6",
        )
        lines = adv.short_banner_lines([hit])
        joined = "\n".join(lines)
        assert fake_advisory.id in joined
        assert fake_advisory.title in joined
        assert "fake-malicious-pkg==6.6.6" in joined
        assert "hermes doctor" in joined

    def test_full_remediation_text_contains_all_steps(self, fake_advisory):
        hit = adv.AdvisoryHit(
            advisory=fake_advisory,
            package="fake-malicious-pkg",
            installed_version="6.6.6",
        )
        body = "\n".join(adv.full_remediation_text(hit))
        # All remediation steps must be present.
        for step in fake_advisory.remediation:
            assert step in body
        assert fake_advisory.url in body
        assert fake_advisory.summary in body

    def test_render_doctor_section_clean_state(self):
        # No hits → success message, has_problems=False.
        has_problems, lines = adv.render_doctor_section([])
        assert has_problems is False
        assert any("No active security advisories" in line for line in lines)

    def test_render_doctor_section_with_unacked_hit(
        self, fake_advisory, monkeypatch
    ):
        monkeypatch.setattr(adv, "get_acked_ids", lambda: set())
        hit = adv.AdvisoryHit(
            advisory=fake_advisory,
            package="fake-malicious-pkg",
            installed_version="6.6.6",
        )
        has_problems, lines = adv.render_doctor_section([hit])
        assert has_problems is True
        body = "\n".join(lines)
        assert fake_advisory.title in body

    def test_gateway_log_message_singular(self, fake_advisory, monkeypatch):
        monkeypatch.setattr(adv, "get_acked_ids", lambda: set())
        hit = adv.AdvisoryHit(
            advisory=fake_advisory,
            package="fake-malicious-pkg",
            installed_version="6.6.6",
        )
        msg = adv.gateway_log_message([hit])
        assert msg is not None
        assert fake_advisory.id in msg
        assert "fake-malicious-pkg==6.6.6" in msg

    def test_gateway_log_message_returns_none_for_no_hits(self):
        assert adv.gateway_log_message([]) is None


# ---------------------------------------------------------------------------
# Real catalog smoke test
# ---------------------------------------------------------------------------


class TestRealCatalog:
    def test_advisories_well_formed(self):
        """Every shipped advisory must be self-consistent.

        Catches data-entry mistakes (empty IDs, missing remediation, bad
        compromised tuples) before they ship.
        """
        seen_ids: set[str] = set()
        for advisory in adv.ADVISORIES:
            assert advisory.id, "advisory has empty id"
            assert advisory.id not in seen_ids, f"duplicate id {advisory.id}"
            seen_ids.add(advisory.id)
            assert advisory.title, f"{advisory.id}: empty title"
            assert advisory.summary, f"{advisory.id}: empty summary"
            assert advisory.remediation, f"{advisory.id}: empty remediation"
            assert advisory.url.startswith("http"), \
                f"{advisory.id}: bad url {advisory.url!r}"
            assert advisory.compromised, \
                f"{advisory.id}: empty compromised tuple"
            for pkg, versions in advisory.compromised:
                assert pkg, f"{advisory.id}: empty package name"
                assert isinstance(versions, frozenset), \
                    f"{advisory.id}: versions must be frozenset"


# ---------------------------------------------------------------------------
# npm advisory catalog + ack mechanism
# ---------------------------------------------------------------------------


class TestNpmAdvisories:
    """Tests for the npm GHSA catalog and ack persistence.

    Mirrors ``TestAck`` above for the npm side — same config-store
    monkeypatching pattern, same idempotency expectations, but exercises
    the NpmAdvisory + ack_npm_advisory + filter_unacked_npm surface.
    """

    def test_catalog_well_formed(self):
        """Every npm advisory must be self-consistent."""
        seen_ids: set[str] = set()
        for npm_adv in adv.NPM_ADVISORIES:
            assert npm_adv.id.startswith("GHSA-"), \
                f"{npm_adv.id}: must start with GHSA-"
            assert npm_adv.id not in seen_ids, \
                f"duplicate npm advisory id {npm_adv.id}"
            seen_ids.add(npm_adv.id)
            assert npm_adv.title, f"{npm_adv.id}: empty title"
            assert npm_adv.summary, f"{npm_adv.id}: empty summary"
            assert npm_adv.url.startswith("http"), \
                f"{npm_adv.id}: bad url {npm_adv.url!r}"
            assert npm_adv.package, f"{npm_adv.id}: empty package name"
            assert npm_adv.severity in ("low", "high", "critical"), \
                f"{npm_adv.id}: bad severity {npm_adv.severity!r}"

    def test_get_acked_npm_ids_empty_when_no_config(self, monkeypatch):
        """load_config raises → returns empty set, doesn't crash."""
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert adv.get_acked_npm_ids() == set()

    def test_filter_unacked_npm_strips_dismissed(self, monkeypatch):
        """An acked GHSA should not appear in filter_unacked_npm output."""
        monkeypatch.setattr(adv, "get_acked_npm_ids", lambda: {"GHSA-qwww-vcr4-c8h2"})
        out = adv.filter_unacked_npm(["GHSA-qwww-vcr4-c8h2", "GHSA-other"])
        assert out == ["GHSA-other"]

    def test_ack_npm_advisory_persists_id(self, isolated_home, monkeypatch):
        """Successful ack writes to security.acked_npm_advisories."""
        store: dict = {"security": {}}
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: store
        )
        monkeypatch.setattr(
            "hermes_cli.config.save_config",
            lambda cfg: store.update(cfg) or None,
        )
        assert adv.ack_npm_advisory("GHSA-qwww-vcr4-c8h2") is True
        assert "GHSA-qwww-vcr4-c8h2" in store["security"]["acked_npm_advisories"]
        # Idempotent.
        adv.ack_npm_advisory("GHSA-qwww-vcr4-c8h2")
        assert (
            store["security"]["acked_npm_advisories"]
            .count("GHSA-qwww-vcr4-c8h2")
            == 1
        )

    def test_ack_npm_advisory_rejects_unknown_id(self, monkeypatch):
        """Unknown GHSA IDs are not silently persisted."""
        # No config stub needed — validation happens before persistence.
        assert adv.ack_npm_advisory("GHSA-does-not-exist") is False

    def test_ack_npm_advisory_rejects_blank(self):
        assert adv.ack_npm_advisory("") is False
        assert adv.ack_npm_advisory("   ") is False

    def test_ack_does_not_pollute_python_catalog(self, isolated_home, monkeypatch):
        """npm acks go to a separate list from Python acks."""
        store: dict = {"security": {}}
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: store
        )
        monkeypatch.setattr(
            "hermes_cli.config.save_config",
            lambda cfg: store.update(cfg) or None,
        )
        adv.ack_npm_advisory("GHSA-qwww-vcr4-c8h2")
        # The npm GHSA must not appear in the Python acks list.
        assert store["security"].get("acked_advisories", []) == []
        assert "GHSA-qwww-vcr4-c8h2" in store["security"]["acked_npm_advisories"]

    def test_unack_npm_advisory_removes_id(self, isolated_home, monkeypatch):
        """Successful unack removes the GHSA from the persisted list."""
        store: dict = {"security": {"acked_npm_advisories": ["GHSA-qwww-vcr4-c8h2"]}}
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: store
        )
        monkeypatch.setattr(
            "hermes_cli.config.save_config",
            lambda cfg: store.update(cfg) or None,
        )
        assert adv.unack_npm_advisory("GHSA-qwww-vcr4-c8h2") is True
        assert "GHSA-qwww-vcr4-c8h2" not in store["security"]["acked_npm_advisories"]

    def test_unack_npm_advisory_idempotent(self, isolated_home, monkeypatch):
        """Unacking a never-acked ID is a no-op (still returns True)."""
        store: dict = {"security": {"acked_npm_advisories": []}}
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: store
        )
        monkeypatch.setattr(
            "hermes_cli.config.save_config",
            lambda cfg: store.update(cfg) or None,
        )
        assert adv.unack_npm_advisory("GHSA-never-acked") is True
        assert store["security"]["acked_npm_advisories"] == []

    def test_unack_npm_advisory_rejects_blank(self):
        assert adv.unack_npm_advisory("") is False
        assert adv.unack_npm_advisory("   ") is False

    def test_unack_preserves_other_acked_ids(self, isolated_home, monkeypatch):
        """Removing one GHSA must not affect sibling acks in the list."""
        store: dict = {"security": {"acked_npm_advisories": [
            "GHSA-qwww-vcr4-c8h2",
            "GHSA-other",
            "GHSA-third",
        ]}}
        monkeypatch.setattr(
            "hermes_cli.config.load_config", lambda: store
        )
        monkeypatch.setattr(
            "hermes_cli.config.save_config",
            lambda cfg: store.update(cfg) or None,
        )
        adv.unack_npm_advisory("GHSA-qwww-vcr4-c8h2")
        assert store["security"]["acked_npm_advisories"] == [
            "GHSA-other",
            "GHSA-third",
        ]
