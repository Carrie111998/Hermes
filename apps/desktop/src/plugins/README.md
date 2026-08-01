# Bundled plugins

Drop a `<name>/plugin.{ts,tsx}` here that default-exports a `HermesPlugin` and
it registers automatically at boot (vite glob in `../contrib/plugins.ts`), with
the same inventory + live enable/disable contract as runtime plugins.

Reference/demo plugins that ship in-tree (the counter example, the gateway-pill
1:1 rebuild, the runtime-loader hello world) must opt out of default activation
with `defaultEnabled: false` so the shipped app stays uncluttered while Settings
can still inventory them for SDK dogfooding.

User- and agent-authored plugins load at runtime from
`$HERMES_HOME/desktop-plugins/<name>/plugin.js` (the disk door) — see the
`hermes-desktop-plugins` skill.
