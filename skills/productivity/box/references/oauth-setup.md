# OAuth setup

Use OAuth when one person wants Hermes to act with the same Box access they have. OAuth follows that user's Box permissions and the app's scopes; it does not grant enterprise-wide access.

## Fast path

Install the CLI if needed, then use the official Box CLI app:

```bash
npm install -g @box/cli
box login --default-box-app --name hermes-oauth
box users:get me --json --fields id,name,login
```

The browser flow creates and selects the named environment. It supports content operations through the official CLI app.

## Headless path

When Hermes has no browser callback port, run:

```bash
box login --default-box-app --code --name hermes-oauth
```

Open the displayed URL in a browser, authorize it, and provide the state and authorization code as prompted.

## Existing environments

The Box CLI stores multiple named environments but uses one current default:

```bash
box configure:environments:list
box configure:environments:set-current hermes-oauth
box users:get me --json --fields id,name,login
```

Ask before switching the current environment, especially on a shared or background installation. Verify the resulting actor after every switch.

## Official links

- [Box CLI quick start](https://developer.box.com/guides/cli/quick-start/)
- [OAuth 2.0 guide](https://developer.box.com/guides/authentication/oauth2/)
