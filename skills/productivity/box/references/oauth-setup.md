# OAuth setup

Use OAuth when one person wants Hermes to act with the same Box access they have. OAuth follows that person's Box permissions and the app's scopes; it does not grant enterprise-wide access.

## Local desktop path (default)

Treat a local desktop as the default unless the user says Hermes is running remotely or without a browser callback. If `box` is not already on `PATH`, install the CLI under the current Hermes home at `tools/box-cli`; use the shell-specific setup in [CLI guide](cli-guide.md). On macOS/Linux, run:

```bash
BOX_CLI_HOME="${HERMES_HOME:-$HOME/.hermes}/tools/box-cli"
npm install --prefix "$BOX_CLI_HOME" @box/cli
npm exec --prefix "$BOX_CLI_HOME" -- box --version
```

Use the same runner for every later Box command in this setup. Start one official local login operation without `--code`, leave its terminal process running until it exits, then verify the actor:

```bash
npm exec --prefix "$BOX_CLI_HOME" -- \
  box login --default-box-app --name hermes-oauth
npm exec --prefix "$BOX_CLI_HOME" -- \
  box users:get me --json --fields id,name,login
```

If `box` already resolves on `PATH`, run the same `box login` and `box users:get me` commands without the `npm exec` prefix. The browser flow creates and selects the named environment. Announce the pending authorization, wait for the CLI process to finish, then continue with the actor check. Let the CLI open the authorization page and receive the local callback. Do not use browser tools, inspect browser tabs, request the resulting URL, navigate to Box, or ask the user to paste a code.

## Remote or headless path

Use this path only after the user explicitly confirms Hermes is running remotely/headlessly. Run:

```bash
box login --default-box-app --code --name hermes-oauth
```

Open the displayed URL with the available browser tool. If the user must sign in or approve access, pause for that human-only step; then continue the interactive CLI code flow and verify the actor. Do not use this fallback merely because a local browser is available.

## Existing environments

The Box CLI stores multiple named environments but uses one current default:

```bash
box configure:environments:list
box configure:environments:set-current hermes-oauth
box users:get me --json --fields id,name,login
```

Request approval before switching the current environment, especially on a shared or background installation. Switch it only after approval and verify the resulting actor.

## Official links

- [Box CLI quick start](https://developer.box.com/guides/cli/quick-start/)
- [OAuth 2.0 guide](https://developer.box.com/guides/authentication/oauth2/)
