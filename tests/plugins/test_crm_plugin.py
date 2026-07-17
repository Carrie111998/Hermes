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


def test_store_survives_corrupt_file(tmp_path):
    from plugins.crm.store import CrmStore

    path = tmp_path / "crm.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = CrmStore(path)  # should not raise
    assert store.stats()["contacts"] == 0


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


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------

def _run(action, **kw):
    """Invoke a CLI handler with an argparse.Namespace built from kwargs."""
    from plugins.crm import cli

    ns = argparse.Namespace(crm_action=action, store_path="", **kw)
    return cli.crm_command(ns)


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
