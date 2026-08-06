"""CLI commands for the personal CRM (keep-in-touch) plugin.

Operator surface for a Dex-style personal relationship manager:

    hermes crm add "Jane Doe" --email jane@x.com --cadence monthly --tag friend
    hermes crm log jane-doe --kind call --summary "caught up on the new job"
    hermes crm touch jane-doe                 # quick "reached out just now"
    hermes crm due                            # who needs a check-in
    hermes crm board                          # kanban keep-in-touch view
    hermes crm digest --silent-if-empty       # cron-ready daily nudge
    hermes crm dates --within 30              # upcoming birthdays

Wire the digest to a scheduled nudge. The digest must be generated fresh at
each fire (shell ``$(...)`` substitution would freeze it at create time), so
tell the agent to run the CLI:

    hermes cron create "0 9 * * *" \\
      "Run \\`hermes crm digest --silent-if-empty\\` in the terminal and relay \\
    its output verbatim. If it prints [SILENT], reply with just [SILENT]." \\
      --name "Keep in touch" --deliver telegram

Or zero-LLM-cost via script mode (stdout becomes the delivery, [SILENT]
suppresses): put ``hermes crm digest --silent-if-empty`` in
``~/.hermes/scripts/crm-digest.sh`` and use ``--script crm-digest.sh --no-agent``.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from plugins.crm.models import (
    Contact,
    ImportantDate,
    Interaction,
    VALID_INTERACTION_KINDS,
    normalize_cadence,
    slugify_contact_id,
)
from plugins.crm.pipeline import (
    BOARD_COLUMNS,
    compute_board,
    compute_status,
    due_contacts,
    render_digest,
    upcoming_dates,
)
from plugins.crm.store import CrmStore, CrmStoreError, resolve_crm_store_path


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="crm_action")

    add_p = subs.add_parser("add", help="Add a new contact")
    add_p.add_argument("name")
    add_p.add_argument("--email", action="append", default=[], dest="emails")
    add_p.add_argument("--phone", action="append", default=[], dest="phones")
    add_p.add_argument("--company", default="")
    add_p.add_argument("--title", default="")
    add_p.add_argument("--location", default="")
    add_p.add_argument("--tag", action="append", default=[], dest="tags")
    add_p.add_argument("--notes", default="")
    add_p.add_argument(
        "--cadence", default="",
        help="Keep-in-touch cadence: weekly/monthly/quarterly/yearly or a number of days",
    )
    add_p.add_argument("--birthday", default="", help="Birthday as MM-DD or YYYY-MM-DD")
    add_p.add_argument("--store-path", default="")

    list_p = subs.add_parser("list", aliases=["ls"], help="List contacts")
    list_p.add_argument("--tag", default="")
    list_p.add_argument("--search", default="")
    list_p.add_argument("--limit", type=int, default=100)
    list_p.add_argument("--json", action="store_true")
    list_p.add_argument("--store-path", default="")

    show_p = subs.add_parser("show", help="Show a contact and its interaction timeline")
    show_p.add_argument("contact_id")
    show_p.add_argument("--json", action="store_true")
    show_p.add_argument("--store-path", default="")

    edit_p = subs.add_parser("edit", help="Update fields on an existing contact")
    edit_p.add_argument("contact_id")
    edit_p.add_argument("--name", default=None)
    edit_p.add_argument("--email", action="append", default=None, dest="emails")
    edit_p.add_argument("--phone", action="append", default=None, dest="phones")
    edit_p.add_argument("--company", default=None)
    edit_p.add_argument("--title", default=None)
    edit_p.add_argument("--location", default=None)
    edit_p.add_argument("--tag", action="append", default=None, dest="tags")
    edit_p.add_argument("--notes", default=None)
    edit_p.add_argument("--store-path", default="")

    rm_p = subs.add_parser("rm", aliases=["delete"], help="Delete a contact")
    rm_p.add_argument("contact_id")
    rm_p.add_argument("--store-path", default="")

    cadence_p = subs.add_parser("cadence", help="Set or clear a keep-in-touch cadence")
    cadence_p.add_argument("contact_id")
    cadence_p.add_argument(
        "value", help="weekly/monthly/quarterly/yearly, a number of days, or 'none'"
    )
    cadence_p.add_argument("--store-path", default="")

    log_p = subs.add_parser("log", help="Log an interaction with a contact")
    log_p.add_argument("contact_id")
    log_p.add_argument("--kind", default="note", choices=sorted(VALID_INTERACTION_KINDS))
    log_p.add_argument("--summary", default="")
    log_p.add_argument("--at", default="", help="When it happened (ISO-8601); defaults to now")
    log_p.add_argument("--store-path", default="")

    touch_p = subs.add_parser("touch", help="Mark a contact as reached out to just now")
    touch_p.add_argument("contact_id")
    touch_p.add_argument("--summary", default="")
    touch_p.add_argument("--store-path", default="")

    date_p = subs.add_parser("date", help="Add an important date (birthday, anniversary, ...)")
    date_p.add_argument("contact_id")
    date_p.add_argument("label")
    date_p.add_argument("value", help="MM-DD or YYYY-MM-DD")
    date_p.add_argument("--note", default="")
    date_p.add_argument("--store-path", default="")

    due_p = subs.add_parser("due", help="List contacts who need a check-in")
    due_p.add_argument("--soon-days", type=int, default=3)
    due_p.add_argument("--tag", default="")
    due_p.add_argument("--no-soon", action="store_true", help="Only overdue, not due-soon")
    due_p.add_argument("--json", action="store_true")
    due_p.add_argument("--store-path", default="")

    board_p = subs.add_parser("board", help="Kanban keep-in-touch board")
    board_p.add_argument("--soon-days", type=int, default=3)
    board_p.add_argument("--tag", default="")
    board_p.add_argument("--json", action="store_true")
    board_p.add_argument("--store-path", default="")

    digest_p = subs.add_parser("digest", help="Render a keep-in-touch digest for delivery")
    digest_p.add_argument("--soon-days", type=int, default=3)
    digest_p.add_argument("--dates-within", type=int, default=14)
    digest_p.add_argument("--tag", default="")
    digest_p.add_argument(
        "--silent-if-empty", action="store_true",
        help="Emit [SILENT] when nothing is due (for cron/no-spam delivery)",
    )
    digest_p.add_argument("--store-path", default="")

    dates_p = subs.add_parser("dates", help="Upcoming birthdays and important dates")
    dates_p.add_argument("--within", type=int, default=30)
    dates_p.add_argument("--tag", default="")
    dates_p.add_argument("--json", action="store_true")
    dates_p.add_argument("--store-path", default="")

    tags_p = subs.add_parser("tags", help="List all tags in use")
    tags_p.add_argument("--store-path", default="")

    export_p = subs.add_parser("export", help="Export the full CRM store as JSON")
    export_p.add_argument("--store-path", default="")

    stats_p = subs.add_parser("stats", aliases=["validate"], help="Show store stats/health")
    stats_p.add_argument("--store-path", default="")

    subparser.set_defaults(func=crm_command)


_ACTIONS = {
    "add": "_cmd_add",
    "list": "_cmd_list",
    "ls": "_cmd_list",
    "show": "_cmd_show",
    "edit": "_cmd_edit",
    "rm": "_cmd_delete",
    "delete": "_cmd_delete",
    "cadence": "_cmd_cadence",
    "log": "_cmd_log",
    "touch": "_cmd_touch",
    "date": "_cmd_date",
    "due": "_cmd_due",
    "board": "_cmd_board",
    "digest": "_cmd_digest",
    "dates": "_cmd_dates",
    "tags": "_cmd_tags",
    "export": "_cmd_export",
    "stats": "_cmd_stats",
    "validate": "_cmd_stats",
}


def crm_command(args: argparse.Namespace) -> int:
    action = getattr(args, "crm_action", None)
    if not action:
        print(
            "Usage: hermes crm "
            "{add|list|show|edit|rm|cadence|log|touch|date|due|board|digest|dates|tags|export|stats}"
        )
        return 2

    handler_name = _ACTIONS.get(action)
    if handler_name is None:
        print(f"Unknown crm action: {action}")
        return 2

    try:
        globals()[handler_name](args)
        return 0
    except (CrmCliError, CrmStoreError) as exc:
        print(str(exc))
        return 1


class CrmCliError(Exception):
    """Raised for user-facing CLI errors (bad id, parse failure, etc.)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _store(args: argparse.Namespace) -> CrmStore:
    return CrmStore(resolve_crm_store_path(getattr(args, "store_path", None)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_contact(store: CrmStore, contact_id: str) -> Contact:
    contact_id = str(contact_id or "").strip()
    record = store.get_contact(contact_id)
    if record is None:
        raise CrmCliError(f"Unknown contact: {contact_id}")
    return Contact.from_dict(record)


def _load_contacts(store: CrmStore) -> list[Contact]:
    return [Contact.from_dict(rec) for rec in store.list_contacts().values()]


def _fmt_ago(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "never"
    days = (now.date() - value.astimezone(timezone.utc).date()).days
    if days < 0:
        return f"in {-days}d"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------

def _cmd_add(args) -> None:
    store = _store(args)
    name = str(args.name or "").strip()
    if not name:
        raise CrmCliError("A contact name is required.")

    try:
        cadence = normalize_cadence(getattr(args, "cadence", "") or None)
    except ValueError as exc:
        raise CrmCliError(str(exc))

    important_dates: list[ImportantDate] = []
    birthday = str(getattr(args, "birthday", "") or "").strip()
    if birthday:
        try:
            important_dates.append(ImportantDate.parse("birthday", birthday))
        except ValueError as exc:
            raise CrmCliError(str(exc))

    contact_id = slugify_contact_id(name, existing=store.contact_ids())
    now = _now()
    contact = Contact(
        contact_id=contact_id,
        name=name,
        emails=list(getattr(args, "emails", []) or []),
        phones=list(getattr(args, "phones", []) or []),
        company=str(getattr(args, "company", "") or "").strip() or None,
        title=str(getattr(args, "title", "") or "").strip() or None,
        location=str(getattr(args, "location", "") or "").strip() or None,
        tags=list(getattr(args, "tags", []) or []),
        notes=str(getattr(args, "notes", "") or "").strip() or None,
        keep_in_touch_days=cadence,
        important_dates=important_dates,
        created_at=now,
    )
    store.upsert_contact(contact_id, contact.to_dict())
    print(f"✅ Added {name} (id: {contact_id})")
    if cadence:
        print(f"   keep-in-touch: every {cadence} day(s)")


def _cmd_list(args) -> None:
    store = _store(args)
    contacts = _load_contacts(store)
    tag = str(getattr(args, "tag", "") or "").strip().lower()
    if tag:
        contacts = [c for c in contacts if tag in {t.lower() for t in c.tags}]
    search = str(getattr(args, "search", "") or "").strip().lower()
    if search:
        contacts = [
            c
            for c in contacts
            if search in c.name.lower()
            or search in (c.company or "").lower()
            or any(search in e.lower() for e in c.emails)
        ]
    contacts.sort(key=lambda c: c.name.lower())
    limit = max(1, int(getattr(args, "limit", 100) or 100))
    contacts = contacts[:limit]

    if getattr(args, "json", False):
        print(json.dumps([c.to_dict() for c in contacts], indent=2, sort_keys=True))
        return

    if not contacts:
        print("No contacts found.")
        return

    now = _now()
    print(f"\n{len(contacts)} contact(s):\n")
    for c in contacts:
        st = compute_status(c, now=now)
        bits = [c.name]
        if c.company:
            bits.append(f"@ {c.company}")
        print(f"  ◆ {c.contact_id}  —  {' '.join(bits)}")
        meta = [f"last contacted {_fmt_ago(c.last_contacted_at, now)}"]
        if c.keep_in_touch_days:
            meta.append(f"{st.status_label.lower()}")
        if c.tags:
            meta.append("#" + " #".join(c.tags))
        print(f"    {'  ·  '.join(meta)}")
    print()


def _cmd_show(args) -> None:
    store = _store(args)
    contact = _require_contact(store, args.contact_id)
    interactions = [
        Interaction.from_dict(rec)
        for rec in store.list_interactions(contact.contact_id).values()
    ]
    interactions.sort(key=lambda i: i.occurred_at, reverse=True)

    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "contact": contact.to_dict(),
                    "status": compute_status(contact).to_dict(),
                    "interactions": [i.to_dict() for i in interactions],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    now = _now()
    st = compute_status(contact, now=now)
    print(f"\n{contact.name}  ({contact.contact_id})")
    if contact.title or contact.company:
        print(f"  {' · '.join(x for x in [contact.title, contact.company] if x)}")
    if contact.location:
        print(f"  📍 {contact.location}")
    for email in contact.emails:
        print(f"  ✉️  {email}")
    for phone in contact.phones:
        print(f"  ☎️  {phone}")
    for platform, handle in sorted(contact.socials.items()):
        print(f"  🔗 {platform}: {handle}")
    if contact.tags:
        print("  🏷️  #" + " #".join(contact.tags))
    print(
        f"  🤝 {st.status_label}"
        + (f" · every {contact.keep_in_touch_days}d" if contact.keep_in_touch_days else "")
        + f" · last contacted {_fmt_ago(contact.last_contacted_at, now)}"
    )
    if st.next_due_at:
        print(f"     next due {st.next_due_at.date().isoformat()}")
    if contact.important_dates:
        print("  🎂 " + ", ".join(
            f"{d.label} {d.month:02d}-{d.day:02d}" for d in contact.important_dates
        ))
    if contact.notes:
        print(f"  📝 {contact.notes}")

    print(f"\n  Timeline ({len(interactions)}):")
    if not interactions:
        print("    (no interactions logged yet)")
    for i in interactions:
        summary = f" — {i.summary}" if i.summary else ""
        print(f"    • {i.occurred_at.date().isoformat()}  [{i.kind}]{summary}")
    print()


def _cmd_edit(args) -> None:
    store = _store(args)
    contact = _require_contact(store, args.contact_id)
    payload: dict[str, Any] = {}

    if getattr(args, "name", None) is not None:
        name = str(args.name).strip()
        if not name:
            raise CrmCliError("Name cannot be empty.")
        payload["name"] = name
    if getattr(args, "emails", None) is not None:
        payload["emails"] = list(args.emails)
    if getattr(args, "phones", None) is not None:
        payload["phones"] = list(args.phones)
    if getattr(args, "tags", None) is not None:
        payload["tags"] = list(args.tags)
    for field_name in ("company", "title", "location", "notes"):
        value = getattr(args, field_name, None)
        if value is not None:
            payload[field_name] = str(value).strip() or None

    if not payload:
        raise CrmCliError("Nothing to update — pass at least one field to change.")

    # Validate the merged record, then persist the NORMALIZED values (deduped
    # lists, trimmed strings) rather than the raw argv, so `edit` writes the
    # same shape `add` does. Normalized-empty fields come back as None and
    # clear the stored key via the store's None-clearing merge.
    merged = {**contact.to_dict(), **payload}
    normalized = Contact.from_dict(merged).to_dict()
    payload = {key: normalized.get(key) for key in payload}
    store.upsert_contact(contact.contact_id, payload)
    print(f"✅ Updated {contact.contact_id}: {', '.join(sorted(payload))}")


def _cmd_delete(args) -> None:
    store = _store(args)
    contact = _require_contact(store, args.contact_id)
    store.delete_contact(contact.contact_id)
    print(f"🗑️  Deleted {contact.name} ({contact.contact_id}) and their interactions.")


def _cmd_cadence(args) -> None:
    store = _store(args)
    contact = _require_contact(store, args.contact_id)
    try:
        cadence = normalize_cadence(args.value)
    except ValueError as exc:
        raise CrmCliError(str(exc))
    store.upsert_contact(contact.contact_id, {"keep_in_touch_days": cadence})
    if cadence:
        print(f"✅ {contact.name}: keep-in-touch every {cadence} day(s).")
    else:
        print(f"✅ {contact.name}: keep-in-touch reminder cleared.")


def _cmd_log(args) -> None:
    store = _store(args)
    contact = _require_contact(store, args.contact_id)
    occurred = _now()
    at = str(getattr(args, "at", "") or "").strip()
    if at:
        try:
            interaction = Interaction(
                interaction_id="probe",
                contact_id=contact.contact_id,
                kind=args.kind,
                occurred_at=at,
            )
            occurred = interaction.occurred_at
        except ValueError as exc:
            raise CrmCliError(f"Could not parse --at: {exc}")
        # A future timestamp (usually a year typo) would silently mark the
        # contact "on track" for years and mute every reminder until then.
        if occurred > _now() + timedelta(minutes=5):
            raise CrmCliError(
                f"--at {occurred.date().isoformat()} is in the future — log "
                "interactions after they happen."
            )

    interaction_id = uuid.uuid4().hex
    record = Interaction(
        interaction_id=interaction_id,
        contact_id=contact.contact_id,
        kind=args.kind,
        occurred_at=occurred,
        summary=str(getattr(args, "summary", "") or "").strip() or None,
        created_at=_now(),
    )
    store.upsert_interaction(interaction_id, record.to_dict())

    # Advance last_contacted_at only when this touchpoint is more recent.
    if contact.last_contacted_at is None or occurred > contact.last_contacted_at:
        store.upsert_contact(contact.contact_id, {"last_contacted_at": _iso(occurred)})

    print(f"✅ Logged {args.kind} with {contact.name} on {occurred.date().isoformat()}.")
    st = compute_status(_require_contact(store, contact.contact_id))
    if st.next_due_at:
        print(f"   next check-in due {st.next_due_at.date().isoformat()}")


def _cmd_touch(args) -> None:
    # Shorthand for `log --kind message` at the current time.
    args.kind = "message"
    args.at = ""
    if not str(getattr(args, "summary", "") or "").strip():
        args.summary = "reached out"
    _cmd_log(args)


def _cmd_date(args) -> None:
    store = _store(args)
    contact = _require_contact(store, args.contact_id)
    try:
        new_date = ImportantDate.parse(
            args.label, args.value, str(getattr(args, "note", "") or "").strip() or None
        )
    except ValueError as exc:
        raise CrmCliError(str(exc))
    dates = list(contact.important_dates)
    # Replace an existing date with the same label rather than duplicating.
    dates = [d for d in dates if d.label.lower() != new_date.label.lower()]
    dates.append(new_date)
    store.upsert_contact(
        contact.contact_id, {"important_dates": [d.to_dict() for d in dates]}
    )
    print(
        f"✅ {contact.name}: {new_date.label} set to "
        f"{new_date.month:02d}-{new_date.day:02d}"
        + (f"-{new_date.year}" if new_date.year else "")
    )


def _cmd_due(args) -> None:
    store = _store(args)
    contacts = _load_contacts(store)
    now = _now()
    items = due_contacts(
        contacts,
        now=now,
        soon_days=int(getattr(args, "soon_days", 3) or 3),
        include_due_soon=not getattr(args, "no_soon", False),
        tag=str(getattr(args, "tag", "") or "").strip() or None,
    )
    if getattr(args, "json", False):
        print(json.dumps([i.to_dict() for i in items], indent=2, sort_keys=True))
        return
    if not items:
        print("✅ No one is due for a check-in right now.")
        return
    print(f"\n{len(items)} contact(s) to reach out to:\n")
    for item in items:
        if item.status == "overdue":
            span = "due today" if not item.days_overdue else f"{item.days_overdue}d overdue"
            marker = "⏰"
        else:
            days = -(item.days_overdue or 0)
            span = "tomorrow" if days == 1 else f"in {days}d"
            marker = "🔜"
        print(f"  {marker} {item.name}  ({item.contact_id}) — {span}")
    print()


def _cmd_board(args) -> None:
    store = _store(args)
    contacts = _load_contacts(store)
    now = _now()
    board = compute_board(
        contacts,
        now=now,
        soon_days=int(getattr(args, "soon_days", 3) or 3),
        tag=str(getattr(args, "tag", "") or "").strip() or None,
    )
    if getattr(args, "json", False):
        print(
            json.dumps(
                {col: [s.to_dict() for s in board[col]] for col in BOARD_COLUMNS},
                indent=2,
                sort_keys=True,
            )
        )
        return

    print()
    for col in BOARD_COLUMNS:
        items = board[col]
        label = items[0].status_label if items else col.replace("_", " ").title()
        print(f"── {label} ({len(items)}) " + "─" * max(0, 24 - len(label)))
        if not items:
            print("   (none)")
        for item in items:
            if item.days_overdue is None:
                detail = ""
            elif item.days_overdue > 0:
                detail = f"  · {item.days_overdue}d overdue"
            elif item.days_overdue == 0:
                detail = "  · due today"
            else:
                detail = f"  · in {-item.days_overdue}d"
            print(f"   • {item.name}{detail}")
        print()


def _cmd_digest(args) -> None:
    store = _store(args)
    contacts = _load_contacts(store)
    text = render_digest(
        contacts,
        now=_now(),
        soon_days=int(getattr(args, "soon_days", 3) or 3),
        dates_within_days=int(getattr(args, "dates_within", 14) or 14),
        tag=str(getattr(args, "tag", "") or "").strip() or None,
        silent_if_empty=bool(getattr(args, "silent_if_empty", False)),
    )
    print(text)


def _cmd_dates(args) -> None:
    store = _store(args)
    contacts = _load_contacts(store)
    items = upcoming_dates(
        contacts,
        now=_now(),
        within_days=int(getattr(args, "within", 30) or 30),
        tag=str(getattr(args, "tag", "") or "").strip() or None,
    )
    if getattr(args, "json", False):
        print(json.dumps([i.to_dict() for i in items], indent=2, sort_keys=True))
        return
    if not items:
        print("No upcoming dates in the window.")
        return
    print(f"\n{len(items)} upcoming date(s):\n")
    for item in items:
        when = "today" if item.days_until == 0 else (
            "tomorrow" if item.days_until == 1 else f"in {item.days_until}d"
        )
        suffix = f" (turning {item.turns})" if item.turns else ""
        print(f"  🎂 {item.date.isoformat()}  {item.name} — {item.label} {when}{suffix}")
    print()


def _cmd_tags(args) -> None:
    store = _store(args)
    tags = store.all_tags()
    if not tags:
        print("No tags in use.")
        return
    counts: dict[str, int] = {}
    for contact in store.list_contacts().values():
        for tag in contact.get("tags") or []:
            counts[str(tag)] = counts.get(str(tag), 0) + 1
    print(f"\n{len(tags)} tag(s):\n")
    for tag in tags:
        print(f"  #{tag}  ({counts.get(tag, 0)})")
    print()


def _cmd_export(args) -> None:
    store = _store(args)
    print(
        json.dumps(
            {
                "contacts": store.list_contacts(),
                "interactions": store.list_interactions(),
            },
            indent=2,
            sort_keys=True,
        )
    )


def _cmd_stats(args) -> None:
    store = _store(args)
    contacts = _load_contacts(store)
    now = _now()
    board = compute_board(contacts, now=now)
    snapshot = {
        "store_path": str(store.path),
        "stats": store.stats(),
        "with_cadence": sum(1 for c in contacts if c.keep_in_touch_days),
        "overdue": len(board["overdue"]),
        "due_soon": len(board["due_soon"]),
        "tags": store.all_tags(),
    }
    print(json.dumps(snapshot, indent=2, sort_keys=True))


__all__ = ["register_cli", "crm_command", "CrmCliError"]
