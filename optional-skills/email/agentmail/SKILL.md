---
name: agentmail
description: "Give the agent its own inbox: send and receive email."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [email, communication, agentmail, mcp]
    category: email
---

# AgentMail — Agent-Owned Email Inboxes

## Requirements

- **AgentMail API key** (required) — sign up at https://console.agentmail.to (free tier: 3 inboxes, 3,000 emails/month; paid plans from $20/mo)
- Node.js 18+ (for the MCP server)

## When to Use

Use this skill when you need to:
- Give the agent its own dedicated email address
- Send emails autonomously on behalf of the agent
- Receive and read incoming emails
- Manage email threads and conversations
- Sign up for services or authenticate via email
- Communicate with other agents or humans via email

This is NOT for reading the user's personal email (use himalaya or Gmail for that).
AgentMail gives the agent its own identity and inbox.

## Setup

### 1. Get an API Key

- Go to https://console.agentmail.to
- Create an account and generate an API key (starts with `am_`)

### 2. Store Your API Key Securely

**Recommended: macOS Keychain** (most secure — never stored in .env or config):
```bash
# Store the key in Keychain (service name MUST match what the tool looks up)
security add-generic-password -a "$USER" -s "AGENTMAIL_API_KEY" -w "«your_api_key_here»"

# Verify it was stored
security find-generic-password -a "$USER" -s "AGENTMAIL_API_KEY" -w
```

The `email_send` tool automatically checks macOS Keychain using the service name `AGENTMAIL_API_KEY`. For MCP servers, you need to explicitly pass the key — see MCP server config below.

**Alternative: .env file**
```bash
echo 'AGENTMAIL_API_KEY=«your_api_key_here»' >> ~/.hermes/.env
```

### 3. Configure MCP Server (for advanced features: reply, forward, delete, list threads)

Choose one approach:

**Option A: Keychain lookup (macOS only, most secure)**
```yaml
mcp_servers:
  agentmail:
    command: "npx"
    args: ["-y", "agentmail-mcp"]
```
The MCP server will automatically resolve the key from macOS Keychain.

**Option B: Environment variable**
```yaml
mcp_servers:
  agentmail:
    command: "npx"
    args: ["-y", "agentmail-mcp"]
    env:
      AGENTMAIL_API_KEY: "«your_key_from_keychain»"
```
Get your key from Keychain: `security find-generic-password -a "$USER" -s "AGENTMAIL_API_KEY" -w`

**Option C: .env file (not recommended for secrets)**
```yaml
mcp_servers:
  agentmail:
    command: "npx"
    args: ["-y", "agentmail-mcp"]
```
Ensure `AGENTMAIL_API_KEY` is set in `~/.hermes/.env`.

### 4. Restart Hermes
```bash
hermes
```
All 11 AgentMail tools are now available automatically.

## Available Tools (via MCP)

| Tool | Description |
|------|-------------|
| `list_inboxes` | List all agent inboxes |
| `get_inbox` | Get details of a specific inbox |
| `create_inbox` | Create a new inbox (gets a real email address) |
| `delete_inbox` | Delete an inbox |
| `list_threads` | List email threads in an inbox |
| `get_thread` | Get a specific email thread |
| `send_message` | Send a new email |
| `reply_to_message` | Reply to an existing email |
| `forward_message` | Forward an email |
| `update_message` | Update message labels/status |
| `get_attachment` | Download an email attachment |

## Procedure

### Create an inbox and send an email
1. Create a dedicated inbox:
   - Use `create_inbox` with a username (e.g. `hermes-agent`)
   - The agent gets address: `hermes-agent@agentmail.to`
2. Send an email:
   - Use `send_message` with `inbox_id`, `to`, `subject`, `text`
3. Check for replies:
   - Use `list_threads` to see incoming conversations
   - Use `get_thread` to read a specific thread

### Check incoming email
1. Use `list_inboxes` to find your inbox ID
2. Use `list_threads` with the inbox ID to see conversations
3. Use `get_thread` to read a thread and its messages

### Reply to an email
1. Get the thread with `get_thread`
2. Use `reply_to_message` with the message ID and your reply text

## Example Workflows

**Sign up for a service:**
```
1. create_inbox (username: "signup-bot")
2. Use the inbox address to register on the service
3. list_threads to check for verification email
4. get_thread to read the verification code
```

**Agent-to-human outreach:**
```
1. create_inbox (username: "hermes-outreach")
2. send_message (to: user@example.com, subject: "Hello", text: "...")
3. list_threads to check for replies
```

## Pitfalls

- Store API keys in macOS Keychain (not .env) for best security — the `email_send` tool auto-checks keychain
- For MCP server env vars: use `security find-generic-password -a "$USER" -s "AGENTMAIL_API_KEY" -w` to get the key for config injection, never hardcode
- Free tier limited to 3 inboxes and 3,000 emails/month
- Emails come from `@agentmail.to` domain on free tier (custom domains on paid plans)
- Node.js (18+) is required for the MCP server (`npx -y agentmail-mcp`)
- The `mcp` Python package must be installed: `pip install mcp`
- Real-time inbound email (webhooks) requires a public server — use `list_threads` polling via cronjob instead for personal use
- **Config YAML format bug**: `hermes config set` can store complex values (like `mcp_servers` dicts) as JSON strings instead of proper YAML, causing `AttributeError: 'str' object has no attribute 'items'`. Edit `config.yaml` directly to ensure `mcp_servers` is a proper YAML mapping
- **MCP server Keychain**: If MCP env vars are empty strings in config, the MCP server may fail to start. Use Option A or C from MCP server config above, or pass the key via Option B

## Verification

After setup, test with:
```
hermes --toolsets mcp -q "Create an AgentMail inbox called test-agent and tell me its email address"
```
You should see the new inbox address returned.

## References

- AgentMail docs: https://docs.agentmail.to/
- AgentMail console: https://console.agentmail.to
- AgentMail MCP repo: https://github.com/agentmail-to/agentmail-mcp
- Pricing: https://www.agentmail.to/pricing
