# Restricted profiles with local knowledge

Use `tools.restricted_knowledge_mcp` when a profile needs to read a small set of
local operating guides but must not receive the built-in `file`, `terminal`,
`code_execution`, `browser`, `computer_use`, or `delegation` toolsets.

The server is generic. The profile supplies a JSON map of short root names to
absolute directories through `HERMES_KNOWLEDGE_ROOTS`. It exposes only four
operations: list roots, list visible files, read bounded text excerpts, and
search bounded text. It does not write files, execute content, refresh an
index, follow symlink escapes, or reveal hidden paths.

Example profile configuration:

```yaml
mcp_servers:
  local_knowledge:
    command: /opt/hermes/venv/bin/python
    args: [-m, tools.restricted_knowledge_mcp]
    env:
      HERMES_KNOWLEDGE_ROOTS: >-
        {"operations":"/srv/operations","product_docs":"/srv/product/docs"}

platform_toolsets:
  slack: &restricted_profile_tools
    - web
    - skills
    - todo
    - memory
    - session_search
    - clarify
    - cronjob
    - local_knowledge
  cron: *restricted_profile_tools
  cli: *restricted_profile_tools
```

Add any profile-specific action server, such as `ab4`, to that same list. Do
not include `no_mcp`; it disables every MCP server, including the knowledge
server.

Tool policy is per execution surface. Restricting only `slack` does not
restrict scheduled jobs: without an explicit `platform_toolsets.cron`, cron
uses its normal default toolset. Configure every surface the profile actually
uses and verify the resolved set before arming unattended work.

Security properties:

- configured roots must be distinct absolute directories and cannot be `/`;
- request paths must be relative and cannot contain hidden or parent
  components;
- symlinks are neither listed nor read, so a symlink cannot escape a root;
- `.git`, `.venv`, `node_modules`, `__pycache__`, and all dotfiles are hidden;
- file size, excerpt length, directory entries, searched files/bytes, and
  results are bounded;
- returned text is marked `untrusted_reference_content` and must be treated as
  evidence, never as authority or executable instructions.

This is a read boundary, not a host-security sandbox. Do not combine it with a
host shell or broad file/code tool in the same restricted profile.
