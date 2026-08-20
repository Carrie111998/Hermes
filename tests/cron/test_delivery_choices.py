"""Unit tests for cron delivery-time action buttons (#78999)."""

from __future__ import annotations

import unittest

from cron.delivery_choices import (
    STALE_HINT,
    clear_delivery_choices,
    normalize_delivery_choices,
    register_delivery_choices,
    resolve_delivery_choice,
    resolve_delivery_choices,
    split_delivery_choices,
    supersede_job_deliveries,
)


class TestNormalizeDeliveryChoices(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(normalize_delivery_choices(None))
        self.assertIsNone(normalize_delivery_choices([]))
        self.assertIsNone(normalize_delivery_choices(["", "  "]))

    def test_strips_and_caps(self):
        self.assertEqual(
            normalize_delivery_choices([" vis bilag ", "skip"]),
            ["vis bilag", "skip"],
        )
        many = [str(i) for i in range(20)]
        self.assertEqual(len(normalize_delivery_choices(many)), 8)

    def test_single_string(self):
        self.assertEqual(normalize_delivery_choices("only"), ["only"])


class TestSplitEnvelope(unittest.TestCase):
    def test_plain_text_untouched(self):
        text, choices, saw = split_delivery_choices("Vercel 163,32 DKK")
        self.assertEqual(text, "Vercel 163,32 DKK")
        self.assertIsNone(choices)
        self.assertFalse(saw)

    def test_strips_last_line_envelope(self):
        body = "Preview line\n\n{\"delivery_choices\": [\"vis bilag\", \"spring over\"]}"
        text, choices, saw = split_delivery_choices(body)
        self.assertEqual(text, "Preview line")
        self.assertEqual(choices, ["vis bilag", "spring over"])
        self.assertTrue(saw)

    def test_json_preview_without_key_untouched(self):
        body = '{"amount": 163.32, "account": 7320}'
        text, choices, saw = split_delivery_choices(body)
        self.assertEqual(text, body)
        self.assertIsNone(choices)
        self.assertFalse(saw)

    def test_invalid_json_untouched(self):
        body = "hello\n{not json}"
        text, choices, saw = split_delivery_choices(body)
        self.assertEqual(text, body)
        self.assertFalse(saw)

    def test_empty_envelope_still_strips(self):
        body = "Preview\n{\"delivery_choices\": []}"
        text, choices, saw = split_delivery_choices(body)
        self.assertEqual(text, "Preview")
        self.assertIsNone(choices)
        self.assertTrue(saw)


class TestResolveDeliveryChoices(unittest.TestCase):
    def test_envelope_wins_over_job(self):
        job = {"delivery_choices": ["job-a", "job-b"]}
        text, choices = resolve_delivery_choices(
            "Hi\n{\"delivery_choices\": [\"vis bilag\"]}",
            job,
        )
        self.assertEqual(text, "Hi")
        self.assertEqual(choices, ["vis bilag"])

    def test_job_fallback_when_no_envelope(self):
        job = {"delivery_choices": ["vis bilag", "spring over"]}
        text, choices = resolve_delivery_choices("Hi", job)
        self.assertEqual(text, "Hi")
        self.assertEqual(choices, ["vis bilag", "spring over"])

    def test_empty_envelope_does_not_fall_back(self):
        job = {"delivery_choices": ["vis bilag"]}
        text, choices = resolve_delivery_choices(
            "Hi\n{\"delivery_choices\": []}",
            job,
        )
        self.assertEqual(text, "Hi")
        self.assertIsNone(choices)

    def test_local_job_without_choices(self):
        text, choices = resolve_delivery_choices("silent", {"deliver": "local"})
        self.assertEqual(text, "silent")
        self.assertIsNone(choices)


class TestPendingStore(unittest.TestCase):
    def setUp(self):
        clear_delivery_choices()

    def tearDown(self):
        clear_delivery_choices()

    def test_register_and_resolve(self):
        register_delivery_choices("d1", ["vis bilag", "spring over"], "job-1")
        self.assertEqual(resolve_delivery_choice("d1", 0), "vis bilag")
        self.assertIsNone(resolve_delivery_choice("d1", 1))

    def test_stale_after_ttl(self):
        register_delivery_choices(
            "d1", ["a"], "job-1", ttl_seconds=10, now=100.0
        )
        self.assertIsNone(resolve_delivery_choice("d1", 0, now=111.0))
        self.assertTrue(STALE_HINT)

    def test_new_delivery_supersedes_old(self):
        register_delivery_choices("old", ["a"], "job-1")
        register_delivery_choices("new", ["b"], "job-1")
        self.assertIsNone(resolve_delivery_choice("old", 0))
        self.assertEqual(resolve_delivery_choice("new", 0), "b")

    def test_other_job_untouched(self):
        register_delivery_choices("a", ["one"], "job-a")
        register_delivery_choices("b", ["two"], "job-b")
        self.assertEqual(supersede_job_deliveries("job-a"), 1)
        self.assertEqual(resolve_delivery_choice("b", 0), "two")

    def test_bad_index(self):
        register_delivery_choices("d1", ["only"], "job-1")
        self.assertIsNone(resolve_delivery_choice("d1", 3))


if __name__ == "__main__":
    unittest.main()
