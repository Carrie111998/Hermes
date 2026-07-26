import fs from 'node:fs'
import path from 'node:path'

export const FEATURE_STATUSES = Object.freeze(['PASS', 'PARTIAL', 'FAIL', 'BLOCKED', 'NOT_TESTED'])
export const VERDICTS = Object.freeze(['READY', 'READY_WITH_WARNINGS', 'NOT_READY'])
const ANSI_ESCAPE_PATTERN = new RegExp(String.raw`\u001B\[[0-?]*[ -/]*[@-~]`, 'g')

const REQUIRED_FEATURE_FIELDS = Object.freeze([
  'id',
  'name',
  'description',
  'criticality',
  'source_files',
  'expected_user_behavior',
  'automated_test_references',
  'development_build_test_method',
  'packaged_build_test_method',
  'required_backend_or_dependency',
  'safe_fallback_behavior',
  'prohibited_side_effects',
  'current_certification_status'
])

export function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'))
}

export function sanitizeText(value) {
  return String(value)
    .replace(ANSI_ESCAPE_PATTERN, '')
    .replace(
      /((?:api[_-]?key|token|secret|password|authorization|credential|private[_-]?key)\s*[:=]\s*)([^\s,;"']+)/gi,
      '$1[REDACTED]'
    )
    .replace(/\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}\b/g, '[REDACTED_TOKEN]')
    .replace(/\b\d{12,19}\b/g, '[REDACTED_NUMBER]')
    .replace(/(account(?:_id)?\s*[:=]\s*)(?!masked|unavailable)[^\s,;"']+/gi, '$1[REDACTED_ACCOUNT]')
}

export function validateFeatureRegistry(registry, { appDirectory }) {
  const errors = []
  const warnings = []

  if (registry.schema_version !== 1) {
    errors.push('Feature registry schema_version must be 1.')
  }

  if (!Array.isArray(registry.features) || registry.features.length === 0) {
    errors.push('Feature registry must contain at least one feature.')
    return { errors, warnings }
  }

  const ids = new Set()

  for (const feature of registry.features) {
    for (const field of REQUIRED_FEATURE_FIELDS) {
      if (!(field in feature)) {
        errors.push(`${feature.id ?? '<unknown>'}: missing required field ${field}.`)
      }
    }

    if (ids.has(feature.id)) {
      errors.push(`Duplicate feature ID: ${feature.id}.`)
    }
    ids.add(feature.id)

    if (!['critical', 'important', 'informational'].includes(feature.criticality)) {
      errors.push(`${feature.id}: invalid criticality ${feature.criticality}.`)
    }

    if (!FEATURE_STATUSES.includes(feature.current_certification_status)) {
      errors.push(`${feature.id}: invalid status ${feature.current_certification_status}.`)
    }

    if (!Array.isArray(feature.automated_test_references)) {
      errors.push(`${feature.id}: automated_test_references must be an array.`)
    } else if (feature.criticality === 'critical' && feature.automated_test_references.length === 0) {
      errors.push(`${feature.id}: critical features require an automated test reference.`)
    }

    for (const relativePath of [...(feature.source_files ?? []), ...(feature.automated_test_references ?? [])]) {
      const resolved = path.resolve(appDirectory, relativePath)
      if (!fs.existsSync(resolved)) {
        errors.push(`${feature.id}: referenced file does not exist: ${relativePath}.`)
      }
    }
  }

  return { errors, warnings }
}

function parseSectionLabels(source) {
  const block = source.match(/const SECTION_LABELS:[\s\S]*?=\s*\{([\s\S]*?)\n\}/)?.[1] ?? ''
  return [...block.matchAll(/^\s*\w+:\s*'([^']+)'/gm)].map(match => match[1])
}

function parseIpcChannels(source) {
  return [...source.matchAll(/SIGIL_[A-Z_]+_CHANNEL\s*=\s*'([^']+)'/g)].map(match => match[1])
}

export function enforceFeatureCoverage({ registry, missionControlSource, electronMainSource }) {
  const errors = []
  const warnings = []
  const navigationLabels = parseSectionLabels(missionControlSource)
  const registeredNavigation = new Set(
    registry.features.map(feature => feature.ui?.navigation_label).filter(Boolean)
  )

  for (const label of navigationLabels) {
    if (!registeredNavigation.has(label)) {
      errors.push(`Visible navigation destination is absent from registry: ${label}.`)
    }
  }

  for (const label of registeredNavigation) {
    if (!navigationLabels.includes(label)) {
      errors.push(`Registry navigation destination is absent from UI: ${label}.`)
    }
  }

  const ipcChannels = [...new Set(parseIpcChannels(electronMainSource))]
  const registeredIpc = new Set(registry.features.flatMap(feature => feature.ipc_channels ?? []))

  for (const channel of ipcChannels) {
    if (!registeredIpc.has(channel)) {
      errors.push(`Electron IPC handler is absent from registry: ${channel}.`)
    }
  }

  for (const channel of registeredIpc) {
    if (!ipcChannels.includes(channel)) {
      errors.push(`Registry IPC channel is absent from Electron main: ${channel}.`)
    }
  }

  const guardedControls = [
    'Enable simulated operator actions',
    'Reject',
    'Approve',
    'Arm simulated launch',
    'Suspend',
    'Engage kill switch',
    'Explain selected proposal'
  ]
  const registeredControls = new Set(registry.features.flatMap(feature => feature.ui?.controls ?? []))

  for (const control of guardedControls) {
    if (missionControlSource.includes(control) && !registeredControls.has(control)) {
      errors.push(`Visible control is absent from registry: ${control}.`)
    }
  }

  return {
    errors,
    warnings,
    inventory: {
      navigation_labels: navigationLabels,
      ipc_channels: ipcChannels,
      guarded_controls: guardedControls.filter(control => missionControlSource.includes(control))
    }
  }
}

export function countFeatureStatuses(results) {
  const counts = Object.fromEntries(FEATURE_STATUSES.map(status => [status, 0]))

  for (const result of results) {
    if (!FEATURE_STATUSES.includes(result.status)) {
      throw new Error(`Invalid feature result status: ${result.status}`)
    }
    counts[result.status] += 1
  }

  return counts
}

export function determineVerdict(featureResults, qualityGates = []) {
  const criticalStop = featureResults.some(
    feature =>
      feature.criticality === 'critical' && ['FAIL', 'BLOCKED', 'NOT_TESTED'].includes(feature.status)
  )
  const failedGate = qualityGates.some(gate => gate.required !== false && ['FAIL', 'BLOCKED'].includes(gate.status))

  if (criticalStop || failedGate) {
    return 'NOT_READY'
  }

  const warning = featureResults.some(feature => feature.status !== 'PASS') ||
    qualityGates.some(gate => gate.status !== 'PASS')

  return warning ? 'READY_WITH_WARNINGS' : 'READY'
}

export function featureResult(feature, status, details = {}) {
  if (!FEATURE_STATUSES.includes(status)) {
    throw new Error(`Invalid feature status ${status} for ${feature.id}.`)
  }

  return {
    feature_id: feature.id,
    name: feature.name,
    criticality: feature.criticality,
    status,
    evidence: details.evidence ?? [],
    test_performed: details.testPerformed ?? '',
    limitations: details.limitations ?? [],
    dependency_state: details.dependencyState ?? 'available',
    remediation: details.remediation ?? ''
  }
}
