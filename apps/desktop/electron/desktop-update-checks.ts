/**
 * Self-update check orchestration extracted from electron/main.ts.
 *
 * The factory keeps the original main-process dependencies explicit while the
 * returned functions preserve the legacy names at the composition seam.
 */
export function createDesktopUpdateChecks(deps: Record<string, any>) {
  const {
    ACTIVE_HERMES_ROOT,
    BrowserWindow,
    DEFAULT_UPDATE_BRANCH,
    DESKTOP_UPDATE_CONFIG_PATH,
    HERMES_HOME,
    IS_PACKAGED,
    IS_WINDOWS,
    OFFICIAL_REPO_HTTPS_URL,
    SOURCE_REPO_ROOT,
    clearStaleGitLocks,
    compareApiUrl,
    directoryExists,
    fs,
    https,
    isHermesSourceRoot,
    isOfficialSshRemote,
    path,
    parseCompareBehindCount,
    rememberLog,
    resolveBehindCount,
    resolveCommitLogSelection,
    resolveGitBinary,
    shouldCountCommits,
    spawn,
    hiddenWindowsChildOptions,
    writeFileAtomic
  } = deps

  function readDesktopUpdateConfig() {
    try {
      const parsed = JSON.parse(fs.readFileSync(DESKTOP_UPDATE_CONFIG_PATH, 'utf8'))
      const branch = typeof parsed?.branch === 'string' ? parsed.branch.trim() : ''

      return { branch: branch || DEFAULT_UPDATE_BRANCH }
    } catch {
      return { branch: DEFAULT_UPDATE_BRANCH }
    }
  }

  function writeDesktopUpdateConfig(config) {
    fs.mkdirSync(path.dirname(DESKTOP_UPDATE_CONFIG_PATH), { recursive: true })
    writeFileAtomic(DESKTOP_UPDATE_CONFIG_PATH, JSON.stringify(config, null, 2))
  }

  // Match the backend's source resolution but bias toward a real git checkout.
  // Dev → SOURCE_REPO_ROOT. Packaged/CLI install → ACTIVE_HERMES_ROOT.
  // HERMES_DESKTOP_HERMES_ROOT always wins so devs can pin a worktree.
  function resolveUpdateRoot() {
    const candidates = [
      process.env.HERMES_DESKTOP_HERMES_ROOT && path.resolve(process.env.HERMES_DESKTOP_HERMES_ROOT),
      !IS_PACKAGED && isHermesSourceRoot(SOURCE_REPO_ROOT) ? SOURCE_REPO_ROOT : null,
      isHermesSourceRoot(ACTIVE_HERMES_ROOT) ? ACTIVE_HERMES_ROOT : null
    ].filter(Boolean)

    return candidates.find(c => directoryExists(path.join(c, '.git'))) || candidates[0] || ACTIVE_HERMES_ROOT
  }

  function runGit(args, options: any = {}): Promise<{ code: number; stdout: string; stderr: string }> {
    return new Promise((resolve, reject) => {
      const child = spawn(
        resolveGitBinary(),
        IS_WINDOWS ? ['-c', 'windows.appendAtomically=false', ...args] : args,
        hiddenWindowsChildOptions({
          cwd: options.cwd,
          env: { ...process.env, ...((options.env || {}) as any), GIT_TERMINAL_PROMPT: '0' },
          stdio: ['ignore', 'pipe', 'pipe']
        })
      )

      let stdout = ''
      let stderr = ''
      child.stdout.on('data', chunk => {
        const text = chunk.toString()
        stdout += text
        options.onLine?.('stdout', text)
      })
      child.stderr.on('data', chunk => {
        const text = chunk.toString()
        stderr += text
        options.onLine?.('stderr', text)
      })
      child.once('error', reject)
      child.once('exit', code => resolve({ code, stdout, stderr }))
    })
  }

  const firstLine = text => (text || '').split('\n').find(Boolean) || ''

  async function getOriginUrl(updateRoot) {
    const origin = await runGit(['remote', 'get-url', 'origin'], { cwd: updateRoot })

    return origin.code === 0 ? origin.stdout.trim() : ''
  }

  function emitUpdateProgress(payload) {
    const merged = { stage: 'idle', message: '', percent: null, error: null, ...payload, at: Date.now() }
    rememberLog(`[updates] ${merged.stage}: ${merged.message || merged.error || ''}`)

    for (const window of BrowserWindow.getAllWindows()) {
      window.webContents.send('hermes:updates:progress', merged)
    }
  }

  // Self-heal the tracked update branch: if origin no longer publishes it (e.g.
  // bb/gui was merged into main and deleted), fall back to main and persist so
  // every later check/apply follows main — no manual flip, even for already-
  // installed clients. Read-only ls-remote probe; only flips on a definitive
  // "ref absent" (exit 2), never on a transient network error, so a flaky
  // connection can't strand a user on the wrong branch.
  async function resolveHealedBranch(updateRoot, branch) {
    if (!branch || branch === 'main') {
      return branch || 'main'
    }

    const originUrl = await getOriginUrl(updateRoot)
    const remote = isOfficialSshRemote(originUrl) ? OFFICIAL_REPO_HTTPS_URL : 'origin'
    const probe = await runGit(['ls-remote', '--exit-code', '--heads', remote, branch], { cwd: updateRoot })

    if (probe.code !== 2) {
      return branch
    }

    rememberLog(`[updates] origin/${branch} is gone (merged?); falling back to main`)
    const config = readDesktopUpdateConfig()

    if (config.branch !== 'main') {
      writeDesktopUpdateConfig({ ...config, branch: 'main' })
    }

    return 'main'
  }

  async function checkUpdates() {
    const updateRoot = resolveUpdateRoot()
    let { branch } = readDesktopUpdateConfig()
    const gitDir = path.join(updateRoot, '.git')

    if (!directoryExists(gitDir)) {
      return {
        supported: false,
        reason: 'not-a-git-checkout',
        message: `${updateRoot} isn't a git checkout — desktop self-update only runs against a source install.`,
        hermesRoot: updateRoot,
        branch
      }
    }

    branch = await resolveHealedBranch(updateRoot, branch)
    const originUrl = await getOriginUrl(updateRoot)

    if (isOfficialSshRemote(originUrl)) {
      const git = args => runGit(args, { cwd: updateRoot }).then(r => r.stdout.trim())

      const [currentSha, target, dirtyStr, currentBranch] = await Promise.all([
        git(['rev-parse', 'HEAD']),
        runGit(['ls-remote', OFFICIAL_REPO_HTTPS_URL, `refs/heads/${branch}`], { cwd: updateRoot }),
        git(['status', '--porcelain']),
        git(['rev-parse', '--abbrev-ref', 'HEAD'])
      ])

      const targetSha = firstLine(target.stdout).split(/\s+/)[0] || ''

      if (target.code !== 0 || !targetSha) {
        return {
          supported: true,
          branch,
          error: 'fetch-failed',
          message: firstLine(target.stderr) || 'git ls-remote failed.',
          hermesRoot: updateRoot,
          fetchedAt: Date.now()
        }
      }

      // Passive SSH-official checks only know tip SHAs (ls-remote) — never
      // fabricate a "1 commit behind". Recover the exact count via the GitHub
      // compare API when possible; otherwise behind stays null ("update
      // available, count unknown") and updateAvailable carries the signal.
      // ahead_by === 0 with differing tips means the remote tip is reachable
      // from our HEAD — a local carried commit sitting AHEAD, not behind:
      // flagging that as an update nudges the user into wiping their work.
      const tipsEqual = Boolean(currentSha && currentSha === targetSha)

      const sshBehind = tipsEqual
        ? 0
        : await fetchCompareBehindCount({ currentSha, originUrl: OFFICIAL_REPO_HTTPS_URL, targetSha })

      const upToDate = tipsEqual || sshBehind === 0

      return {
        supported: true,
        branch,
        currentBranch,
        behind: upToDate ? 0 : sshBehind,
        updateAvailable: !upToDate,
        currentSha,
        targetSha,
        commits: [],
        dirty: dirtyStr.length > 0,
        hermesRoot: updateRoot,
        fetchedAt: Date.now()
      }
    }

    // Self-heal abandoned git lock files before fetching. A stale
    // .git/shallow.lock from a crashed/interrupted fetch otherwise fails every
    // later fetch ("Unable to create '.git/shallow.lock': File exists") and this
    // check reports 'fetch-failed' forever — git never removes these itself.
    await clearStaleGitLocks(updateRoot)

    const fetched = await runGit(['fetch', '--quiet', 'origin', branch], { cwd: updateRoot })

    if (fetched.code !== 0) {
      return {
        supported: true,
        branch,
        error: 'fetch-failed',
        message: firstLine(fetched.stderr) || 'git fetch failed.',
        hermesRoot: updateRoot,
        fetchedAt: Date.now()
      }
    }

    const git = args => runGit(args, { cwd: updateRoot }).then(r => r.stdout.trim())

    const [currentSha, targetSha, dirtyStr, currentBranch, shallowStr] = await Promise.all([
      git(['rev-parse', 'HEAD']),
      git(['rev-parse', `origin/${branch}`]),
      git(['status', '--porcelain']),
      git(['rev-parse', '--abbrev-ref', 'HEAD']),
      git(['rev-parse', '--is-shallow-repository'])
    ])

    const isShallow = shallowStr === 'true'

    // A shallow graph cannot provide a trustworthy exact count, even when it has
    // a visible merge-base. Skip the ancestry walk and use the SHA fallback.
    const countStr = shouldCountCommits({ isShallow }) ? await git(['rev-list', `HEAD..origin/${branch}`, '--count']) : ''

    // A positive directional ancestry result remains trustworthy in a shallow
    // graph and prevents a local commit on top of origin from looking outdated.
    const targetIsAncestorOfHead =
      isShallow &&
      currentSha !== targetSha &&
      (await runGit(['merge-base', '--is-ancestor', `origin/${branch}`, 'HEAD'], { cwd: updateRoot })).code === 0

    let behind = resolveBehindCount({
      countStr,
      currentSha,
      targetSha,
      isShallow,
      targetIsAncestorOfHead
    })

    // Recover the exact count a shallow clone can't compute: the GitHub compare
    // API knows the full graph regardless of local clone depth. Best-effort —
    // offline, rate-limited, or non-GitHub origins keep the honest null
    // ("update available", no fabricated number).
    if (behind === null) {
      behind = await fetchCompareBehindCount({ currentSha, originUrl, targetSha })
    }

    // behind === null means "update available, exact count unknown" (shallow
    // clone): still list what origin offers — resolveCommitLogSelection keeps
    // the shallow log to the fetched tip so the range walk can't enumerate the
    // contaminated ancestry — so "See what's new" stays useful and honest.
    const commits = behind !== 0 ? await readCommitLog(updateRoot, branch, isShallow) : []

    return {
      supported: true,
      branch,
      currentBranch,
      behind,
      updateAvailable: behind === null || behind > 0,
      currentSha,
      targetSha,
      commits,
      dirty: dirtyStr.length > 0,
      hermesRoot: updateRoot,
      fetchedAt: Date.now()
    }
  }

  // Best-effort exact behind-count for graphs the local clone can't measure.
  // Delegates URL building + response parsing to update-count.ts (pure, unit
  // tested); this wrapper only does the bounded network call. Any failure —
  // offline, 4xx/5xx, rate limit, shape surprise — returns null so callers keep
  // the honest "update available, count unknown" state.
  async function fetchCompareBehindCount({ currentSha, originUrl, targetSha }) {
    const url = compareApiUrl({ currentSha, originUrl, targetSha })

    if (!url) {
      return null
    }

    try {
      const payload = await new Promise((resolve, reject) => {
        const req = https.get(
          url,
          {
            headers: {
              Accept: 'application/vnd.github+json',
              // GitHub requires a UA on api.github.com; requests without one 403.
              'User-Agent': 'hermes-desktop-update-check'
            },
            timeout: 10_000
          },
          res => {
            const chunks = []
            res.on('error', reject)
            res.on('data', chunk => chunks.push(chunk))
            res.on('end', () => {
              if ((res.statusCode || 500) >= 400) {
                reject(new Error(`compare API ${res.statusCode}`))

                return
              }

              try {
                resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')))
              } catch (error) {
                reject(error)
              }
            })
          }
        )

        req.on('timeout', () => req.destroy(new Error('compare API timeout')))
        req.on('error', reject)
      })

      return parseCompareBehindCount(payload)
    } catch {
      return null
    }
  }

  async function readCommitLog(cwd, branch, isShallow) {
    const SEP = '\x1f'
    const REC = '\x1e'
    const { limit, revision } = resolveCommitLogSelection({ branch, isShallow })

    const { stdout } = await runGit(
      ['log', revision, `--pretty=format:%H${SEP}%s${SEP}%an${SEP}%at${REC}`, '-n', String(limit)],
      { cwd }
    )

    return stdout
      .split(REC)
      .map(line => line.trim())
      .filter(Boolean)
      .map(line => {
        const [sha, summary, author, at] = line.split(SEP)

        return { sha, summary, author, at: Number.parseInt(at, 10) * 1000 }
      })
  }

  return {
    checkUpdates,
    emitUpdateProgress,
    fetchCompareBehindCount,
    firstLine,
    getOriginUrl,
    readCommitLog,
    readDesktopUpdateConfig,
    resolveHealedBranch,
    resolveUpdateRoot,
    runGit,
    writeDesktopUpdateConfig
  }
}
