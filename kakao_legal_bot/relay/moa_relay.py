#!/usr/bin/env python3
"""Outbox relay — run this on the PC that hosts the emulator.

Why it exists: the Railway server can always be *reached* by Iris (outbound
webhook), but the reverse is not true — the emulator sits behind a home
router with no public address. Rather than asking you to punch a hole in
your network, the server queues replies and this script pulls them.

    python moa_relay.py --server https://moa.up.railway.app \\
                        --token "$OUTBOX_TOKEN" --iris http://127.0.0.1:3000

Only two dependencies-free stdlib calls per second, so it happily runs in a
`pythonw` window or as a Windows service next to the emulator.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _post(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip().startswith(("{", "[")) else {}


def _get(url: str, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8", errors="replace"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="모아 아웃박스 릴레이")
    parser.add_argument("--server", required=True, help="예: https://moa.up.railway.app")
    parser.add_argument("--token", required=True, help="서버의 OUTBOX_TOKEN 과 동일한 값")
    parser.add_argument("--iris", default="http://127.0.0.1:3000", help="Iris 주소")
    parser.add_argument("--interval", type=float, default=1.5, help="폴링 간격(초)")
    parser.add_argument("--batch", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    server = args.server.rstrip("/")
    iris = args.iris.rstrip("/")
    headers = {"X-Outbox-Token": args.token}
    backoff = args.interval

    print(f"[moa-relay] {server} → {iris} (every {args.interval}s)", flush=True)
    while True:
        try:
            data = _get(f"{server}/outbox?limit={args.batch}", headers, args.timeout)
            messages = data.get("messages") or []
            backoff = args.interval
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # Server restart / deploy / laptop asleep — back off, don't spin.
            backoff = min(backoff * 2, 60.0)
            print(f"[moa-relay] pull failed: {exc} (retry in {backoff:.0f}s)", file=sys.stderr, flush=True)
            time.sleep(backoff)
            continue

        if not messages:
            time.sleep(args.interval)
            continue

        delivered: list[int] = []
        failed: list[int] = []
        error = ""
        for message in messages:
            payload = {"type": "text", "room": message["room"], "data": message["text"]}
            try:
                _post(f"{iris}/reply", payload, {}, args.timeout)
                delivered.append(int(message["id"]))
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                error = str(exc)
                failed.append(int(message["id"]))
                print(f"[moa-relay] iris send failed: {exc}", file=sys.stderr, flush=True)

        try:
            if delivered:
                _post(f"{server}/outbox/ack", {"ids": delivered, "ok": True}, headers, args.timeout)
                print(f"[moa-relay] delivered {len(delivered)}", flush=True)
            if failed:
                # ok=False puts them back in the queue for the next round.
                _post(
                    f"{server}/outbox/ack",
                    {"ids": failed, "ok": False, "error": error[:200]},
                    headers,
                    args.timeout,
                )
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            print(f"[moa-relay] ack failed: {exc}", file=sys.stderr, flush=True)

        time.sleep(0.2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[moa-relay] stopped", flush=True)
