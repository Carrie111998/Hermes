"""Tests for the sillytavern plugin data parsers (stdlib only)."""

import base64
import importlib.util
import json
import os
import struct
import tempfile

_PLUGIN = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "plugins",
    "sillytavern",
    "st_import.py",
)


def _load():
    spec = importlib.util.spec_from_file_location("st_import", _PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_card_png(path, card):
    """Write a minimal PNG with a 'chara' tEXt chunk holding base64 card JSON."""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(ctype, data):
        return (
            struct.pack(">I", len(data))
            + ctype
            + data
            + struct.pack(">I", 0)  # dummy CRC — parser doesn't check
        )

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    b64 = base64.b64encode(json.dumps(card).encode("utf-8"))
    text = chunk(b"tEXt", b"chara\x00" + b64)
    iend = chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(sig + ihdr + text + iend)


def test_parse_character_card():
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "c.png")
        _make_card_png(p, {"data": {"name": "Test", "description": "d", "first_mes": "hi"}})
        card = mod.parse_character_card(p)
        assert card["name"] == "Test"
        assert card["description"] == "d"
        assert card["first_mes"] == "hi"


def test_parse_chat_jsonl_skips_metadata():
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "chat.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"chat_metadata": {"x": 1}}) + "\n")
            f.write(json.dumps({"name": "A", "mes": "hello", "is_user": True}) + "\n")
            f.write(json.dumps({"name": "B", "mes": "hi"}) + "\n")
        msgs = mod.parse_chat_jsonl(p)
        assert len(msgs) == 2
        assert msgs[0]["name"] == "A" and msgs[0]["mes"] == "hello"


def test_parse_lorebook_skips_empty():
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "w.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "entries": {
                        "0": {"key": ["a"], "comment": "c", "content": "body"},
                        "1": {"key": ["b"], "content": ""},
                    }
                },
                f,
            )
        entries = mod.parse_lorebook(p)
        assert len(entries) == 1
        assert entries[0]["content"] == "body"


def test_scan_full_tree():
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "data", "default-user")
        os.makedirs(os.path.join(base, "characters"))
        os.makedirs(os.path.join(base, "chats", "X"))
        os.makedirs(os.path.join(base, "worlds"))
        _make_card_png(
            os.path.join(base, "characters", "x.png"), {"data": {"name": "X"}}
        )
        with open(
            os.path.join(base, "chats", "X", "c.jsonl"), "w", encoding="utf-8"
        ) as f:
            f.write(json.dumps({"name": "X", "mes": "yo"}) + "\n")
        with open(os.path.join(base, "worlds", "W.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": {"0": {"key": ["k"], "content": "lore"}}}, f)

        result = mod.scan(tmp)
        assert len(result["characters"]) == 1
        assert len(result["chats"]) == 1
        assert result["chats"][0]["message_count"] == 1
        assert len(result["lorebooks"]) == 1
        assert result["lorebooks"][0]["entry_count"] == 1
