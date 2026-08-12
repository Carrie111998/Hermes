"""Tests for the cron-side morning briefing weather data source."""

from unittest.mock import patch

from cron.weather import fetch_morning_brief_weather


def _payload():
    return {
        "current_condition": [
            {
                "weatherDesc": [{"value": "Partly cloudy"}],
                "temp_C": "28",
                "FeelsLikeC": "31",
                "humidity": "70",
                "chanceofrain": "20",
                "windspeedKmph": "12",
            }
        ]
    }


def test_fetch_formats_current_conditions_without_agent_network_tools():
    with patch("cron.weather._weather_config", return_value={"weather_location": "Taipei"}), \
         patch("cron.weather._request_json", return_value=_payload()):
        result = fetch_morning_brief_weather()

    assert result.startswith("WEATHER_OK")
    assert "Location: Taipei" in result
    assert "Partly cloudy" in result
    assert "28 C" in result


def test_missing_location_is_non_blocking():
    with patch("cron.weather._weather_config", return_value={}):
        result = fetch_morning_brief_weather()

    assert result.startswith("WEATHER_UNAVAILABLE:")
    assert "config.yaml" in result


def test_null_location_is_non_blocking():
    with patch("cron.weather._weather_config", return_value={"weather_location": None}), \
         patch("cron.weather._request_json") as request:
        result = fetch_morning_brief_weather()

    assert result.startswith("WEATHER_UNAVAILABLE:")
    request.assert_not_called()


def test_upstream_failure_retries_then_returns_non_blocking_marker():
    with patch("cron.weather._weather_config", return_value={"weather_location": "Taipei"}), \
         patch("cron.weather._request_json", side_effect=OSError("network down")) as request, \
         patch("cron.weather._RETRY_BACKOFF_SECONDS", 0):
        result = fetch_morning_brief_weather()

    assert result == "WEATHER_UNAVAILABLE: weather service did not respond with usable data"
    assert request.call_count == 2


def test_scheduler_dispatches_only_the_vetted_builtin_name():
    from cron.scheduler import _run_builtin_cron_script

    with patch("cron.weather.fetch_morning_brief_weather", return_value="WEATHER_OK"):
        assert _run_builtin_cron_script("builtin:morning-brief-weather") == (True, "WEATHER_OK")
    assert _run_builtin_cron_script("builtin:arbitrary-import") is None
