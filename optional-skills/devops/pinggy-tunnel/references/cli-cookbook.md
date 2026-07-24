# Pinggy — CLI Cookbook, Access Control & Web Debugger

## Quick Reference

```bash
# Plain HTTP/HTTPS tunnel for port 8000 (free tier)
ssh -p 443 -o StrictHostKeyChecking=no -o ServerAliveInterval=30 \
    -R0:localhost:8000 free@a.pinggy.io

# TCP tunnel (databases, raw SSH, etc.)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:5432 tcp@a.pinggy.io

# TLS tunnel (Pinggy can't decrypt — bring your own certs at origin)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:443 tls@a.pinggy.io

# Basic auth gate (b:user:pass)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:8000 \
    "b:admin:secret+free@a.pinggy.io"

# Bearer token gate (k:token)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:8000 \
    "k:mysecrettoken+free@a.pinggy.io"

# IP whitelist (w:CIDR)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:8000 \
    "w:203.0.113.0/24+free@a.pinggy.io"

# Enable CORS + force HTTPS redirect
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:8000 \
    "co+x:https+free@a.pinggy.io"

# Pro tier (persistent URL, no 60-min cap)
ssh -p 443 -o StrictHostKeyChecking=no -R0:localhost:8000 "$PINGGY_TOKEN+a.pinggy.io"
```

## Access Control via Username Keywords

Pinggy stacks control flags into the SSH username separated by `+`. Always quote the whole `user@host` argument when it contains a `+`:

| Keyword | Effect |
|---------|--------|
| `b:user:pass` | HTTP Basic auth gate |
| `k:token` | Bearer-token header gate (`Authorization: Bearer <token>`) |
| `w:CIDR` | IP whitelist (single IP or CIDR, repeatable) |
| `co` | Add `Access-Control-Allow-Origin: *` (CORS) |
| `x:https` | Force HTTPS — auto-redirect HTTP to HTTPS |
| `a:Name:Value` | Add request header |
| `u:Name:Value` | Update request header |
| `r:Name` | Remove request header |
| `qr` | Print a QR code of the URL to stdout (handy for mobile sharing) |

Combine freely: `"b:admin:secret+co+x:https+free@a.pinggy.io"`.

## Web Debugger (optional)

Pinggy can mirror the inbound traffic to `localhost:4300` for inspection. Add a local forward to the SSH command:

```bash
ssh -p 443 -L4300:localhost:4300 -R0:localhost:8000 free@a.pinggy.io
```

Then open `http://localhost:4300` in a browser to see live request/response pairs.
