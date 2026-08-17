"""Phase 9 / Packet B5 -- synthetic credential canary suite.

Drives every persistence sink with synthetic credentials of materially
different shapes and asserts that none of them reach disk.

SPLIT ASSERTIONS -- read this before adding a test.

Sinks 1-3 (request dumps, session_*.json, gateway .jsonl) are write-mostly
artifacts. They are redacted, and this suite ASSERTS ZERO canary bytes.

Sink 4 (state.db `messages` plus its FTS shadow tables) is the resume source
of truth and is deliberately NOT redacted -- doing so would strip credentials
out of a running agent's own working context. This suite therefore MEASURES
AND RECORDS canary presence there instead of asserting it away. Those counts
are the quantified input to Phase E (targeted delete, FTS drop/rebuild,
secure_delete=ON, VACUUM, verified against the resulting file). A state.db
canary count is an expected positive, not a failure.

All canary values here are synthetic and safe to print. Nothing in this file
touches the live HERMES_HOME.
"""

import sqlite3
import threading
from pathlib import Path

import pytest

from agent.redact import redact_object
from gateway.session import SessionStore
from hermes_state import SessionDB


# --------------------------------------------------------------------------
# Canary corpus -- materially different shapes, not ten variations of one.
# --------------------------------------------------------------------------

CANARIES = {
    "openai_prefixed":  "sk-proj-CANARY0000aaaabbbbccccddddeeeeffff",
    "github_pat":       "ghp_CANARY0000aaaabbbbccccddddeeeeffff",
    "bearer_header":    "Bearer CANARY0000aaaabbbbccccddddeeeeffff",
    "jwt":              "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
                        "eyJzdWIiOiJDQU5BUlkiLCJuYW1lIjoiY2FuYXJ5In0."
                        "CANARYsignature0000aaaabbbbccccdddd",
    "basic_auth_url":   "https://canaryuser:CANARYpassword123@example.com/path",
    "db_connstring":    "postgresql://canary:CANARYdbpw9876@db.example.com:5432/prod",
    "private_key":      "-----BEGIN RSA PRIVATE KEY-----\n"
                        "CANARYkeymaterial0000aaaabbbbcccc\n"
                        "-----END RSA PRIVATE KEY-----",
    "telegram_token":   "1234567890:CANARYaaaabbbbccccddddeeeeffffgggg",
    # The two that matter most: high-entropy, matching NO known vendor format.
    # These are what catch a matcher that only recognises known vendors.
    "opaque_entropy_1": "Zq7Z4mKp2Wf9Lx3Rv8Tn1Yb6Hd5Gs0Jc",
    "opaque_entropy_2": "8fK2nR7vQ4wL9pT3xM6yB1cH5jD0sG8a",
}

# Shapes recognised by the value-based matchers. The opaque pair is excluded:
# only key-based matching catches those, so they are asserted separately.
VALUE_MATCHABLE = [k for k in CANARIES if not k.startswith("opaque_")]
OPAQUE = [k for k in CANARIES if k.startswith("opaque_")]


def _scan(text: str) -> list:
    """Return the names of canaries appearing verbatim in *text*."""
    return sorted(name for name, value in CANARIES.items() if value in text)


def _scan_bytes(path: Path) -> list:
    """Byte-level scan of a file -- catches content the SQL layer hides."""
    raw = path.read_bytes()
    return sorted(
        name for name, value in CANARIES.items() if value.encode("utf-8") in raw
    )


def _scan_db_family(db_path: Path) -> dict:
    """Byte-scan the database AND its WAL/SHM sidecars, separately.

    PHASE E FINDING (measured 2026-08-17, not assumed). SessionDB runs in WAL
    mode. Freshly written rows live in ``<db>-wal`` and do not appear in the
    main ``.db`` file until a checkpoint. In this suite's own run the main file
    was 4 KB with zero canaries while the WAL held all ten.

    Consequence for Phase E: "verify against the resulting database file
    rather than trusting SQL row deletion" is necessary but NOT sufficient.
    Scanning ``state.db`` alone yields a false negative while plaintext sits
    in ``state.db-wal``. Phase E must checkpoint/truncate the WAL (or scan the
    whole family) before it can claim a surface is clean.
    """
    out = {}
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(db_path) + suffix)
        out[candidate.name] = _scan_bytes(candidate) if candidate.exists() else None
    return out


# --------------------------------------------------------------------------
# Sinks 1-3 -- ASSERT ZERO
# --------------------------------------------------------------------------

class TestSink3GatewayTranscript:
    @pytest.fixture
    def store(self, tmp_path):
        s = SessionStore.__new__(SessionStore)
        s.sessions_dir = tmp_path
        s._lock = threading.Lock()
        s._db = None
        return s

    @pytest.mark.parametrize("name", VALUE_MATCHABLE)
    def test_value_matchable_canary_absent_from_jsonl(self, store, name):
        store.append_to_transcript(
            "canary", {"role": "user", "content": f"the value is {CANARIES[name]}"}
        )
        found = _scan(store.get_transcript_path("canary").read_text())
        assert found == [], f"{found} reached the JSONL transcript"

    @pytest.mark.parametrize("name", OPAQUE)
    def test_opaque_canary_absent_when_under_sensitive_key(self, store, name):
        """Opaque credentials are caught by key name, not by shape."""
        store.append_to_transcript(
            "canary", {"role": "tool", "content": {"api_key": CANARIES[name]}}
        )
        found = _scan(store.get_transcript_path("canary").read_text())
        assert found == [], f"{found} reached the JSONL transcript"

    def test_nested_and_in_array(self, store):
        store.append_to_transcript(
            "canary",
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"function": {"name": "f", "arguments": {
                        "deep": {"deeper": {"token": CANARIES["opaque_entropy_1"]}},
                        "list": [{"secret": CANARIES["opaque_entropy_2"]}],
                        "text": CANARIES["openai_prefixed"],
                    }}}
                ],
            },
        )
        found = _scan(store.get_transcript_path("canary").read_text())
        assert found == [], f"{found} reached the JSONL transcript"


class TestWalkerDirect:
    """The shared seam all three sinks route through."""

    @pytest.mark.parametrize("name", VALUE_MATCHABLE)
    def test_value_matchable_redacted(self, name):
        out = repr(redact_object({"content": CANARIES[name]}))
        assert CANARIES[name] not in out

    @pytest.mark.parametrize("name", OPAQUE)
    @pytest.mark.parametrize(
        "key", ["api_key", "token", "secret", "authorization", "password"]
    )
    def test_opaque_redacted_under_each_sensitive_key(self, name, key):
        out = repr(redact_object({key: CANARIES[name]}))
        assert CANARIES[name] not in out

    def test_unusual_field_name_with_matchable_value(self):
        """A credential under a field name nobody allowlisted is still caught
        when its shape is recognisable."""
        out = repr(redact_object({"wibble_wobble_field": CANARIES["openai_prefixed"]}))
        assert CANARIES["openai_prefixed"] not in out

    @pytest.mark.parametrize("name", list(CANARIES))
    def test_every_canary_still_contained_under_the_ambiguous_key(self, name):
        """Phase 9 / C2 added a shape exemption to the bare `key` name so that
        browser_press ("Enter", "Tab") survives. This asserts the exemption did
        not open a hole: every canary in the corpus is long enough or
        odd-shaped enough to still be redacted there.

        The exemption's residual -- a SHORT identifier-shaped credential under
        bare `key` -- is pinned separately in tests/agent/test_redact_object.py.
        """
        out = repr(redact_object({"key": CANARIES[name]}))
        assert CANARIES[name] not in out


# --------------------------------------------------------------------------
# Sink 4 -- MEASURE AND RECORD (expected positive; Phase E input)
# --------------------------------------------------------------------------

class TestSink4StateDbMeasurement:
    """Not a containment test. This quantifies what Phase E must remove.

    It fails only if the measurement itself cannot be taken -- e.g. the FTS
    shadow tables stop existing, which would mean the Phase E plan is aimed at
    the wrong target and needs revisiting.
    """

    @pytest.fixture
    def db(self, tmp_path):
        return SessionDB(db_path=tmp_path / "canary_state.db")

    def test_measure_canary_persistence_in_state_db(self, db, capsys):
        db.create_session("canary-session", source="test")
        for name, value in CANARIES.items():
            db.append_message(
                session_id="canary-session",
                role="user",
                content=f"canary {name}: {value}",
            )

        db_path = Path(db.db_path)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }

        # The Phase E plan targets these specifically. If they vanish, the
        # plan is wrong -- fail loudly rather than silently measuring nothing.
        assert "messages" in tables
        shadow = {t for t in tables if t.startswith("messages_fts")}
        assert shadow, "FTS shadow tables absent -- Phase E plan needs revisiting"

        report = {}
        for table in ["messages"] + sorted(shadow):
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608
            except sqlite3.DatabaseError:
                continue  # fts5 internal tables (_data, _idx) are not readable as rows
            blob = " ".join(str(cell) for row in rows for cell in row)
            report[table] = _scan(blob)

        conn.close()

        # Byte-level scan of the whole DB family -- the check that does not
        # trust SQL, and the form Phase E must verify against.
        file_report = _scan_db_family(db_path)

        with capsys.disabled():
            print("\n  Phase E input -- canary persistence in state.db")
            print("  (expected positive: sink 4 is deliberately unredacted)")
            print("  SQL surfaces:")
            for surface, found in report.items():
                print(f"    {surface:<34} {len(found):>2} / {len(CANARIES)}")
            print("  On-disk files:")
            for fname, found in file_report.items():
                shown = "absent" if found is None else f"{len(found):>2} / {len(CANARIES)}"
                print(f"    {fname:<34} {shown}")

        total_on_disk = {
            name for found in file_report.values() if found for name in found
        }

        # Recorded, not asserted away. What WOULD be a defect is the on-disk
        # scan seeing less than the SQL scan -- that means the file-level
        # verification Phase E depends on is blind to something.
        assert len(total_on_disk) >= len(report["messages"]), (
            "on-disk scan is blind to content SQL can see -- "
            f"files={sorted(total_on_disk)} sql={report['messages']}"
        )

    def test_wal_sidecar_must_be_in_scope_for_phase_e(self, db):
        """Pins the finding directly, so Phase E cannot regress into scanning
        only state.db.

        Asserts the weaker, durable claim -- that the canary is somewhere in
        the db family, not specifically in the WAL -- so a future checkpoint
        or journal-mode change does not make this a flaky test. The point it
        defends is that scanning the main file ALONE is not a valid check.
        """
        db.create_session("wal-session", source="test")
        canary = CANARIES["opaque_entropy_1"]
        db.append_message(session_id="wal-session", role="user", content=canary)

        db_path = Path(db.db_path)
        family = _scan_db_family(db_path)
        found_anywhere = any(f for f in family.values() if f)

        assert found_anywhere, "canary vanished entirely -- measurement is broken"

        main_only = family[db_path.name]
        if not main_only:
            # This is the observed case: WAL mode keeps it out of the main file.
            assert any(
                f for name, f in family.items() if name != db_path.name and f
            ), "content is in neither the main file nor its sidecars"
