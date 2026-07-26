/**
 * HTTP fallback for update checks when git.exe is unavailable.
 *
 * On some Windows machines, security software (360, Huorong, Defender)
 * injects DLLs that crash git.exe with STATUS_ENTRYPOINT_NOT_FOUND
 * (0xC0000139). This module lets the desktop still answer "is an update
 * available?" by:
 *
 *   1. Reading the local HEAD SHA directly from .git plumbing files.
 *   2. Querying the Gitee API for the remote branch's latest commit(s).
 *   3. Comparing SHAs — same shape as checkUpdates() in main.cjs.
 *
 * The actual *apply* (git pull) still needs git.exe; this fallback only
 * covers detection so the user sees "update available" instead of a
 * silent failure.
 */

const fs = require('node:fs')
const path = require('node:path')
const https = require('node:https')

// ─── Config ────────────────────────────────────────────────────────────────

const GITEE_API_BASE = 'https://gitee.com/api/v5/repos'
const HTTP_TIMEOUT_MS = 15_000
const COMMITS_PAGE_SIZE = 30

// ─── Local SHA (no git.exe) ─────────────────────────────────────────────────

/**
 * Read the current HEAD commit SHA from raw .git plumbing files.
 *
 * Handles: detached HEAD (raw SHA), normal ref, and packed-refs.
 * Returns '' if the SHA cannot be determined.
 */
function readLocalHeadSha(updateRoot) {
  const gitDir = path.join(updateRoot, '.git')
  if (!fs.existsSync(gitDir)) return ''

  // .git/HEAD is either "ref: refs/heads/<branch>" or a raw SHA (detached)
  const headPath = path.join(gitDir, 'HEAD')
  let headRaw
  try {
    headRaw = fs.readFileSync(headPath, 'utf8').trim()
  } catch {
    return ''
  }

  // Detached HEAD — raw SHA
  if (/^[0-9a-f]{40}$/i.test(headRaw)) return headRaw

  // Normal: "ref: refs/heads/<branch>"
  const refMatch = headRaw.match(/^ref:\s*(.+)$/)
  if (!refMatch) return ''

  const refPath = refMatch[1] // e.g. "refs/heads/main"
  const loosePath = path.join(gitDir, refPath)

  // Loose ref file
  try {
    const sha = fs.readFileSync(loosePath, 'utf8').trim()
    if (/^[0-9a-f]{40}$/i.test(sha)) return sha
  } catch {
    // fall through to packed-refs
  }

  // packed-refs fallback
  const packedPath = path.join(gitDir, 'packed-refs')
  try {
    const packed = fs.readFileSync(packedPath, 'utf8')
    for (const line of packed.split('\n')) {
      // Format: "<40-char-sha> <ref-name>"
      const m = line.match(/^([0-9a-f]{40})\s+(.+)$/i)
      if (m && m[2].trim() === refPath) return m[1]
    }
  } catch {
    // no packed-refs
  }

  return ''
}

/**
 * Read the current branch name from .git/HEAD.
 * Returns '' for detached HEAD.
 */
function readLocalBranch(updateRoot) {
  const gitDir = path.join(updateRoot, '.git')
  try {
    const head = fs.readFileSync(path.join(gitDir, 'HEAD'), 'utf8').trim()
    const m = head.match(/^ref:\s*refs\/heads\/(.+)$/)
    return m ? m[1] : ''
  } catch {
    return ''
  }
}

// ─── Remote SHA (Gitee API) ────────────────────────────────────────────────

/**
 * Parse owner/repo from a Git remote URL.
 * Supports HTTPS and SSH forms for GitHub and Gitee.
 * Returns { owner, repo, apiBase } or null.
 */
function parseRepoUrl(remoteUrl) {
  if (!remoteUrl) return null

  // Normalize to owner/repo path
  let repoPath = remoteUrl.trim().replace(/\.git$/, '').replace(/\/+$/, '')

  // SSH: git@github.com:owner/repo or git@gitee.com:owner/repo
  const sshMatch = repoPath.match(/^git@([^:]+):(.+)$/)
  if (sshMatch) {
    const host = sshMatch[1]
    repoPath = `${host}/${sshMatch[2]}`
  } else if (repoPath.startsWith('ssh://')) {
    repoPath = repoPath.replace(/^ssh:\/\/git@/, '')
  }

  // HTTPS: https://host/owner/repo
  if (repoPath.startsWith('http')) {
    try {
      const u = new URL(repoPath)
      repoPath = `${u.hostname}${u.pathname}`
    } catch {
      return null
    }
  }

  // Extract owner/repo
  const parts = repoPath.split('/').filter(Boolean)
  if (parts.length < 2) return null

  const host = parts[0].toLowerCase()
  const owner = parts[1]
  const repo = parts[2] || ''

  if (!owner || !repo) return null

  // Build API base
  let apiBase
  if (host === 'gitee.com') {
    apiBase = `${GITEE_API_BASE}/${owner}/${repo}`
  } else if (host === 'github.com') {
    apiBase = `https://api.github.com/repos/${owner}/${repo}`
  } else {
    // Generic: try Gitee-style API (works for most Gitea/Gitea forks)
    apiBase = `https://${host}/api/v5/repos/${owner}/${repo}`
  }

  return { owner, repo, host, apiBase }
}

/**
 * HTTPS GET returning parsed JSON. Follows up to 3 redirects.
 */
function httpsGetJson(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { timeout: HTTP_TIMEOUT_MS }, (res) => {
      // Follow redirects
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        res.resume()
        if (res.headers.location.startsWith('http')) {
          return httpsGetJson(res.headers.location).then(resolve, reject)
        }
      }
      if (res.statusCode !== 200) {
        res.resume()
        return reject(new Error(`HTTP ${res.statusCode}`))
      }
      let body = ''
      res.on('data', chunk => { body += chunk })
      res.on('end', () => {
        try {
          resolve(JSON.parse(body))
        } catch (e) {
          reject(new Error(`JSON parse: ${e.message}`))
        }
      })
    })
    req.on('error', reject)
    req.on('timeout', () => req.destroy(new Error('timeout')))
  })
}

/**
 * Fetch the latest commit SHA + metadata for a branch from the remote API.
 * Returns { sha, message, author, date } or null on failure.
 */
async function fetchRemoteHead(apiBase, branch) {
  const url = `${apiBase}/commits?sha=${encodeURIComponent(branch)}&page=1&per_page=1`
  const commits = await httpsGetJson(url)
  if (!Array.isArray(commits) || commits.length === 0) return null
  const c = commits[0]
  return {
    sha: c.sha,
    message: c.commit?.message?.split('\n')[0] || '',
    author: c.commit?.author?.name || '',
    date: c.commit?.author?.date || ''
  }
}

/**
 * Fetch recent commits (up to ~30) to determine how many the local HEAD is behind.
 * Returns [{ sha, message, author, date }, ...].
 */
async function fetchRecentCommits(apiBase, branch) {
  const url = `${apiBase}/commits?sha=${encodeURIComponent(branch)}&page=1&per_page=${COMMITS_PAGE_SIZE}`
  const commits = await httpsGetJson(url)
  if (!Array.isArray(commits)) return []
  return commits.map(c => ({
    sha: c.sha,
    message: c.commit?.message?.split('\n')[0] || '',
    author: c.commit?.author?.name || '',
    date: c.commit?.author?.date || ''
  }))
}

/**
 * Check if a specific commit SHA exists on the remote.
 * Used to distinguish "behind by N" from "local has unpushed commits".
 */
async function fetchCommitExists(apiBase, sha) {
  try {
    const url = `${apiBase}/commits/${encodeURIComponent(sha)}`
    const commit = await httpsGetJson(url)
    return !!commit?.sha
  } catch {
    return false
  }
}

// ─── Bootstrap marker SHA (when .git is missing) ───────────────────────────

/**
 * Read the pinnedCommit SHA from the .hermes-bootstrap-complete marker.
 *
 * When git.exe is broken AND install.ps1's git init also failed, there is no
 * .git directory at all.  The bootstrap marker may still carry a pinnedCommit
 * SHA that tells us what the install was based on.
 *
 * @returns {string} 40-char SHA or ''
 */
function readShaFromBootstrapMarker(updateRoot) {
  const markerPath = path.join(updateRoot, '.hermes-bootstrap-complete')
  try {
    const marker = JSON.parse(fs.readFileSync(markerPath, 'utf8'))
    const sha = marker?.pinnedCommit
    if (typeof sha === 'string' && /^[0-9a-f]{40}$/i.test(sha)) return sha
  } catch {
    // no marker or unreadable
  }
  return ''
}

// ─── Orchestrator ──────────────────────────────────────────────────────────

/**
 * Check for updates using HTTP API only (no git.exe).
 *
 * @param {string} updateRoot  — path to the .git checkout
 * @param {string} branch      — branch to compare against
 * @param {string} remoteUrl   — git remote URL (HTTPS or SSH)
 * @returns {object} same shape as checkUpdates() in main.cjs, with
 *                   `method: 'http-fallback'` added.
 */
async function checkUpdatesViaHttp(updateRoot, branch, remoteUrl) {
  const parsed = parseRepoUrl(remoteUrl)
  if (!parsed) {
    return {
      supported: false,
      reason: 'http-fallback-no-api',
      message: `Cannot parse remote URL for HTTP fallback: ${remoteUrl}`,
      hermesRoot: updateRoot,
      branch,
      method: 'http-fallback'
    }
  }

  // Prefer the bootstrap marker's pinnedCommit over git HEAD. For vendor/offline
  // installs, install.ps1 creates a stub commit that doesn't match the remote —
  // the pinnedCommit from install-stamp.json is the ground truth for "what was
  // installed." git HEAD is only reliable for normal `git clone` installs and
  // after `hermes update` (which moves HEAD and the desktop now refreshes the
  // marker after a successful update).
  let localSha = readShaFromBootstrapMarker(updateRoot)
  if (!localSha) {
    localSha = readLocalHeadSha(updateRoot)
  }
  const currentBranch = readLocalBranch(updateRoot)

  let remoteHead, recentCommits
  try {
    remoteHead = await fetchRemoteHead(parsed.apiBase, branch)
    if (!remoteHead) {
      return {
        supported: true,
        branch,
        error: 'fetch-failed',
        message: `Gitee API returned no commits for branch "${branch}".`,
        hermesRoot: updateRoot,
        currentBranch,
        method: 'http-fallback'
      }
    }

    // If local SHA matches remote head, we're up to date
    if (localSha && localSha === remoteHead.sha) {
      return {
        supported: true,
        branch,
        currentBranch,
        behind: 0,
        currentSha: localSha,
        targetSha: remoteHead.sha,
        commits: [],
        dirty: false, // can't determine without git.exe
        hermesRoot: updateRoot,
        fetchedAt: Date.now(),
        method: 'http-fallback'
      }
    }

    // No local SHA at all (.git missing AND no bootstrap marker).  We can't
    // know if the install is behind, so report behind:0 to avoid falsely
    // showing "30 updates available".  The button stays enabled.
    if (!localSha) {
      return {
        supported: true,
        branch,
        currentBranch,
        behind: 0,
        currentSha: '(unknown)',
        targetSha: remoteHead.sha,
        commits: [],
        dirty: false,
        hermesRoot: updateRoot,
        fetchedAt: Date.now(),
        method: 'http-fallback',
        note: 'no-local-sha'
      }
    }

    // Fetch recent commits to count how far behind
    recentCommits = await fetchRecentCommits(parsed.apiBase, branch)
  } catch (e) {
    return {
      supported: true,
      branch,
      error: 'fetch-failed',
      message: `HTTP fallback failed: ${e.message}`,
      hermesRoot: updateRoot,
      currentBranch,
      fetchedAt: Date.now(),
      method: 'http-fallback'
    }
  }

  // Find local SHA in recent history
  let behind = 0
  let found = false
  const commitList = []

  for (const c of recentCommits) {
    if (localSha && c.sha === localSha) {
      found = true
      break
    }
    behind++
    commitList.push({
      sha: c.sha,
      summary: c.message,
      author: c.author,
      at: c.date ? new Date(c.date).getTime() : 0
    })
  }

  // Local SHA not in the recent 30 commits. Two possibilities:
  //  a) Local is far behind (>30)  → report as behind
  //  b) Local has unpushed commits → report up-to-date (or ahead)
  // Query the API to find out.
  if (!found && localSha) {
    const existsOnRemote = await fetchCommitExists(parsed.apiBase, localSha)
    if (!existsOnRemote) {
      // Local commits not on remote → treat as up-to-date / diverged
      return {
        supported: true,
        branch,
        currentBranch,
        behind: 0,
        currentSha: localSha,
        targetSha: remoteHead.sha,
        commits: [],
        dirty: false,
        hermesRoot: updateRoot,
        fetchedAt: Date.now(),
        method: 'http-fallback',
        note: 'local-diverged'
      }
    }
    // Exists on remote but >30 behind — report what we have
    behind = Math.max(behind, COMMITS_PAGE_SIZE)
  }

  return {
    supported: true,
    branch,
    currentBranch,
    behind,
    currentSha: localSha || '(unknown)',
    targetSha: remoteHead.sha,
    commits: commitList.slice(0, 40),
    dirty: false,
    hermesRoot: updateRoot,
    fetchedAt: Date.now(),
    method: 'http-fallback'
  }
}

module.exports = {
  readLocalHeadSha,
  readLocalBranch,
  readShaFromBootstrapMarker,
  parseRepoUrl,
  fetchRecentCommits,
  fetchCommitExists,
  checkUpdatesViaHttp
}
