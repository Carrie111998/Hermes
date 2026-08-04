# OAuth setup

Use OAuth when one person wants Hermes to act with the same Box access they have. OAuth follows that person's Box permissions and the app's scopes; it does not grant enterprise-wide access.

## Same-host interactive path

Use this path only when the CLI process and the browser in which the user will authorize run on the same host. Do not infer this from operating system alone. If the runtime topology is unclear, ask before starting OAuth. If `box` is not already on `PATH`, install the CLI under the current Hermes home at `tools/box-cli`; use the shell-specific setup in [CLI guide](cli-guide.md). On macOS/Linux, run:

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

## Separate-host or headless path

Use this path only after the user explicitly confirms that the CLI and authorization browser are on separate hosts, or that the runtime is headless. Run:

```bash
box login --default-box-app --code --name hermes-oauth
```

Open the displayed URL with a browser tool only when it controls the human's authorization browser. Otherwise present the URL and pause for the user to sign in and approve access, then continue the CLI's code-and-state prompts and verify the actor. Do not use this path when the same-host callback is available.

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
