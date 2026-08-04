# Client Credentials Grant (CCG) setup

Use CCG when Hermes needs a separate, unattended identity: a background agent, a shared gateway, or a bot whose access should be granted folder by folder.

Use two identities deliberately:

- **Service Account — control plane:** Box creates this API-only identity when an administrator authorizes the CCG app. Use it only to provision and administer the integration, such as creating the App User. It starts with an empty root; its capabilities depend on the app's scopes and enterprise authorization.
- **App User — Hermes runtime identity:** Create one dedicated App User for each Hermes deployment or isolation boundary. Configure the normal Hermes CLI environment to act as this user. Its root also starts empty, and it can access only folders explicitly shared with it.

Do not configure normal Hermes work to run as the Service Account. Although a Service Account is not automatically an enterprise administrator, the CCG credentials can carry broader application capabilities than the runtime needs. Use a dedicated CCG Platform App with **App Access Only**, the minimum scopes, **Manage users**, and **Generate User Access Tokens** enabled. Store its credentials in the runtime's secret store; do not print them or put them in chat.

## Create and authorize the app

Open the [Box Developer Console](https://app.box.com/developers/console) with browser tools when available, then create a **Platform App** using **Client Credentials Grant**. Select **App Access Only**, enable **Manage users** and **Generate User Access Tokens**, and choose only the remaining scopes required for the work; the authorization method is fixed at creation. **Manage users is required to create the App User through this CCG app.** Reauthorize the app if changing these settings requires it.

Complete every available browser step. Pause only when a Box administrator must approve the app or when the human must sign in. Find the Client ID, Client Secret, and Enterprise ID in the app's **App Details** sidebar. Never ask for a Client Secret in chat. Ask the human to store the values directly in the active Hermes home's `.env` file, then resume after they confirm it is ready:

```text
BOX_CLIENT_ID=your_client_id
BOX_CLIENT_SECRET=your_client_secret
BOX_ENTERPRISE_ID=your_enterprise_id
```

## Provision the Hermes App User

After the credentials exist locally, copy [the CCG configuration template](../templates/ccg-config.json.example), replace its placeholders without printing secrets, then add a short-lived provisioning environment. Verify that it acts as the Service Account:

```bash
box configure:environments:add /path/to/ccg-config.json --ccg-auth --name hermes-provisioner --set-as-current
box users:get me --json --fields id,name,login
```

The returned `login` is the Service Account email. Do not routinely print environment configuration: it may contain sensitive information. Before creating an App User, explain its name and purpose and get approval: it creates a new Box identity. Then create and record the App User ID and email without printing credentials:

```bash
box users:create "Hermes Production Agent" --app-user --json --fields id,name,login
```

Immediately pause the setup. Ask the person who receives the App User confirmation email to open it and follow its activation link. Do not configure Hermes as the App User or make its first API call until they confirm activation is complete; Box can reject actions against an unactivated App User.

## Add the App User runtime environment

Configure the persistent Hermes environment with the App User ID. `--ccg-user` makes the CCG token represent the App User instead of the Service Account:

```bash
box configure:environments:add /path/to/ccg-config.json --ccg-auth --ccg-user <APP_USER_ID> --name hermes-agent --set-as-current
box users:get me --json --fields id,name,login
```

Confirm that the returned `id` is exactly `<APP_USER_ID>` before any ordinary Hermes file operation. Keep `hermes-agent` as the current environment; switch to `hermes-provisioner` only for an approved control-plane action, then immediately switch back and verify the App User again.

## Give the App User content access

The App User begins with its own empty root and cannot see a human's existing Box content until it is invited.

Open the selected top-level folder in [Box](https://app.box.com), then open its sharing flow and add the App User email from the provisioning result. Request approval before changing collaboration. Choose the narrowest role: Viewer for read/search, Editor for upload/move/version, and Co-owner only when required. Share the parent folder when Hermes needs the subtree.

When the current actor is already an editor, create the collaboration through the CLI after approval:

```bash
box collaborations:create <FOLDER_ID> folder --role editor --login <APP_USER_EMAIL> --json
```

If a CCG operation yields 404 or an empty search, verify that `hermes-agent` is current, that its actor ID is the App User ID, and that the App User has the required collaboration before assuming the object is missing.

## Advanced impersonation

Use `--as-user <USER_ID>` only when the app is authorized for it and the user explicitly needs work performed as a managed user. Treat it as a separate, exceptional actor; do not use it as a substitute for the dedicated App User runtime identity. Include it in the result summary.

## Official links

- [CCG setup](https://developer.box.com/guides/authentication/client-credentials/client-credentials-setup/)
- [Box user types](https://developer.box.com/platform/user-types/)
- [Create an App User](https://developer.box.com/guides/users/create-app-user/)
