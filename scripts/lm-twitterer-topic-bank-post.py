#!/usr/bin/env python3
"""
Hermes Agent 自動ツイート投稿スクリプト（ネタ帳ベース・ローテーション管理付き）

機能:
- topic_bank.json から未使用トピックを選択
- rotation_state で使用済みIDを管理、全消化でリセット
- 必ず "#hermesagent #はくあ" を含める
- lm-twitterer core.py 経由で投稿（SourceFileLoader bootstrap）
- 実行結果を topic_bank.json の rotation_state に反映
"""

import json
import sys
import subprocess
from pathlib import Path
from datetime import datetime, date
import importlib.util
import importlib.machinery

# パス設定
HERMES_HOME = Path(r"C:\Users\downl\.hermes")
TOPIC_BANK_FILE = HERMES_HOME / "lm-twitterer" / "topic_bank.json"
CORE_PY = HERMES_HOME / "plugins" / "lm-twitterer" / "core.py"
VENV_PYTHON = r"C:\Users\downl\Documents\New project\hermes-agent\.venv\Scripts\python.exe"

REQUIRED_HASHTAGS = "#hermesagent #はくあ"
MAX_TWEET_LENGTH = 240  # X's weighted limit (conservative)


def load_topic_bank():
    """Load topic bank from JSON file"""
    with open(TOPIC_BANK_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_topic_bank(bank):
    """Save topic bank to JSON file"""
    bank["updated"] = str(date.today())
    with open(TOPIC_BANK_FILE, "w", encoding="utf-8") as f:
        json.dump(bank, f, ensure_ascii=False, indent=2)


def pick_next_topic(bank):
    """Pick next unused topic from bank"""
    topics = bank["topics"]
    rotation = bank.get("rotation_state", {"used_ids": [], "last_updated": "", "cycle_count": 0})
    used_ids = set(rotation.get("used_ids", []))
    
    # Find available topics
    available = [t for t in topics if t["id"] not in used_ids]
    
    # If all used, reset cycle
    if not available:
        print("All topics used, resetting rotation cycle")
        used_ids = set()
        available = topics
        rotation["cycle_count"] = rotation.get("cycle_count", 0) + 1
    
    # Pick first available (sequential for predictability)
    chosen = min(available, key=lambda t: t["id"])
    used_ids.add(chosen["id"])
    
    # Update rotation state
    rotation["used_ids"] = sorted(list(used_ids))
    rotation["last_updated"] = datetime.now().isoformat()
    bank["rotation_state"] = rotation
    
    return chosen, bank


def build_tweet(topic):
    """Build tweet text from topic, ensuring required hashtags"""
    angle = topic["angle"]
    title = topic["title"]
    
    # Construct tweet: title + angle + hashtags
    tweet = f"【{title}】\n{angle}\n\n{REQUIRED_HASHTAGS}"
    
    # Ensure length limit
    if len(tweet) > MAX_TWEET_LENGTH:
        # Truncate angle if needed, keep title and hashtags
        max_angle_len = MAX_TWEET_LENGTH - len(f"【{title}】\n\n{REQUIRED_HASHTAGS}") - 2
        if max_angle_len > 20:
            angle = angle[:max_angle_len] + "…"
            tweet = f"【{title}】\n{angle}\n\n{REQUIRED_HASHTAGS}"
        else:
            # Fallback: just title + hashtags
            tweet = f"【{title}】\n{REQUIRED_HASHTAGS}"
    
    return tweet


def validate_tweet(text):
    """Validate tweet meets requirements"""
    errors = []
    
    if REQUIRED_HASHTAGS not in text:
        errors.append(f"Missing required hashtags: {REQUIRED_HASHTAGS}")
    
    if len(text) > MAX_TWEET_LENGTH:
        errors.append(f"Tweet too long: {len(text)} > {MAX_TWEET_LENGTH}")
    
    if not text.strip():
        errors.append("Empty tweet text")
    
    return errors


def post_tweet(text):
    """Post tweet via lm-twitterer core.py using SourceFileLoader bootstrap"""
    script = f'''
import sys
import importlib.machinery
import importlib.util

# Bootstrap core.py via SourceFileLoader (spec_from_file_location fails on Windows)
loader = importlib.machinery.SourceFileLoader("lm_twitterer_core", r"{CORE_PY}")
spec = importlib.util.spec_from_loader(loader.name, loader)
core = importlib.util.module_from_spec(spec)
sys.modules["lm_twitterer_core"] = core
loader.exec_module(core)

# Auth check then post
auth = core.auth_check()
print(f"Auth: {{auth}}", flush=True)

if auth.get("ok"):
    result = core.post("", text={json.dumps(text, ensure_ascii=False)}, dry_run=False)
    print(f"Result: {{result}}", flush=True)
    if result.get("posted") and result.get("url"):
        print(f"SUCCESS: {{result['url']}}", flush=True)
        sys.exit(0)
    else:
        print(f"FAIL: {{result}}", flush=True)
        sys.exit(1)
else:
    print(f"AUTH FAIL: {{auth}}", flush=True)
    sys.exit(1)
'''
    
    result = subprocess.run(
        [VENV_PYTHON, "-c", script],
        capture_output=True,
        text=True,
        timeout=120
    )
    
    print("STDOUT:", result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr)
    
    # Parse URL from output
    for line in result.stdout.splitlines():
        if line.startswith("SUCCESS:"):
            return line.replace("SUCCESS:", "").strip()
    
    return None


def main():
    print(f"=== Hermes Auto Tweet - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
    
    # Load topic bank
    bank = load_topic_bank()
    
    # Pick next topic
    topic, bank = pick_next_topic(bank)
    print(f"Selected topic ID {topic['id']}: {topic['title']}")
    
    # Build tweet
    tweet = build_tweet(topic)
    print(f"Tweet text:\\n{tweet}\\n")
    
    # Validate
    errors = validate_tweet(tweet)
    if errors:
        print("Validation errors:", errors, file=sys.stderr)
        return 1
    
    # Post tweet
    url = post_tweet(tweet)
    
    if url:
        print(f"Posted successfully: {url}")
        # Save updated bank with rotation state
        save_topic_bank(bank)
        print(f"Updated topic_bank.json. Cycle: {bank['rotation_state']['cycle_count']}, Used: {bank['rotation_state']['used_ids']}")
        return 0
    else:
        print("Failed to post tweet", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())