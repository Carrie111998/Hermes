# OpenViking Memory Provider

Context database by Volcengine (ByteDance) with filesystem-style knowledge hierarchy, tiered retrieval, and automatic memory extraction.

## Requirements

- OpenViking installed with the `openviking-server` command available
- OpenViking server config initialized and validated (`openviking-server init`,
  then `openviking-server doctor`)
- OpenViking server running and reachable from Hermes

OpenViking 0.2.10 or newer is recommended. For backward compatibility,
Hermes can identify older servers that expose the legacy status-only health
response, but only when anonymous OpenAPI metadata also identifies the service
as OpenViking. OpenViking 0.2.6 and earlier are deprecated for this integration;
upgrade them to receive the current health contract and compatibility fixes.

## Setup

Prepare OpenViking first:

```bash
openviking-server init
openviking-server doctor
openviking-server
```

Then configure Hermes:

```bash
hermes memory setup    # select "openviking"
```

Or manually:

```bash
hermes config set memory.provider openviking
```

Add the connection settings to the active profile's `.env` file. For the
default profile that is `~/.hermes/.env`; for a named profile use
`~/.hermes/profiles/<profile>/.env`.

```text
OPENVIKING_ENDPOINT=http://127.0.0.1:1933
# OPENVIKING_API_KEY=...
# OPENVIKING_ACCOUNT=default
# OPENVIKING_USER=default
# OPENVIKING_AGENT=hermes
```

## Config

OpenViking's server config is separate from Hermes:

- `ov.conf` configures OpenViking storage, embedding/VLM models, auth, and
  server behavior. OpenViking reads it from `--config`,
  `OPENVIKING_CONFIG_FILE`, or `~/.openviking/ov.conf`.
- `ovcli.conf` stores client/CLI connection values such as `url`, `api_key`,
  `account`, and `user`. It is read from `OPENVIKING_CLI_CONFIG_FILE` or
  `~/.openviking/ovcli.conf`.

Hermes-side provider config is read from environment variables in the active
profile's `.env`:

| Env Var | Default | Description |
|---------|---------|-------------|
| `OPENVIKING_ENDPOINT` | `http://127.0.0.1:1933` | Server URL |
| `OPENVIKING_API_KEY` | (none) | API key (optional) |

## Tools

| Tool | Description |
|------|-------------|
| `viking_search` | Semantic search with fast/deep/auto modes |
| `viking_read` | Read content at a viking:// URI (abstract/overview/full) |
| `viking_browse` | Filesystem-style navigation (list/tree/stat) |
| `viking_remember` | Store a fact for extraction on session commit |
| `viking_add_resource` | Ingest URLs/docs into the knowledge base |
