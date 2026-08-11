#!/usr/bin/env python3
"""
Weekly Topic Bank Update Script (ネタ帳週次更新)

機能:
- 過去1週間の実運用・活用実績を収集
- topic_bank.json のトピックを見直し・追加・削除・並び替え
- 実用面での「今週の気づき」をネタ帳に反映
- 手動実行でも、cronでも動作可能
- 更新履歴を保持

実行: python lm-twitterer-topic-bank-weekly-update.py [--dry-run]
"""

import json
import sys
import subprocess
from pathlib import Path
from datetime import datetime, date, timedelta
from collections import Counter

HERMES_HOME = Path(r"C:\Users\downl\.hermes")
TOPIC_BANK_FILE = HERMES_HOME / "lm-twitterer" / "topic_bank.json"
HERMES_REPO = Path(r"C:\Users\downl\Documents\New project\hermes-agent")
LOGS_DIR = HERMES_REPO / "_docs"

# 実運用ログの収集元
LOG_SOURCES = [
    ("cron", "Cronジョブ実行ログ"),
    ("impl_logs", "_docs 実装ログ"),
    ("memory", "Ebbinghaus記憶・Hermesメモリ"),
    ("skills", "新規・更新スキル"),
    ("commits", "Gitコミット履歴"),
]


def load_topic_bank():
    with open(TOPIC_BANK_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_topic_bank(bank):
    bank["updated"] = str(date.today())
    with open(TOPIC_BANK_FILE, "w", encoding="utf-8") as f:
        json.dump(bank, f, ensure_ascii=False, indent=2)


def get_recent_impl_logs(days=7):
    """最近の実装ログからキーワード抽出"""
    if not LOGS_DIR.exists():
        return []
    
    keywords = []
    cutoff = datetime.now() - timedelta(days=days)
    
    for log_file in LOGS_DIR.glob("*.md"):
        try:
            stat = log_file.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime)
            if mtime < cutoff:
                continue
            
            content = log_file.read_text(encoding="utf-8", errors="ignore")
            # 実装内容のセクションからキーワード抽出
            if "実装内容" in content or "概要" in content:
                # 簡易キーワード抽出
                for line in content.splitlines():
                    line = line.strip()
                    if any(kw in line for kw in ["実装", "追加", "修正", "自動化", "統合", "新機能", "cron", "VRChat", "NotebookLM", "ComfyUI", "MiniMax", "Ebbinghaus", "LINE", "MoA", "OSINT", "わらしべ", "AI従業員"]):
                        keywords.append(line[:100])
        except Exception:
            pass
    
    return keywords[:20]


def get_recent_commits(days=7):
    """最近のGitコミットからトピック抽出"""
    try:
        result = subprocess.run(
            ["git", "log", "--since", f"{days} days ago", "--oneline", "--no-merges"],
            cwd=str(HERMES_REPO),
            capture_output=True,
            text=True,
            timeout=30
        )
        commits = result.stdout.strip().splitlines()
        return commits[:30]
    except Exception:
        return []


def get_cron_job_status():
    """アクティブなcronジョブ一覧取得"""
    try:
        result = subprocess.run(
            ["hermes", "cron", "list"],
            capture_output=True,
            text=True,
            timeout=30
        )
        # 簡易パース
        lines = result.stdout.splitlines()
        active_jobs = [l for l in lines if "scheduled" in l and "enabled" in l]
        return active_jobs[:20]
    except Exception:
        return []


def get_memory_stats():
    """Ebbinghaus/Hermesメモリ統計"""
    try:
        result = subprocess.run(
            ["hermes", "memory", "stats"],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout.strip()
    except Exception:
        return ""


def generate_weekly_summary():
    """今週の実運用サマリー生成"""
    print("=== 週次ネタ帳更新: 実運用データ収集 ===")
    
    summary = {
        "week_start": str(date.today() - timedelta(days=7)),
        "week_end": str(date.today()),
        "impl_log_keywords": get_recent_impl_logs(7),
        "git_commits": get_recent_commits(7),
        "active_crons": get_cron_job_status(),
        "memory_stats": get_memory_stats(),
    }
    
    return summary


def suggest_topic_updates(bank, summary):
    """収集データからトピック更新提案"""
    suggestions = {
        "add": [],
        "update": [],
        "remove": [],
        "reorder": [],
    }
    
    existing_titles = {t["title"]: t for t in bank["topics"]}
    existing_ids = {t["id"] for t in bank["topics"]}
    next_id = max(existing_ids) + 1 if existing_ids else 1
    
    # キーワードから新規トピック候補生成
    keyword_topics = {
        "VRChat": "VRChat関連の新機能・運用改善",
        "NotebookLM": "NotebookLMパイプラインの進化",
        "ComfyUI": "ComfyUI/動画生成の新知見",
        "MiniMax": "MiniMax-H3関連のブレイクスルー",
        "Ebbinghaus": "記憶システムの改善・実績",
        "LINE": "LINEブリッジの安定化・新機能",
        "MoA": "Mixture-of-Agentsの新構成・モデル追加",
        "OSINT": "OSINT自動化の精度向上・新ソース",
        "わらしべ": "せどり自動化の利益実績・新カテゴリ",
        "AI従業員": "AI従業員組織の新プロファイル・成果",
        "cron": "Cron信頼性向上・新ジョブ追加",
        "自律": "自律エージェントの新ユースケース",
        "スキル": "新スキル追加・既存スキル大幅改善",
        "メモリ": "メモリ統合・検索精度向上",
        "音声": "TTS/音声合成の品質向上・新ボイス",
    }
    
    # 実装ログキーワードからマッチング
    all_keywords = " ".join(summary["impl_log_keywords"]).lower()
    for kw, angle in keyword_topics.items():
        if kw.lower() in all_keywords:
            # 既存トピックと重複しないかチェック
            if not any(kw.lower() in t["title"].lower() for t in bank["topics"]):
                suggestions["add"].append({
                    "id": next_id,
                    "title": f"{kw}関連の新展開",
                    "angle": angle,
                    "category": "auto-detected",
                    "source": "週次自動検出",
                    "tags": [kw, "自動検出"]
                })
                next_id += 1
    
    # Gitコミットからも検出
    commit_text = " ".join(summary["git_commits"]).lower()
    for kw, angle in keyword_topics.items():
        if kw.lower() in commit_text:
            if not any(kw.lower() in t["title"].lower() for t in bank["topics"]):
                if not any(s["title"] == f"{kw}関連の新展開" for s in suggestions["add"]):
                    suggestions["add"].append({
                        "id": next_id,
                        "title": f"{kw}関連の新展開",
                        "angle": angle,
                        "category": "auto-detected",
                        "source": "Gitコミット検出",
                        "tags": [kw, "自動検出"]
                    })
                    next_id += 1
    
    return suggestions


def print_summary(summary):
    """収集サマリー表示"""
    print(f"\n--- 実装ログキーワード ({len(summary['impl_log_keywords'])}件) ---")
    for kw in summary["impl_log_keywords"][:10]:
        print(f"  - {kw}")
    
    print(f"\n--- Gitコミット ({len(summary['git_commits'])}件) ---")
    for c in summary["git_commits"][:10]:
        print(f"  - {c}")
    
    print(f"\n--- アクティブCron ({len(summary['active_crons'])}件) ---")
    for c in summary["active_crons"][:10]:
        print(f"  - {c[:80]}")


def interactive_review(bank, suggestions):
    """対話的レビュー（手動実行時）"""
    print("\n=== 更新提案 ===")
    
    if suggestions["add"]:
        print(f"\n【追加提案】({len(suggestions['add'])}件)")
        for s in suggestions["add"]:
            print(f"  + ID {s['id']}: {s['title']} - {s['angle'][:60]}... (source: {s['source']})")
    
    # 既存トピックの使用状況
    rotation = bank.get("rotation_state", {})
    used = set(rotation.get("used_ids", []))
    print(f"\n【現在のローテーション状態】")
    print(f"  使用済みID: {sorted(used)}")
    print(f"  サイクル数: {rotation.get('cycle_count', 0)}")
    print(f"  全トピック数: {len(bank['topics'])}")
    
    # 未使用トピック
    unused = [t for t in bank["topics"] if t["id"] not in used]
    if unused:
        print(f"\n【未使用トピック】({len(unused)}件)")
        for t in unused:
            print(f"  - ID {t['id']}: {t['title']}")
    
    return suggestions


def apply_updates(bank, suggestions, dry_run=False):
    """更新適用"""
    changes = []
    
    # 追加
    for s in suggestions["add"]:
        new_topic = {
            "id": s["id"],
            "title": s["title"],
            "angle": s["angle"],
            "category": s.get("category", "auto-detected"),
            "source": s.get("source", "週次自動更新"),
            "tags": s.get("tags", ["自動追加"])
        }
        if not dry_run:
            bank["topics"].append(new_topic)
        changes.append(f"ADD: {new_topic['title']} (ID:{new_topic['id']})")
    
    # IDの再割り当て（重複回避）
    if not dry_run:
        bank["topics"].sort(key=lambda t: t["id"])
        for i, t in enumerate(bank["topics"]):
            t["id"] = i + 1
    
    return changes


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Weekly topic bank update")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without applying")
    parser.add_argument("--auto", action="store_true", help="Auto-apply without confirmation")
    args = parser.parse_args()
    
    print(f"=== ネタ帳週次更新 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    
    # Load current bank
    bank = load_topic_bank()
    print(f"Current topics: {len(bank['topics'])}")
    
    # Collect weekly data
    summary = generate_weekly_summary()
    print_summary(summary)
    
    # Generate suggestions
    suggestions = suggest_topic_updates(bank, summary)
    
    # Interactive review
    suggestions = interactive_review(bank, suggestions)
    
    if args.dry_run:
        print("\n=== DRY RUN - No changes applied ===")
        changes = apply_updates(bank, suggestions, dry_run=True)
        for c in changes:
            print(f"  Would: {c}")
        return 0
    
    if not args.auto:
        confirm = input("\nApply these updates? (y/N): ").strip().lower()
        if confirm != 'y':
            print("Cancelled.")
            return 0
    
    # Apply updates
    changes = apply_updates(bank, suggestions, dry_run=False)
    
    if changes:
        save_topic_bank(bank)
        print(f"\n=== Applied {len(changes)} changes ===")
        for c in changes:
            print(f"  {c}")
        print(f"\nTotal topics: {len(bank['topics'])}")
    else:
        print("\nNo changes needed.")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())