/**
 * Pure helpers for choosing a remote URL during passive update checks.
 *
 * Passive checks follow the checkout's origin. Known SSH remotes are converted
 * to their HTTPS equivalent for read-only probes so a background check does
 * not unexpectedly ask for an SSH hardware-key touch. Active update/apply
 * flows still use the configured `origin` remote.
 *
 * Extracted from main.ts so the security-critical remote detection is unit
 * testable without booting Electron (main.ts requires('electron') at load).
 */

const OFFICIAL_REPO_HTTPS_URL = 'https://github.com/NousResearch/hermes-agent.git'
const OFFICIAL_REPO_CANONICAL = 'github.com/nousresearch/hermes-agent'

// Normalize common GitHub remote URL forms to `host/owner/repo` (lowercased,
// no trailing slash, no .git suffix) so SSH and HTTPS forms of the same repo
// compare equal.
function canonicalGitHubRemote(url) {
  if (!url) {
    return ''
  }

  let value = String(url).trim()

  if (value.startsWith('git@github.com:')) {
    value = `github.com/${value.slice('git@github.com:'.length)}`
  } else if (value.startsWith('ssh://git@github.com/')) {
    value = `github.com/${value.slice('ssh://git@github.com/'.length)}`
  } else {
    try {
      const parsed = new URL(value)

      if (parsed.hostname && parsed.pathname) {
        value = `${parsed.hostname}${parsed.pathname}`
      }
    } catch {
      // Leave non-URL forms unchanged.
    }
  }

  value = value.trim().replace(/\/+$/, '')

  if (value.endsWith('.git')) {
    value = value.slice(0, -4)
  }

  return value.toLowerCase()
}

function isSshRemote(url) {
  const value = String(url || '')
    .trim()
    .toLowerCase()

  return value.startsWith('git@') || value.startsWith('ssh://')
}

function httpsProbeRemote(url) {
  if (!url) {
    return ''
  }

  const value = String(url).trim()
  let host = ''
  let pathname = ''

  if (value.startsWith('git@')) {
    const separator = value.indexOf(':')
    if (separator > 4) {
      host = value.slice(4, separator)
      pathname = value.slice(separator + 1)
    }
  } else if (value.startsWith('ssh://')) {
    try {
      const parsed = new URL(value)
      host = parsed.hostname
      pathname = parsed.pathname.slice(1)
    } catch {
      return value
    }
  } else {
    return value
  }

  if (!host || !pathname || !['github.com', 'gitee.com'].includes(host.toLowerCase())) {
    return value
  }

  return `https://${host.toLowerCase()}/${pathname.replace(/\/+$/, '')}`
}

function isOfficialSshRemote(url) {
  return isSshRemote(url) && canonicalGitHubRemote(url) === OFFICIAL_REPO_CANONICAL
}

export {
  canonicalGitHubRemote,
  httpsProbeRemote,
  isOfficialSshRemote,
  isSshRemote,
  OFFICIAL_REPO_CANONICAL,
  OFFICIAL_REPO_HTTPS_URL
}
