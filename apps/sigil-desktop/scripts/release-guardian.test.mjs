import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'
import provider from 'electron-updater/out/providers/Provider.js'
import semver from 'semver'

import {
  countFeatureStatuses,
  determineVerdict,
  enforceFeatureCoverage,
  readJson,
  sanitizeText,
  validateFeatureRegistry
} from './release-guardian-lib.mjs'

const appDirectory = path.resolve('.')
const registry = readJson(path.join(appDirectory, 'release-certification/features.json'))
const missionControlSource = fs.readFileSync(path.join(appDirectory, 'src/mission-control/index.tsx'), 'utf8')
const electronMainSource = fs.readFileSync(path.join(appDirectory, 'electron/main.ts'), 'utf8')

describe('Sigil Release Guardian', () => {
  it('keeps the feature registry structurally valid with real source and test references', () => {
    expect(validateFeatureRegistry(registry, { appDirectory })).toEqual({ errors: [], warnings: [] })
  })

  it('covers every visible navigation destination, guarded control, and IPC handler', () => {
    const coverage = enforceFeatureCoverage({ registry, missionControlSource, electronMainSource })

    expect(coverage.errors).toEqual([])
    expect(coverage.inventory.navigation_labels).toEqual([
      'Overview',
      'Proposals',
      'Launch',
      'Executions',
      'Reconciliation',
      'Audit',
      'Settings'
    ])
    expect(coverage.inventory.ipc_channels).toEqual([
      'sigil:get-backend-status',
      'sigil:explain-proposal',
      'sigil:get-runtime-snapshot',
      'sigil:control-paper-cycle',
      'sigil:check-for-updates',
      'sigil:release-certification'
    ])
  })

  it('never treats critical FAIL, BLOCKED, or NOT_TESTED as ready', () => {
    for (const status of ['FAIL', 'BLOCKED', 'NOT_TESTED']) {
      expect(determineVerdict([{ criticality: 'critical', status }])).toBe('NOT_READY')
    }
  })

  it('keeps partial certification distinct from pass and produces warnings', () => {
    const results = [
      { criticality: 'critical', status: 'PASS' },
      { criticality: 'important', status: 'PARTIAL' }
    ]

    expect(countFeatureStatuses(results)).toEqual({
      PASS: 1,
      PARTIAL: 1,
      FAIL: 0,
      BLOCKED: 0,
      NOT_TESTED: 0
    })
    expect(determineVerdict(results)).toBe('READY_WITH_WARNINGS')
  })

  it('redacts common credential and account patterns from evidence', () => {
    const sanitized = sanitizeText(
      'token=sensitive-value authorization: SensitiveHeader account_id=live-account-123 credential=examplecredential'
    )

    expect(sanitized).not.toContain('sensitive-value')
    expect(sanitized).not.toContain('SensitiveHeader')
    expect(sanitized).not.toContain('live-account-123')
    expect(sanitized).not.toContain('examplecredential')
  })

  it('parses deterministic updater fixtures and derives version decisions from evidence', () => {
    const parse = name =>
      provider.parseUpdateInfo(
        fs.readFileSync(path.join(appDirectory, `release-certification/updater-fixtures/${name}/latest-mac.yml`), 'utf8'),
        'latest-mac.yml',
        new URL('file:///release-certification/latest-mac.yml')
      )
    const current = parse('current')
    const newer = parse('newer')

    expect(current.version).toBe('0.1.0')
    expect(semver.gt(current.version, '0.1.0')).toBe(false)
    expect(newer.version).toBe('0.1.1')
    expect(semver.gt(newer.version, '0.1.0')).toBe(true)
    expect(() => semver.gt(parse('malformed').version, '0.1.0')).toThrow()
  })

  it('keeps certification IPC token-gated, synthetic, in-memory, and side-effect-free', () => {
    expect(electronMainSource).toContain("['CERT-APPROVE', 'pending']")
    expect(electronMainSource).toContain("['CERT-REJECT', 'pending']")
    expect(electronMainSource).toContain("['CERT-CANCEL', 'pending']")
    expect(electronMainSource).toContain("token === process.env.SIGIL_RELEASE_CERTIFICATION_TOKEN")
    expect(electronMainSource).toContain("new Map([")
    expect(electronMainSource).toContain("persistent_financial_mutation: 0")
    expect(electronMainSource).toContain("external_network: 0")
    expect(electronMainSource).not.toContain("SIGIL_RELEASE_CERTIFICATION_TOKEN ??")
  })

  it('stages packaged certification in a temporary directory without replacing an installed app', () => {
    const certificationSource = fs.readFileSync(path.join(appDirectory, 'scripts/certify-release.mjs'), 'utf8')

    expect(certificationSource).toContain("fs.mkdtempSync(path.join(os.tmpdir(), 'sigil-release-certification.'))")
    expect(certificationSource).not.toContain("'/Applications/Sigil.app'")
    expect(certificationSource).not.toContain('backupPath')
  })
})
