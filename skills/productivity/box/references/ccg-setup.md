# Client Credentials Grant (CCG) setup

Use CCG when Hermes needs a separate, unattended Box identity. **Always configure normal Hermes work as a dedicated App User.** Create one App User per Hermes deployment or isolation boundary and share only the files, folders, or Hubs it needs.

A Service Account is automatically created when an administrator authorizes the CCG app. Use it only as the provisioning identity for the App User; it is not Hermes's runtime actor. Both are API-only and begin with empty roots. Store CCG credentials in the runtime's secret store; never print them or put them in chat.

## Create and authorize the app

First ask whether the user wants Hermes to use the current local computer user's signed-in Box browser session to create and configure the Platform App. If they approve, open the [Box Developer Console](https://app.box.com/developers/console) and complete all non-secret steps. Do not attempt browser-driven setup on a remote or headless runtime unless the user confirms that a usable browser session exists there.

If they decline, give this path with clickable links and wait for the values to be stored locally: open the [Box Developer Console](https://app.box.com/developers/console), select **Create Platform App**, enter an app name, choose **Client Credentials Grant** and **App Access Only**, enable **Manage users**, **Make API calls using the as-user header**, and **Generate User Access Tokens**, choose the minimum additional scopes, then ask the administrator to [authorize the app](https://app.box.com/master/console). Link the user to the [official Platform App creation steps](https://developer.box.com/guides/applications/platform-apps/create/) if they need the Console flow. If on free developer account, the app is auto authorized. **Manage users is required to create the App User through this CCG app.** If the deployment will use Box AI, also enable **Configuration → Required Access Scopes → Content Actions → Manage AI** (`ai.readwrite`). Reauthorize the app after changing scopes.

Pause only when a Box administrator must approve the app or when the human must sign in. Find the Client ID, Client Secret, and Enterprise ID in the app's **App Details** sidebar. Never ask for a Client Secret in chat. Store the values directly in the active Hermes home's `.env` file, then resume after it is ready. When Hermes creates or updates that file, write only the required assignments—no prose, comments, code fences, placeholders, or other text:

```text
BOX_CLIENT_ID=your_client_id
BOX_CLIENT_SECRET=your_client_secret
BOX_ENTERPRISE_ID=your_enterprise_id
```

## Provision the Hermes App User

After the credentials exist locally, copy [the CCG configuration template](../templates/ccg-config.json.example) and replace its placeholders without printing secrets. Add and verify the Service Account environment only for provisioning:

```bash
box configure:environments:add /path/to/ccg-config.json --ccg-auth --name hermes-provisioner --set-as-current
box users:get me --json --fields id,name,login
```

The returned `login` is the Service Account email. Do not routinely print environment configuration: it may contain sensitive information.

Before creating the dedicated App User, explain its name and purpose and get approval: it creates a new Box identity. Then create and record the App User ID and email without printing credentials:

```bash
box users:create "Hermes Production Agent" --app-user --json --fields id,name,login
```

Do not assume an App User confirmation email is delivered or required. Configure the App User environment and verify it with `box users:get me`; continue when the returned actor ID is the new App User ID. If configuration or a first request returns `user_email_confirmation_required`, `password_reset_required`, or another activation-related error, pause and ask the Box administrator to complete the required account action. Do not tell the user to look for an email unless Box reports that requirement.

### Add the App User runtime environment

Configure the persistent Hermes environment with the App User ID. `--ccg-user` makes the CCG token represent the App User instead of the Service Account:

```bash
box configure:environments:add /path/to/ccg-config.json --ccg-auth --ccg-user <APP_USER_ID> --name hermes-agent --set-as-current
box users:get me --json --fields id,name,login
```

Confirm that the returned `id` is exactly `<APP_USER_ID>` before any ordinary Hermes file operation. Keep `hermes-agent` as the current environment; switch to `hermes-provisioner` only for an approved provisioning action, then immediately switch back and verify the App User again.

## Give the App User content or Hub access

The App User begins with an empty root. A shared file or folder can be verified directly by ID even if it is absent from folder `0`; a Box Hub is a separate resource and never appears in folder `0`.

Ask which specific file, folder, or Hub the App User should access. Accept a Box URL or ID. Do not choose a top-level folder, infer a parent folder, or grant subtree access by default.

If the user prefers a manual invite, report the App User's email and ID and ask the resource owner to invite it to that exact file, folder, or Hub in Box. If the user provides an ID, request approval for the exact resource and role, then create the collaboration only when the current actor is authorized to manage collaborators on that resource. If the current actor lacks that authority, do not retry with a broader identity or ask the user to change app scopes; provide the App User email and ID for a manual invite instead.

Use ordinary collaboration for an exact file or folder, and Hub collaboration for an exact Hub:

```bash
box collaborations:create <FILE_ID> file --role viewer --login <APP_USER_EMAIL> --json
box collaborations:create <FOLDER_ID> folder --role editor --login <APP_USER_EMAIL> --json
box hubs:collaborations:create <HUB_ID> --role viewer --user-id <APP_USER_ID> --json
```

After the invite, use the selected runtime environment to verify the exact resource, not its root listing:

```bash
box files:get <FILE_ID> --json --fields id,name,parent
box folders:get <FOLDER_ID> --json --fields id,name,parent
box hubs --scope all --max-items 1000 --json
box hubs:get <HUB_ID> --json
```

File/folder collaboration and Hub collaboration are distinct; neither proves access to the other. If a CCG operation yields 404 or an empty search, verify the selected CCG environment, its actor ID, and the App User's resource-specific collaboration before assuming the object is missing. Read [Box Hubs](hubs.md) before sharing or verifying a Hub.

## Advanced impersonation

Use `--as-user <USER_ID>` only when the app is authorized for it and the user explicitly needs work performed as a managed user. Treat it as a separate, exceptional actor; do not use it as a substitute for the App User runtime identity. For the CCG App User Box AI fallback, follow [Search and AI](search-and-ai.md).

## Official links

- [CCG setup](https://developer.box.com/guides/authentication/client-credentials/client-credentials-setup/)
- [Box user types](https://developer.box.com/platform/user-types/)
- [Create an App User](https://developer.box.com/guides/users/create-app-user/)
