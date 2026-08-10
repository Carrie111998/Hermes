# OAuth setup

Use OAuth for every Hermes-to-Box connection. OAuth follows the signed-in Box user's permissions and the app's scopes; it does not grant enterprise-wide access.

## Choose the OAuth account

Authorize the Box account that Hermes should act as. OAuth follows that account's permissions. If the user wants a narrower permission boundary, authorize an account that is invited only to the files, folders, or Hubs Hermes should access. Do not make that account an administrator merely to unlock an exceptional operation.

Everyone who uses a shared or background Hermes deployment receives the access of the one Box account it authorizes, so do not connect it to a broader personal or administrator account. Before starting the browser flow, make sure the authorization browser is signed in as the intended Box account.

Choose a descriptive environment name, such as `hermes-box-oauth`. Do not overwrite or reauthorize an existing environment until its identity is confirmed.

## Same-host interactive path

First ask whether Hermes runs on the same computer as the browser the user will use to authorize Box. Use this path only when they confirm that it does. This is normally a local computer setup. Do not infer this from the operating system alone. If `box` is not already on `PATH`, install the CLI under the current Hermes home at `tools/box-cli`; use the shell-specific setup in [CLI guide](cli-guide.md). On macOS/Linux, run:

```bash
BOX_CLI_HOME="${HERMES_HOME:-$HOME/.hermes}/tools/box-cli"
npm install --prefix "$BOX_CLI_HOME" @box/cli
npm exec --prefix "$BOX_CLI_HOME" -- box --version
```

Use the same runner for every later Box command in this setup. Start one official local login operation without `--code`, leave its terminal process running until it exits, then verify the actor:

```bash
npm exec --prefix "$BOX_CLI_HOME" -- \
  box login --default-box-app --name <ENVIRONMENT_NAME>
npm exec --prefix "$BOX_CLI_HOME" -- \
  box users:get me --json --fields id,name,login
```

If `box` already resolves on `PATH`, run the same `box login` and `box users:get me` commands without the `npm exec` prefix. The browser flow creates and selects the named environment. Announce the pending authorization, wait for the CLI process to finish, then continue with the actor check. Let the CLI open the authorization page and receive the local callback. Do not use browser tools, inspect browser tabs, request the resulting URL, navigate to Box, or ask the user to paste a code.

## Separate-host or headless path

Use this path only after the user explicitly confirms that Hermes runs on a remote host—such as a VPS, container, or cloud VM—or that it is headless and the authorization browser is on a different computer. Run:

```bash
box login --default-box-app --code --name <ENVIRONMENT_NAME>
```

Open the displayed URL with a browser tool only when it controls the human's authorization browser. Otherwise present the URL and pause for the user to sign in and approve access, then continue the CLI's code-and-state prompts and verify the actor. Do not use this path when the same-host callback is available.

## Existing environments

The Box CLI stores multiple named environments but uses one current default:

```bash
box configure:environments:list
box configure:environments:set-current <ENVIRONMENT_NAME>
box users:get me --json --fields id,name,login
```

Request approval before switching the current environment, especially on a shared or background installation. Switch it only after approval and verify the resulting actor. If the returned identity is API-only or has no normal Box login, do not use it for Hermes; connect a normal Box account through OAuth instead.

## Custom OAuth Platform App

Use this path only when the requested operation needs a scope unavailable through the official CLI app, such as **Manage webhooks**. Create a Platform App with **User Authentication (OAuth 2.0)**, select only the required scope, and use the CLI's interactive flow:

```bash
box login --platform-app --name <ENVIRONMENT_NAME>
```

Let the local CLI prompt for the Client ID and Client Secret. Do not ask the user to paste either value into chat, write either value to Hermes configuration, or reuse a broader administrator identity for normal work. Authenticate the intended user in the browser, then verify the resulting actor. If the operation also requires an administrator, use a separately approved administrator OAuth session only for that operation; do not elevate the account Hermes normally uses.

## Official links

- [Box CLI quick start](https://developer.box.com/guides/cli/quick-start/)
- [OAuth 2.0 guide](https://developer.box.com/guides/authentication/oauth2/)
- [Box OAuth scopes](https://developer.box.com/guides/api-calls/permissions-and-errors/scopes/)
