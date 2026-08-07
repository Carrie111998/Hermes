"""``hermes remote`` — the host side of Hermes Remote.

Commands: start (run the TLS device gateway), pair (print an ``hra://``
code + QR), confirm (finish the ceremony with the phone's 6-digit
code), revoke (kill a device token), status (host + devices).
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path
from typing import List, Optional

DEFAULT_TTL_SECONDS = 600


def _state():
    from gateway.platforms.remote import RemoteState

    return RemoteState()


def _detect_external_urls(port: int) -> List[str]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        ip = "127.0.0.1"
    return [f"https://{ip}:{port}"]


def _render_qr_ascii(url: str) -> str:
    import io

    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.StringIO()
    qr.print_ascii(out=buf)
    return buf.getvalue()


def _render_qr_png(url: str, path: str) -> None:
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img.save(path)


def _cmd_start(args) -> None:
    from gateway.config import PlatformConfig
    from gateway.platforms.remote import RemoteDeviceAdapter

    host = str(getattr(args, "host", None) or "0.0.0.0")
    port = int(getattr(args, "port", None) or 8643)
    urls = [u.strip() for u in (getattr(args, "urls", "") or "").split(",") if u.strip()]
    adapter = RemoteDeviceAdapter(PlatformConfig(
        enabled=True,
        extra={"host": host, "port": port, "urls": urls},
    ))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(adapter.start())
    except OSError as exc:
        print(f"hermes remote: cannot bind https://{host}:{port} — {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"hermes remote: {adapter.state.host_name()} gateway on https://{host}:{port}")
    print("Phone: open Hermes Remote, add a host, and scan a fresh "
          "`hermes remote pair` code.")
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(adapter.stop())


def _cmd_pair(args) -> None:
    from gateway.platforms.remote import (
        RemoteDeviceAdapter,
        derive_confirmation_code,
        qr_url_for,
    )

    state = _state()
    port = int(getattr(args, "port", None) or 8643)
    urls = getattr(args, "urls", None) or []
    if isinstance(urls, str):
        urls = [u.strip() for u in urls.split(",") if u.strip()]
    if not urls:
        urls = _detect_external_urls(port)
    pairing = state.create_pairing(ttl_seconds=DEFAULT_TTL_SECONDS)
    fp = state.spki_fingerprint_hex()
    if not fp:
        # The server has not run yet — generate the TLS material now so
        # the QR's fp is the exact pin the app will verify.
        from gateway.config import PlatformConfig

        RemoteDeviceAdapter(PlatformConfig(
            enabled=True,
            extra={"host": "0.0.0.0", "port": port, "urls": urls},
        ))._ensure_tls()
        fp = state.spki_fingerprint_hex()
    url = qr_url_for(
        host_name=state.host_name(),
        urls=urls,
        fp=fp or "",
        secret_hex=pairing["secret_hex"],
        ttl_seconds=pairing["ttl_seconds"],
    )
    print(f"Pairing code (valid 10 minutes, single use):\n  {url}")
    print(f"\nRegistration id: {pairing['registration_id']}")
    print(f"Confirmation code shown on the phone: "
          f"{derive_confirmation_code(pairing['secret_hex'])}")
    qr_out = getattr(args, "qr_out", None)
    if qr_out:
        try:
            _render_qr_png(url, qr_out)
            print(f"QR image written: {qr_out}")
        except Exception as exc:
            print(f"QR image failed ({exc}); the text code above still works.")
    else:
        try:
            print("\n" + _render_qr_ascii(url))
        except Exception:
            pass
    print("\nIn the app: pair a host, then confirm the code on this machine "
          "with: hermes remote confirm <CODE>")


def _cmd_confirm(args) -> None:
    state = _state()
    code = (getattr(args, "code", "") or "").strip()
    result = state.confirm_by_code(code)
    if result is None:
        print("No pending pairing matches that code. Run `hermes remote pair` "
              "for a fresh code, then try again.")
        return
    print(f"Paired device {result['device_id']} ({result['name']}) — "
          f"scopes: {', '.join(result['scopes'])}")
    print("The phone will now receive its token and connect.")


def _cmd_revoke(args) -> None:
    state = _state()
    device_id = (getattr(args, "device_id", "") or "").strip()
    if state.revoke_device(device_id):
        print(f"Revoked device {device_id}")
    else:
        print(f"No such device: {device_id}")


def _cmd_status(args) -> None:
    state = _state()
    devices = state.list_devices()
    active = [d for d in devices.values() if not d.get("revoked")]
    print(f"Host id:    {state.host_id()}")
    print(f"Host name:  {state.host_name()}")
    fp = state.spki_fingerprint_hex()
    print(f"SPKI pin:   {fp or '(server not started yet)'}")
    print(f"Devices:    {len(devices)} paired ({len(active)} active)")
    for device_id, d in devices.items():
        print(f"  - {device_id}  {d.get('name')}  "
              f"scopes={','.join(d.get('scopes') or [])}  "
              f"revoked={bool(d.get('revoked'))}")


def remote_command(args) -> None:
    command = getattr(args, "remote_command", "") or ""
    if command == "start":
        _cmd_start(args)
    elif command == "pair":
        _cmd_pair(args)
    elif command == "confirm":
        _cmd_confirm(args)
    elif command == "revoke":
        _cmd_revoke(args)
    elif command == "status":
        _cmd_status(args)
    else:
        print("hermes remote: unknown command. Try: start | pair | confirm | revoke | status",
              file=sys.stderr)
        raise SystemExit(2)
