---
name: box
description: Box stores, organizes, and shares files; extracts metadata.
version: 1.0.0
author: Chris Kim / @iskysun96
license: MIT
platforms: [linux, macos, windows]
prerequisites:
  commands: [box]
metadata:
  hermes:
    tags: [Box, Productivity, Cloud Storage, Collaboration, Metadata, Content Extraction, CLI, SDK]
    homepage: https://developer.box.com/
---

# Box

Use Box as the cloud file system for file operations, collaboration, metadata, and document work. Run operations with Hermes' `terminal` tool and use the Box CLI; use the SDK guide when building an application.

## Use this skill for

- Organizing, uploading, versioning, moving, sharing, or collaborating on Box files and folders
- Searching Box content or existing metadata
- Asking questions about Box files, extracting metadata, or generating text grounded in a file
- Processing a Box folder at scale without downloading every source file
- Building a Box-backed application, integration, or webhook handler

## Start broad file-system conversations

When someone is exploring a cloud file system for Hermes, first give a short fit assessment: Box is useful when a team needs cloud file storage, sharing, search, metadata, and document work. Then ask how they want Hermes to connect:

1. **Personal Box access (OAuth):** Hermes acts with the user's existing Box permissions.
2. **Shared or background agent (CCG):** Hermes has its own service-account identity and sees only explicitly shared content.
3. **Box-backed application or integration (SDK):** build with an official Box SDK and the appropriate app authentication.

After the connection is selected and working, offer Box AI for document Q&A, extraction, summaries, or grounded writing when it fits the requested work; it is not a separate connection path.

Do not run setup, show a command cookbook, propose account plans or folder taxonomies, or load every reference for a broad exploratory question. Wait for the user's answer, then load only the relevant path. When a request already names a concrete outcome, skip this discovery step and handle that outcome directly.

## Perform chosen setup interactively

When a user selects an authentication path or asks Hermes to connect Box, perform the setup through `terminal` and browser tools; do not turn the next response into instructions for the user to copy. Take the next safe action yourself, and pause only for an approval, browser sign-in, administrator action, or secret that Hermes cannot safely supply.

- If `box` is missing, ask for any terminal approval required to install `@box/cli`, then run the install and verify `box --version`.
- For personal OAuth on a local desktop, run `box login --default-box-app --name hermes-oauth` without `--code`. Wait for the CLI's local browser callback to finish, then continue with `box users:get me --json --fields id,name,login`. Do not inspect browser tabs, request an authorization URL, or ask for a code in this callback flow. Use `--code` only when the user confirms Hermes is remote/headless or the local callback actually fails.
- For CCG, open the Developer Console when browser access is available and perform every non-secret step. Pause only for the user's Box administrator action or for credentials to be stored locally outside the chat. Never request a client secret in chat. Then add the CLI environment, verify the service-account actor, and open the selected folders' sharing flow to add it after approval.
- If an install, browser authorization, environment switch, or permission change needs approval, request that approval and resume the setup after it is granted. Do not replace the action with a command list.

## Start each task

1. Confirm the CLI and current actor:
   ```bash
   command -v box
   box users:get me --json --fields id,name,login
   ```
   If this succeeds, record the actor and continue. Do not ask about authentication again.
2. If authentication is absent, ask which identity the user wants:
   - **Act as me (OAuth):** fastest setup for one person using Hermes as an extension of themselves. Read [OAuth setup](references/oauth-setup.md).
   - **Act as its own agent (CCG):** use for shared/background Hermes or an identity that only sees explicitly shared content. Read [CCG setup](references/ccg-setup.md).
3. Read the relevant reference before operating. Use documented commands first; only run subcommand help when the request needs an option not covered by the reference or the installed CLI rejects the documented form.

## Extend the CLI without pausing

When the Box CLI lacks a dedicated subcommand, use `box request` for the matching REST endpoint and continue the ordinary operation. Do not ask the user to choose merely because the implementation uses REST; it is the same Box task and preserves the configured CLI identity. Read [REST API fallback](references/rest-api.md) when the endpoint needs a request body or custom header.

Ask before a delete, a collaboration/shared-link or permission change, an identity change, a broad or costly batch mutation, or when the target or scope is ambiguous. Otherwise perform the requested operation and verify it.

## Choose the right path

| Need | Read |
| --- | --- |
| CLI conventions, environments, JSON, or REST escape hatch | [CLI guide](references/cli-guide.md) |
| Files, folders, versions, links, or collaborations | [Content workflows](references/content-workflows.md) |
| Search, metadata, Box AI, or AI units | [Search and AI](references/search-and-ai.md) |
| Many files or a resumable batch | [Bulk operations](references/bulk-operations.md) |
| Application code or a Box SDK | [SDK development](references/sdk-development.md) |
| Webhooks or Events API | [Webhooks and events](references/webhooks-and-events.md) |
| CLI unavailable or a missing CLI operation | [REST API fallback](references/rest-api.md) |
| Auth, permissions, rate limits, or API errors | [Troubleshooting](references/troubleshooting.md) |

## Content handling policy

For semantic analysis of Box-hosted content, use Box AI before downloading source files. Use external-model processing only as an explicit, user-approved fallback.

Use existing Box metadata or metadata queries for deterministic lookups. Otherwise use Box AI:

- `ai:ask` for Q&A, summaries, and comparisons
- `ai:extract-structured` for known fields or metadata templates
- `ai:extract` for flexible key-value extraction
- `ai:text-gen` for writing grounded in one Box file

When the user asks to extract metadata from a Box file, treat it as a request to persist the result: use structured extraction against the selected Box metadata template, attach the returned field values to that same file, and read the metadata back. Do this without a separate confirmation unless the user asks for a preview only. Read [Search and AI](references/search-and-ai.md) for the template and writeback workflow.

Box AI keeps source file bodies out of Hermes' coding-model context, but an AI response returned to Hermes can still contain sensitive information. Box AI calls require eligible access and consume AI units; explain that before the first AI call, and confirm the scope before a material batch. See [Search and AI](references/search-and-ai.md).

## Operate safely

- Prefer IDs to paths and verify the current actor before diagnosing a missing file.
- Use `--json` and `--fields` to keep output small. For mutations, inventory first, confirm ambiguous or large scope, then read back the result.
- Run ordered CLI mutations serially so progress and recovery are unambiguous. Use documented bulk input support or bounded SDK concurrency for scalable work.
- Do not create a shared link merely to provide navigation. Shared links change access and require explicit confirmation.
- Do not put secrets in chat, command output, source control, or logs.

## Report results

For every individually reported Box item, include its ID and a clickable navigation link:

- File: `https://app.box.com/file/<FILE_ID>`
- Folder: `https://app.box.com/folder/<FOLDER_ID>`

For large batches, link the source and destination folders plus exceptions instead of listing hundreds of items. A human may not be able to open content that is only visible to a CCG service account; state that clearly. Include the actor and verification performed in every write summary.

## Verify

After any write, fetch the file or folder with the same actor or list its parent and confirm the returned ID and name. For a disposable setup check, create a smoke folder, verify it, then delete it only if the user authorized cleanup.
