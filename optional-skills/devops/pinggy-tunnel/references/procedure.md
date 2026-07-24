# Pinggy — Full Procedure: Start a Tunnel, Get the URL, Tear Down

## Procedure — Start a Tunnel and Get the URL

The model SHOULD use the `terminal` tool. The tunnel must stay alive for the duration of the share, so run it as a background process and parse the public URL from stdout.

### 1. Confirm a local origin is up

```bash
curl -sI http://127.0.0.1:8000/ | head -1
# expect HTTP/1.x 200 (or any non-connection-refused response)
```

If nothing is listening yet, start it first (e.g. `python3 -m http.server 8000 --bind 127.0.0.1`). Pinggy will happily return a URL pointed at nothing — the user will see 502 until the origin comes up.

### 2. Launch the tunnel as a background process

Use `terminal(background=True)` and capture output to a logfile (Pinggy prints the URLs on stdout, then keeps the connection open):

```bash
LOG=/tmp/pinggy-8000.log
nohup ssh -p 443 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -R0:localhost:8000 free@a.pinggy.io \
    > "$LOG" 2>&1 &
echo $! > /tmp/pinggy-8000.pid
```

`StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null` skips the first-run host-key prompt. `ServerAliveInterval=30` keeps the SSH session from getting torn down by an idle NAT.

### 3. Parse the URL out of the log

```bash
sleep 4
grep -oE 'https://[a-z0-9-]+\.[a-z]+\.pinggy\.link' /tmp/pinggy-8000.log | head -1
```

Expected output looks like:

```
You are not authenticated.
Your tunnel will expire in 60 minutes.
http://yqycl-98-162-69-48.a.free.pinggy.link
https://yqycl-98-162-69-48.a.free.pinggy.link
```

Hand the `https://...pinggy.link` URL to the user.

### 4. Verify

```bash
curl -sI https://<the-url>/ | head -3
# expect 200/302/whatever the local origin actually returns
```

If you get `502 Bad Gateway`, the SSH session is up but the local origin isn't listening — fix step 1 first.

### 5. Teardown

```bash
kill "$(cat /tmp/pinggy-8000.pid)"
# or, if the pid file got lost:
pkill -f 'ssh -p 443 .* free@a\.pinggy\.io'
```

If you have a session_id from `terminal(background=True)`, prefer `process(action='kill', session_id=...)`.
