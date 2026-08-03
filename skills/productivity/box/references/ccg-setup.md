# Client Credentials Grant (CCG) setup

Use CCG when Hermes needs its own service-account identity: a background agent, a shared gateway, or a bot whose access should be granted folder by folder.

## Create and authorize the app

1. In the [Box Developer Console](https://app.box.com/developers/console), create a **Platform App** using **Client Credentials Grant**.
2. Choose the scopes and access level needed for the work. Authorization method is fixed when the app is created.
3. Authorize the app. Enterprise users may need an administrator to approve it.
4. Copy the Client ID, Client Secret, and Enterprise ID into `~/.hermes/.env`; never paste the secret into chat.

```text
BOX_CLIENT_ID=your_client_id
BOX_CLIENT_SECRET=your_client_secret
BOX_ENTERPRISE_ID=your_enterprise_id
```

## Add a CLI environment

Copy [the CCG configuration template](../templates/ccg-config.json.example), replace its placeholders locally, then run:

```bash
box configure:environments:add /path/to/ccg-config.json --ccg-auth --name hermes-ccg --set-as-current
box users:get me --json --fields id,name,login
```

The returned `login` is the service-account email. Do not routinely print environment configuration: it may contain sensitive information.

## Give the service account content access

A CCG service account begins with its own empty root and cannot see a human's existing Box content until it is invited.

1. In [Box](https://app.box.com), open the top-level folder Hermes should access.
2. Invite the service-account email from `box users:get me`.
3. Choose the narrowest role: Viewer for read/search, Editor for upload/move/version, and Co-owner only when required.
4. Share the parent folder when Hermes needs the subtree.

An existing editor can also collaborate the service account through the CLI:

```bash
box collaborations:create <FOLDER_ID> folder --role editor --login <SERVICE_ACCOUNT_EMAIL> --json
```

If a CCG operation yields 404 or an empty search, verify the actor and collaboration before assuming the object is missing.

## Advanced impersonation

Use `--as-user <USER_ID>` only when the app is authorized for it and the user explicitly needs work performed as a managed user. Treat it as a separate actor and include it in the result summary.

## Official links

- [CCG setup](https://developer.box.com/guides/authentication/client-credentials/client-credentials-setup/)
- [Box user types](https://developer.box.com/platform/user-types/)
