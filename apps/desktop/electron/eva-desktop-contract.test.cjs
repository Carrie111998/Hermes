const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')

const desktopRoot = path.resolve(__dirname, '..')
const read = relativePath => fs.readFileSync(path.join(desktopRoot, relativePath), 'utf8')

test('evaOS Agent package identity and customer artifact contract are exact', () => {
  const pkg = JSON.parse(read('package.json'))
  assert.equal(pkg.productName, 'evaOS Agent')
  assert.equal(pkg.version, '2026.7.20-es.8')
  assert.equal(pkg.build.appId, 'com.electricsheephq.evaos.agent')
  assert.equal(pkg.build.executableName, 'evaOS Agent')
  assert.deepEqual(pkg.build.protocols[0].schemes, ['evaos-agent'])
  assert.equal(pkg.build.artifactName, 'evaOS-Agent-${version}-${arch}.${ext}')
  assert.equal(pkg.build.detectUpdateChannel, false)
  assert.equal(pkg.build.electronUpdaterCompatibility, '>=2.16')
  assert.deepEqual(pkg.build.publish, [
    {
      provider: 'generic',
      url: 'https://github.com/electricsheephq/evaOS-hermes-desktop-app-adapter/releases/latest/download/',
      channel: 'latest'
    }
  ])
  assert.deepEqual(pkg.build.releaseInfo, { releaseNotesFile: 'release-notes.md' })
  assert.equal(pkg.build.icon, 'assets/eva')
  assert.deepEqual(
    pkg.build.extraResources.find(resource => resource.to === 'eva.ico'),
    { from: 'assets/eva.ico', to: 'eva.ico' }
  )
  assert.equal(
    pkg.build.mac.extendInfo.NSCameraUsageDescription,
    'evaOS Agent uses the camera when a plugin or feature you enable requests it.'
  )
  assert.equal(pkg.build.mac.forceCodeSigning, true)
  assert.equal(pkg.build.afterSign, 'scripts/notarize.mjs')
  assert.equal(
    pkg.build.extraResources.find(resource => resource.from === '../../LICENSE')?.to,
    'licenses/evaOS-Agent-MIT-LICENSE.txt'
  )
})

test('managed build makes local startup unreachable and consumes auth callbacks in main', () => {
  const main = read('electron/main.ts')
  const preload = read('electron/preload.ts')
  const runtime = read('electron/eva-runtime.cjs')
  const relay = read('electron/eva-ws-relay.cjs')
  const start = main.slice(
    main.indexOf('async function startHermes()'),
    main.indexOf('// Latched-failure short-circuit')
  )
  assert.match(main, /const EVA_MANAGED_BUILD = true/)
  assert.match(start, /if \(EVA_MANAGED_BUILD\)/)
  assert.match(start, /evaManagedRuntime\.resolveBackend/)
  assert.doesNotMatch(start, /resolveHermesBackend|ensureRuntime|spawn/)
  const managedStart = start.slice(0, start.indexOf('// Latched-failure short-circuit'))
  assert.match(managedStart, /Connecting to your managed evaOS agent/)
  assert.match(managedStart, /evaOS Agent is connected/)
  assert.doesNotMatch(managedStart, /managed Hermes agent|Eva is connected/)
  assert.match(runtime, /phase: 'eva\.sign-in-required'/)
  assert.match(runtime, /async function ensureDesktopSession/)
  assert.match(runtime, /return requireSignIn\(\)/)
  assert.match(runtime, /async function signIn/)
  assert.match(runtime, /beginSignIn\(\)/)
  assert.match(main, /parsed\.hostname === 'auth'/)
  assert.match(main, /evaManagedRuntime\.completeCallback/)
  assert.match(runtime, /wsUrl: await getWsRelay\(\)\.mintTicket\(\{/)
  assert.match(runtime, /baseUrl: `eva-managed:\/\/\$\{runtime\.customerId\}`/)
  assert.match(runtime, /token: ''/)
  assert.match(runtime, /runtime = await ensureRuntimeEnrollment\(\{ force: true \}\)/)
  assert.match(relay, /buildEvaManagedWsUrl\(upstream\.baseUrl, upstream\.token\)/)
  assert.match(preload, /eva: \{/)
  assert.match(main, /HERMES_PROTOCOL = EVA_MANAGED_BUILD \? EVA_MANAGED_POLICY\.callbackScheme/)
})

test('managed session persistence encrypts both tokens and restricts the state file', () => {
  const runtime = read('electron/eva-runtime.cjs')
  assert.match(runtime, /token: options\.encryptSecret\(state\.desktop\.token\)/)
  assert.match(runtime, /token: options\.encryptSecret\(state\.runtime\.token\)/)
  assert.match(runtime, /fs\.chmodSync\(statePath, 0o600\)/)
  assert.match(runtime, /async function requestApi/)
  assert.match(runtime, /error\?\.statusCode !== 401/)
  assert.match(runtime, /assertEvaManagedApiRequestAllowed\(request\)/)
  assert.match(runtime, /invalidateAuthWork\(\)/)
  assert.match(runtime, /resetRenderer\(\)/)
})

test('managed member UI exposes upstream features inside one server-bound agent context', () => {
  const main = read('electron/main.ts')
  const sidebar = read('src/app/chat/sidebar/index.tsx')
  const statusbar = read('src/app/shell/hooks/use-statusbar-items.tsx')
  assert.match(main, /Desktop profiles cannot change it/)
  assert.doesNotMatch(sidebar, /item\.id === 'new-session'.*item\.id === 'skills'.*item\.id === 'artifacts'/s)
  assert.match(sidebar, /<ProfileRail \/>/)
  assert.doesNotMatch(statusbar, /id: 'agents',[\s\S]*?hidden: managedEva/)
  assert.doesNotMatch(statusbar, /id: 'cron',[\s\S]*?hidden: managedEva/)
})

test('signed app updater and editable gateway paths stay Electric Sheep managed', () => {
  const main = read('electron/main.ts')
  const about = read('src/app/settings/about-settings.tsx')
  const chat = read('src/app/chat/index.tsx')
  const gatewayBoot = read('src/app/gateway/hooks/use-gateway-boot.ts')
  const connectingOverlay = read('src/components/gateway-connecting-overlay.tsx')
  const gateway = read('src/app/settings/gateway-settings.tsx')
  const onboarding = read('src/components/onboarding/index.tsx')
  const settings = read('src/app/settings/index.tsx')
  const updates = read('src/store/updates.ts')
  const smoke = read('scripts/test-desktop.mjs')
  assert.match(main, /createEvaAppUpdater/)
  assert.match(main, /EVA_MANAGED_BUILD\s*\?\s*getEvaAppUpdater\(\)\.check\(\)/)
  assert.match(main, /EVA_MANAGED_BUILD\s*\?\s*getEvaAppUpdater\(\)\.apply\(\)/)
  assert.match(main, /gateway settings are managed by Electric Sheep/)
  assert.match(about, /Signed updates from Electric Sheep/)
  assert.doesNotMatch(about, /Dorman/)
  assert.doesNotMatch(about, /NousResearch\/hermes-agent\/releases/)
  assert.match(gatewayBoot, /evaSignInRequired/)
  assert.match(gatewayBoot, /Sign in to evaOS Agent from Settings → Gateway/)
  assert.match(gatewayBoot, /navigate\(`\$\{SETTINGS_ROUTE\}\?tab=gateway`/)
  assert.match(connectingOverlay, /boot\.phase === 'renderer\.enrollment'/)
  assert.match(connectingOverlay, /boot\.phase === 'eva\.sign-in-required'/)
  assert.match(onboarding, /if \(window\.hermesDesktop\?\.eva\)/)
  assert.match(gateway, /Local backends, custom/)
  assert.doesNotMatch(settings, /managedViews/)
  assert.match(chat, /onPickFiles=\{onPickFiles\}/)
  assert.match(chat, /onTranscribeAudio=\{onTranscribeAudio\}/)
  assert.match(updates, /if \(isManagedEva\(\)\)/)
  assert.match(smoke, /evaOS-Agent-/)
  assert.doesNotMatch(smoke, /Hermes\.app|Hermes-\$\{PACKAGE_JSON\.version\}/)
})
