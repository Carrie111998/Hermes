#!/usr/bin/env python3
"""
Quản lý thứ tự pool openai-codex trong codexpool.

Dùng:
  sudo python3 manage-codexpool.py                        # xem thứ tự và trạng thái quota thực tế
  sudo python3 manage-codexpool.py --raw                  # chỉ đọc trạng thái last_status trong auth.json
  sudo python3 manage-codexpool.py reorder                # sắp lại theo thứ tự mặc định
  sudo python3 manage-codexpool.py reorder leo,nocobase,zeo,neo,llgap
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, "/root/.hermes/hermes-agent")
try:
    from agent.account_usage import fetch_codex_usage_for_token
except ImportError:
    fetch_codex_usage_for_token = None

AUTH = Path("/root/.hermes/profiles/codexpool/auth.json")
DEFAULT_ORDER = ["leo", "nocobase", "zeo", "neo", "llgap"]


def load():
    data = json.loads(AUTH.read_text())
    return data, data["credential_pool"]["openai-codex"]


def get_credential_status(entry: dict, raw: bool = False) -> str:
    runtime_status = entry.get("last_status") or "ok"
    if raw or not fetch_codex_usage_for_token:
        return runtime_status

    token = str(entry.get("access_token") or "").strip()
    if not token:
        return "missing token"

    try:
        snapshot = fetch_codex_usage_for_token(token, entry.get("base_url"), timeout=4.0)
        if snapshot and getattr(snapshot, "windows", None):
            target = next((w for w in snapshot.windows if w.label in ("Weekly", "Monthly")), None)
            if not target and snapshot.windows:
                target = snapshot.windows[0]
            if target and target.used_percent is not None:
                used = float(target.used_percent)
                rem = max(0, round(100 - used))
                plan_tag = f"({snapshot.plan})" if snapshot.plan else ""
                if rem <= 2:
                    return f"exhausted  {plan_tag:<10}  0% {target.label.lower()} remaining"
                return f"ok         {plan_tag:<10}  {rem}% {target.label.lower()} remaining"
    except Exception:
        pass

    return runtime_status


def show(raw: bool = False):
    _, pool = load()
    print(f"openai-codex ({len(pool)} credentials):")
    for e in pool:
        status = get_credential_status(e, raw=raw)
        print(f"  #{e['priority']+1}  {e['label']:<12}  {status}")


def reorder(order: list[str]):
    data, pool = load()
    pool_by_label = {e["label"]: e for e in pool}

    missing = [l for l in order if l not in pool_by_label]
    if missing:
        print(f"ERROR: labels không tìm thấy trong pool: {missing}")
        print(f"Pool hiện có: {list(pool_by_label.keys())}")
        sys.exit(1)

    backup = str(AUTH) + ".bak-reorder"
    shutil.copy(AUTH, backup)
    print(f"Backup: {backup}")

    new_pool = []
    for i, label in enumerate(order):
        entry = pool_by_label[label]
        entry["priority"] = i
        new_pool.append(entry)

    data["credential_pool"]["openai-codex"] = new_pool
    AUTH.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    print("Done. Thứ tự mới:")
    show()


def main():
    args = sys.argv[1:]

    if not args:
        show()
        return

    if args[0] in ("--raw", "-r", "--offline"):
        show(raw=True)
        return

    if args[0] == "reorder":
        order = args[1].split(",") if len(args) > 1 else DEFAULT_ORDER
        order = [x.strip() for x in order]
        reorder(order)
        return

    print(__doc__)
    sys.exit(1)


if __name__ == "__main__":
    main()
