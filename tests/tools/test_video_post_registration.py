"""Registration contract for the built-in video_post tools.

Guards against regressions in how the tools ship with hermes: auto-discovery,
toolset membership, the single-check_fn-per-toolset rule, and schema validity.
"""

import unittest

import tools.video_post_tools  # noqa: F401  (triggers module-level registration)
from tools.registry import discover_builtin_tools, registry

EXPECTED = ["video_concat", "video_add_captions", "video_audio_mix",
            "video_pip", "html_to_video"]


class VideoPostRegistrationTest(unittest.TestCase):
    def _entry(self, name):
        return registry._tools[name]

    def test_module_is_auto_discovered(self):
        self.assertIn("tools.video_post_tools", discover_builtin_tools())

    def test_all_five_registered_in_video_post_toolset_sync(self):
        for name in EXPECTED:
            self.assertIn(name, registry._tools, f"{name} not registered")
            entry = self._entry(name)
            self.assertEqual(entry.toolset, "video_post")
            self.assertFalse(entry.is_async)

    def test_only_first_tool_carries_check_fn(self):
        # The registry keeps a single check_fn per toolset; ffmpeg availability
        # gates all five, so only video_concat carries it.
        self.assertIsNotNone(self._entry("video_concat").check_fn)
        for name in EXPECTED[1:]:
            self.assertIsNone(self._entry(name).check_fn, f"{name} should have no check_fn")

    def test_schemas_are_well_formed(self):
        for name in EXPECTED:
            schema = self._entry(name).schema
            self.assertEqual(schema["name"], name)
            params = schema["parameters"]
            self.assertIs(params.get("additionalProperties"), False,
                          f"{name} schema must forbid additionalProperties")
            props = params.get("properties", {})
            for req in params.get("required", []):
                self.assertIn(req, props, f"{name}: required '{req}' missing from properties")

    def test_handlers_are_callable(self):
        for name in EXPECTED:
            self.assertTrue(callable(self._entry(name).handler))


if __name__ == "__main__":
    unittest.main()
