/**
 * Repository-wide Node runtime contract.
 *
 * Local development, GitHub Actions, and the published Docker image all use
 * Node. They must share one repository pin so a green local gate exercises the
 * same major version that CI and the container build use. The package engine
 * declares that major as the minimum supported runtime.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

import { test } from 'vitest'

const REPO_ROOT = path.resolve(__dirname, '..')
const NODE_PIN = path.join(REPO_ROOT, '.nvmrc')
const ROOT_PACKAGE = path.join(REPO_ROOT, 'package.json')
const DOCKERFILE = path.join(REPO_ROOT, 'Dockerfile')
const WORKFLOWS = path.join(REPO_ROOT, '.github', 'workflows')
const POSIX_INSTALLER = path.join(REPO_ROOT, 'scripts', 'install.sh')
const POWERSHELL_INSTALLER = path.join(REPO_ROOT, 'scripts', 'install.ps1')
const NODE_BOOTSTRAP = path.join(REPO_ROOT, 'scripts', 'lib', 'node-bootstrap.sh')
const HERMES_CONSTANTS = path.join(REPO_ROOT, 'hermes_constants.py')
const NIX_PACKAGE = path.join(REPO_ROOT, 'nix', 'hermes-agent.nix')
const NIX_CHECKS = path.join(REPO_ROOT, 'nix', 'checks.nix')
const NIXOS_MODULE = path.join(REPO_ROOT, 'nix', 'nixosModules.nix')

type Version = readonly [major: number, minor: number, patch: number]

function parseVersion(value: string, source: string): Version {
  const match = value.trim().match(/^v?(\d+)\.(\d+)\.(\d+)$/)
  assert.ok(match, `${source} must contain an exact Node version, got ${value.trim() || '<empty>'}`)

  return [Number(match[1]), Number(match[2]), Number(match[3])]
}

test('local, CI, Docker, and package engines share one compatible Node runtime', () => {
  assert.ok(fs.existsSync(NODE_PIN), '.nvmrc must pin the repository Node runtime')
  const pinned = parseVersion(fs.readFileSync(NODE_PIN, 'utf-8'), '.nvmrc')
  const active = parseVersion(process.versions.node, 'active Node runtime')
  assert.deepEqual(active, pinned, 'run the canonical gate with the exact Node version in .nvmrc')

  const packageJson = JSON.parse(fs.readFileSync(ROOT_PACKAGE, 'utf-8')) as {
    engines?: { node?: string }
    workspaces?: string[]
  }

  const engine = packageJson.engines?.node
  assert.ok(engine, 'package.json must declare engines.node')
  const floorMatch = engine.match(/^>=\s*(\d+)\.(\d+)\.(\d+)$/)
  assert.ok(floorMatch, `engines.node must be an explicit minimum version, got ${engine}`)
  const floor: Version = [Number(floorMatch[1]), Number(floorMatch[2]), Number(floorMatch[3])]
  assert.deepEqual(
    floor,
    [pinned[0], 0, 0],
    `package.json engines.node must declare Node ${pinned[0]} as the minimum supported runtime`
  )

  const workspaceManifests = (packageJson.workspaces ?? [])
    .flatMap(workspace => {
      if (!workspace.endsWith('/*')) {
        return [path.join(REPO_ROOT, workspace, 'package.json')]
      }

      const parent = path.join(REPO_ROOT, workspace.slice(0, -2))

      return fs
        .readdirSync(parent, { withFileTypes: true })
        .filter(entry => entry.isDirectory())
        .map(entry => path.join(parent, entry.name, 'package.json'))
    })
    .filter(manifest => fs.existsSync(manifest))

  for (const manifest of workspaceManifests) {
    const workspacePackage = JSON.parse(fs.readFileSync(manifest, 'utf-8')) as {
      engines?: { node?: string }
    }

    const workspaceEngine = workspacePackage.engines?.node

    if (workspaceEngine) {
      assert.equal(
        workspaceEngine,
        engine,
        `${path.relative(REPO_ROOT, manifest)} must use the repository Node engine contract`
      )
    }
  }

  const dockerfile = fs.readFileSync(DOCKERFILE, 'utf-8')

  const dockerSource = dockerfile.match(/^FROM node:(\d+)-bookworm-slim@sha256:[0-9a-f]{64} AS node_source$/m)

  assert.ok(dockerSource, 'Dockerfile must use a digest-pinned node:<major>-bookworm-slim stage')
  assert.equal(Number(dockerSource[1]), pinned[0], 'Docker node_source major must match the repository Node pin')

  const setupNodeWorkflows = fs
    .readdirSync(WORKFLOWS)
    .filter(name => name.endsWith('.yml') || name.endsWith('.yaml'))
    .filter(name => fs.readFileSync(path.join(WORKFLOWS, name), 'utf-8').includes('actions/setup-node@'))

  assert.ok(setupNodeWorkflows.length > 0, 'expected at least one setup-node workflow')

  for (const workflow of setupNodeWorkflows) {
    const source = fs.readFileSync(path.join(WORKFLOWS, workflow), 'utf-8')
    assert.match(source, /^\s+node-version-file:\s*\.nvmrc\s*$/m, `${workflow} must consume the shared .nvmrc pin`)
    assert.doesNotMatch(source, /^\s+node-version:\s*/m, `${workflow} must not duplicate the Node version inline`)
  }
})

test('installers, managed healing, and Nix provisioning follow the repository Node major', () => {
  const [major] = parseVersion(fs.readFileSync(NODE_PIN, 'utf-8'), '.nvmrc')

  const posixInstaller = fs.readFileSync(POSIX_INSTALLER, 'utf-8')
  assert.match(posixInstaller, new RegExp(`^NODE_VERSION="${major}"$`, 'm'))
  assert.match(
    posixInstaller,
    /\[ "\$major" -ge "\$NODE_VERSION" \]/,
    'install.sh must reject system Node versions below the managed target'
  )
  assert.match(posixInstaller, /need Node\.js \$NODE_VERSION\+/, 'install.sh must report the repository Node floor')

  const powershellInstaller = fs.readFileSync(POWERSHELL_INSTALLER, 'utf-8')
  assert.match(powershellInstaller, new RegExp(`^\\$NodeVersion = "${major}"$`, 'm'))
  assert.match(
    powershellInstaller,
    /return \$v\.Major -ge \[int\]\$NodeVersion/,
    'install.ps1 must reject system Node versions below the managed target'
  )
  assert.match(
    powershellInstaller,
    /need Node\.js \$NodeVersion\+/,
    'install.ps1 must report the repository Node floor'
  )

  const bootstrap = fs.readFileSync(NODE_BOOTSTRAP, 'utf-8')
  assert.match(bootstrap, new RegExp(`HERMES_NODE_MIN_VERSION="\\$\\{HERMES_NODE_MIN_VERSION:-${major}\\}"`))
  assert.match(bootstrap, new RegExp(`HERMES_NODE_TARGET_MAJOR="\\$\\{HERMES_NODE_TARGET_MAJOR:-${major}\\}"`))

  const constants = fs.readFileSync(HERMES_CONSTANTS, 'utf-8')
  assert.match(
    constants,
    new RegExp(`_HERMES_NODE_TARGET_MAJOR = int\\(os\\.environ\\.get\\("HERMES_NODE_TARGET_MAJOR", "${major}"\\)\\)`)
  )

  const nixPackage = fs.readFileSync(NIX_PACKAGE, 'utf-8')
  assert.match(nixPackage, new RegExp(`^  nodejs_${major},$`, 'm'))
  assert.match(nixPackage, new RegExp(`^  nodejs = nodejs_${major};$`, 'm'))

  const nixChecks = fs.readFileSync(NIX_CHECKS, 'utf-8')
  assert.match(nixChecks, new RegExp(`test "\\$NODE_MAJOR" -eq ${major}`))

  const nixosModule = fs.readFileSync(NIXOS_MODULE, 'utf-8')
  assert.match(nixosModule, new RegExp(`^    nodeMajor = ${major};$`, 'm'))
  assert.match(nixosModule, /node_\$\{toString nodeMajor\}\.x/)
  assert.match(
    nixosModule,
    /inherit nodeMajor;/,
    'the container identity must change when the provisioned Node channel changes'
  )
})
