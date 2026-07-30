"""Tests for dashboard analytics days clamping."""

from hermes_cli.web_server import _clamp_analytics_days


class TestClampAnalyticsDays:
    def test_invalid_value_uses_default(self):
        assert _clamp_analytics_days("bad") == 30

    def test_zero_clamped_to_one(self):
        assert _clamp_analytics_days(0) == 1

    def test_negative_clamped_to_one(self):
        assert _clamp_analytics_days(-7) == 1

    def test_excessive_days_capped(self):
        assert _clamp_analytics_days(9999) == 365

    def test_ui_presets_unchanged(self):
        assert _clamp_analytics_days(7) == 7
        assert _clamp_analytics_days(30) == 30
        assert _clamp_analytics_days(90) == 90
