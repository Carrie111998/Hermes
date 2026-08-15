from __future__ import annotations

import unittest

from hermes_cli.kanban_store.canonical import WIRE_PREFIX, canonical_json_bytes, prepare_intent, sha256_hex
from hermes_cli.kanban_store.types import (
    ContractError,
    DraftIntent,
    PublicationKind,
    TrustedIntentPolicy,
)


class CanonicalTests(unittest.TestCase):
    def test_key_order_is_fixed(self):
        self.assertEqual(canonical_json_bytes({"b": 1, "a": 2}), b'{"a":2,"b":1}')

    def test_unicode_is_not_normalized(self):
        self.assertNotEqual(canonical_json_bytes("é"), canonical_json_bytes("e\u0301"))

    def test_newline_variant_changes_bytes(self):
        self.assertNotEqual(canonical_json_bytes("a\n"), canonical_json_bytes("a\r\n"))

    def test_floats_are_forbidden(self):
        with self.assertRaises(ContractError):
            canonical_json_bytes({"x": 1.0})

    def test_wire_binds_exact_body_and_headers(self):
        draft = DraftIntent(
            PublicationKind.GITHUB_ISSUE_CREATE,
            {},
            {"title": "Issue", "body": "Body"},
            "nonce",
        )
        policy = TrustedIntentPolicy(
            True,
            "github-app:1",
            "github-issues-v1",
            {"repository_id": 1, "owner": "NousResearch", "repo": "hermes-agent"},
        )
        prepared = prepare_intent(intent_id="intent-1", draft=draft, policy=policy)
        self.assertEqual(sha256_hex(prepared.request_body_bytes), prepared.request_body_sha256)
        self.assertEqual(sha256_hex(WIRE_PREFIX + prepared.prepared_bytes), prepared.wire_sha256)
        self.assertIn(b"hermes-kanban:github.issue.create:intent-1", prepared.request_body_bytes)
        self.assertIn("Authorization", prepared.application_headers)

    def test_marker_conflict_is_rejected(self):
        draft = DraftIntent(
            PublicationKind.HERMES_TASK_COMPLETION_NOTIFY,
            {},
            {"marker": "wrong"},
            "nonce",
        )
        policy = TrustedIntentPolicy(
            True,
            "gateway:1",
            "hermes-delivery-v1",
            {
                "route_version": "v1",
                "platform": "discord",
                "account_id": "a",
                "conversation_id": "c",
                "thread_id": "t",
            },
        )
        with self.assertRaises(ContractError):
            prepare_intent(intent_id="intent-2", draft=draft, policy=policy)


if __name__ == "__main__":
    unittest.main()
