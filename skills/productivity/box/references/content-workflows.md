# Content workflows

Use IDs, not paths, once an item is resolved. Read [CCG setup](ccg-setup.md) when the acting service account cannot see the target folder.

## Browse and create folders

```bash
box folders:get <FOLDER_ID> --json --fields id,name,parent,item_collection
box folders:items <FOLDER_ID> --json --max-items 100 --fields id,name,type
box folders:create <PARENT_ID> "Customer-123" --json --fields id,name,parent
```

Duplicate names in one parent return `409`. Reuse the existing folder ID instead of retrying blindly.

## Upload, download, and version files

```bash
box files:upload ./artifact.pdf --parent-id <FOLDER_ID> --json --fields id,name,size
box files:get <FILE_ID> --json --fields id,name,size,sha1,parent
box files:download <FILE_ID> --destination . --save-as local-copy.pdf
box files:versions:upload <FILE_ID> ./updated.pdf --json --fields id,name,sha1
box files:versions:list <FILE_ID> --json
box files:versions:download <FILE_ID> <VERSION_ID> --destination . --save-as older.pdf
```

Download source bytes only when the task truly requires local editing or the user explicitly approves external analysis. Prefer a new version over replacing an unrelated file by name.

## Rename, tag, and move

```bash
box files:update <FILE_ID> --name "Renamed.pdf" --json --fields id,name
box files:update <FILE_ID> --description "Updated by Hermes" --tags "reviewed,2026" --json
box files:move <FILE_ID> <NEW_PARENT_ID> --json --fields id,name,parent
box folders:move <FOLDER_ID> <NEW_PARENT_ID> --json --fields id,name,parent
```

Read back the item or its parent after every write. Moving a folder moves its contents; confirm broad moves before executing them.

## Collaborate and share

```bash
box collaborations:create <FOLDER_ID> folder --role editor --login collaborator@example.com --json
box shared-links:create <FILE_ID> file --access company --json
box shared-links:create <FOLDER_ID> folder --access open --json
```

Use the narrowest collaboration role. Creating or widening a shared link changes access, so require explicit confirmation.

## Navigate without changing permissions

Report these links for items already known to the caller; they do not create a shared link:

- File: `https://app.box.com/file/<FILE_ID>`
- Folder: `https://app.box.com/folder/<FOLDER_ID>`

Include the item ID with the link. If a human cannot open a service-account-only item, state that rather than creating a link with broader access.

## Read and write metadata

```bash
box files:metadata:get <FILE_ID> --scope enterprise --template-key properties --json
box files:metadata:create <FILE_ID> --scope enterprise --template-key properties \
  --data invoice_id=INV-001 --json
```

Read the template definition and existing metadata before writing. Use [Search and AI](search-and-ai.md) when metadata must be extracted from document content.
