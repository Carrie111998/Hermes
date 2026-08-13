#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
batch_sso2cpa.py — Grok 账号（邮箱|密码|SSO）批量并发转 CPA（CLIProxyAPI）xai JSON。

流程：
  1. 归一化输入（支持 | 或 ---- 分隔、纯 SSO、去重）
  2. 网络窗口检测（auth.x.ai 被 GFW 概率性阻断，先测再跑）
  3. 并发 Device Flow（复用 grokRegister-cpa 的 sso_to_token）
  4. 写 xai-<email>.json 到 CPA auth-dir，统计结果

用法:
  PYTHONPATH= python batch_sso2cpa.py --input accounts.txt --cpa-auth-dir "C:/Users/nagi_z/.cli-proxy-api" \
      --workers 3 --retries 3 --max-wait 300

输入格式（每行一个）:
  email|password|sso
  email----password----sso
  email|sso
  email----sso
  纯 sso 一行
  # 开头为注释

依赖: curl_cffi；须在 FlClash TUN 模式下运行（禁 --proxy，TUN 直连）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR  # grokRegister-cpa 仓库目录；可被 --repo / GROK_REPO_DIR 覆盖
sys.path.insert(0, str(SCRIPT_DIR))

import importlib  # noqa: E402
m = None  # sso_to_auth_json 模块，main() 里按 REPO_DIR 加载

# ---------- 输入归一化 ----------

_SSO_RE = re.compile(r"^eyJ[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+\.[a-zA-Z0-9_\-]+$")


def parse_line(line: str) -> tuple[str, str] | None:
    """解析一行 → (email, sso)。支持 | 或 ---- 分隔。"""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if "----" in line:
        parts = [p.strip() for p in line.split("----")]
    elif "|" in line:
        parts = [p.strip() for p in line.split("|")]
    else:
        parts = [line]
    # 最后一段必须是 SSO
    sso = parts[-1]
    if not _SSO_RE.match(sso):
        return None
    email = ""
    for p in parts[:-1]:
        if "@" in p:
            email = p
            break
    return (email, sso)


def load_accounts(path: str) -> list[tuple[str, str]]:
    seen = set()
    out = []
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = parse_line(raw)
        if not parsed:
            continue
        email, sso = parsed
        if sso in seen:
            continue
        seen.add(sso)
        out.append((email, sso))
    return out


# ---------- 网络窗口检测 ----------

def check_window(times: int = 3, timeout: int = 10, proxy: str = "") -> bool:
    """auth.x.ai discovery 连续 N 次 200 才算窗口开。proxy 非空时显式走代理。"""
    from curl_cffi import requests as cr
    ok = 0
    for _ in range(times):
        try:
            r = cr.get(
                "https://auth.x.ai/.well-known/openid-configuration",
                impersonate="chrome",
                timeout=timeout,
                proxy=proxy or None,
            )
            if r.status_code == 200:
                ok += 1
        except Exception:
            pass
        time.sleep(1)
    return ok >= max(2, times - 1)


def wait_for_window(max_wait: int, check_times: int = 3, proxy: str = "") -> bool:
    deadline = time.time() + max_wait
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        print(f"[window] 检测 auth.x.ai 连通性 (第 {attempt} 次)...", flush=True)
        if check_window(check_times, proxy=proxy):
            print("[window] ✅ 网络窗口开启", flush=True)
            return True
        time.sleep(min(30, max(5, max_wait // 10)))
    return False


# ---------- 单个账号转换 ----------

def convert_one(entry: tuple[str, str], cpa_dir: str, proxy: str = "") -> dict:
    email, sso = entry
    result = {"email": email or "(unknown)", "status": "failed", "detail": ""}
    token = m.sso_to_token(sso, proxy=proxy, log=lambda *a, **k: None)
    if not token:
        result["detail"] = "device flow failed"
        return result
    record = m.token_to_cpa_record(token, email=email)
    fname = f"xai-{email}.json" if email else "xai-unknown.json"
    out = Path(cpa_dir) / fname
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    result.update(
        status="ok",
        file=str(out),
        sub=record.get("sub", ""),
        expires_in=record.get("expires_in"),
    )
    return result


# ---------- 依赖预检 ----------

def default_cpa_auth_dir() -> Path:
    """返回当前系统常见的 CPA 默认 auth-dir，不创建目录。"""
    env_dir = os.environ.get("CLI_PROXY_API_AUTH_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path.home() / ".cli-proxy-api"


def desktop_grok_dir() -> Path:
    """返回桌面上的 grok-cpa 目录，兼容 Windows 中文/OneDrive 桌面。"""
    candidates = [
        Path.home() / "Desktop",
        Path.home() / "OneDrive" / "Desktop",
        Path.home() / "OneDrive" / "桌面",
        Path.home() / "桌面",
    ]
    for desktop in candidates:
        if desktop.exists():
            return desktop / "grok-cpa"
    return candidates[0] / "grok-cpa"


def resolve_output_dir(requested: Path, force_cpa_dir: bool = False) -> tuple[Path, bool]:
    """选择输出目录。

    CPA 默认 auth-dir 已存在时使用它；不存在时认为 CPA 尚未安装，切换到桌面
    grok-cpa。返回 (目录, desktop_fallback)。不会把不存在的默认 auth-dir
    误创建成“已安装”标志。
    """
    if requested.exists() and requested.is_dir():
        return requested, False
    if force_cpa_dir:
        requested.mkdir(parents=True, exist_ok=True)
        return requested, False
    fallback = desktop_grok_dir()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback, True


def preflight_check(script_dir: Path, input_path: str, cpa_dir: Path) -> list[str]:
    """返回缺失项列表；空列表 = 全部就绪。"""
    missing = []
    if not sys.version_info >= (3, 9):
        missing.append(f"Python >= 3.9 需要（当前 {sys.version_info.major}.{sys.version_info.minor}）")
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        missing.append("curl_cffi 未安装 → pip install curl_cffi")
    if not (script_dir / "sso_to_auth_json.py").exists():
        missing.append(f"缺少 sso_to_auth_json.py（需先克隆 Git-creat7/grokRegister-cpa 到 {script_dir}）")
    if not Path(input_path).exists():
        missing.append(f"输入文件不存在: {input_path}")
    try:
        cpa_dir.mkdir(parents=True, exist_ok=True)
        test = cpa_dir / ".write_test"
        test.write_text("ok", encoding="utf-8")
        test.unlink()
    except OSError as e:
        missing.append(f"CPA auth-dir 不可写: {cpa_dir} ({e})")
    return missing


# ---------- 主流程 ----------

def main() -> int:
    global m, REPO_DIR
    ap = argparse.ArgumentParser(description="Grok SSO → CPA 批量转换")
    ap.add_argument("--input", required=True, help="账号文件（每行 email|password|sso 或 ---- 分隔）")
    ap.add_argument("--cpa-auth-dir", default="", help="CPA auth-dir；留空自动检测 ~/.cli-proxy-api，不存在则输出到桌面/grok-cpa")
    ap.add_argument("--repo", default=os.environ.get("GROK_REPO_DIR", ""),
                    help="grokRegister-cpa 仓库目录（含 sso_to_auth_json.py）；默认=脚本同目录")
    ap.add_argument("--proxy", default="", help="代理，如 http://127.0.0.1:7890 或 socks5://127.0.0.1:1080；留空=直连(TUN)")
    ap.add_argument("--workers", type=int, default=3, help="并发数（≤4 防风控）")
    ap.add_argument("--retries", type=int, default=3, help="每个账号失败重试次数")
    ap.add_argument("--max-wait", type=int, default=300, help="网络窗口等待上限(秒)")
    ap.add_argument("--skip-preflight", action="store_true", help="跳过依赖预检")
    ap.add_argument("--keep-input", action="store_true", help="保留原始凭据文件（默认跑完删除）")
    args = ap.parse_args()

    # 定位并加载转换模块（grokRegister-cpa 的 sso_to_auth_json.py）
    if args.repo:
        REPO_DIR = Path(args.repo)
    sys.path.insert(0, str(REPO_DIR))
    try:
        m = importlib.import_module("sso_to_auth_json")
    except ImportError:
        print(f"[!] 找不到 sso_to_auth_json.py（仓库目录: {REPO_DIR}）")
        print("[!] 请先克隆: git clone https://github.com/Git-creat7/grokRegister-cpa.git")
        print(f"[!] 然后加 --repo <克隆路径> 重试（或设环境变量 GROK_REPO_DIR）")
        return 2

    # CPA 输出目录：默认检测现有 auth-dir；不存在则切换到桌面/grok-cpa。
    requested_dir = Path(args.cpa_auth_dir).expanduser() if args.cpa_auth_dir else default_cpa_auth_dir()
    force_cpa_dir = bool(args.cpa_auth_dir)
    cpa_dir, desktop_fallback = resolve_output_dir(requested_dir, force_cpa_dir=force_cpa_dir)
    if desktop_fallback:
        print("[!] 未检测到 CPA 默认 auth-dir，判断为 CPA 尚未安装或尚未初始化。")
        print(f"[*] 已创建桌面输出目录：{cpa_dir}")
        print("[*] 转换完成后请安装 CPA，并将这些 JSON 导入其 auth-dir。")

    # 依赖预检（他人环境未装好配置时给出明确提示）
    if not args.skip_preflight:
        missing = preflight_check(REPO_DIR, args.input, cpa_dir)
        if missing:
            print("[!] 环境未就绪，缺少以下配置：")
            for item in missing:
                print(f"    - {item}")
            print("[!] 请先补齐上述配置再运行。若确认已就绪可加 --skip-preflight 跳过。")
            return 2
    cpa_dir.mkdir(parents=True, exist_ok=True)

    accounts = load_accounts(args.input)
    if not accounts:
        print("[!] 未解析到任何有效账号（检查格式：email|password|sso）")
        return 1
    print(f"[*] 解析到 {len(accounts)} 个账号")
    if args.proxy:
        print(f"[*] 显式代理: {args.proxy}")
    else:
        print("[*] 直连模式（需 TUN/系统代理接管，或海外网络直连）")

    if not wait_for_window(args.max_wait, proxy=args.proxy):
        print("[!] 网络窗口未开启（auth.x.ai 被 GFW 阻断）。换节点/检查代理后重试。")
        return 2

    total, ok_n, fail_n = len(accounts), 0, 0
    # 每账号一条最终结果（dict 按账号索引，避免失败账号每轮重试被重复计数）
    results: dict[tuple[str, str], dict] = {}
    for attempt in range(args.retries + 1):
        pending = [a for a in accounts if a not in results or results[a].get("status") != "ok"]
        if not pending:
            break
        print(f"[*] 第 {attempt + 1} 轮，剩余 {len(pending)} 个", flush=True)
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(convert_one, a, str(cpa_dir), args.proxy): a for a in pending}
            for fut in as_completed(futs):
                a = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:  # noqa: BLE001
                    res = {"email": a[0] or "(unknown)", "status": "failed", "detail": str(e)[:120]}
                results[a] = res  # 覆盖式更新，每账号只有一条
        ok_n = sum(1 for r in results.values() if r.get("status") == "ok")
        fail_n = len(results) - ok_n
        if ok_n == total:
            break
        if attempt < args.retries:
            time.sleep(5)

    final_results = list(results.values())
    print("\n===== 结果 =====")
    for r in final_results:
        mark = "✅" if r["status"] == "ok" else "❌"
        print(f"  {mark} {r['email']}: {r['status']}" + (f" → {r.get('file')}" if r["status"] == "ok" else f" ({r.get('detail')})"))
    print(f"成功 {ok_n}/{total}，失败 {fail_n}")

    # 汇总写一份 JSONL 便于复盘
    Path(str(cpa_dir) + "/_batch_results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in final_results), encoding="utf-8"
    )

    # 默认删除原始凭据文件（防泄露）；--keep-input 可保留
    if not args.keep_input:
        try:
            Path(args.input).unlink()
            print(f"[*] 已删除原始凭据文件: {args.input}")
        except OSError as e:
            print(f"[!] 删除原始凭据文件失败: {e}")
    return 0 if fail_n == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
