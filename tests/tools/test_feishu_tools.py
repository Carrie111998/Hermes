"""Tests for feishu_doc_tool and feishu_drive_tool — registration and schema validation."""

import importlib
import unittest
from unittest.mock import patch

from tools.registry import registry

# Trigger tool discovery so feishu tools get registered
importlib.import_module("tools.feishu_doc_tool")
importlib.import_module("tools.feishu_drive_tool")


class TestFeishuToolRegistration(unittest.TestCase):
    """Verify feishu tools are registered and have valid schemas."""

    EXPECTED_TOOLS = {
        "feishu_doc_read": "feishu_doc",
        "feishu_drive_list_comments": "feishu_drive",
        "feishu_drive_list_comment_replies": "feishu_drive",
        "feishu_drive_reply_comment": "feishu_drive",
        "feishu_drive_add_comment": "feishu_drive",
    }

    def test_all_tools_registered(self):
        for tool_name, toolset in self.EXPECTED_TOOLS.items():
            entry = registry.get_entry(tool_name)
            self.assertIsNotNone(entry, f"{tool_name} not registered")
            self.assertEqual(entry.toolset, toolset)

    def test_schemas_have_required_fields(self):
        for tool_name in self.EXPECTED_TOOLS:
            entry = registry.get_entry(tool_name)
            schema = entry.schema
            self.assertIn("name", schema)
            self.assertEqual(schema["name"], tool_name)
            self.assertIn("description", schema)
            self.assertIn("parameters", schema)
            self.assertIn("type", schema["parameters"])
            self.assertEqual(schema["parameters"]["type"], "object")

    def test_handlers_are_callable(self):
        for tool_name in self.EXPECTED_TOOLS:
            entry = registry.get_entry(tool_name)
            self.assertTrue(callable(entry.handler))

    def test_doc_read_schema_params(self):
        entry = registry.get_entry("feishu_doc_read")
        props = entry.schema["parameters"].get("properties", {})
        self.assertIn("doc_token", props)

    def test_drive_tools_require_file_token(self):
        for tool_name in self.EXPECTED_TOOLS:
            if tool_name == "feishu_doc_read":
                continue
            entry = registry.get_entry(tool_name)
            props = entry.schema["parameters"].get("properties", {})
            self.assertIn("file_token", props, f"{tool_name} missing file_token param")
            self.assertIn("file_type", props, f"{tool_name} missing file_type param")


class TestFeishuCommentAuthorization(unittest.TestCase):
    """Comment mutations require a permit bound to their exact payload."""

    def setUp(self):
        self.module = importlib.import_module("tools.feishu_drive_tool")
        self.module.set_client(object())

    def tearDown(self):
        self.module._local.client = None

    def test_reply_denied_before_provider_request(self):
        with patch.object(self.module, "_authorize_comment_action", side_effect=ValueError("outside grant")) as auth, \
             patch.object(self.module, "_do_request") as request:
            result = self.module._handle_reply_comment({
                "file_token": "doc-1", "comment_id": "comment-1", "content": "hello",
            })
        self.assertIn("authorization denied", result)
        auth.assert_called_once()
        request.assert_not_called()

    def test_add_comment_binds_exact_scope(self):
        with patch.object(self.module, "_authorize_comment_action") as auth, \
             patch.object(self.module, "_do_request", return_value=(0, "ok", {"id": "c1"})) as request:
            result = self.module._handle_add_comment({
                "file_token": "doc-1", "content": "hello", "file_type": "docx",
            })
        self.assertIn('"id": "c1"', result)
        auth.assert_called_once()
        capability, scope = auth.call_args.args
        self.assertEqual(capability, "feishu.comment.add")
        self.assertEqual(scope["file_token"], "doc-1")
        self.assertEqual(scope["content"], "hello")
        request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
