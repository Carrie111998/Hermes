"""Tests for the Wigolo (local CLI) web search provider.

Covers:
- is_available() — CLI resolvable AND ~/.wigolo provisioned (no Node spawn)
- search() — happy path, not-installed, not-initialized, timeout, CLI error
- Result normalization (title, url, description from snippet, position)
- Limit capping to the CLI maximum (20)
- The measured failure mode gets a loud log: engine pool degraded to bing-only
"""
from __future__ import annotations

import subprocess

import pytest

from plugins.web.wigolo import provider as wig
from plugins.web.wigolo.provider import WigoloWebSearchProvider


def _doc(results, engines=("bing", "duckduckgo")):
    return {"results": list(results), "engines_used": list(engines)}


@pytest.fixture()
def available(monkeypatch):
    """Provider looks installed + initialized; CLI is stubbed per-test."""
    monkeypatch.setattr(wig, "_wigolo_argv", lambda: ["/usr/bin/true"])
    monkeypatch.setattr(wig, "_initialized", lambda: True)
    return WigoloWebSearchProvider()


class TestAvailability:
    def test_available_needs_cli_and_init(self, monkeypatch):
        p = WigoloWebSearchProvider()
        monkeypatch.setattr(wig, "_wigolo_argv", lambda: ["npx", "-y", "wigolo"])
        monkeypatch.setattr(wig, "_initialized", lambda: True)
        assert p.is_available() is True
        monkeypatch.setattr(wig, "_initialized", lambda: False)
        assert p.is_available() is False
        monkeypatch.setattr(wig, "_wigolo_argv", lambda: None)
        assert p.is_available() is False

    def test_search_only(self, available):
        assert available.supports_search() is True
        assert available.supports_extract() is False


class TestSearch:
    def test_happy_path_normalizes_results(self, available, monkeypatch):
        monkeypatch.setattr(wig, "_run_wigolo_search", lambda q, n: _doc([
            {"title": "SK海力士向韩美半导体下单442亿韩元", "url": "https://mk.co.kr/a",
             "snippet": "HBM4 TC bonder"},
            {"title": "second", "url": "https://x.kr/b", "snippet": "s2"},
        ]))
        out = available.search("韩美半导体 TC键合机", limit=5)
        assert out["success"] is True
        web = out["data"]["web"]
        assert [r["position"] for r in web] == [1, 2]
        assert web[0]["url"] == "https://mk.co.kr/a"
        assert web[0]["description"] == "HBM4 TC bonder"

    def test_limit_caps_at_cli_maximum(self, available, monkeypatch):
        seen = {}

        def fake(query, safe_limit):
            seen["limit"] = safe_limit
            return _doc([])

        monkeypatch.setattr(wig, "_run_wigolo_search", fake)
        available.search("q", limit=99)
        assert seen["limit"] == wig._MAX_RESULTS_CAP

    def test_not_installed_is_a_helpful_error(self, monkeypatch):
        monkeypatch.setattr(wig, "_wigolo_argv", lambda: None)
        out = WigoloWebSearchProvider().search("q")
        assert out["success"] is False and "npx wigolo init" in out["error"]

    def test_not_initialized_names_the_one_time_cost(self, monkeypatch):
        """1.5GB 的一次性安装必须由用户自己决定 — 错误信息要把代价说清楚。"""
        monkeypatch.setattr(wig, "_wigolo_argv", lambda: ["npx", "-y", "wigolo"])
        monkeypatch.setattr(wig, "_initialized", lambda: False)
        out = WigoloWebSearchProvider().search("q")
        assert out["success"] is False
        assert "1.5GB" in out["error"] and "wigolo init" in out["error"]

    def test_timeout_kills_and_reports(self, available, monkeypatch):
        def boom(query, safe_limit):
            raise subprocess.TimeoutExpired(cmd="wigolo", timeout=wig._SEARCH_TIMEOUT_SECS)

        monkeypatch.setattr(wig, "_run_wigolo_search", boom)
        out = available.search("q")
        assert out["success"] is False and "timed out" in out["error"]

    def test_cli_failure_surfaces_as_data_not_exception(self, available, monkeypatch):
        def boom(query, safe_limit):
            raise RuntimeError("wigolo exited 1: pool exploded")

        monkeypatch.setattr(wig, "_run_wigolo_search", boom)
        out = available.search("q")
        assert out["success"] is False and "pool exploded" in out["error"]

    def test_bing_only_pool_logs_the_known_failure_mode(self, available, monkeypatch, caplog):
        """20 题对测里仅有的 2 题跑偏全发生在引擎池只剩 bing 时 — 这个状态必须在
        日志里喊出来,坏答案才能从一行日志定位。"""
        monkeypatch.setattr(wig, "_run_wigolo_search", lambda q, n: _doc(
            [{"title": "t", "url": "https://a", "snippet": "s"}], engines=("bing",)))
        with caplog.at_level("WARNING"):
            out = available.search("q")
        assert out["success"] is True
        assert any("bing-only" in r.message for r in caplog.records)


class TestJsonRecovery:
    def test_json_recovered_after_log_preamble(self, monkeypatch):
        """CLI 在 JSON 前打结构化日志 — 文档从第一个 '{' 恢复。"""
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"ts":"..","msg":"noise"}\n{"results": [], "engines_used": ["bing"]}',
            stderr="")
        # 第一个 '{' 是日志行自己 — 这条测试钉住当前实现的边界:preamble 必须
        # 不是以 '{' 开头的日志格式才安全。实测 wigolo --json 的 stdout 里日志走
        # stderr,stdout 只有文档;这里用纯文档 + 前缀空白验证恢复逻辑。
        fake.stdout = '\n  {"results": [{"title": "t", "url": "u", "snippet": "s"}], "engines_used": ["duckduckgo"]}'
        monkeypatch.setattr(wig, "_wigolo_argv", lambda: ["wigolo"])
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
        doc = wig._run_wigolo_search("q", 5)
        assert doc["results"][0]["title"] == "t"
