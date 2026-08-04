# Client Credentials Grant (CCG) setup

Use CCG when Hermes needs a separate, unattended Box identity. Choose the runtime identity before configuring the CLI:

| Runtime identity | Choose it when | Do not choose it when |
| --- | --- | --- |
| **Service Account** | One centrally managed Hermes agent serves a shared Slack channel, gateway, or background workflow, and every allowed caller should operate one shared Box permission set. The agent owns its common content and is invited to the shared folders or Hubs it needs. | Different callers or Hermes profiles need separate Box permission boundaries. A Box Service Account does not apply each Slack caller's personal Box permissions. |
| **Dedicated App User** | One person uses a Hermes profile, or multiple profiles have distinct purposes, content, or permission scopes. Create one App User per profile or isolation boundary and share only its required folders or Hubs. | The team deliberately wants one shared runtime identity and one common content/permission boundary. |

Both identities are API-only and begin with empty roots. A Service Account is automatically created when an administrator authorizes the CCG app. An App User is created by that Service Account and is tied to its application. The Service Account's capabilities depend on the app scopes and enterprise authorization; it is not automatically an enterprise administrator, but can have elevated capabilities. Store CCG credentials in the runtime's secret store; never print them or put them in chat.

## Create and authorize the app

Open the [Box Developer Console](https://app.box.com/developers/console) with browser tools when available, then create a **Platform App** using **Client Credentials Grant**. Select **App Access Only** and choose only the scopes required for the work; the authorization method is fixed at creation. For an App User runtime, enable **Manage users** and **Generate User Access Tokens**: **Manage users is required to create the App User through this CCG app.** A direct Service Account runtime does not need either setting unless another approved workflow requires it. Reauthorize the app if changing these settings requires it.

Complete every available browser step. Pause only when a Box administrator must approve the app or when the human must sign in. Find the Client ID, Client Secret, and Enterprise ID in the app's **App Details** sidebar. Never ask for a Client Secret in chat. Ask the human to store the values directly in the active Hermes home's `.env` file, then resume after they confirm it is ready:

```text
BOX_CLIENT_ID=your_client_id
BOX_CLIENT_SECRET=your_client_secret
BOX_ENTERPRISE_ID=your_enterprise_id
```

## Configure the chosen runtime identity

After the credentials exist locally, copy [the CCG configuration template](../templates/ccg-config.json.example) and replace its placeholders without printing secrets. Add and verify the Service Account environment. Name it `hermes-service` for a direct Service Account runtime or `hermes-provisioner` when it will only create an App User:

```bash
box configure:environments:add /path/to/ccg-config.json --ccg-auth --name <SERVICE_ENVIRONMENT_NAME> --set-as-current
box users:get me --json --fields id,name,login
```

The returned `login` is the Service Account email. Do not routinely print environment configuration: it may contain sensitive information.

### Shared agent: use the Service Account directly

If the chosen design is one shared agent identity, set `<SERVICE_ENVIRONMENT_NAME>` to `hermes-service`. Keep it current, verify `box users:get me`, and invite the Service Account email to the shared folders or Hubs after approval. Report that everyone interacting with this Hermes deployment uses that one Box actor and permission boundary.

### Isolated profile: provision an App User

If the chosen design requires an isolated profile, use the Service Account only to provision it. Before creating an App User, explain its name and purpose and get approval: it creates a new Box identity. Then create and record the App User ID and email without printing credentials:

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

## Give the runtime identity content or Hub access

The selected Service Account or App User begins with an empty root. A shared file or folder can be verified directly by ID even if it is absent from folder `0`; a Box Hub is a separate resource and never appears in folder `0`.

Open the selected top-level folder in [Box](https://app.box.com), then open its sharing flow and add the selected runtime identity's email. Request approval before changing collaboration. Choose the narrowest role: Viewer for read/search, Editor for upload/move/version, and Co-owner only when required. Share the parent folder when Hermes needs the subtree.

When the current actor is already an editor, create the collaboration through the CLI after approval:

```bash
box collaborations:create <FOLDER_ID> folder --role editor --login <RUNTIME_IDENTITY_EMAIL> --json
```

After the invite, use the selected runtime environment to verify the exact resource, not its root listing:

```bash
box files:get <FILE_ID> --json --fields id,name,parent
box folders:get <FOLDER_ID> --json --fields id,name,parent
box hubs --scope all --max-items 1000 --json
box hubs:get <HUB_ID> --json
```

Use ordinary file/folder collaboration for a file or folder, and Hub collaboration for a Hub; neither proves access to the other. If a CCG operation yields 404 or an empty search, verify the selected CCG environment, its actor ID, and that runtime identity's resource-specific collaboration before assuming the object is missing. Read [Box Hubs](hubs.md) before sharing or verifying a Hub.

## Advanced impersonation

Use `--as-user <USER_ID>` only when the app is authorized for it and the user explicitly needs work performed as a managed user. Treat it as a separate, exceptional actor; do not use it as a substitute for the chosen Service Account or App User runtime identity. Include it in the result summary.

## Official links

- [CCG setup](https://developer.box.com/guides/authentication/client-credentials/client-credentials-setup/)
- [Box user types](https://developer.box.com/platform/user-types/)
- [Create an App User](https://developer.box.com/guides/users/create-app-user/)
