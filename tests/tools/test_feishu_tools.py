"""Tests for feishu_doc_tool and feishu_drive_tool — registration and schema validation."""

import importlib
import threading
import unittest

from tools.registry import registry

# Trigger tool discovery so feishu tools get registered
doc_tool = importlib.import_module("tools.feishu_doc_tool")
drive_tool = importlib.import_module("tools.feishu_drive_tool")


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


    def test_drive_tools_require_file_token(self):
        for tool_name in self.EXPECTED_TOOLS:
            if tool_name == "feishu_doc_read":
                continue
            entry = registry.get_entry(tool_name)
            props = entry.schema["parameters"].get("properties", {})
            self.assertIn("file_token", props, f"{tool_name} missing file_token param")
            self.assertIn("file_type", props, f"{tool_name} missing file_type param")


class TestFeishuToolClientFallback(unittest.TestCase):
    """Verify the profile-qualified client binding resolves in worker
    threads, which is the DM/gateway tool-execution path (issue #29760)."""

    def setUp(self):
        import tools.feishu_client_binding as binding

        binding.clear_all()
        self._old_local_doc = doc_tool.get_client()
        self._old_local_drive = drive_tool.get_client()
        doc_tool.set_client(None)
        drive_tool.set_client(None)

    def tearDown(self):
        import tools.feishu_client_binding as binding

        doc_tool.set_client(self._old_local_doc)
        drive_tool.set_client(self._old_local_drive)
        binding.clear_all()

    def _resolve_in_worker_thread(self, module):
        result = {}

        def _worker():
            result["client"] = module.get_client()

        t = threading.Thread(target=_worker)
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "worker thread did not finish")
        return result["client"]

    def test_doc_tool_binding_resolves_in_worker_thread(self):
        import tools.feishu_client_binding as binding

        shared = object()
        binding.publish(shared)
        self.assertIs(self._resolve_in_worker_thread(doc_tool), shared)

    def test_drive_tool_binding_resolves_in_worker_thread(self):
        import tools.feishu_client_binding as binding

        shared = object()
        binding.publish(shared)
        self.assertIs(self._resolve_in_worker_thread(drive_tool), shared)

    def test_thread_local_precedence_over_binding(self):
        import tools.feishu_client_binding as binding

        shared = object()
        local = object()
        binding.publish(shared)
        doc_tool.set_client(local)
        self.assertIs(doc_tool.get_client(), local)

    def test_no_binding_returns_none(self):
        self.assertIsNone(self._resolve_in_worker_thread(doc_tool))


if __name__ == "__main__":
    unittest.main()
