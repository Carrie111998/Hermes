"""Seam checks for the LB8 summary-kernel mixin extraction."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

from agent import context_compressor as cc
from agent import context_compressor_summary_kernel as sk


MEMBERS = [
    "_strip_summary_prefix",
    "_with_summary_prefix",
    "_starts_with_summary_prefix",
    "classify_summary_content",
    "_is_context_summary_content",
    "_has_compressed_summary_metadata",
    "_transcript_has_real_user_turn",
    "_is_synthetic_compression_user_turn",
    "_validate_summary_user_provenance",
    "_is_context_summary_message",
]


def test_summary_kernel_mro_and_raw_descriptor_identity():
    assert cc.ContextCompressor.__mro__.index(
        sk.ContextCompressorSummaryKernelMixin
    ) < cc.ContextCompressor.__mro__.index(cc.ContextEngine)
    for name in MEMBERS:
        assert inspect.getattr_static(
            cc.ContextCompressor, name
        ) is inspect.getattr_static(sk.ContextCompressorSummaryKernelMixin, name)


def test_godfile_has_no_duplicate_kernel_defs_and_exports_mixin():
    source = Path(cc.__file__).read_text(encoding="utf-8")
    assert "context_compressor_summary_kernel" in source
    for name in MEMBERS:
        assert f"def {name}" not in source
    assert cc.SUMMARY_PREFIX is sk.SUMMARY_PREFIX
    assert cc._HISTORICAL_SUMMARY_PREFIXES is sk._HISTORICAL_SUMMARY_PREFIXES


def test_summary_kernel_behavior_and_consumers():
    summary = cc.SUMMARY_PREFIX + "\nBODY"
    assert cc.ContextCompressor._strip_summary_prefix(summary) == "BODY"
    assert cc.ContextCompressor.classify_summary_content(summary) == "standalone"
    assert cc.ContextCompressor._is_context_summary_message({
        "role": "assistant",
        "content": summary,
    })
    assert cc.ContextCompressor._transcript_has_real_user_turn([
        {"role": "user", "content": "real ask"}
    ])
    assert not cc.ContextCompressor._transcript_has_real_user_turn([
        {"role": "user", "content": cc.COMPRESSION_CONTINUATION_USER_CONTENT}
    ])


def test_summary_kernel_import_orders_in_fresh_interpreters():
    snippets = [
        "import agent.context_compressor_summary_kernel as sk; "
        "import agent.context_compressor as cc; "
        "assert cc.ContextCompressorSummaryKernelMixin is sk.ContextCompressorSummaryKernelMixin",
        "import agent.context_compressor as cc; "
        "import agent.context_compressor_summary_kernel as sk; "
        "assert cc.ContextCompressorSummaryKernelMixin is sk.ContextCompressorSummaryKernelMixin",
    ]
    for snippet in snippets:
        result = subprocess.run(
            [sys.executable, "-c", snippet],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
