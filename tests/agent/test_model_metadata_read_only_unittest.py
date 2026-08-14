from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _snapshot(root: Path) -> dict[str, tuple]:
    result: dict[str, tuple] = {}
    for path in root.rglob("*"):
        key = path.relative_to(root).as_posix()
        result[key] = ("dir",) if path.is_dir() else ("file", path.read_bytes())
    return result


class ModelMetadataReadOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        import agent.model_metadata as metadata

        metadata._model_metadata_cache = {}
        metadata._model_metadata_cache_time = 0

    def _write_cache(self, home: Path, *, age_seconds: float = 0) -> Path:
        path = home / "cache" / "openrouter_model_metadata.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"fake/model": {"pricing": {"prompt": "0"}}}),
            encoding="utf-8",
        )
        timestamp = time.time() - age_seconds
        os.utime(path, (timestamp, timestamp))
        return path

    def test_read_only_fresh_and_stale_cache_are_read_without_network_or_write(self):
        import agent.model_metadata as metadata

        for age in (0, metadata._MODEL_CACHE_TTL + 60):
            with self.subTest(age=age), tempfile.TemporaryDirectory() as raw_home:
                home = Path(raw_home)
                self._write_cache(home, age_seconds=age)
                before = _snapshot(home)
                metadata._model_metadata_cache = {}
                metadata._model_metadata_cache_time = 0
                with patch.dict(os.environ, {"HERMES_HOME": raw_home}, clear=False), patch.object(
                    metadata.requests,
                    "get",
                    side_effect=AssertionError("metadata network attempted"),
                ):
                    result = metadata.fetch_model_metadata(read_only=True)
                self.assertIn("fake/model", result)
                self.assertEqual(before, _snapshot(home))

    def test_read_only_missing_and_malformed_cache_return_empty_without_side_effects(self):
        import agent.model_metadata as metadata

        for malformed in (False, True):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as raw_home:
                home = Path(raw_home)
                if malformed:
                    path = home / "cache" / "openrouter_model_metadata.json"
                    path.parent.mkdir(parents=True)
                    path.write_text("{malformed", encoding="utf-8")
                before = _snapshot(home)
                metadata._model_metadata_cache = {}
                metadata._model_metadata_cache_time = 0
                with patch.dict(os.environ, {"HERMES_HOME": raw_home}, clear=False), patch.object(
                    metadata.requests,
                    "get",
                    side_effect=AssertionError("metadata network attempted"),
                ):
                    result = metadata.fetch_model_metadata(read_only=True)
                self.assertEqual({}, result)
                self.assertEqual(before, _snapshot(home))

    def test_normal_mode_stale_cache_refreshes_and_writes(self):
        import agent.model_metadata as metadata

        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "data": [
                    {
                        "id": "fake/refreshed",
                        "context_length": 8192,
                        "top_provider": {"max_completion_tokens": 1024},
                        "pricing": {"prompt": "0", "completion": "0"},
                    }
                ]
            },
        )
        with tempfile.TemporaryDirectory() as raw_home:
            home = Path(raw_home)
            cache_path = self._write_cache(
                home, age_seconds=metadata._MODEL_CACHE_TTL + 60
            )
            before = cache_path.read_bytes()
            with patch.dict(os.environ, {"HERMES_HOME": raw_home}, clear=False), patch.object(
                metadata.requests, "get", return_value=response
            ) as get:
                result = metadata.fetch_model_metadata()
            self.assertEqual(1, get.call_count)
            self.assertIn("fake/refreshed", result)
            self.assertNotEqual(before, cache_path.read_bytes())

    def test_pricing_propagates_read_only_to_metadata_loader(self):
        import agent.usage_pricing as pricing

        usage = pricing.CanonicalUsage(input_tokens=1, output_tokens=1)
        with patch.object(pricing, "fetch_model_metadata", return_value={}) as fetch:
            result = pricing.estimate_usage_cost(
                "fake/model",
                usage,
                provider="openrouter",
                metadata_read_only=True,
            )
        fetch.assert_called_once_with(read_only=True)
        self.assertEqual("unknown", result.status)


if __name__ == "__main__":
    unittest.main()
