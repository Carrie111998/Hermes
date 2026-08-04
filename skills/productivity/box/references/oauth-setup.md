# OAuth setup

Use OAuth when one person wants Hermes to act with the same Box access they have. OAuth follows that person's Box permissions and the app's scopes; it does not grant enterprise-wide access.

## Local desktop path (default)

Treat a local desktop as the default unless the user says Hermes is running remotely or without a browser callback. Install the CLI through `terminal` if needed, start the official Box CLI login without `--code`, wait for its callback to complete, and verify the actor:

```bash
npm install -g @box/cli
box login --default-box-app --name hermes-oauth
box users:get me --json --fields id,name,login
```

The browser flow creates and selects the named environment. Announce the pending authorization, wait for the CLI process to finish, then continue with the actor check. Do not inspect browser tabs, request the resulting URL, or ask the user to paste a code.

## Remote or headless path

Use this path only after the user confirms Hermes is running remotely/headlessly, or after the local callback path fails. Run:

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
