from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _hermes_home import get_hermes_home

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents.readonly",
]


def _normalize_authorized_user_payload(payload: dict) -> dict:
    normalized = dict(payload)
    if not normalized.get("type"):
        normalized["type"] = "authorized_user"
    return normalized


def _ensure_authenticated():
    if not TOKEN_PATH.exists():
        print("Not authenticated. Run the setup script first:", file=sys.stderr)
        print(f"  python {Path(__file__).parent / 'setup.py'}", file=sys.stderr)
        sys.exit(1)


def _stored_token_scopes() -> list[str]:
    try:
        data = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return list(SCOPES)
    scopes = data.get("scopes")
    if isinstance(scopes, list) and scopes:
        return scopes
    return list(SCOPES)


def _gws_binary() -> str | None:
    return shutil.which("gws")


def _ensure_authenticated() -> None:
    if not TOKEN_PATH.exists():
        print(f"Missing Google token file: {TOKEN_PATH}", file=sys.stderr)
        raise SystemExit(1)


def _run_gws(
    parts: list[str],
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    _ensure_authenticated()
    binary = _gws_binary()
    if not binary:
        raise SystemExit("gws CLI is not installed")

    cmd = [binary, *parts]
    if params is not None:
        cmd.extend(["--params", json.dumps(params, separators=(",", ":"))])
    if body is not None:
        cmd.extend(["--json", json.dumps(body)])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True, encoding='utf-8', errors='replace',
        env=_gws_env(),
    )
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "Unknown gws error"
        print(err, file=sys.stderr)
        sys.exit(result.returncode or 1)

    stdout = result.stdout.strip()
    if not stdout:
        return {}
    return json.loads(proc.stdout)

    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        print("ERROR: Unexpected non-JSON output from gws:", file=sys.stderr)
        print(stdout, file=sys.stderr)
        sys.exit(1)


def _headers_dict(msg: dict) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
        if h.get("name")
    }


def _extract_message_body(msg: dict) -> str:
    body = ""
    payload = msg.get("payload", {})
    if payload.get("body", {}).get("data"):
        body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    elif payload.get("parts"):
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                break
        if not body:
            for part in payload["parts"]:
                if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
                    body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                    break
    return body


def _extract_doc_text(doc: dict) -> str:
    text_parts = []
    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph", {})
        for pe in paragraph.get("elements", []):
            text_run = pe.get("textRun", {})
            if text_run.get("content"):
                text_parts.append(text_run["content"])
    return "".join(text_parts)


def _datetime_with_timezone(value: str) -> str:
    if not value:
        return value
    if "T" not in value:
        return value
    if value.endswith("Z"):
        return value
    tail = value[10:]
    if "+" in tail or "-" in tail:
        return value
    return value + "Z"


def get_credentials():
    """Load and refresh credentials from token file."""
    _ensure_authenticated()

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), _stored_token_scopes())
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(
            json.dumps(
                _normalize_authorized_user_payload(json.loads(creds.to_json())),
                indent=2,
            ), encoding="utf-8"
        )
    if not creds.valid:
        print("Token is invalid. Re-run setup.", file=sys.stderr)
        sys.exit(1)
    return creds


def build_service(api, version):
    from googleapiclient.discovery import build

    return build(api, version, credentials=get_credentials())

def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso_later(days: int) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(days=days))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _headers_by_lower(message: dict[str, Any]) -> dict[str, str]:
    headers = message.get("payload", {}).get("headers", [])
    return {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in headers
        if isinstance(item, dict)
    }


def _message_summary(message: dict[str, Any]) -> dict[str, Any]:
    headers = _headers_by_lower(message)
    return {
        "id": message.get("id"),
        "threadId": message.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": message.get("snippet", ""),
        "labels": message.get("labelIds", []),
    }


def _raw_message(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


def calendar_list(args: argparse.Namespace) -> None:
    params = {
        "calendarId": args.calendar,
        "timeMin": args.start or _iso_now(),
        "timeMax": args.end or _iso_later(7),
        "maxResults": args.max,
    }
    result = _run_gws(["calendar", "events", "list"], params=params)
    print(json.dumps(result))


def gmail_get(args: argparse.Namespace) -> None:
    message = _run_gws(
        ["gmail", "users", "messages", "get"],
        params={"userId": "me", "id": args.message_id, "format": "full"},
    )
    print(json.dumps(_message_summary(message)))


def gmail_search(args: argparse.Namespace) -> None:
    listing = _run_gws(
        ["gmail", "users", "messages", "list"],
        params={"userId": "me", "q": args.query, "maxResults": args.max},
    )
    results = []
    for item in listing.get("messages", []):
        message = _run_gws(
            ["gmail", "users", "messages", "get"],
            params={
                "userId": "me",
                "id": item.get("id"),
                "format": "metadata",
                "metadataHeaders": ["From", "To", "Subject", "Date"],
            },
        )
        results.append(_message_summary(message))
    print(json.dumps(results))


def gmail_send(args: argparse.Namespace) -> None:
    msg = EmailMessage()
    msg["To"] = args.to
    msg["Subject"] = args.subject
    if getattr(args, "cc", ""):
        msg["Cc"] = args.cc
    if getattr(args, "from_header", ""):
        msg["From"] = args.from_header
    if getattr(args, "html", False):
        msg.add_alternative(args.body, subtype="html")
    else:
        msg.set_content(args.body)

    body: dict[str, Any] = {"raw": _raw_message(msg)}
    if getattr(args, "thread_id", ""):
        body["threadId"] = args.thread_id
    _run_gws(["gmail", "users", "messages", "send"], params={"userId": "me"}, body=body)


def gmail_reply(args: argparse.Namespace) -> None:
    original = _run_gws(
        ["gmail", "users", "messages", "get"],
        params={
            "userId": "me",
            "id": args.message_id,
            "format": "metadata",
            "metadataHeaders": ["From", "Subject", "Message-ID"],
        },
    )
    headers = _headers_by_lower(original)
    subject = headers.get("subject", "")
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    msg = EmailMessage()
    msg["To"] = headers.get("from", "")
    msg["Subject"] = reply_subject
    if getattr(args, "from_header", ""):
        msg["From"] = args.from_header
    message_id = headers.get("message-id", "")
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id
    msg.set_content(args.body)

    body = {"raw": _raw_message(msg), "threadId": original.get("threadId")}
    _run_gws(["gmail", "users", "messages", "send"], params={"userId": "me"}, body=body)


def get_credentials() -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
        creds.refresh(Request())
        payload = json.loads(creds.to_json())
        payload["type"] = "authorized_user"
        TOKEN_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return creds


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Google Workspace API helper")
    sub = parser.add_subparsers(dest="command")

    cal = sub.add_parser("calendar-list")
    cal.add_argument("--start", default="")
    cal.add_argument("--end", default="")
    cal.add_argument("--max", type=int, default=25)
    cal.add_argument("--calendar", default="primary")
    cal.set_defaults(func=calendar_list)

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
