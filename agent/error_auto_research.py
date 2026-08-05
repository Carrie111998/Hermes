"""错误自动查证管道（2026-08-02 用户定稿范式）。

范式：错误 1-2 次 → 带着"这个时间点的具体问题"去查 GitHub issues / 社区 / 文档
      → 结果注入上下文 → 继续执行原任务。

为什么是管道而不是工具/提示词：
  - 文本规则依赖 LLM 自觉（2026-08-02 SEND 分享 40 分钟弯路：宪法早有"先查后写"，执行时忘了）
  - 工具依赖"想起来调用"（失败循环里恰恰是想不起来）
  - 管道在错误处理代码里自动搜索——不依赖任何自觉（消费者优先：改不了生产者就改消费者）

触发规则：
  - 第 1 次失败即查（轻量一轮 GitHub issues，错误核心作 query）
  - 排除瞬时错误（timeout/connection 类——重试即可，不值得惊动搜索）
  - 同错误核心只查一次（会话缓存）；会话上限 8 次防滥用
  - 失败 2 次仍不定位 → 注入 web_search 建议（换角度，交给 LLM 兜底）
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any

# 瞬时错误特征：重试即可，不惊动搜索
_TRANSIENT_PATTERNS = re.compile(
    r"(timed?\s*out|timeout|connection (refused|reset|closed)|"
    r"ECONNREFUSED|ECONNRESET|network (error|unreachable)|"
    r"getaddrinfo|socket|proxy|429|502|503|504)",
    re.IGNORECASE,
)

# 噪声：行号 / 路径 / 十六进制地址 / 堆栈帧 / native 构造
_NOISE_PATTERNS = [
    re.compile(r"\b\d{1,4}:\d{1,4}\b"),
    re.compile(r"(/home|/mnt|/usr|/data|C:\\|/Users)[^\s'\"]*"),
    re.compile(r"0x[0-9a-fA-F]{4,}"),
    re.compile(r"\bat\s+[\w./-]+:\d+"),
    re.compile(r"at\s+construct\s*\(native\)"),
]

_MAX_SEARCHES_PER_SESSION = 8


def clean_error_text(result: str, max_len: int = 160) -> str:
    """从工具结果提取可搜索的错误核心：去行号/路径/堆栈，截断。

    策略：优先取含错误关键词的行（error/exception/failed/rejected/fatal），
    拼接后清洗噪声——行号路径堆栈是"这个时间点"的噪音，不是问题本质。
    """
    text = (result or "")[:2000]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    picked = [
        ln for ln in lines
        if re.search(r"(error|exception|failed|rejected|fatal|crash|E/|Error)", ln, re.IGNORECASE)
    ]
    core = " ".join(picked[:6]) if picked else text[:300]
    for pat in _NOISE_PATTERNS:
        core = pat.sub(" ", core)
    core = re.sub(r"\s+", " ", core).strip()
    return core[:max_len]


def is_transient_error(result: str) -> bool:
    """瞬时错误（超时/连接/服务端 5xx）——重试即可，不值得惊动搜索。"""
    return bool(_TRANSIENT_PATTERNS.search(result or ""))


def _github_token() -> str:
    tok = os.environ.get("GH_READ_TOKEN") or ""
    if tok:
        return tok
    try:
        env_path = os.path.expanduser("~/.hermes/.env")
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("GH_READ_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def search_github_issues(query: str, limit: int = 3, timeout: float = 6.0) -> list[dict[str, Any]]:
    """GitHub issues 搜索——错误核心最可能出现在 issue 标题/正文（社区踩坑聚合地）。

    用 GH_READ_TOKEN（匿名限流 60/时，带 token 5000/时——hermes.md 事实段）。
    """
    if not query.strip():
        return []
    params = urllib.parse.urlencode({
        "q": query,
        "sort": "relevance",
        "per_page": str(limit),
    })
    url = f"https://api.github.com/search/issues?{params}"
    headers = {"Accept": "application/vnd.github+json"}
    tok = _github_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        out: list[dict[str, Any]] = []
        for item in (data.get("items") or [])[:limit]:
            out.append({
                "title": item.get("title", ""),
                "url": item.get("html_url", ""),
                "repo": (item.get("repository_url") or "").rstrip("/").rsplit("/", 1)[-1] or "",
                "state": item.get("state", ""),
            })
        return out
    except Exception:
        return []


def build_guidance(tool_name: str, error_core: str, results: list[dict[str, Any]], query: str) -> str:
    """构造注入文本：查证结果（命中 → 标题/链接；未命中 → web_search 建议换角度）。"""
    lines = [f"\n[自动查证] 工具 {tool_name} 失败，已自动搜索 GitHub issues（query: {query}）："]
    if not results:
        lines.append("  未命中。建议用 web_search 换角度搜索（技术栈版本/目标场景）：")
        lines.append(f"    {query}")
        lines.append(f"    {query} github issues")
    else:
        for r in results[:3]:
            lines.append(f"  • [{r['repo']}][{r['state']}] {r['title']}")
            lines.append(f"    {r['url']}")
        lines.append("建议：优先查看以上 issue 的结论/workaround；未定位再用 web_search 换角度")
    return "\n".join(lines)


class ErrorAutoResearch:
    """有状态管道：会话级缓存（同错误核心只查一次）+ 上限防滥用。"""

    def __init__(self, max_searches: int = _MAX_SEARCHES_PER_SESSION) -> None:
        self._searched_keys: set[str] = set()
        self._search_count = 0
        self._max = max_searches

    def process(self, tool_name: str, result: str) -> str:
        """工具失败结果 → 附带查证 guidance 的新结果（不命中规则则原样返回）。"""
        try:
            if not result or self._search_count >= self._max:
                return result
            if is_transient_error(result):
                return result
            core = clean_error_text(result)
            if len(core) < 12:
                return result
            key = core[:80]
            if key in self._searched_keys:
                return result
            self._searched_keys.add(key)
            self._search_count += 1
            results = search_github_issues(core)
            return result + build_guidance(tool_name, core, results, core)
        except Exception:
            return result
