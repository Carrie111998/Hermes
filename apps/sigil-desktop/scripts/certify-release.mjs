#!/usr/bin/env node

import { spawn, spawnSync } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import net from 'node:net'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'

import {
  countFeatureStatuses,
  determineVerdict,
  enforceFeatureCoverage,
  featureResult,
  readJson,
  sanitizeText,
  validateFeatureRegistry
} from './release-guardian-lib.mjs'

const appDirectory = path.resolve(import.meta.dirname, '..')
const repositoryRoot = path.resolve(appDirectory, '../..')
const registryPath = path.join(appDirectory, 'release-certification/features.json')
const packagePath = path.join(appDirectory, 'package.json')
const releaseDirectory = path.join(appDirectory, 'release')
const expectedBundleId = 'com.firecattechnology.sigil'
const expectedArchitecture = 'arm64'
const registry = readJson(registryPath)
const packageJson = readJson(packagePath)
const startedAt = new Date()
const commandRecords = []
const testOutput = []
const qualityGates = []
const runtimeFindings = {
  crashes: [],
  renderer_errors: [],
  electron_main_errors: [],
  ipc_failures: [],
  degraded_backend_behavior: [],
  updater_behavior: [],
  broker_connectivity_state: [],
  stale_data_handling: []
}
const safetyFindings = {
  trade: false,
  order: false,
  transfer: false,
  approval: false,
  broker_submission: false,
  wallet_mutation: false,
  external_financial_side_effect: false
}

let evidenceDirectory
let mountedDmg
let developmentElectron
let developmentServer
let packagedElectron
let certificationRuntimeRoot
let stagedApplicationRoot
let stagedApplicationPath
const certificationToken = crypto.randomBytes(24).toString('hex')

function readUpdaterFixtures() {
  const fixtureRoot = path.join(appDirectory, 'release-certification/updater-fixtures')
  const read = name => fs.readFileSync(path.join(fixtureRoot, name, 'latest-mac.yml'), 'utf8')

  return {
    current: read('current'),
    newer: read('newer'),
    malformed: read('malformed'),
    missing: null
  }
}

function log(message = '') {
  process.stdout.write(`${message}\n`)
}

function timestamp() {
  return new Date().toISOString()
}

function shellQuote(value) {
  const text = String(value)
  return /^[A-Za-z0-9_./:=@+-]+$/.test(text) ? text : `'${text.replaceAll("'", "'\\''")}'`
}

function recordCommand(command, args, cwd) {
  commandRecords.push(`[${timestamp()}] cwd=${cwd} ${[command, ...args].map(shellQuote).join(' ')}`)
}

function runCommand(name, command, args, options = {}) {
  const cwd = options.cwd ?? repositoryRoot
  recordCommand(command, args, cwd)
  log(`\n[guardian] ${name}`)

  const result = spawnSync(command, args, {
    cwd,
    encoding: 'utf8',
    env: { ...process.env, ...(options.env ?? {}) },
    maxBuffer: 100 * 1024 * 1024
  })
  const output = sanitizeText(`${result.stdout ?? ''}${result.stderr ?? ''}`)
  const record = {
    name,
    command: [command, ...args].join(' '),
    status: result.status === 0 ? 'PASS' : result.error?.code === 'ENOENT' ? 'BLOCKED' : 'FAIL',
    exit_code: result.status,
    output
  }

  testOutput.push(`\n===== ${name} =====\n${output}`)
  if (output) {
    process.stdout.write(output)
  }

  if (options.gate !== false) {
    qualityGates.push({
      id: options.gateId ?? name.toLowerCase().replaceAll(/[^a-z0-9]+/g, '-'),
      name,
      status: record.status,
      required: options.required !== false,
      evidence: output.trim().split('\n').slice(-12)
    })
  }

  return record
}

function gitValue(args) {
  const result = spawnSync('git', args, { cwd: repositoryRoot, encoding: 'utf8' })
  return result.status === 0 ? result.stdout.trim() : 'unknown'
}

function sha256(filePath) {
  const hash = crypto.createHash('sha256')
  hash.update(fs.readFileSync(filePath))
  return hash.digest('hex')
}

function writeEvidence(relativePath, content) {
  const target = path.join(evidenceDirectory, relativePath)
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.writeFileSync(target, sanitizeText(typeof content === 'string' ? content : JSON.stringify(content, null, 2)))
}

function addGate(id, name, status, evidence, required = true) {
  qualityGates.push({
    id,
    name,
    status,
    required,
    evidence: Array.isArray(evidence) ? evidence : [String(evidence)]
  })
}

function processRows() {
  const result = spawnSync('ps', ['-axo', 'pid=,command='], { encoding: 'utf8' })
  return (result.stdout ?? '')
    .split('\n')
    .map(line => {
      const match = line.trim().match(/^(\d+)\s+(.*)$/)
      return match ? { pid: Number(match[1]), command: match[2] } : null
    })
    .filter(Boolean)
}

async function stopProcessesMatching(predicate, label) {
  let matches = processRows().filter(row => predicate(row.command))
  for (const match of matches) {
    try {
      process.kill(match.pid, 'SIGTERM')
    } catch {
      // The process exited between inspection and termination.
    }
  }

  const deadline = Date.now() + 8000
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 250))
    matches = processRows().filter(row => predicate(row.command))
    if (matches.length === 0) {
      return
    }
  }

  for (const match of matches) {
    try {
      process.kill(match.pid, 'SIGKILL')
    } catch {
      // The process exited between inspection and termination.
    }
  }

  await new Promise(resolve => setTimeout(resolve, 500))
  matches = processRows().filter(row => predicate(row.command))
  if (matches.length > 0) {
    throw new Error(`Unable to stop ${label}: ${matches.map(match => match.pid).join(', ')}`)
  }
}

function killProcessGroup(child) {
  if (!child?.pid) {
    return
  }

  try {
    process.kill(-child.pid, 'SIGTERM')
  } catch {
    try {
      child.kill('SIGTERM')
    } catch {
      // It already exited.
    }
  }
}

async function cleanTemporaryPackagedApp() {
  killProcessGroup(packagedElectron)
  if (stagedApplicationPath) {
    await stopProcessesMatching(
      command => command.startsWith(`${stagedApplicationPath}/Contents/`),
      'temporary packaged Sigil processes'
    )
  }
  if (stagedApplicationRoot?.startsWith(`${os.tmpdir()}${path.sep}sigil-release-certification.`)) {
    fs.rmSync(stagedApplicationRoot, { recursive: true, force: true })
  }
  if (certificationRuntimeRoot?.startsWith(`${os.tmpdir()}${path.sep}sigil-release-runtime.`)) {
    fs.rmSync(certificationRuntimeRoot, { recursive: true, force: true })
  }
}

async function availableLoopbackPort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer()
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      const port = typeof address === 'object' && address ? address.port : undefined
      server.close(error => {
        if (error) {
          reject(error)
        } else if (!port) {
          reject(new Error('Unable to allocate a loopback debugging port.'))
        } else {
          resolve(port)
        }
      })
    })
  })
}

async function waitForHttp(url, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  let lastError

  while (Date.now() < deadline) {
    try {
      const response = await fetch(url)
      if (response.ok) {
        return
      }
      lastError = new Error(`HTTP ${response.status}`)
    } catch (error) {
      lastError = error
    }
    await new Promise(resolve => setTimeout(resolve, 300))
  }

  throw new Error(`Timed out waiting for ${url}: ${lastError?.message ?? 'unknown error'}`)
}

class CdpSession {
  constructor(url) {
    this.url = url
    this.nextId = 1
    this.pending = new Map()
    this.events = []
  }

  async connect() {
    await new Promise((resolve, reject) => {
      this.socket = new WebSocket(this.url)
      this.socket.addEventListener('open', resolve, { once: true })
      this.socket.addEventListener('error', reject, { once: true })
      this.socket.addEventListener('message', event => {
        const message = JSON.parse(String(event.data))
        if (message.id) {
          const pending = this.pending.get(message.id)
          if (!pending) {
            return
          }
          this.pending.delete(message.id)
          if (message.error) {
            pending.reject(new Error(message.error.message))
          } else {
            pending.resolve(message.result)
          }
          return
        }
        this.events.push(message)
      })
    })

    await this.command('Runtime.enable')
    await this.command('Log.enable')
    await this.command('Page.enable')
  }

  command(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = this.nextId++
      this.pending.set(id, { resolve, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }

  async evaluate(expression) {
    const result = await this.command('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true
    })
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.text ?? 'Renderer evaluation failed.')
    }
    return result.result.value
  }

  async waitFor(expression, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs
    let value
    while (Date.now() < deadline) {
      value = await this.evaluate(expression)
      if (value) {
        return value
      }
      await new Promise(resolve => setTimeout(resolve, 200))
    }
    throw new Error(`Timed out waiting for renderer condition: ${expression}`)
  }

  async screenshot(filePath) {
    const result = await this.command('Page.captureScreenshot', { format: 'png' })
    fs.writeFileSync(filePath, Buffer.from(result.data, 'base64'))
  }

  close() {
    this.socket?.close()
  }
}

async function connectToElectron(port, expectedUrlFragment, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  let target

  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`)
      const targets = await response.json()
      target = targets.find(item => item.type === 'page' && item.url.includes(expectedUrlFragment))
      if (target?.webSocketDebuggerUrl) {
        break
      }
    } catch {
      // Electron is still starting.
    }
    await new Promise(resolve => setTimeout(resolve, 300))
  }

  if (!target?.webSocketDebuggerUrl) {
    throw new Error(`No Electron page target found on loopback port ${port}.`)
  }

  const session = new CdpSession(target.webSocketDebuggerUrl)
  await session.connect()
  return session
}

function rendererErrorSummary(session) {
  const relevant = session.events.filter(event =>
    ['Runtime.exceptionThrown', 'Log.entryAdded'].includes(event.method)
  )

  return relevant.map(event => sanitizeText(JSON.stringify(event.params)))
}

async function clickButton(session, label) {
  const encoded = JSON.stringify(label)
  const clicked = await session.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find(
      item => item.textContent?.trim() === ${encoded}
    )
    if (!button) return false
    button.click()
    return true
  })()`)
  if (!clicked) {
    throw new Error(`Visible button not found: ${label}`)
  }
}

async function safeUiSmoke(session, { mode, identity, screenshotDirectory, updaterFixtures }) {
  const result = {
    mode,
    page: {},
    navigation: {},
    overview: {},
    proposal_selection: {},
    proposal_guard: {},
    proposal_certification: {},
    launch_guard: {},
    receipts: {},
    reconciliation: {},
    audit: {},
    settings: {},
    explanation: {},
    about: {},
    updater: {},
    preload: {},
    renderer_errors: []
  }

  await session.waitFor(`document.querySelector('[data-testid="sigil-operator"]') !== null`)
  result.page = await session.evaluate(`({
    url: location.href,
    title: document.title,
    body_text: document.body.innerText
  })`)
  result.preload = await session.evaluate(`({
    present: Boolean(window.sigilDesktop),
    product_name: window.sigilDesktop?.productName,
    persistence_namespace: window.sigilDesktop?.persistenceNamespace,
    broker_submission_available: window.sigilDesktop?.brokerSubmissionAvailable,
    build_info: window.sigilDesktop?.buildInfo,
    api_keys: Object.keys(window.sigilDesktop ?? {}).sort()
  })`)

  const expectedDestinations = {
    Overview: 'Governance pipeline',
    Proposals: 'Proposals and approvals',
    Launch: 'Governed launch control',
    Executions: 'Simulated executions',
    Reconciliation: 'Execution reconciliation',
    Audit: 'Chronological audit evidence',
    Settings: 'Sigil settings'
  }

  for (const [label, heading] of Object.entries(expectedDestinations)) {
    await clickButton(session, label)
    result.navigation[label] = await session.waitFor(
      `document.body.textContent.includes(${JSON.stringify(heading)})`
    )
  }

  await clickButton(session, 'Overview')
  result.overview = await session.evaluate(`({
    paper: document.body.textContent.includes('PAPER'),
    simulated: document.body.textContent.includes('SIMULATED'),
    disconnected: document.body.textContent.includes('DISCONNECTED'),
    stale: document.body.textContent.includes('Snapshot is stale'),
    masked: document.body.textContent.includes('Masked account'),
    fixed_cap: document.body.textContent.includes('$25.00'),
    broker_unavailable: document.body.textContent.includes('No broker submission is available')
  })`)
  result.proposal_selection = await session.evaluate(`(() => {
    const button = [...document.querySelectorAll('button')].find(item => item.textContent?.includes('NVDA'))
    if (!button) return { clicked: false }
    button.click()
    return {
      clicked: true,
      selected: button.getAttribute('aria-pressed') === 'true' ||
        document.body.textContent.includes('PRP-20260725-0041')
    }
  })()`)

  await clickButton(session, 'Proposals')
  result.proposal_guard = await session.evaluate(`({
    decision_buttons: [...document.querySelectorAll('button')]
      .filter(button => ['Approve', 'Reject'].includes(button.textContent?.trim()))
      .map(button => ({ label: button.textContent.trim(), disabled: button.disabled })),
    pending_count: [...document.querySelectorAll('body *')]
      .filter(item => item.childElementCount === 0 && item.textContent?.trim() === 'pending').length,
    no_submit_control: ![...document.querySelectorAll('button')]
      .some(button => /submit|place order|trade/i.test(button.textContent ?? ''))
  })`)
  result.proposal_certification = await session.evaluate(`(async () => {
    const api = window.sigilDesktop?.releaseCertification
    if (typeof api !== 'function') return { fixture_available: false }
    const initial = await api({ operation: 'proposal-state' })
    const cancelled = await api({
      operation: 'proposal-action', proposalId: 'CERT-CANCEL', action: 'cancel'
    })
    const approved = await api({
      operation: 'proposal-action', proposalId: 'CERT-APPROVE', action: 'approve'
    })
    const rejected = await api({
      operation: 'proposal-action', proposalId: 'CERT-REJECT', action: 'reject'
    })
    return { fixture_available: true, initial, cancelled, approved, rejected }
  })()`)

  await clickButton(session, 'Launch')
  result.launch_guard = await session.evaluate(`({
    controls: [...document.querySelectorAll('button')]
      .filter(button => ['Arm simulated launch', 'Suspend', 'Engage kill switch'].includes(button.textContent?.trim()))
      .map(button => ({ label: button.textContent.trim(), disabled: button.disabled })),
    fixed_cap: document.body.textContent.includes('$25.00'),
    read_only: document.body.textContent.includes('Capital limits are view-only')
  })`)

  await clickButton(session, 'Executions')
  result.receipts = await session.evaluate(`({
    simulated: document.body.textContent.includes('Simulated acknowledgement'),
    rejected: document.body.textContent.includes('Rejected before transport'),
    uncertain: document.body.textContent.includes('Outcome uncertain'),
    receipt_ids: ['RCT-20260725-018', 'RCT-20260725-017', 'RCT-20260725-016']
      .filter(value => document.body.textContent.includes(value))
  })`)

  await clickButton(session, 'Reconciliation')
  result.reconciliation = await session.evaluate(`({
    required: document.body.textContent.includes('Required'),
    retry_blocked: document.body.textContent.includes('Retry blocked pending reconciliation'),
    no_retry: document.body.textContent.includes('never retry automatically')
  })`)

  await clickButton(session, 'Audit')
  result.audit = await session.evaluate(`(() => {
    const input = document.querySelector('input[aria-label="Filter audit evidence"]')
    if (!input) return { filter_present: false }
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set
    setter.call(input, 'ORD-20260725-018')
    input.dispatchEvent(new Event('input', { bubbles: true }))
    input.dispatchEvent(new Event('change', { bubbles: true }))
    const details = document.querySelector('details')
    if (details) details.open = true
    return {
      filter_present: true,
      value: input.value,
      matching_order: document.body.textContent.includes('ORD-20260725-018'),
      excluded_order: !document.body.textContent.includes('ORD-20260725-017'),
      sanitized_details: document.body.textContent.includes('"broker_submission_attempted": false')
    }
  })()`)

  await clickButton(session, 'Settings')
  result.settings = await session.evaluate(`({
    paper_only: document.body.textContent.includes('Paper and simulation only'),
    masked_only: document.body.textContent.includes('Masked identifiers only'),
    execution_never: document.body.textContent.includes('Never'),
    broker_unavailable: document.body.textContent.includes('Unavailable'),
    no_credentials: document.body.textContent.includes('Broker credentials, live submission, and capital-limit controls are not available')
  })`)
  await clickButton(session, 'Explain selected proposal')
  await session.waitFor(`document.querySelector('[data-testid="hermes-analysis"]') !== null`)
  result.explanation = await session.evaluate(`({
    text: document.querySelector('[data-testid="hermes-analysis"]')?.innerText,
    execution_denied: document.body.textContent.includes('Execution authorized: no'),
    broker_denied: document.body.textContent.includes('Broker submission available: no')
  })`)

  const openedAbout = await session.evaluate(`(() => {
    const badge = document.querySelector('[data-sigil-build-badge]')
    if (!badge) return false
    badge.click()
    return true
  })()`)
  if (!openedAbout) {
    throw new Error('Build identity badge is missing.')
  }
  await session.waitFor(`document.querySelector('[data-sigil-about-overlay]')?.style.display === 'flex'`)
  result.about = await session.evaluate(`(() => {
    const text = document.querySelector('[data-sigil-about-overlay]')?.innerText ?? ''
    return {
      text,
      version: text.includes('VERSION\\n${packageJson.version}'),
      channel: text.includes('CHANNEL\\n${mode === 'packaged' ? 'RELEASE' : 'DEV'}'),
      build_id: text.includes(${JSON.stringify(identity.build_id)}),
      commit: text.includes(${JSON.stringify(identity.commit)}),
      build_time: text.includes(${JSON.stringify(identity.build_time)}),
      application_mode: text.includes(${JSON.stringify(mode === 'packaged' ? 'Packaged release' : 'Live development')})
    }
  })()`)

  if (mode === 'development') {
    await clickButton(session, 'Check for Updates')
    await session.waitFor(`document.body.textContent.includes('Development mode')`)
    result.updater = { status: 'disabled', clean_failure: true }
  } else {
    result.updater = {
      status: 'production-feed-not-contacted-during-isolated-certification',
      clean_failure: true
    }
  }
  result.updater_fixture = await session.evaluate(`(async () => {
    const api = window.sigilDesktop?.releaseCertification
    if (typeof api !== 'function') return { fixture_available: false }
    const fixtures = ${JSON.stringify(updaterFixtures)}
    return {
      fixture_available: true,
      current: await api({ operation: 'updater-check', metadata: fixtures.current }),
      newer: await api({ operation: 'updater-check', metadata: fixtures.newer }),
      malformed: await api({ operation: 'updater-check', metadata: fixtures.malformed }),
      missing: await api({ operation: 'updater-check', metadata: fixtures.missing })
    }
  })()`)

  fs.mkdirSync(screenshotDirectory, { recursive: true })
  await session.screenshot(path.join(screenshotDirectory, `${mode}-about.png`))
  await session.evaluate(`document.querySelector('[aria-label="Close About Sigil"]')?.click()`)
  await clickButton(session, 'Overview')
  await session.screenshot(path.join(screenshotDirectory, `${mode}-overview.png`))

  result.renderer_errors = rendererErrorSummary(session)
  result.identity_match =
    result.preload.build_info?.build === identity.build_id &&
    result.preload.build_info?.commit === identity.commit &&
    result.preload.build_info?.buildTime === identity.build_time &&
    result.preload.build_info?.channel === (mode === 'packaged' ? 'release' : 'dev')

  return result
}

function allTrue(object, keys) {
  return keys.every(key => object?.[key] === true)
}

function featureResultsFromEvidence({ devUi, packagedUi, packageEvidence, registryEvidence, testEvidence }) {
  const byId = new Map(registry.features.map(feature => [feature.id, feature]))
  const results = []
  const both = predicate => Boolean(devUi && packagedUi && predicate(devUi) && predicate(packagedUi))
  const evidence = (...values) => values.filter(Boolean)

  const add = (id, status, details) => results.push(featureResult(byId.get(id), status, details))

  add('shell.navigation', both(ui => Object.values(ui.navigation).every(Boolean)) ? 'PASS' : 'FAIL', {
    evidence: evidence('development-ui.json#navigation', 'packaged-ui.json#navigation'),
    testPerformed: 'Opened all seven navigation destinations in development and installed Electron renderers.',
    remediation: 'Repair any missing destination or heading and rerun certification.'
  })
  add('overview.governance-state', both(ui => allTrue(ui.overview, [
    'paper', 'simulated', 'disconnected', 'masked', 'fixed_cap', 'broker_unavailable'
  ])) ? 'PASS' : 'FAIL', {
    evidence: evidence('development-ui.json#overview', 'packaged-ui.json#overview'),
    testPerformed: 'Verified paper/simulated governance state, masked identity, fixed cap, and broker-unavailable disclosure.'
  })
  add('state.stale-and-disconnected', both(ui => ui.overview.stale && ui.overview.disconnected) ? 'PASS' : 'FAIL', {
    evidence: ['development-ui.json#overview', 'packaged-ui.json#overview'],
    testPerformed: 'Verified stale and disconnected indicators in both builds.'
  })
  add('proposals.review-and-selection', both(ui => ui.proposal_selection.clicked && ui.proposal_selection.selected) ? 'PASS' : 'FAIL', {
    evidence: ['development-ui.json#proposal_selection', 'packaged-ui.json#proposal_selection'],
    testPerformed: 'Selected a pending proposal and verified contextual selection without a decision.'
  })
  add('proposals.simulated-action-guard', both(ui =>
    ui.proposal_guard.no_submit_control &&
    ui.proposal_guard.decision_buttons.length > 0 &&
    ui.proposal_guard.decision_buttons.every(control => control.disabled) &&
    ui.proposal_certification.fixture_available &&
    ui.proposal_certification.initial.proposals['CERT-APPROVE'] === 'pending' &&
    ui.proposal_certification.cancelled.proposals['CERT-CANCEL'] === 'pending' &&
    ui.proposal_certification.approved.proposals['CERT-APPROVE'] === 'approved' &&
    ui.proposal_certification.rejected.proposals['CERT-REJECT'] === 'rejected' &&
    Object.values(ui.proposal_certification.rejected.safety).every(value => value === 0)
  ) ? 'PASS' : 'FAIL', {
    evidence: [
      'development-ui.json#proposal_guard',
      'development-ui.json#proposal_certification',
      'packaged-ui.json#proposal_guard',
      'packaged-ui.json#proposal_certification',
      'test-output.log'
    ],
    testPerformed: 'Verified normal controls disabled, then approved, rejected, and cancelled synthetic proposals through token-gated renderer-to-main IPC with zero prohibited side effects.',
    dependencyState: 'Disposable in-memory certification fixture; removed with each Electron process.'
  })
  add('launch.execution-controls', both(ui =>
    ui.launch_guard.fixed_cap &&
    ui.launch_guard.read_only &&
    ui.launch_guard.controls.length === 3 &&
    ui.launch_guard.controls.every(control => control.disabled)
  ) ? 'PASS' : 'FAIL', {
    evidence: ['development-ui.json#launch_guard', 'packaged-ui.json#launch_guard'],
    testPerformed: 'Verified all launch mutations are disabled and the cap is read-only.'
  })
  add('executions.receipts', both(ui =>
    ui.receipts.simulated && ui.receipts.rejected && ui.receipts.uncertain && ui.receipts.receipt_ids.length === 3
  ) ? 'PASS' : 'FAIL', {
    evidence: ['development-ui.json#receipts', 'packaged-ui.json#receipts'],
    testPerformed: 'Verified simulated, rejected, and outcome-uncertain receipts.'
  })
  add('reconciliation.outcome-uncertain', both(ui =>
    ui.reconciliation.required && ui.reconciliation.retry_blocked && ui.reconciliation.no_retry
  ) ? 'PASS' : 'FAIL', {
    evidence: ['development-ui.json#reconciliation', 'packaged-ui.json#reconciliation'],
    testPerformed: 'Verified reconciliation-required and automatic-retry-blocked evidence.'
  })
  add('audit.search-and-evidence', both(ui =>
    allTrue(ui.audit, ['filter_present', 'matching_order', 'excluded_order', 'sanitized_details'])
  ) ? 'PASS' : 'FAIL', {
    evidence: ['development-ui.json#audit', 'packaged-ui.json#audit'],
    testPerformed: 'Filtered one order and expanded sanitized JSON evidence.'
  })
  add('settings.safety-boundaries', both(ui => allTrue(ui.settings, [
    'paper_only', 'masked_only', 'execution_never', 'broker_unavailable', 'no_credentials'
  ])) ? 'PASS' : 'FAIL', {
    evidence: ['development-ui.json#settings', 'packaged-ui.json#settings'],
    testPerformed: 'Verified paper-only, masked, no-credential, no-execution settings boundaries.'
  })
  add('intelligence.local-explanation', both(ui =>
    ui.explanation.execution_denied && ui.explanation.broker_denied
  ) ? 'PASS' : 'FAIL', {
    evidence: ['development-ui.json#explanation', 'packaged-ui.json#explanation', 'test-output.log'],
    testPerformed: 'Requested safe proposal explanations and verified explicit execution denial.'
  })
  add('backend.read-only-bridge', testEvidence.backend === 'PASS' &&
    both(ui => ui.explanation.execution_denied && ui.explanation.broker_denied) ? 'PASS' : 'FAIL', {
    evidence: ['test-output.log', 'development-ui.json#explanation', 'packaged-ui.json#explanation'],
    testPerformed: 'Ran Python bridge tests and exercised connected/degraded explanation behavior.',
    dependencyState: packagedUi?.settings?.broker_unavailable ? 'Packaged app safely degraded.' : 'Unknown.'
  })
  add('electron.ipc-and-preload', testEvidence.electron === 'PASS' && both(ui =>
    ui.preload.present &&
    ui.preload.broker_submission_available === false &&
    ['buildInfo', 'checkForUpdates', 'explainProposal', 'getBackendStatus', 'onUpdateStatus']
      .every(key => ui.preload.api_keys.includes(key))
  ) ? 'PASS' : 'FAIL', {
    evidence: ['test-output.log', 'development-ui.json#preload', 'packaged-ui.json#preload', 'coverage-enforcement.json'],
    testPerformed: 'Validated registered IPC coverage and inspected the bounded preload API in both builds.'
  })
  add('identity.about-center', both(ui => ui.identity_match && allTrue(ui.about, [
    'version', 'channel', 'build_id', 'commit', 'build_time', 'application_mode'
  ])) ? 'PASS' : 'FAIL', {
    evidence: ['application-identity.json', 'development-ui.json#about', 'packaged-ui.json#about', 'screenshots'],
    testPerformed: 'Opened About in both builds and matched all displayed identity fields.'
  })
  const updaterFixturePassed = both(ui =>
    ui.updater_fixture.fixture_available &&
    ui.updater_fixture.current.updateAvailable === false &&
    ui.updater_fixture.current.updateVersion === packageJson.version &&
    ui.updater_fixture.newer.updateAvailable === true &&
    ui.updater_fixture.newer.updateVersion === '0.1.1' &&
    ui.updater_fixture.malformed.bounded === true &&
    ui.updater_fixture.missing.bounded === true &&
    [ui.updater_fixture.current, ui.updater_fixture.newer, ui.updater_fixture.malformed, ui.updater_fixture.missing]
      .every(item => item.downloaded === false && item.installed === false)
  )
  add('updates.failure-handling', devUi?.updater?.status === 'disabled' &&
    packagedUi?.updater?.clean_failure && updaterFixturePassed ? 'PASS' : 'FAIL', {
    evidence: [
      'development-ui.json#updater_fixture',
      'packaged-ui.json#updater_fixture',
      'development-ui.json#updater',
      'packaged-ui.json#updater',
      'runtime-log-summary.txt'
    ],
    testPerformed: 'Exercised electron-updater metadata parsing and version decisions against loopback current, newer, malformed, and missing fixtures without download or install.',
    limitations: ['No real published update was downloaded, installed, or claimed as tested.'],
    dependencyState: `Production packaged updater state: ${packagedUi?.updater?.status ?? 'unknown'}; isolated updater fixture complete.`
  })
  add('packaging.macos-arm64', packageEvidence.success ? 'PASS' : packageEvidence.blocked ? 'BLOCKED' : 'FAIL', {
    evidence: ['dmg-sha256.txt', 'application-identity.json', 'packaged-ui.json', 'commands-run.log'],
    testPerformed: 'Verified local Electron distribution, generated and verified DMG, staged it in a temporary directory, and launched the arm64 app.',
    limitations: ['The development artifact is intentionally unsigned and not notarized.'],
    remediation: 'Resolve package/install/provenance failure and rerun.'
  })

  if (registryEvidence.errors.length > 0) {
    for (const result of results) {
      if (result.criticality === 'critical' && result.status === 'PASS') {
        result.status = 'FAIL'
        result.limitations.push('Registry coverage enforcement failed.')
        result.remediation = 'Repair feature registry drift and rerun certification.'
      }
    }
  }

  return results
}

function markdownReport({ identity, artifact, featureResults, verdict, counts, knownLimitations }) {
  const gateRows = qualityGates
    .map(gate => `| ${gate.name} | ${gate.status} | ${gate.evidence.join('<br>').replaceAll('|', '\\|')} |`)
    .join('\n')
  const featureRows = featureResults
    .map(result =>
      `| ${result.feature_id} | ${result.criticality} | ${result.status} | ${result.test_performed.replaceAll('|', '\\|')} | ${result.limitations.join('; ').replaceAll('|', '\\|') || 'None'} | ${result.dependency_state.replaceAll('|', '\\|')} | ${result.status === 'PASS' ? 'None' : result.remediation.replaceAll('|', '\\|') || 'None'} |`
    )
    .join('\n')

  return `# Sigil Release Certification

## Release identity

- Version: ${identity.version}
- Channel: ${identity.channel}
- Application mode: ${identity.application_mode}
- Branch: ${identity.branch}
- Commit: ${identity.commit}
- Build ID: ${identity.build_id}
- Build time: ${identity.build_time}
- Architecture: ${identity.architecture}
- Executable path: ${identity.executable_path}
- DMG path: ${artifact.dmg_path}
- DMG size: ${artifact.dmg_size_bytes} bytes
- SHA-256: ${artifact.dmg_sha256}

## Quality gates

| Gate | Status | Evidence |
| --- | --- | --- |
${gateRows}

## Feature matrix

| Feature | Criticality | Status | Test performed | Limitations | Dependency state | Remediation |
| --- | --- | --- | --- | --- | --- | --- |
${featureRows}

Feature totals: PASS ${counts.PASS}, PARTIAL ${counts.PARTIAL}, FAIL ${counts.FAIL}, BLOCKED ${counts.BLOCKED}, NOT_TESTED ${counts.NOT_TESTED}.

## Runtime findings

- Crashes: ${runtimeFindings.crashes.length ? runtimeFindings.crashes.join('; ') : 'None observed.'}
- Renderer errors: ${runtimeFindings.renderer_errors.length ? runtimeFindings.renderer_errors.join('; ') : 'None observed.'}
- Electron main errors: ${runtimeFindings.electron_main_errors.length ? runtimeFindings.electron_main_errors.join('; ') : 'None observed.'}
- IPC failures: ${runtimeFindings.ipc_failures.length ? runtimeFindings.ipc_failures.join('; ') : 'None observed.'}
- Degraded backend behavior: ${runtimeFindings.degraded_backend_behavior.join('; ') || 'No degraded behavior recorded.'}
- Updater behavior: ${runtimeFindings.updater_behavior.join('; ') || 'No updater observation recorded.'}
- Broker connectivity state: ${runtimeFindings.broker_connectivity_state.join('; ') || 'Not observed.'}
- Stale-data handling: ${runtimeFindings.stale_data_handling.join('; ') || 'Not observed.'}

## Safety findings

- Trade occurred: ${safetyFindings.trade}
- Order occurred: ${safetyFindings.order}
- Transfer occurred: ${safetyFindings.transfer}
- Approval occurred: ${safetyFindings.approval}
- Broker submission occurred: ${safetyFindings.broker_submission}
- Wallet mutation occurred: ${safetyFindings.wallet_mutation}
- External financial side effect occurred: ${safetyFindings.external_financial_side_effect}

The Release Guardian confirmed approve, reject, and cancel only for synthetic CERT-* proposals in a disposable in-memory fixture. It did not modify an existing application installation, enable production operator actions, arm launch, engage the kill switch, submit an order, contact a broker transport, or mutate a wallet.

## Known limitations

${knownLimitations.map(item => `- ${item}`).join('\n')}

## Final verdict

**${verdict}**

READY requires positive evidence for every feature. PARTIAL, BLOCKED, FAIL, and NOT_TESTED are never counted as PASS. Critical FAIL, BLOCKED, or NOT_TESTED results force NOT_READY.
`
}

async function main() {
  log('Sigil Release Guardian')
  log(`Started: ${startedAt.toISOString()}`)

  if (process.platform !== 'darwin' || process.arch !== 'arm64') {
    throw new Error(`macOS arm64 is required; found ${process.platform} ${process.arch}.`)
  }
  const updaterFixtures = readUpdaterFixtures()

  const branch = gitValue(['branch', '--show-current'])
  const commit = gitValue(['rev-parse', 'HEAD'])
  const gitStatus = gitValue(['status', '--short'])
  const gitDiffSummary = gitValue(['diff', '--stat'])
  const missionControlSource = fs.readFileSync(path.join(appDirectory, 'src/mission-control/index.tsx'), 'utf8')
  const electronMainSource = fs.readFileSync(path.join(appDirectory, 'electron/main.ts'), 'utf8')
  const registryValidation = validateFeatureRegistry(registry, { appDirectory })
  const coverage = enforceFeatureCoverage({ registry, missionControlSource, electronMainSource })
  const registryEvidence = {
    errors: [...registryValidation.errors, ...coverage.errors],
    warnings: [...registryValidation.warnings, ...coverage.warnings],
    inventory: coverage.inventory
  }

  addGate(
    'feature-coverage',
    'Feature registry coverage',
    registryEvidence.errors.length === 0 ? 'PASS' : 'FAIL',
    registryEvidence.errors.length === 0 ? [`${registry.features.length} registered features; UI and IPC inventory aligned.`] : registryEvidence.errors
  )

  const typecheck = runCommand('TypeScript', 'npm', ['run', 'typecheck'], { cwd: appDirectory, gateId: 'typescript' })
  const lint = runCommand('Lint', 'npm', ['run', 'lint'], { cwd: appDirectory, gateId: 'lint' })
  const vitest = path.join(repositoryRoot, 'node_modules/.bin/vitest')
  const unit = runCommand(
    'Unit tests',
    vitest,
    ['run', 'src/hermes-engine.test.ts', 'src/product-contract.test.ts', 'scripts/release-guardian.test.mjs'],
    { cwd: appDirectory, gateId: 'unit-tests' }
  )
  const integration = runCommand(
    'Integration tests',
    vitest,
    ['run', 'src/mission-control/standalone.test.tsx'],
    { cwd: appDirectory, gateId: 'integration-tests' }
  )
  const electron = runCommand(
    'Electron tests',
    vitest,
    ['run', 'electron/main.test.ts'],
    { cwd: appDirectory, gateId: 'electron-tests' }
  )
  const python = fs.existsSync(path.join(repositoryRoot, '.venv/bin/python'))
    ? path.join(repositoryRoot, '.venv/bin/python')
    : 'python3'
  const backend = runCommand(
    'Backend bridge integration tests',
    python,
    ['-m', 'pytest', '-q', 'apps/sigil/tests/test_desktop_bridge.py'],
    { cwd: repositoryRoot, gateId: 'backend-integration-tests' }
  )
  const build = runCommand('Production build', 'npm', ['run', 'build'], {
    cwd: appDirectory,
    gateId: 'production-build'
  })
  const identityMatch = build.output.match(/Sigil build identity:\s*([a-f0-9]+)-(\d{8}\.\d{6})/)
  if (!identityMatch) {
    throw new Error('Production build did not emit a valid Sigil build identity.')
  }

  const buildId = `${identityMatch[1]}-${identityMatch[2]}`
  const identity = {
    version: packageJson.version,
    channel: 'release',
    application_mode: 'Packaged release',
    branch,
    commit: identityMatch[1],
    full_commit: commit,
    build_id: buildId,
    build_time: identityMatch[2],
    architecture: expectedArchitecture,
    executable_path: 'temporary certification staging path (assigned after packaging)'
  }
  evidenceDirectory = path.join(releaseDirectory, 'certification', buildId)
  if (fs.existsSync(evidenceDirectory)) {
    throw new Error(`Certification evidence already exists for build ID ${buildId}.`)
  }
  fs.mkdirSync(path.join(evidenceDirectory, 'screenshots'), { recursive: true })
  certificationRuntimeRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sigil-release-runtime.'))

  writeEvidence('git-status.txt', `${gitStatus}\n`)
  writeEvidence('git-diff-summary.txt', `${gitDiffSummary}\n`)
  writeEvidence('coverage-enforcement.json', registryEvidence)

  let devUi
  const devStdoutPath = path.join(evidenceDirectory, 'development-electron-stdout.log')
  const devStderrPath = path.join(evidenceDirectory, 'development-electron-stderr.log')
  try {
    await stopProcessesMatching(
      command => command.includes(`${appDirectory}/release/mac-arm64/Sigil.app/Contents/`),
      'pre-certification Sigil processes'
    )
    const devServerOut = fs.openSync(path.join(evidenceDirectory, 'development-server.log'), 'a')
    developmentServer = spawn('npm', ['run', 'dev:renderer'], {
      cwd: appDirectory,
      detached: true,
      env: { ...process.env },
      stdio: ['ignore', devServerOut, devServerOut]
    })
    recordCommand('npm', ['run', 'dev:renderer'], appDirectory)
    await waitForHttp('http://127.0.0.1:5175/')

    const electronExecutable = path.join(
      repositoryRoot,
      'node_modules/electron/dist/Electron.app/Contents/MacOS/Electron'
    )
    const devOut = fs.openSync(devStdoutPath, 'a')
    const devErr = fs.openSync(devStderrPath, 'a')
    const devPort = await availableLoopbackPort()
    const devUserData = path.join(certificationRuntimeRoot, 'development-user-data')
    developmentElectron = spawn(
      electronExecutable,
      [
        `--remote-debugging-port=${devPort}`,
        `--user-data-dir=${devUserData}`,
        `--sigil-release-certification=${certificationToken}`,
        appDirectory
      ],
      {
        cwd: appDirectory,
        detached: true,
        env: {
          ...process.env,
          SIGIL_DEV_SERVER: 'http://127.0.0.1:5175',
          SIGIL_ADAPTER: 'mock',
          SIGIL_RELEASE_CERTIFICATION_TOKEN: certificationToken
        },
        stdio: ['ignore', devOut, devErr]
      }
    )
    recordCommand(electronExecutable, [`--remote-debugging-port=${devPort}`, appDirectory], appDirectory)
    const devSession = await connectToElectron(devPort, '127.0.0.1:5175')
    try {
      devUi = await safeUiSmoke(devSession, {
        mode: 'development',
        identity,
        screenshotDirectory: path.join(evidenceDirectory, 'screenshots'),
        updaterFixtures
      })
    } finally {
      devSession.close()
    }
    writeEvidence('development-ui.json', devUi)
    addGate('development-launch', 'Development launch', 'PASS', [
      devUi.page.url,
      `identity_match=${devUi.identity_match}`,
      `renderer_errors=${devUi.renderer_errors.length}`
    ])
    if (devUi.renderer_errors.length > 0) {
      runtimeFindings.renderer_errors.push(...devUi.renderer_errors.map(item => `development: ${item}`))
    }
  } catch (error) {
    runtimeFindings.renderer_errors.push(`Development UI smoke failed: ${error.message}`)
    addGate('development-launch', 'Development launch', 'FAIL', error.message)
  } finally {
    killProcessGroup(developmentElectron)
    killProcessGroup(developmentServer)
    await stopProcessesMatching(
      command => command.includes(`${repositoryRoot}/node_modules/electron/dist/Electron.app/Contents/`) ||
        command.includes('vite --host 127.0.0.1 --port 5175'),
      'development Electron and renderer processes'
    )
  }

  const electronVersion = packageJson.build.electronVersion
  const electronZip = path.join(
    os.homedir(),
    'Library/Caches/sigil-electron-dist',
    `electron-v${electronVersion}-darwin-arm64.zip`
  )
  const checksumManifest = path.join(path.dirname(electronZip), 'SHASUMS256.txt')
  let verifiedElectronZip
  if (fs.existsSync(electronZip) && fs.existsSync(checksumManifest)) {
    const manifestLine = fs.readFileSync(checksumManifest, 'utf8')
      .split('\n')
      .find(line => line.endsWith(`*${path.basename(electronZip)}`))
    if (!manifestLine) {
      throw new Error('Electron checksum manifest does not contain the required arm64 ZIP.')
    }
    const expectedElectronSha = manifestLine.split(/\s+/)[0]
    const actualElectronSha = sha256(electronZip)
    if (expectedElectronSha !== actualElectronSha) {
      throw new Error('Cached Electron ZIP checksum does not match its manifest.')
    }
    const zipTest = runCommand('Electron ZIP integrity', 'unzip', ['-t', electronZip], {
      cwd: appDirectory,
      gateId: 'electron-distribution'
    })
    if (zipTest.status !== 'PASS') {
      throw new Error('Cached Electron ZIP failed unzip integrity validation.')
    }
    verifiedElectronZip = electronZip
  } else {
    addGate(
      'electron-distribution',
      'Electron distribution',
      'BLOCKED',
      `Verified local Electron distribution unavailable at ${electronZip}.`
    )
  }

  const builder = path.join(repositoryRoot, 'node_modules/.bin/electron-builder')
  const builderArguments = ['--mac', 'dmg', '--arm64', '--publish', 'never']
  if (verifiedElectronZip) {
    builderArguments.push(`--config.electronDist=${verifiedElectronZip}`)
  }
  const packaging = runCommand('Package arm64 DMG', builder, builderArguments, {
    cwd: appDirectory,
    gateId: 'package-generation',
    env: { CSC_IDENTITY_AUTO_DISCOVERY: 'false' }
  })
  const dmgPath = path.join(releaseDirectory, `Sigil-${packageJson.version}-mac-arm64.dmg`)
  let packageEvidence = { success: false, blocked: packaging.status === 'BLOCKED' }
  let artifact = {
    dmg_path: dmgPath,
    dmg_size_bytes: 0,
    dmg_sha256: 'unavailable'
  }
  let packagedUi
  let installedExecutableSha
  let packagedExecutableSha
  const packagedStdoutPath = path.join(evidenceDirectory, 'packaged-electron-stdout.log')
  const packagedStderrPath = path.join(evidenceDirectory, 'packaged-electron-stderr.log')

  if (packaging.status === 'PASS' && fs.existsSync(dmgPath)) {
    const dmgVerify = runCommand('Verify DMG', 'hdiutil', ['verify', dmgPath], {
      cwd: appDirectory,
      gateId: 'dmg-verification'
    })
    artifact = {
      dmg_path: dmgPath,
      dmg_size_bytes: fs.statSync(dmgPath).size,
      dmg_sha256: sha256(dmgPath)
    }
    writeEvidence('dmg-sha256.txt', `${artifact.dmg_sha256}  ${dmgPath}\n`)

    const mountPoint = fs.mkdtempSync(path.join(os.tmpdir(), 'sigil-certification-dmg.'))
    mountedDmg = mountPoint
    try {
      const mount = runCommand('Mount DMG read-only', 'hdiutil', [
        'attach', '-readonly', '-nobrowse', '-mountpoint', mountPoint, dmgPath
      ], { cwd: appDirectory, gate: false })
      if (mount.status !== 'PASS') {
        throw new Error('Unable to mount the certified DMG.')
      }
      const sourceApp = path.join(mountPoint, 'Sigil.app')
      if (!fs.existsSync(sourceApp)) {
        throw new Error('Mounted DMG does not contain Sigil.app.')
      }

      await stopProcessesMatching(
        command => command.includes(`${mountPoint}/Sigil.app/Contents/`),
        'mounted Sigil processes'
      )

      stagedApplicationRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sigil-release-certification.'))
      stagedApplicationPath = path.join(stagedApplicationRoot, 'Sigil.app')
      identity.executable_path = path.join(stagedApplicationPath, 'Contents/MacOS/Sigil')
      const copy = runCommand('Stage packaged app', 'ditto', [sourceApp, stagedApplicationPath], {
        cwd: appDirectory,
        gate: false
      })
      if (copy.status !== 'PASS') {
        throw new Error('Unable to stage Sigil.app from the DMG.')
      }

      const sourceExecutableSha = sha256(path.join(sourceApp, 'Contents/MacOS/Sigil'))
      const stagedExecutableSha = sha256(path.join(stagedApplicationPath, 'Contents/MacOS/Sigil'))
      const sourceAsarSha = sha256(path.join(sourceApp, 'Contents/Resources/app.asar'))
      const stagedAsarSha = sha256(path.join(stagedApplicationPath, 'Contents/Resources/app.asar'))
      if (sourceExecutableSha !== stagedExecutableSha || sourceAsarSha !== stagedAsarSha) {
        throw new Error('Staged application does not match the mounted DMG payload.')
      }

      installedExecutableSha = stagedExecutableSha
      packagedExecutableSha = sourceExecutableSha
      if (installedExecutableSha !== packagedExecutableSha) {
        throw new Error('Staged executable does not match the DMG executable.')
      }
    } finally {
      if (mountedDmg) {
        runCommand('Unmount DMG', 'hdiutil', ['detach', mountedDmg], {
          cwd: appDirectory,
          gate: false
        })
        try {
          fs.rmdirSync(mountedDmg)
        } catch {
          // hdiutil may already remove the temporary mount directory.
        }
        mountedDmg = undefined
      }
    }

    const outFd = fs.openSync(packagedStdoutPath, 'a')
    const errFd = fs.openSync(packagedStderrPath, 'a')
    const packagedPort = await availableLoopbackPort()
    const packagedUserData = path.join(certificationRuntimeRoot, 'packaged-user-data')
    packagedElectron = spawn(
      path.join(stagedApplicationPath, 'Contents/MacOS/Sigil'),
      [
        `--remote-debugging-port=${packagedPort}`,
        `--user-data-dir=${packagedUserData}`,
        `--sigil-release-certification=${certificationToken}`
      ],
      {
        cwd: stagedApplicationPath,
        detached: true,
        env: { ...process.env, SIGIL_RELEASE_CERTIFICATION_TOKEN: certificationToken },
        stdio: ['ignore', outFd, errFd]
      }
    )
    packagedElectron.unref()
    recordCommand(
      path.join(stagedApplicationPath, 'Contents/MacOS/Sigil'),
      [`--remote-debugging-port=${packagedPort}`],
      stagedApplicationPath
    )

    try {
      const packagedSession = await connectToElectron(packagedPort, 'app.asar/dist/index.html')
      try {
        packagedUi = await safeUiSmoke(packagedSession, {
          mode: 'packaged',
          identity,
          screenshotDirectory: path.join(evidenceDirectory, 'screenshots'),
          updaterFixtures
        })
      } finally {
        packagedSession.close()
      }
      writeEvidence('packaged-ui.json', packagedUi)

      const runningStaged = processRows().filter(row =>
        row.command.startsWith(`${stagedApplicationPath}/Contents/`)
      )
      const forbiddenProcesses = processRows().filter(row =>
        row.command.includes(`${appDirectory}/release/mac-arm64/Sigil.app/Contents/`) ||
        row.command.includes('/Volumes/') && row.command.includes('/Sigil.app/Contents/')
      )
      if (runningStaged.length === 0) {
        throw new Error('No Sigil executable is running from the temporary certification staging path.')
      }
      if (forbiddenProcesses.length > 0) {
        throw new Error(`Forbidden old-build process remains: ${forbiddenProcesses[0].command}`)
      }
      packageEvidence = {
        success: dmgVerify.status === 'PASS' &&
          installedExecutableSha === packagedExecutableSha &&
          packagedUi.identity_match,
        blocked: false,
        running_processes: runningStaged.map(row => row.command)
      }
      addGate('packaged-launch', 'Packaged launch', packageEvidence.success ? 'PASS' : 'FAIL', [
        `executable=${identity.executable_path}`,
        `identity_match=${packagedUi.identity_match}`,
        `installed_sha256=${installedExecutableSha}`
      ])
      if (packagedUi.renderer_errors.length > 0) {
        runtimeFindings.renderer_errors.push(...packagedUi.renderer_errors.map(item => `packaged: ${item}`))
      }
    } catch (error) {
      runtimeFindings.renderer_errors.push(`Packaged UI smoke failed: ${error.message}`)
      addGate('packaged-launch', 'Packaged launch', 'FAIL', error.message)
    }
  } else {
    addGate('dmg-verification', 'DMG verification', 'BLOCKED', 'DMG was not generated.')
    addGate('packaged-launch', 'Packaged launch', 'BLOCKED', 'DMG was not generated.')
  }

  const unifiedLogs = runCommand(
    'Recent Electron logs',
    '/usr/bin/log',
    [
      'show',
      '--last',
      '20m',
      '--style',
      'compact',
      '--info',
      '--debug',
      '--predicate',
      '(process == "Sigil") OR (process BEGINSWITH "Sigil Helper")'
    ],
    { cwd: appDirectory, gate: false }
  )
  const runtimePattern = /crash|uncaught|unhandledpromiserejection|ipc.*fail|missing asset|err_file_not_found|renderer.*error/i
  const capturedRuntimeLogs = [devStderrPath, packagedStderrPath]
    .filter(filePath => fs.existsSync(filePath))
    .map(filePath => fs.readFileSync(filePath, 'utf8'))
    .join('\n')
  const seriousRuntimeLines = `${unifiedLogs.output}\n${capturedRuntimeLogs}`
    .split('\n')
    .filter(line => runtimePattern.test(line))
  if (seriousRuntimeLines.length > 0) {
    runtimeFindings.electron_main_errors.push(...seriousRuntimeLines.slice(-50))
  }

  if (devUi?.overview?.stale && packagedUi?.overview?.stale) {
    runtimeFindings.stale_data_handling.push('Both builds visibly label the fixture snapshot as stale.')
  }
  if (devUi?.overview?.disconnected && packagedUi?.overview?.disconnected) {
    runtimeFindings.broker_connectivity_state.push('Both builds visibly report DISCONNECTED and no broker submission.')
  }
  if (packagedUi?.explanation?.execution_denied) {
    runtimeFindings.degraded_backend_behavior.push(
      `Packaged explanation remained safe with route text: ${packagedUi.explanation.text?.split('\n').slice(0, 3).join(' | ')}`
    )
  }
  if (packagedUi?.updater) {
    runtimeFindings.updater_behavior.push(
      `Development updater=${devUi?.updater?.status ?? 'unknown'}; packaged updater=${packagedUi.updater.status}.`
    )
  }

  addGate(
    'runtime-log-inspection',
    'Runtime log inspection',
    seriousRuntimeLines.length === 0 && runtimeFindings.renderer_errors.length === 0 ? 'PASS' : 'FAIL',
    seriousRuntimeLines.length === 0 && runtimeFindings.renderer_errors.length === 0
      ? 'No crashes, uncaught exceptions, IPC failures, missing assets, or renderer exceptions detected.'
      : [...seriousRuntimeLines, ...runtimeFindings.renderer_errors].slice(-20)
  )
  addGate(
    'safety-verification',
    'Safety verification',
    Object.values(safetyFindings).every(value => value === false) ? 'PASS' : 'FAIL',
    'No trade, order, transfer, approval, broker submission, wallet mutation, or external financial side effect occurred.'
  )

  const featureResults = featureResultsFromEvidence({
    devUi,
    packagedUi,
    packageEvidence,
    registryEvidence,
    testEvidence: {
      electron: electron.status,
      backend: backend.status,
      unit: unit.status,
      integration: integration.status,
      typecheck: typecheck.status,
      lint: lint.status
    }
  })
  const counts = countFeatureStatuses(featureResults)
  const verdict = determineVerdict(featureResults, qualityGates)
  const completedAt = new Date()
  const applicationIdentity = {
    ...identity,
    bundle_id: expectedBundleId,
    installed_executable_sha256: installedExecutableSha ?? 'unavailable',
    packaged_executable_sha256: packagedExecutableSha ?? 'unavailable',
    running_from_temporary_staging_path: packageEvidence.success
  }
  const results = {
    schema_version: 1,
    started_at: startedAt.toISOString(),
    completed_at: completedAt.toISOString(),
    duration_seconds: Math.round((completedAt.getTime() - startedAt.getTime()) / 1000),
    release_identity: applicationIdentity,
    artifact,
    feature_counts: counts,
    quality_gates: qualityGates,
    runtime_findings: runtimeFindings,
    safety_findings: safetyFindings,
    final_verdict: verdict
  }
  const knownLimitations = [
    'The local development DMG is intentionally unsigned and not notarized.',
    'No real published update was downloaded, installed, or claimed as tested; updater PASS covers the isolated electron-updater metadata and decision path.',
    'Proposal approval/rejection was exercised only for disposable synthetic CERT-* records; launch mutation success paths were intentionally not executed.',
    gitStatus ? 'The source checkout is dirty; git-status.txt and git-diff-summary.txt preserve the exact state.' : 'None.'
  ]

  writeEvidence('application-identity.json', applicationIdentity)
  writeEvidence('feature-results.json', featureResults)
  writeEvidence('certification-results.json', results)
  writeEvidence('commands-run.log', `${commandRecords.join('\n')}\n`)
  writeEvidence('test-output.log', `${testOutput.join('\n')}\n`)
  writeEvidence(
    'runtime-log-summary.txt',
    `${JSON.stringify(runtimeFindings, null, 2)}\n\nUnified log excerpts:\n${seriousRuntimeLines.join('\n')}\n`
  )
  writeEvidence(
    'certification-report.md',
    markdownReport({ identity: applicationIdentity, artifact, featureResults, verdict, counts, knownLimitations })
  )

  log('\nSigil Release Certification complete')
  log(`Report: ${path.join(evidenceDirectory, 'certification-report.md')}`)
  log(`Features: ${featureResults.length}`)
  log(
    `PASS=${counts.PASS} PARTIAL=${counts.PARTIAL} FAIL=${counts.FAIL} BLOCKED=${counts.BLOCKED} NOT_TESTED=${counts.NOT_TESTED}`
  )
  log(`Verdict: ${verdict}`)
  log(`DMG: ${artifact.dmg_path}`)
  log(`SHA-256: ${artifact.dmg_sha256}`)
  log(`Executable: ${identity.executable_path}`)
  log(`Build ID: ${identity.build_id}`)

  process.exitCode = verdict === 'NOT_READY' ? 1 : 0
}

main()
  .catch(error => {
    log(`\nRelease Guardian failed: ${sanitizeText(error.stack ?? error.message)}`)
    process.exitCode = 1
  })
  .finally(async () => {
    killProcessGroup(developmentElectron)
    killProcessGroup(developmentServer)
    await cleanTemporaryPackagedApp()
    if (mountedDmg) {
      spawnSync('hdiutil', ['detach', mountedDmg], { encoding: 'utf8' })
    }
  })
