# Client Credentials Grant (CCG) setup

Use CCG when Hermes needs its own service-account identity: a background agent, a shared gateway, or a bot whose access should be granted folder by folder.

## Create and authorize the app

Open the [Box Developer Console](https://app.box.com/developers/console) with browser tools when available, then create a **Platform App** using **Client Credentials Grant**. Choose only the scopes and access level required for the work; the authorization method is fixed at creation.

Complete every available browser step. Pause only when a Box administrator must approve the app or when the human must sign in. Never ask for a Client Secret in chat. Ask the human to store the Client ID, Client Secret, and Enterprise ID directly in the active Hermes home's `.env` file, then resume after they confirm it is ready:

```text
BOX_CLIENT_ID=your_client_id
BOX_CLIENT_SECRET=your_client_secret
BOX_ENTERPRISE_ID=your_enterprise_id
```

## Add a CLI environment

After the credentials exist locally, copy [the CCG configuration template](../templates/ccg-config.json.example), replace its placeholders without printing secrets, add the environment, and verify the actor:

```bash
box configure:environments:add /path/to/ccg-config.json --ccg-auth --name hermes-ccg --set-as-current
box users:get me --json --fields id,name,login
```

The returned `login` is the service-account email. Do not routinely print environment configuration: it may contain sensitive information.

## Give the service account content access

A CCG service account begins with its own empty root and cannot see a human's existing Box content until it is invited.

Open the selected top-level folder in [Box](https://app.box.com), then open its sharing flow and add the service-account email from `box users:get me`. Request approval before changing collaboration. Choose the narrowest role: Viewer for read/search, Editor for upload/move/version, and Co-owner only when required. Share the parent folder when Hermes needs the subtree.

When the current actor is already an editor, create the collaboration through the CLI after approval:

```bash
box collaborations:create <FOLDER_ID> folder --role editor --login <SERVICE_ACCOUNT_EMAIL> --json
```

If a CCG operation yields 404 or an empty search, verify the actor and collaboration before assuming the object is missing.

## Advanced impersonation

Use `--as-user <USER_ID>` only when the app is authorized for it and the user explicitly needs work performed as a managed user. Treat it as a separate actor and include it in the result summary.

## Official links

- [CCG setup](https://developer.box.com/guides/authentication/client-credentials/client-credentials-setup/)
- [Box user types](https://developer.box.com/platform/user-types/)
