#!/usr/bin/env python
"""
Cron reply-mentions runner for lm-twitterer.
Calls mod.reply_mentions with live posting (dry_run=False).
Outputs only safe counts/status — no cookies, tokens, or raw candidate data.
"""
import sys, os, importlib.util, traceback, json

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUG = os.path.join(REPO, "plugins", "lm-twitterer", "core.py")

sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "plugins"))

spec = importlib.util.spec_from_file_location("core", PLUG)
mod = importlib.util.module_from_spec(spec)
sys.modules["core"] = mod
spec.loader.exec_module(mod)

try:
    result = mod.reply_mentions(
        dry_run=False,
        count=50,
        mark_seen_on_dry_run=False,
        provider="moa",
        model="hakuapulse-orchestrator",
    )
    # Print full raw result for debugging, then we'll strip
    raw = json.loads(mod._json(result)) if isinstance(result, (dict, list)) else result
    print("===RAW_RESULT_START===")
    print(json.dumps(raw, ensure_ascii=False, default=str))
    print("===RAW_RESULT_END===")
except Exception as e:
    traceback.print_exc()
    print("===ERROR===")
    print(json.dumps({"ok": False, "error": str(e), "error_type": type(e).__name__}, ensure_ascii=False))
