"""Tests for the personal CRM (keep-in-touch) plugin.

Covers:
  * model normalization (cadence parsing, slug ids, date parsing, round-trip)
  * durable store CRUD + cascade delete + atomic persistence
  * keep-in-touch pipeline math (status, board, due list, upcoming dates, digest)
  * CLI handlers end-to-end against a temp store
  * plugin register() wires the `crm` CLI command
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

def test_normalize_cadence_presets_and_numbers():
    from plugins.crm.models import normalize_cadence

    assert normalize_cadence("weekly") == 7
    assert normalize_cadence("monthly") == 30
    assert normalize_cadence("quarterly") == 90
    assert normalize_cadence("yearly") == 365
    assert normalize_cadence(45) == 45
    assert normalize_cadence("45") == 45
    assert normalize_cadence("6w") == 42
    assert normalize_cadence("3m") == 90
    assert normalize_cadence("1y") == 365
    assert normalize_cadence("none") is None
    assert normalize_cadence("") is None
    assert normalize_cadence(None) is None


def test_normalize_cadence_rejects_bad_values():
    from plugins.crm.models import normalize_cadence

    with pytest.raises(ValueError):
        normalize_cadence("banana")
    with pytest.raises(ValueError):
        normalize_cadence(-5)
    with pytest.raises(ValueError):
        normalize_cadence(True)


def test_normalize_cadence_caps_and_float_edges():
    from plugins.crm.models import MAX_CADENCE_DAYS, normalize_cadence

    assert normalize_cadence(MAX_CADENCE_DAYS) == MAX_CADENCE_DAYS
    assert normalize_cadence(45.0) == 45  # integral float OK
    # Over-cap values would overflow date arithmetic in every read command.
    with pytest.raises(ValueError, match="capped"):
        normalize_cadence("9999y")
    with pytest.raises(ValueError, match="capped"):
        normalize_cadence(MAX_CADENCE_DAYS + 1)
    # Documented contract is ValueError, never OverflowError or truncation.
    with pytest.raises(ValueError):
        normalize_cadence(float("inf"))
    with pytest.raises(ValueError):
        normalize_cadence(float("nan"))
    with pytest.raises(ValueError, match="whole number"):
        normalize_cadence(2.9)


def test_compute_status_clamps_stored_overflow_cadence():
    from plugins.crm.pipeline import compute_status

    # A hand-edited store can carry a cadence past normalize_cadence's cap;
    # reads must not crash with OverflowError.
    c = _contact("hand-edited", keep_in_touch_days=10**8,
                 last_contacted_at="2026-01-01T00:00:00Z")
    st = compute_status(c, now=_dt(2026, 3, 1))
    assert st.status == "on_track"


def test_slugify_contact_id_dedupes_and_strips_accents():
    from plugins.crm.models import slugify_contact_id

    assert slugify_contact_id("Jane Doe") == "jane-doe"
    assert slugify_contact_id("Renée Zellweger") == "renee-zellweger"
    assert slugify_contact_id("!!!") == "contact"
    existing = {"jane-doe"}
    assert slugify_contact_id("Jane Doe", existing=existing) == "jane-doe-2"
    existing.add("jane-doe-2")
    assert slugify_contact_id("Jane Doe", existing=existing) == "jane-doe-3"


def test_contact_round_trip_and_normalization():
    from plugins.crm.models import Contact

    c = Contact(
        contact_id="jane-doe",
        name="Jane Doe",
        emails="jane@x.com, jane@x.com, other@x.com",  # dedupes + splits
        tags=["friend", "friend", "vc"],
        keep_in_touch_days=30,
        created_at="2026-01-01T00:00:00Z",
    )
    assert c.emails == ["jane@x.com", "other@x.com"]
    assert c.tags == ["friend", "vc"]
    restored = Contact.from_dict(c.to_dict())
    assert restored.to_dict() == c.to_dict()
    assert restored.created_at == _dt(2026, 1, 1)


def test_contact_requires_name_and_id():
    from plugins.crm.models import Contact

    with pytest.raises(ValueError):
        Contact(contact_id="", name="X")
    with pytest.raises(ValueError):
        Contact(contact_id="x", name="  ")


def test_interaction_validates_kind():
    from plugins.crm.models import Interaction

    ok = Interaction(
        interaction_id="i1", contact_id="jane", kind="CALL",
        occurred_at="2026-02-02T10:00:00Z",
    )
    assert ok.kind == "call"
    with pytest.raises(ValueError):
        Interaction(interaction_id="i2", contact_id="jane", kind="smoke-signal",
                    occurred_at="2026-02-02T10:00:00Z")


def test_important_date_parse_formats():
    from plugins.crm.models import ImportantDate

    a = ImportantDate.parse("birthday", "03-14")
    assert (a.month, a.day, a.year) == (3, 14, None)
    b = ImportantDate.parse("birthday", "1990-03-14")
    assert (b.month, b.day, b.year) == (3, 14, 1990)
    c = ImportantDate.parse("anniversary", "07/04/2010")
    assert (c.month, c.day, c.year) == (7, 4, 2010)
    with pytest.raises(ValueError):
        ImportantDate.parse("x", "not-a-date")


def test_important_date_rejects_impossible_calendar_days():
    from plugins.crm.models import ImportantDate

    # These used to persist and then crash every `dates`/`digest` run.
    for bad in ("02-30", "02-31", "04-31", "06-31", "2026-02-31"):
        with pytest.raises(ValueError, match="not a valid calendar date"):
            ImportantDate.parse("birthday", bad)
    with pytest.raises(ValueError, match="not a valid calendar date"):
        ImportantDate(label="x", month=2, day=30)
    # Feb 29 stays valid (clamped to Feb 28 in non-leap years at render time).
    ok = ImportantDate(label="birthday", month=2, day=29)
    assert (ok.month, ok.day) == (2, 29)


def test_important_date_rejects_two_digit_years():
    from plugins.crm.models import ImportantDate

    # '10-11-98' used to store year=98 and render "turning 1928" in digests.
    with pytest.raises(ValueError, match="4-digit year"):
        ImportantDate.parse("birthday", "10-11-98")
    with pytest.raises(ValueError, match="4-digit year"):
        ImportantDate(label="x", month=6, day=15, year=90)
    assert ImportantDate.parse("birthday", "1998-10-11").year == 1998


def test_contact_coerces_important_date_dicts():
    from plugins.crm.models import Contact, ImportantDate

    c = Contact(
        contact_id="b", name="B",
        important_dates=[{"label": "birthday", "month": 1, "day": 2}],
    )
    assert isinstance(c.important_dates[0], ImportantDate)
    assert c.to_dict()["important_dates"][0]["label"] == "birthday"


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def test_store_crud_and_cascade_delete(tmp_path):
    from plugins.crm.store import CrmStore

    store = CrmStore(tmp_path / "crm.json")
    store.upsert_contact("jane", {"contact_id": "jane", "name": "Jane"})
    store.upsert_interaction("i1", {"contact_id": "jane", "kind": "call"})
    store.upsert_interaction("i2", {"contact_id": "bob", "kind": "note"})

    assert store.get_contact("jane")["name"] == "Jane"
    assert store.get_contact("jane")["created_at"]  # stamped
    assert len(store.list_interactions("jane")) == 1

    assert store.delete_contact("jane") is True
    # jane's interaction cascaded away; bob's remains
    assert store.get_interaction("i1") is None
    assert store.get_interaction("i2") is not None
    assert store.delete_contact("jane") is False


def test_store_persists_across_instances(tmp_path):
    from plugins.crm.store import CrmStore

    path = tmp_path / "crm.json"
    CrmStore(path).upsert_contact("jane", {"name": "Jane", "tags": ["vc"]})
    reopened = CrmStore(path)
    assert reopened.get_contact("jane")["name"] == "Jane"
    assert reopened.all_tags() == ["vc"]


def test_store_upsert_clears_none_fields(tmp_path):
    from plugins.crm.store import CrmStore

    store = CrmStore(tmp_path / "crm.json")
    store.upsert_contact("jane", {"name": "Jane", "keep_in_touch_days": 30})
    store.upsert_contact("jane", {"keep_in_touch_days": None})
    assert "keep_in_touch_days" not in store.get_contact("jane")


def test_store_refuses_corrupt_file_and_never_clobbers_it(tmp_path):
    from plugins.crm.store import CrmStore, CrmStoreError

    path = tmp_path / "crm.json"
    corrupt = '{"contacts": {"jane": {"name": "Jane"'  # truncated mid-write
    path.write_text(corrupt, encoding="utf-8")
    with pytest.raises(CrmStoreError, match="not valid JSON"):
        CrmStore(path)
    # The damaged file must be left untouched for manual recovery.
    assert path.read_text(encoding="utf-8") == corrupt


def test_store_refuses_wrong_shape_files(tmp_path):
    from plugins.crm.store import CrmStore, CrmStoreError

    list_shaped = tmp_path / "list.json"
    list_shaped.write_text('{"contacts": [1, 2]}', encoding="utf-8")
    with pytest.raises(CrmStoreError, match="malformed 'contacts'"):
        CrmStore(list_shaped)

    non_dict_record = tmp_path / "record.json"
    non_dict_record.write_text('{"contacts": {"x": "not-a-dict"}}', encoding="utf-8")
    with pytest.raises(CrmStoreError, match="malformed 'contacts'"):
        CrmStore(non_dict_record)

    top_level_list = tmp_path / "top.json"
    top_level_list.write_text("[]", encoding="utf-8")
    with pytest.raises(CrmStoreError, match="JSON object"):
        CrmStore(top_level_list)


def test_cli_reports_corrupt_store_as_error_not_traceback(tmp_path, capsys):
    from plugins.crm import cli

    path = tmp_path / "crm.json"
    path.write_text("{ not valid json", encoding="utf-8")
    ns = argparse.Namespace(
        crm_action="list", store_path=str(path), tag="", search="", limit=100, json=False
    )
    rc = cli.crm_command(ns)
    out = capsys.readouterr().out
    assert rc == 1
    assert "not valid JSON" in out


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

def _contact(cid, **kw):
    from plugins.crm.models import Contact

    kw.setdefault("name", cid.title())
    return Contact(contact_id=cid, **kw)


def test_compute_status_overdue_due_soon_on_track():
    from plugins.crm.pipeline import compute_status

    now = _dt(2026, 3, 1)
    # last contacted 40d ago, monthly cadence -> overdue by ~10d
    overdue = _contact("a", keep_in_touch_days=30, last_contacted_at="2026-01-20T00:00:00Z")
    st = compute_status(overdue, now=now)
    assert st.status == "overdue"
    assert st.days_overdue == 10

    # last contacted 28d ago, monthly -> due in 2d -> due_soon
    soon = _contact("b", keep_in_touch_days=30, last_contacted_at="2026-02-01T00:00:00Z")
    st = compute_status(soon, now=now)
    assert st.status == "due_soon"
    assert st.days_overdue == -2

    # last contacted 5d ago, monthly -> on track
    ok = _contact("c", keep_in_touch_days=30, last_contacted_at="2026-02-24T00:00:00Z")
    assert compute_status(ok, now=now).status == "on_track"

    # no cadence
    none = _contact("d")
    assert compute_status(none, now=now).status == "no_cadence"


def test_never_contacted_anchors_to_created_at():
    from plugins.crm.pipeline import compute_status

    now = _dt(2026, 3, 1)
    c = _contact("new", keep_in_touch_days=7, created_at="2026-02-01T00:00:00Z")
    st = compute_status(c, now=now)
    assert st.status == "overdue"  # created 29d ago, weekly cadence


def test_board_grouping_and_ordering_and_tag_filter():
    from plugins.crm.pipeline import compute_board

    now = _dt(2026, 3, 1)
    contacts = [
        _contact("a", keep_in_touch_days=30, last_contacted_at="2026-01-01T00:00:00Z",
                 tags=["friend"]),        # very overdue
        _contact("b", keep_in_touch_days=30, last_contacted_at="2026-01-25T00:00:00Z",
                 tags=["friend"]),        # less overdue
        _contact("c", keep_in_touch_days=30, last_contacted_at="2026-02-27T00:00:00Z"),  # on track
        _contact("d"),                    # no cadence
    ]
    board = compute_board(contacts, now=now)
    assert [s.contact_id for s in board["overdue"]] == ["a", "b"]  # most overdue first
    assert [s.contact_id for s in board["on_track"]] == ["c"]
    assert [s.contact_id for s in board["no_cadence"]] == ["d"]

    friends = compute_board(contacts, now=now, tag="friend")
    assert {s.contact_id for s in friends["overdue"]} == {"a", "b"}
    assert friends["on_track"] == []


def test_due_contacts_respects_include_due_soon():
    from plugins.crm.pipeline import due_contacts

    now = _dt(2026, 3, 1)
    contacts = [
        _contact("a", keep_in_touch_days=30, last_contacted_at="2026-01-01T00:00:00Z"),
        _contact("b", keep_in_touch_days=30, last_contacted_at="2026-02-01T00:00:00Z"),  # due soon
    ]
    assert {s.contact_id for s in due_contacts(contacts, now=now)} == {"a", "b"}
    only_overdue = due_contacts(contacts, now=now, include_due_soon=False)
    assert {s.contact_id for s in only_overdue} == {"a"}


def test_upcoming_dates_wraps_year_and_computes_turns():
    from plugins.crm.models import ImportantDate
    from plugins.crm.pipeline import upcoming_dates

    now = _dt(2026, 3, 1)
    c = _contact("a")
    c.important_dates = [
        ImportantDate(label="birthday", month=3, day=10, year=1990),
        ImportantDate(label="anniversary", month=1, day=1),  # already passed -> next year
    ]
    within = upcoming_dates([c], now=now, within_days=30)
    assert len(within) == 1
    assert within[0].label == "birthday"
    assert within[0].days_until == 9
    assert within[0].turns == 36  # 2026 - 1990

    wide = upcoming_dates([c], now=now, within_days=400)
    labels = {u.label for u in wide}
    assert labels == {"birthday", "anniversary"}


def test_upcoming_dates_handles_leap_day():
    from plugins.crm.models import ImportantDate
    from plugins.crm.pipeline import upcoming_dates

    now = _dt(2027, 2, 1)  # 2027 is not a leap year
    c = _contact("a")
    c.important_dates = [ImportantDate(label="birthday", month=2, day=29)]
    got = upcoming_dates([c], now=now, within_days=60)
    assert len(got) == 1
    assert got[0].date.month == 2 and got[0].date.day == 28


def test_render_digest_content_and_silent():
    from plugins.crm.models import ImportantDate
    from plugins.crm.pipeline import render_digest

    now = _dt(2026, 3, 1)
    overdue = _contact("a", name="Alice", keep_in_touch_days=30,
                       last_contacted_at="2026-01-01T00:00:00Z")
    birthday = _contact("b", name="Bob")
    birthday.important_dates = [ImportantDate(label="birthday", month=3, day=5)]

    text = render_digest([overdue, birthday], now=now)
    assert "Alice" in text
    assert "Overdue" in text
    assert "Bob" in text and "birthday" in text

    empty = render_digest([], now=now, silent_if_empty=True)
    assert empty == "[SILENT]"
    caught_up = render_digest([], now=now)
    assert "caught up" in caught_up


def test_digest_tag_filters_dates_and_respects_silent():
    from plugins.crm.models import ImportantDate
    from plugins.crm.pipeline import render_digest, upcoming_dates

    now = _dt(2026, 3, 1)
    friend = _contact("a", name="Friend Person", tags=["friend"])
    work = _contact("b", name="Work Person", tags=["work"])
    work.important_dates = [ImportantDate(label="birthday", month=3, day=5)]

    # A friend-tagged digest must not leak the work contact's birthday...
    text = render_digest([friend, work], now=now, tag="friend",
                         silent_if_empty=True)
    # ...and with nothing due for #friend, it must go [SILENT].
    assert text == "[SILENT]"

    # The work digest still sees it.
    assert "Work Person" in render_digest([friend, work], now=now, tag="work")

    # upcoming_dates supports the same filter directly.
    assert upcoming_dates([friend, work], now=now, within_days=30, tag="friend") == []
    assert len(upcoming_dates([friend, work], now=now, within_days=30, tag="work")) == 1


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------

def _cli(argv, capsys):
    """Drive the real argparse tree end-to-end and return (rc, stdout).

    Parsing real argv (instead of hand-building Namespaces) verifies the
    parser dest names and the handlers' getattr defaults together.
    """
    from plugins.crm.cli import crm_command, register_cli

    parser = argparse.ArgumentParser(prog="hermes crm")
    register_cli(parser)
    rc = crm_command(parser.parse_args(argv))
    return rc, capsys.readouterr().out


def test_cli_add_show_log_flow(tmp_path, capsys):
    store_path = str(tmp_path / "crm.json")

    def run(action, **kw):
        from plugins.crm import cli
        ns = argparse.Namespace(crm_action=action, store_path=store_path, **kw)
        rc = cli.crm_command(ns)
        return rc, capsys.readouterr().out

    rc, out = run(
        "add", name="Jane Doe", emails=["jane@x.com"], phones=[], company="Acme",
        title="", location="", tags=["friend"], notes="", cadence="monthly", birthday="03-14",
    )
    assert rc == 0
    assert "Added Jane Doe" in out
    assert "id: jane-doe" in out

    rc, out = run("log", contact_id="jane-doe", kind="call", summary="caught up", at="")
    assert rc == 0
    assert "Logged call" in out

    rc, out = run("show", contact_id="jane-doe", json=False)
    assert rc == 0
    assert "Jane Doe" in out
    assert "[call]" in out
    assert "caught up" in out

    # last_contacted_at advanced -> status should now be on_track
    rc, out = run("due", soon_days=3, tag="", no_soon=False, json=True)
    assert rc == 0
    assert json.loads(out) == []


def test_cli_unknown_contact_returns_error(tmp_path, capsys):
    from plugins.crm import cli

    ns = argparse.Namespace(crm_action="show", store_path=str(tmp_path / "c.json"),
                            contact_id="nobody", json=False)
    rc = cli.crm_command(ns)
    out = capsys.readouterr().out
    assert rc == 1
    assert "Unknown contact" in out


def test_cli_cadence_and_board(tmp_path, capsys):
    store_path = str(tmp_path / "crm.json")

    def run(action, **kw):
        from plugins.crm import cli
        ns = argparse.Namespace(crm_action=action, store_path=store_path, **kw)
        rc = cli.crm_command(ns)
        return rc, capsys.readouterr().out

    run("add", name="Old Friend", emails=[], phones=[], company="", title="",
        location="", tags=[], notes="", cadence="", birthday="")
    # No cadence yet -> lands in no_cadence column
    rc, out = run("board", soon_days=3, tag="", json=True)
    board = json.loads(out)
    assert [s["contact_id"] for s in board["no_cadence"]] == ["old-friend"]

    # Set a weekly cadence; brand-new contact with old anchor becomes overdue-ish.
    rc, out = run("cadence", contact_id="old-friend", value="weekly")
    assert "every 7 day(s)" in out

    rc, out = run("cadence", contact_id="old-friend", value="none")
    assert "cleared" in out


def test_cli_add_rejects_impossible_birthday(tmp_path, capsys):
    rc, out = _cli(
        ["add", "Bad Birthday", "--birthday", "04-31",
         "--store-path", str(tmp_path / "crm.json")],
        capsys,
    )
    assert rc == 1
    assert "not a valid calendar date" in out


def test_cli_bad_cadence_reports_error(tmp_path, capsys):
    from plugins.crm import cli

    ns = argparse.Namespace(
        crm_action="add", store_path=str(tmp_path / "c.json"), name="X",
        emails=[], phones=[], company="", title="", location="", tags=[],
        notes="", cadence="every blue moon", birthday="",
    )
    rc = cli.crm_command(ns)
    out = capsys.readouterr().out
    assert rc == 1
    assert "Unrecognized cadence" in out


def test_cli_edit_updates_and_clears_fields(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    rc, _ = _cli(["add", "Edit Me", "--company", "Acme", "--tag", "vc", *sp], capsys)
    assert rc == 0

    rc, out = _cli(
        ["edit", "edit-me", "--name", "Edited Name", "--company", "", *sp], capsys
    )
    assert rc == 0 and "Updated edit-me" in out

    rc, out = _cli(["show", "edit-me", "--json", *sp], capsys)
    payload = json.loads(out)["contact"]
    assert payload["name"] == "Edited Name"
    assert "company" not in payload  # empty string clears the field

    rc, out = _cli(["edit", "unknown-id", "--name", "X", *sp], capsys)
    assert rc == 1 and "Unknown contact" in out

    rc, out = _cli(["edit", "edit-me", *sp], capsys)
    assert rc == 1 and "Nothing to update" in out


def test_cli_edit_persists_normalized_lists(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    _cli(["add", "Norm Test", *sp], capsys)
    # An empty-string tag/email must not persist as a phantom "" entry.
    rc, out = _cli(["edit", "norm-test", "--tag", "", *sp], capsys)
    assert rc == 0

    rc, out = _cli(["show", "norm-test", "--json", *sp], capsys)
    payload = json.loads(out)["contact"]
    assert "tags" not in payload or payload["tags"] == []

    rc, out = _cli(["tags", *sp], capsys)
    assert rc == 0 and "No tags in use" in out


def test_cli_rm_deletes_contact_and_interactions(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    _cli(["add", "Delete Me", *sp], capsys)
    _cli(["log", "delete-me", "--kind", "note", "--summary", "x", *sp], capsys)

    rc, out = _cli(["rm", "delete-me", *sp], capsys)
    assert rc == 0 and "Deleted" in out

    rc, out = _cli(["show", "delete-me", *sp], capsys)
    assert rc == 1 and "Unknown contact" in out

    rc, out = _cli(["rm", "delete-me", *sp], capsys)
    assert rc == 1


def test_cli_touch_logs_message_and_advances_clock(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    _cli(["add", "Touch Me", "--cadence", "weekly", *sp], capsys)

    rc, out = _cli(["touch", "touch-me", *sp], capsys)
    assert rc == 0 and "Logged message" in out

    rc, out = _cli(["show", "touch-me", "--json", *sp], capsys)
    payload = json.loads(out)
    assert payload["contact"]["last_contacted_at"]
    assert payload["interactions"][0]["kind"] == "message"
    assert payload["interactions"][0]["summary"] == "reached out"


def test_cli_date_adds_and_replaces_by_label(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    _cli(["add", "Date Test", *sp], capsys)

    rc, out = _cli(["date", "date-test", "birthday", "07-04", *sp], capsys)
    assert rc == 0 and "07-04" in out

    # Same label, different case -> replaces rather than duplicating.
    rc, out = _cli(["date", "date-test", "Birthday", "12-25", *sp], capsys)
    assert rc == 0 and "12-25" in out

    rc, out = _cli(["show", "date-test", "--json", *sp], capsys)
    dates = json.loads(out)["contact"]["important_dates"]
    assert len(dates) == 1
    assert dates[0]["month"] == 12 and dates[0]["day"] == 25


def test_cli_date_rejects_impossible_calendar_day(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    _cli(["add", "Bad Date", *sp], capsys)
    rc, out = _cli(["date", "bad-date", "anniversary", "04-31", *sp], capsys)
    assert rc == 1
    assert "not a valid calendar date" in out


def test_cli_log_rejects_future_timestamp(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    _cli(["add", "Future Log", *sp], capsys)
    rc, out = _cli(
        ["log", "future-log", "--kind", "note", "--at", "2099-01-01", *sp], capsys
    )
    assert rc == 1
    assert "future" in out.lower()


def test_cli_list_filters_and_json(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    _cli(["add", "Alice Friend", "--tag", "friend", *sp], capsys)
    _cli(["add", "Bob Work", "--tag", "work", *sp], capsys)

    rc, out = _cli(["list", "--tag", "friend", "--json", *sp], capsys)
    assert rc == 0
    names = [c["name"] for c in json.loads(out)]
    assert names == ["Alice Friend"]

    rc, out = _cli(["list", "--search", "bob", *sp], capsys)
    assert rc == 0 and "Bob Work" in out and "Alice Friend" not in out

    rc, out = _cli(["list", "--limit", "1", "--json", *sp], capsys)
    assert len(json.loads(out)) == 1


def test_cli_export_round_trips_contacts_and_interactions(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    _cli(["add", "Export Me", *sp], capsys)
    _cli(["log", "export-me", "--kind", "call", *sp], capsys)

    rc, out = _cli(["export", *sp], capsys)
    assert rc == 0
    payload = json.loads(out)
    assert set(payload.keys()) == {"contacts", "interactions"}
    assert "export-me" in payload["contacts"]
    assert len(payload["interactions"]) == 1


def test_cli_stats_reports_health_snapshot(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    _cli(["add", "Stats Test", "--cadence", "monthly", "--tag", "vc", *sp], capsys)

    rc, out = _cli(["stats", *sp], capsys)
    assert rc == 0
    payload = json.loads(out)
    assert payload["stats"]["contacts"] == 1
    assert payload["with_cadence"] == 1
    assert payload["tags"] == ["vc"]


def test_cli_digest_end_to_end(tmp_path, capsys):
    sp = ["--store-path", str(tmp_path / "crm.json")]
    rc, out = _cli(["digest", "--silent-if-empty", *sp], capsys)
    assert rc == 0 and out.strip() == "[SILENT]"

    _cli(["add", "Digest Test", "--cadence", "monthly", *sp], capsys)
    _cli(["log", "digest-test", "--kind", "note",
          "--at", "2000-01-01", *sp], capsys)
    rc, out = _cli(["digest", *sp], capsys)
    assert rc == 0 and "Keep in touch" in out


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------

def test_register_wires_crm_cli_command():
    import plugins.crm as plugin

    registered = {}

    class _Ctx:
        def register_cli_command(self, **kw):
            registered.update(kw)

    plugin.register(_Ctx())
    assert registered["name"] == "crm"
    assert callable(registered["setup_fn"])
    assert callable(registered["handler_fn"])


def test_register_cli_argparse_tree_parses():
    import argparse
    from plugins.crm.cli import register_cli

    parser = argparse.ArgumentParser(prog="hermes crm")
    register_cli(parser)

    ns = parser.parse_args(["add", "Jane Doe", "--cadence", "monthly", "--tag", "friend"])
    assert ns.crm_action == "add"
    assert ns.name == "Jane Doe"
    assert ns.cadence == "monthly"
    assert ns.tags == ["friend"]

    ns = parser.parse_args(["digest", "--silent-if-empty"])
    assert ns.crm_action == "digest"
    assert ns.silent_if_empty is True
