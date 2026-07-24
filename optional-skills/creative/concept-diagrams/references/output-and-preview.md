# Concept Diagrams — Output & Preview

### Default: standalone HTML file

Write a single `.html` file the user can open directly. No server, no dependencies, works offline. Pattern:

```python
# 1. Load the template
template = skill_view("concept-diagrams", "templates/template.html")

# 2. Fill in title, subtitle, and paste your SVG
html = template.replace(
    "<!-- DIAGRAM TITLE HERE -->", "SN2 reaction mechanism"
).replace(
    "<!-- OPTIONAL SUBTITLE HERE -->", "Bimolecular nucleophilic substitution"
).replace(
    "<!-- PASTE SVG HERE -->", svg_content
)

# 3. Write to a user-chosen path (or ./ by default)
write_file("./sn2-mechanism.html", html)
```

Tell the user how to open it:

```
# macOS
open ./sn2-mechanism.html
# Linux
xdg-open ./sn2-mechanism.html
```

### Optional: local preview server (multi-diagram gallery)

Only use this when the user explicitly wants a browsable gallery of multiple diagrams.

**Rules:**
- Bind to `127.0.0.1` only. Never `0.0.0.0`. Exposing diagrams on all network interfaces is a security hazard on shared networks.
- Pick a free port (do NOT hard-code one) and tell the user the chosen URL.
- The server is optional and opt-in — prefer the standalone HTML file first.

Recommended pattern (lets the OS pick a free ephemeral port):

```bash
# Put each diagram in its own folder under .diagrams/
mkdir -p .diagrams/sn2-mechanism
# ...write .diagrams/sn2-mechanism/index.html...

# Serve on loopback only, free port
cd .diagrams && python3 -c "
import http.server, socketserver
with socketserver.TCPServer(('127.0.0.1', 0), http.server.SimpleHTTPRequestHandler) as s:
    print(f'Serving at http://127.0.0.1:{s.server_address[1]}/')
    s.serve_forever()
" &
```

If the user insists on a fixed port, use `127.0.0.1:<port>` — still never `0.0.0.0`. Document how to stop the server (`kill %1` or `pkill -f "http.server"`).
