# debugpy Remote Debugging & IDE Setup

Attach to long-lived processes: Hermes gateway, tui_gateway, a daemon, a process that's already
misbehaving and can't be restarted clean.

## Setup

```bash
cd "$(git rev-parse --show-toplevel)"
source .venv/bin/activate
pip install debugpy
```

## Pattern A: Source-edit — process waits for debugger at launch

Add near the top of the entry point (or inside the function you want to debug):

```python
import debugpy
debugpy.listen(("127.0.0.1", 5678))
print("debugpy listening on 5678, waiting for client...", flush=True)
debugpy.wait_for_client()
debugpy.breakpoint()       # optional: pause immediately once attached
```

Start the process; it blocks on `wait_for_client()`.

## Pattern B: No source edit — launch with `-m debugpy`

```bash
python -m debugpy --listen 127.0.0.1:5678 --wait-for-client your_script.py arg1
```

Equivalent for module entry:

```bash
python -m debugpy --listen 127.0.0.1:5678 --wait-for-client -m your.module
```

## Pattern C: Attach to an already-running process

Needs the PID and debugpy preinstalled in the target's environment:

```bash
python -m debugpy --listen 127.0.0.1:5678 --pid <pid>
# debugpy injects itself into the process. Then attach a client as below.
```

Some kernels/security configs block the ptrace-based injection
(`/proc/sys/kernel/yama/ptrace_scope`). Fix with:
```bash
echo 0 | sudo tee /proc/sys/kernel/yama/ptrace_scope
```

## Connecting a client from the terminal

The easiest terminal-side DAP client is VS Code CLI or a small script. From inside Hermes you have
three practical options.

### Option 1: a tiny hand-rolled DAP client

Not an official debugpy feature, just a minimal script:

```python
# /tmp/dap_client.py
import socket, json, itertools, time, sys

HOST, PORT = "127.0.0.1", 5678
s = socket.create_connection((HOST, PORT))
seq = itertools.count(1)

def send(msg):
    msg["seq"] = next(seq)
    body = json.dumps(msg).encode()
    s.sendall(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)

def recv():
    header = b""
    while b"\r\n\r\n" not in header:
        header += s.recv(1)
    length = int(header.decode().split("Content-Length:")[1].split("\r\n")[0].strip())
    body = b""
    while len(body) < length:
        body += s.recv(length - len(body))
    return json.loads(body)

send({"type": "request", "command": "initialize", "arguments": {"adapterID": "python"}})
print(recv())
send({"type": "request", "command": "attach", "arguments": {}})
print(recv())
send({"type": "request", "command": "setBreakpoints",
      "arguments": {"source": {"path": sys.argv[1]},
                    "breakpoints": [{"line": int(sys.argv[2])}]}})
print(recv())
send({"type": "request", "command": "configurationDone"})
# ... loop reading events and sending continue/stepIn/etc.
```

This is fine for one-off automation but painful as an interactive UX.

### Option 2: Attach from VS Code / Cursor / Zed

If the user has one open, they can add a `launch.json`:

```json
{
  "name": "Attach to Hermes",
  "type": "debugpy",
  "request": "attach",
  "connect": { "host": "127.0.0.1", "port": 5678 },
  "justMyCode": false,
  "pathMappings": [
    { "localRoot": "${workspaceFolder}", "remoteRoot": "<absolute path of the hermes-agent checkout the process runs from>" }
  ]
}
```

### Option 3: Ditch DAP, use `remote-pdb`

Usually what you actually want from a terminal agent:

```bash
pip install remote-pdb
```

In your code:
```python
from remote_pdb import set_trace
set_trace(host="127.0.0.1", port=4444)   # blocks until connection
```

Then from the terminal:
```bash
nc 127.0.0.1 4444
# You get a (Pdb) prompt exactly as if debugging locally.
```

`remote-pdb` is the cleanest agent-friendly choice when `debugpy`'s DAP protocol is overkill.
Use `debugpy` only when you actually need IDE integration.
