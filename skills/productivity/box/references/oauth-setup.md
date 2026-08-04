# OAuth setup

Use OAuth when one person wants Hermes to act with the same Box access they have. OAuth follows that person's Box permissions and the app's scopes; it does not grant enterprise-wide access.

## Fast path

Run this path after the user selects personal access. Install the CLI through `terminal` if needed, start the official Box CLI login, and verify the actor:

```bash
npm install -g @box/cli
box login --default-box-app --name hermes-oauth
box users:get me --json --fields id,name,login
```

The browser flow creates and selects the named environment. Announce the pending authorization, wait for it to complete, then continue with the actor check instead of returning the commands as a setup checklist.

## Headless path

When the terminal cannot receive a browser callback, run:

```bash
box login --default-box-app --code --name hermes-oauth
```

Open the displayed URL with the available browser tool. If the user must sign in or approve access, pause for that human-only step; then collect the state and authorization code from the interactive CLI prompt and verify the actor.

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
