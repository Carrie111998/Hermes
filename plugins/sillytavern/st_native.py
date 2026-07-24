"""SillyTavern-native features reimplemented for Hermes (stdlib + sqlite3).

Reproduces SillyTavern's core roleplay primitives inside Hermes so a character
persona, chat history, lorebook, and user persona can be assembled into a
system prompt — and bridged into Hermes' ebbinghaus memory layer.

Storage: a single SQLite DB under HERMES_HOME/sillytavern/native.db.

Tables:
  characters (id, name, description, personality, scenario, first_mes,
              system_prompt, created_at)
  personas   (id, name, description, is_default, created_at)
  sessions   (id, character_id, persona_id, title, summary, created_at)
  messages   (id, session_id, role, name, content, created_at)
  lore       (id, book, keys, content, enabled, created_at)

Everything here is pure stdlib; no ST server, no network.
"""

import json
import os
import sqlite3
import time
from pathlib import Path

_HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def _db_path() -> Path:
    d = _HERMES_HOME / "sillytavern"
    d.mkdir(parents=True, exist_ok=True)
    return d / "native.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_db_path()))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    _init(c)
    return c


def _init(c: sqlite3.Connection) -> None:
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS characters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            personality TEXT DEFAULT '',
            scenario TEXT DEFAULT '',
            first_mes TEXT DEFAULT '',
            system_prompt TEXT DEFAULT '',
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS personas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            is_default INTEGER DEFAULT 0,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character_id INTEGER,
            persona_id INTEGER,
            title TEXT DEFAULT '',
            summary TEXT DEFAULT '',
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            name TEXT DEFAULT '',
            content TEXT NOT NULL,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS lore (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book TEXT NOT NULL,
            keys TEXT DEFAULT '',
            content TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_lore_book ON lore(book);
        """
    )
    c.commit()


# ── Characters ──────────────────────────────────────────────────────

def create_character(name, description="", personality="", scenario="",
                     first_mes="", system_prompt="") -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO characters(name,description,personality,scenario,"
        "first_mes,system_prompt,created_at) VALUES(?,?,?,?,?,?,?)",
        (name, description, personality, scenario, first_mes, system_prompt, time.time()),
    )
    c.commit()
    cid = cur.lastrowid
    c.close()
    return cid


def get_character(character_id) -> dict:
    c = _conn()
    row = c.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
    c.close()
    return dict(row) if row else {}


def list_characters() -> list:
    c = _conn()
    rows = c.execute("SELECT id,name,description FROM characters ORDER BY id").fetchall()
    c.close()
    return [dict(r) for r in rows]


# ── Personas ────────────────────────────────────────────────────────

def create_persona(name, description="", is_default=False) -> int:
    c = _conn()
    if is_default:
        c.execute("UPDATE personas SET is_default=0")
    cur = c.execute(
        "INSERT INTO personas(name,description,is_default,created_at) VALUES(?,?,?,?)",
        (name, description, 1 if is_default else 0, time.time()),
    )
    c.commit()
    pid = cur.lastrowid
    c.close()
    return pid


def get_default_persona() -> dict:
    c = _conn()
    row = c.execute("SELECT * FROM personas WHERE is_default=1 ORDER BY id DESC LIMIT 1").fetchone()
    c.close()
    return dict(row) if row else {}


# ── Sessions & messages ─────────────────────────────────────────────

def create_session(character_id, persona_id=None, title="") -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO sessions(character_id,persona_id,title,created_at) VALUES(?,?,?,?)",
        (character_id, persona_id, title, time.time()),
    )
    sid = cur.lastrowid
    # Seed with the character's first_mes if present.
    char = c.execute("SELECT name,first_mes FROM characters WHERE id=?", (character_id,)).fetchone()
    if char and char["first_mes"]:
        c.execute(
            "INSERT INTO messages(session_id,role,name,content,created_at) VALUES(?,?,?,?,?)",
            (sid, "assistant", char["name"], char["first_mes"], time.time()),
        )
    c.commit()
    c.close()
    return sid


def add_message(session_id, role, content, name="") -> int:
    c = _conn()
    cur = c.execute(
        "INSERT INTO messages(session_id,role,name,content,created_at) VALUES(?,?,?,?,?)",
        (session_id, role, name, content, time.time()),
    )
    c.commit()
    mid = cur.lastrowid
    c.close()
    return mid


def get_messages(session_id, limit=None) -> list:
    c = _conn()
    q = "SELECT role,name,content,created_at FROM messages WHERE session_id=? ORDER BY id"
    rows = c.execute(q, (session_id,)).fetchall()
    c.close()
    msgs = [dict(r) for r in rows]
    return msgs[-limit:] if limit else msgs


def set_summary(session_id, summary) -> None:
    c = _conn()
    c.execute("UPDATE sessions SET summary=? WHERE id=?", (summary, session_id))
    c.commit()
    c.close()


def get_session(session_id) -> dict:
    c = _conn()
    row = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    c.close()
    return dict(row) if row else {}


# ── Lorebook / World Info ───────────────────────────────────────────

def add_lore(book, keys, content, enabled=True) -> int:
    """keys: list[str] or comma string."""
    if isinstance(keys, (list, tuple)):
        keys = ",".join(keys)
    c = _conn()
    cur = c.execute(
        "INSERT INTO lore(book,keys,content,enabled,created_at) VALUES(?,?,?,?,?)",
        (book, keys, content, 1 if enabled else 0, time.time()),
    )
    c.commit()
    lid = cur.lastrowid
    c.close()
    return lid


def match_lore(book, text) -> list:
    """Return lore entries whose keys appear (case-insensitive) in text."""
    c = _conn()
    rows = c.execute(
        "SELECT keys,content FROM lore WHERE book=? AND enabled=1", (book,)
    ).fetchall()
    c.close()
    low = text.lower()
    hits = []
    for r in rows:
        keys = [k.strip().lower() for k in (r["keys"] or "").split(",") if k.strip()]
        if any(k in low for k in keys):
            hits.append({"keys": r["keys"], "content": r["content"]})
    return hits


# ── Prompt assembly (ST-style) ──────────────────────────────────────

def build_prompt(session_id, user_message, lore_book=None, history_limit=20) -> dict:
    """Assemble an ST-style prompt: system block + message list.

    Returns {"system": str, "messages": [{role, content}], "lore_hits": int}.
    """
    sess = get_session(session_id)
    char = get_character(sess.get("character_id")) if sess else {}
    persona = get_default_persona()
    if sess and sess.get("persona_id"):
        c = _conn()
        prow = c.execute("SELECT * FROM personas WHERE id=?", (sess["persona_id"],)).fetchone()
        c.close()
        if prow:
            persona = dict(prow)

    history = get_messages(session_id, limit=history_limit)

    # Lorebook: match against recent history + the new user message.
    scan_text = user_message + "\n" + "\n".join(m["content"] for m in history[-6:])
    lore_hits = match_lore(lore_book, scan_text) if lore_book else []

    blocks = []
    if char.get("system_prompt"):
        blocks.append(char["system_prompt"])
    name = char.get("name", "Character")
    if char.get("description"):
        blocks.append(f"{name}'s description: {char['description']}")
    if char.get("personality"):
        blocks.append(f"{name}'s personality: {char['personality']}")
    if char.get("scenario"):
        blocks.append(f"Scenario: {char['scenario']}")
    if persona.get("description"):
        blocks.append(f"{persona.get('name','User')} (the user): {persona['description']}")
    if sess.get("summary"):
        blocks.append(f"[Summary of earlier events: {sess['summary']}]")
    for h in lore_hits:
        blocks.append(f"[Lore: {h['content']}]")

    system = "\n\n".join(b for b in blocks if b)

    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": user_message})

    return {"system": system, "messages": messages, "lore_hits": len(lore_hits)}


# ── Memory bridge (ebbinghaus connector) ────────────────────────────

def session_to_memory_records(session_id) -> list:
    """Emit {content, tags, salience} records from a session for Hermes memory.

    One record for the session summary (if any), plus one condensed record of
    the whole conversation. The agent persists chosen records via the
    ebbinghaus_memory / memory tools — this function does not write memory.
    """
    sess = get_session(session_id)
    if not sess:
        return []
    char = get_character(sess.get("character_id"))
    cname = char.get("name", "?")
    records = []

    if sess.get("summary"):
        records.append(
            {
                "content": f"ST session '{sess.get('title') or session_id}' with "
                f"{cname} — summary: {sess['summary']}",
                "tags": f"sillytavern,session,summary,{cname}",
                "salience": 0.6,
            }
        )

    msgs = get_messages(session_id)
    if msgs:
        convo = "\n".join(
            f"{m['name'] or m['role']}: {m['content']}" for m in msgs if m["content"]
        )
        records.append(
            {
                "content": f"ST session '{sess.get('title') or session_id}' with "
                f"{cname} ({len(msgs)} msgs): {convo[:1500]}",
                "tags": f"sillytavern,session,chat,{cname}",
                "salience": 0.5,
            }
        )
    return records


def import_memory_to_lore(book, records) -> int:
    """Ingest Hermes memory records into a lorebook.

    records: list of {content, tags?}. tags become lore keys so the entry
    re-triggers on the same topics. Returns the count added.
    """
    added = 0
    for rec in records:
        content = (rec.get("content") or "").strip()
        if not content:
            continue
        tags = rec.get("tags", "")
        if isinstance(tags, (list, tuple)):
            tags = ",".join(tags)
        add_lore(book, tags, content, enabled=True)
        added += 1
    return added

