"""Tests for Feishu markdown post-payload preprocessor (2026-08-20).

Covers:
- _optimize_markdown_style_for_feishu: H1→H4, H2-H6→H5
- _convert_markdown_tables_for_feishu: 表格前后加 <br>
- _prepare_markdown_for_feishu_post: 组合调用
- _build_outbound_payload: 表格不再被强制降级到 text (Ben 报告的根因)
"""
import json
import sys
import unittest
from pathlib import Path

# Make hermes_agent importable
ROOT = Path("/Users/bw/.hermes/hermes-agent").resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.platforms.feishu import (  # noqa: E402
    _build_markdown_post_payload,
    _convert_markdown_tables_for_feishu,
    _optimize_markdown_style_for_feishu,
    _prepare_markdown_for_feishu_post,
)


class TestOptimizeMarkdownStyle(unittest.TestCase):
    def test_h1_downgraded_to_h4(self):
        text = "# Title here\n\nbody"
        out = _optimize_markdown_style_for_feishu(text)
        self.assertIn("#### Title here", out)
        self.assertNotIn("\n# Title", out)

    def test_h2_h6_downgraded_to_h5(self):
        text = "## H2\n\n### H3\n\n#### H4\n\n##### H5\n\n###### H6\n"
        out = _optimize_markdown_style_for_feishu(text)
        # All should become H5 (##### ...)
        self.assertEqual(out.count("##### "), 5)
        # 用行首正则精确判断: 不应有 H2/H3/H4/H6 行首
        self.assertNotRegex(out, r"^## ", "不应再有 H2 行首")
        self.assertNotRegex(out, r"^### ", "不应再有 H3 行首")
        self.assertNotRegex(out, r"^#### ", "不应再有 H4 行首")
        self.assertNotRegex(out, r"^###### ", "不应再有 H6 行首")

    def test_empty_text_unchanged(self):
        self.assertEqual("", _optimize_markdown_style_for_feishu(""))
        self.assertIsNone(_optimize_markdown_style_for_feishu(None))

    def test_no_headings_unchanged(self):
        text = "Just plain text\nwith multiple lines"
        self.assertEqual(text, _optimize_markdown_style_for_feishu(text))


class TestConvertMarkdownTablesForFeishu(unittest.TestCase):
    def test_no_tables_unchanged(self):
        text = "Just text\nno tables here"
        self.assertEqual(text, _convert_markdown_tables_for_feishu(text))

    def test_table_gets_br_around_it(self):
        text = "Before text\n\n| col1 | col2 |\n|---|---|\n| a | b |\n\nAfter text"
        out = _convert_markdown_tables_for_feishu(text)
        # 表格前必须有 <br>
        self.assertIn("Before text\n\n<br>\n\n| col1 | col2 |", out)
        # 表格后必须有 <br>
        self.assertIn("| a | b |\n\n<br>\n\nAfter text", out)

    def test_multiple_tables_each_get_br(self):
        text = (
            "para1\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\npara2\n\n| c | d |\n|---|---|\n| 3 | 4 |\n\npara3"
        )
        out = _convert_markdown_tables_for_feishu(text)
        # 应有 2 对 <br> (每个表格前后各 1)
        self.assertEqual(out.count("<br>"), 4)
        self.assertIn("para1\n\n<br>\n\n| a | b |", out)
        self.assertIn("| 1 | 2 |\n\n<br>\n\npara2", out)
        self.assertIn("para2\n\n<br>\n\n| c | d |", out)
        self.assertIn("| 3 | 4 |\n\n<br>\n\npara3", out)


class TestPrepareMarkdownForFeishuPost(unittest.TestCase):
    def test_combines_both(self):
        text = "# Heading\n\nBody\n\n| x | y |\n|---|---|\n| 1 | 2 |"
        out = _prepare_markdown_for_feishu_post(text)
        # Heading downgraded
        self.assertIn("#### Heading", out)
        # Table has <br> around
        self.assertIn("Body\n\n<br>\n\n| x | y |", out)
        self.assertIn("| 1 | 2 |\n\n<br>", out)


class TestBuildMarkdownPostPayload(unittest.TestCase):
    def test_returns_valid_post_payload(self):
        text = "# Title\n\nBody"
        out = _build_markdown_post_payload(text)
        parsed = json.loads(out)
        self.assertIn("zh_cn", parsed)
        self.assertIn("content", parsed["zh_cn"])
        # 至少一个 row, 每个 row 是 list of dicts
        self.assertIsInstance(parsed["zh_cn"]["content"], list)
        self.assertGreater(len(parsed["zh_cn"]["content"]), 0)


class TestBuildOutboundPayloadFix(unittest.TestCase):
    """核心回归测试: 表格消息不再被强制降级到 msg_type=text.

    之前的 bug (Ben 2026-08-20 报告): 含表格的消息被强制 msg_type=text,
    飞书客户端不解析 markdown, 显示 | col | col | 源码.
    修复后: 含 markdown 提示 (含表格) 都走 post + 预处理.
    """

    def _get_payload(self, text):
        # _build_outbound_payload 是 FeishuAdapter 实例方法,
        # 我们用最小 Mock 实例来测 (不触发构造时的真实连接)
        from gateway.platforms.feishu import FeishuAdapter
        adapter = FeishuAdapter.__new__(FeishuAdapter)
        return adapter._build_outbound_payload(text)

    def test_table_message_uses_post_not_text(self):
        """核心修复: 表格走 post, 不再走 text 显示源码."""
        msg_type, payload = self._get_payload(
            "看下这个清单:\n\n| Article | 状态 |\n|---|---|\n| 123 | ✅ |\n| 456 | ❌ |"
        )
        self.assertEqual(msg_type, "post", "含表格的消息必须走 post 富文本")
        # payload 是 JSON 字符串, 含 zh_cn 结构
        parsed = json.loads(payload)
        self.assertIn("zh_cn", parsed)

    def test_markdown_message_uses_post(self):
        """普通 markdown (列表/粗体等) 也走 post."""
        msg_type, _ = self._get_payload("**粗体** 和 *斜体* 还有 - 列表项")
        self.assertEqual(msg_type, "post")

    def test_plain_text_uses_text(self):
        """纯文本 (无 markdown 提示) 仍然走 text."""
        msg_type, _ = self._get_payload("hello world")
        self.assertEqual(msg_type, "text")

    def test_h1_message_uses_post_with_h4_downgrade(self):
        """含 H1 标题走 post + H1 降级为 H4."""
        msg_type, payload = self._get_payload("# 这是大标题\n\n正文")
        self.assertEqual(msg_type, "post")
        # payload 里的 md text 应该 H1 已降级
        parsed = json.loads(payload)
        rows = parsed["zh_cn"]["content"]
        all_text = " ".join(
            d.get("text", "")
            for row in rows
            for d in row
            if d.get("tag") == "md"
        )
        # 必须是 #### 形式 (降级后)
        self.assertIn("#### 这是大标题", all_text)
        # 行首不应再有 H1 (#) 标记 (用正则看 markdown 行首模式)
        self.assertNotRegex(all_text, r"^# [^#]", "不应再有 H1 标记在行首")


if __name__ == "__main__":
    unittest.main(verbosity=2)