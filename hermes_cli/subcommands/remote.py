"""``hermes remote`` subcommand parser (wired into hermes_cli/main.py)."""

from __future__ import annotations


def build_remote_parser(subparsers, cmd_remote=None):
    parser = subparsers.add_parser(
        "remote",
        help="host gateway for the Hermes Remote Android app",
    )
    sub = parser.add_subparsers(dest="remote_command", required=True)

    start = sub.add_parser("start", help="run the device gateway (TLS)")
    start.add_argument("--host", default="0.0.0.0", help="bind address")
    start.add_argument("--port", type=int, default=8643, help="bind port")
    start.add_argument("--urls", default="", help="comma-separated public URLs")
    start.set_defaults(func=cmd_remote)

    pair = sub.add_parser("pair", help="print a pairing code + QR")
    pair.add_argument("--port", type=int, default=8643, help="gateway port")
    pair.add_argument("--urls", default="", help="comma-separated public URLs")
    pair.add_argument("--qr-out", default="", help="write the QR to a PNG file")
    pair.set_defaults(func=cmd_remote)

    confirm = sub.add_parser("confirm", help="confirm a pairing with the 6-digit code")
    confirm.add_argument("code", help="the code shown on the phone")
    confirm.set_defaults(func=cmd_remote)

    revoke = sub.add_parser("revoke", help="revoke a paired device")
    revoke.add_argument("device_id", help="the device id from `hermes remote status`")
    revoke.set_defaults(func=cmd_remote)

    status = sub.add_parser("status", help="show host + device status")
    status.set_defaults(func=cmd_remote)

    return parser
