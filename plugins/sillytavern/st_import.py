"""Parse SillyTavern data (character cards, chats, lorebooks) for Hermes import.

Pure stdlib. No PIL/Pillow (it hangs on some Windows setups) — reads PNG tEXt
chunks manually to extract V2/V3 character card JSON.

Usage (invoked by the sillytavern plugin, or standalone):
    python st_import.py <install_dir> [--json]
"""

import base64
import json
import os
import struct
import sys


def read_png_text_chunks(path: str) -> dict:
    """Extract tEXt chunks from a PNG as {key: bytes}."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return {}
    pos = 8
    chunks = {}
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8].decode("latin1")
        cdata = data[pos + 8 : pos + 8 + length]
        if ctype == "tEXt":
            key, _, val = cdata.partition(b"\x00")
            chunks[key.decode("latin1")] = val
        pos += 12 + length
        if ctype == "IEND":
            break
    return chunks


def parse_character_card(png_path: str) -> dict:
    """Return {name, description, personality, scenario, first_mes, ...}."""
    chunks = read_png_text_chunks(png_path)
    key = "ccv3" if "ccv3" in chunks else ("chara" if "chara" in chunks else None)
    if not key:
        return {}
    try:
        raw = base64.b64decode(chunks[key].decode("latin1")).decode("utf-8")
        obj = json.loads(raw)
        d = obj.get("data", obj)
        return {
            "name": d.get("name", ""),
            "description": d.get("description", ""),
            "personality": d.get("personality", ""),
            "scenario": d.get("scenario", ""),
            "first_mes": d.get("first_mes", ""),
            "mes_example": d.get("mes_example", ""),
            "system_prompt": d.get("system_prompt", ""),
            "creator_notes": d.get("creator_notes", ""),
            "tags": d.get("tags", []),
        }
    except Exception as exc:
        return {"_error": str(exc)}


def parse_chat_jsonl(path: str) -> list:
    """Return a list of {name, mes} message dicts (skips metadata line)."""
    messages = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if "chat_metadata" in obj:
                continue
            if "mes" in obj:
                messages.append(
                    {
                        "name": obj.get("name", "?"),
                        "is_user": obj.get("is_user", False),
                        "mes": obj.get("mes", ""),
                        "send_date": obj.get("send_date", ""),
                    }
                )
    return messages


def parse_lorebook(path: str) -> list:
    """Return a list of {keys, comment, content} lore entries."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    entries = []
    for entry in (data.get("entries") or {}).values():
        content = entry.get("content", "")
        if not content:
            continue
        entries.append(
            {
                "keys": entry.get("key", []),
                "comment": entry.get("comment", ""),
                "content": content,
            }
        )
    return entries


def scan(install_dir: str) -> dict:
    """Scan a SillyTavern install and return a structured summary."""
    base = os.path.join(install_dir, "data", "default-user")
    result = {"characters": [], "chats": [], "lorebooks": []}

    # Characters
    char_dir = os.path.join(base, "characters")
    if os.path.isdir(char_dir):
        for name in os.listdir(char_dir):
            if name.endswith(".png"):
                card = parse_character_card(os.path.join(char_dir, name))
                if card:
                    card["_file"] = name
                    result["characters"].append(card)

    # Chats
    chats_dir = os.path.join(base, "chats")
    if os.path.isdir(chats_dir):
        for char_folder in os.listdir(chats_dir):
            folder_path = os.path.join(chats_dir, char_folder)
            if not os.path.isdir(folder_path):
                continue
            for fn in os.listdir(folder_path):
                if fn.endswith(".jsonl"):
                    msgs = parse_chat_jsonl(os.path.join(folder_path, fn))
                    result["chats"].append(
                        {
                            "character": char_folder,
                            "file": fn,
                            "message_count": len(msgs),
                            "messages": msgs,
                        }
                    )

    # Lorebooks
    worlds_dir = os.path.join(base, "worlds")
    if os.path.isdir(worlds_dir):
        for fn in os.listdir(worlds_dir):
            if fn.endswith(".json"):
                try:
                    entries = parse_lorebook(os.path.join(worlds_dir, fn))
                    result["lorebooks"].append(
                        {"name": fn[:-5], "entry_count": len(entries), "entries": entries}
                    )
                except Exception as exc:
                    result["lorebooks"].append({"name": fn, "_error": str(exc)})

    return result


if __name__ == "__main__":
    install = sys.argv[1] if len(sys.argv) > 1 else "."
    summary = scan(install)
    if "--json" in sys.argv:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Characters: {len(summary['characters'])}")
        for c in summary["characters"]:
            print(f"  - {c.get('name')} ({c.get('_file')})")
        print(f"Chats: {len(summary['chats'])}")
        for c in summary["chats"]:
            print(f"  - {c['character']}/{c['file']}: {c['message_count']} msgs")
        print(f"Lorebooks: {len(summary['lorebooks'])}")
        for lb in summary["lorebooks"]:
            print(f"  - {lb.get('name')}: {lb.get('entry_count', '?')} entries")
