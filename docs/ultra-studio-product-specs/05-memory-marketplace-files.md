# Memory, Marketplace, And Files

Status: product/platform specification  
Date: 2026-06-10

## Goal

Define the left navigation surfaces that are visible in creative agent products:
Marketplace, Files, Memory, and Tasks. These are not the same as Inspector.

Inspector is for the selected object. These surfaces are for browsing durable
workspace state.

## Marketplace

Marketplace is the catalog of reusable creative capabilities.

It should contain:

- workflow skills
- prompt recipes
- storyboard templates
- model recipes
- reusable Elements
- character packs
- project templates

Marketplace item fields:

```yaml
id:
kind: skill | recipe | template | element_pack | character_pack
title:
description:
category:
inputs_schema:
output_type:
required_tools:
provider_constraints:
version:
status: installed | available | disabled | deprecated
```

Marketplace is not a public app store in the first version. It can start as a
local catalog backed by checked-in skill metadata and curated templates.

## Memory

Memory stores durable facts that should influence future work.

Memory categories:

- user preferences
- brand rules
- project facts
- reusable prompt decisions
- model preferences
- rejected styles
- safety/policy notes

Memory must be visible and editable. Hidden memory creates trust problems.

Required behavior:

- show what memory exists for the current workspace/project
- allow delete/revoke
- show source session or user action
- distinguish user-authored memory from inferred memory
- never store provider secrets

## Files

Files are task/workspace objects, not necessarily reusable assets.

File categories:

- uploaded originals
- downloaded web artifacts
- generated task files
- logs
- prompt plans
- storyboard sheets
- rendered outputs

Files can be promoted into assets, but should not automatically become reusable
project assets.

## Tasks

Tasks represent work history and running jobs.

A task row should show:

- title
- session id
- last user request
- status
- active jobs
- output count
- date
- source: web | tui | cli | panel

Clicking a task should restore:

- transcript
- active/complete jobs
- task files
- selected model
- active skill profile
- relevant memory

## Search

Search should cover:

- messages
- tasks
- files
- assets
- memory
- marketplace entries

Search result cards must show type and source. A model recipe should not look
like a generated image. A memory should not look like a file.

## Access Control

Minimum permissions:

- read
- use
- update
- delete
- revoke
- share

Rules:

- Marketplace items can be visible without being enabled.
- Memory is scoped by user/workspace/project.
- Files are scoped by session/project.
- Assets are scoped by project/workspace and ACL.
- Shared conversations do not imply shared sandbox or credentials.

## Acceptance

- Left nav exposes Marketplace, Files, Memory, and Tasks.
- Memory entries can be inspected and revoked.
- Files can be promoted to assets.
- Marketplace can show installed and disabled workflows.
- Search results are typed and do not mix surfaces.

