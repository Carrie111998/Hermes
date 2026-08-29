import fs from 'node:fs'
import path from 'node:path'

const SQLITE_HEADER = Buffer.from('SQLite format 3\0')
const MIN_SQLITE_BYTES = 100

/** Homes that own a `state.db`: the root install plus each `profiles/<name>/`. */
export function listStateDbHomes(hermesHome: string): string[] {
  const homes = [hermesHome]
  const profilesDir = path.join(hermesHome, 'profiles')

  let entries: string[] = []

  try {
    entries = fs.readdirSync(profilesDir)
  } catch {
    return homes
  }

  for (const name of entries.sort()) {
    const candidate = path.join(profilesDir, name)

    try {
      if (fs.statSync(candidate).isDirectory()) {
        homes.push(candidate)
      }
    } catch {
      void 0
    }
  }

  return homes
}

function preflightOneStateDb(dbHome: string, rememberLog: (message: string) => void): void {
  const stateDbPath = path.join(dbHome, 'state.db')

  if (!fs.existsSync(stateDbPath)) {
    rememberLog(`[updates] state.db pre-flight: not found (fresh install?) ${stateDbPath}`)

    return
  }

  try {
    const stat = fs.statSync(stateDbPath)

    if (stat.size <= MIN_SQLITE_BYTES) {
      rememberLog(`[updates] state.db too small (${stat.size} bytes) for a valid SQLite database: ${stateDbPath}`)

      return
    }

    const fd = fs.openSync(stateDbPath, 'r')
    const header = Buffer.alloc(16)

    fs.readSync(fd, header, 0, 16, 0)
    fs.closeSync(fd)

    const headerOk = header.equals(SQLITE_HEADER)

    rememberLog(
      `[updates] state.db pre-flight: path=${stateDbPath} size=${stat.size}, ` +
        `headerOk=${headerOk}, headerHex=${header.toString('hex')}`
    )

    if (!headerOk) {
      rememberLog(
        `[updates] state.db header is INVALID before update — ` +
          `this indicates pre-existing corruption or a concurrent write issue: ${stateDbPath}`
      )
    }

    const ts = new Date().toISOString().replace(/[:.]/g, '-')
    const emergencyPath = path.join(dbHome, `state.db.pre-update-emergency-${ts}.bak`)

    try {
      fs.copyFileSync(stateDbPath, emergencyPath)
      const emergStat = fs.statSync(emergencyPath)

      rememberLog(`[updates] emergency state.db backup: ${emergencyPath} ` + `(${emergStat.size} bytes)`)

      try {
        const backups = fs
          .readdirSync(dbHome)
          .filter(
            f =>
              f.startsWith('state.db.pre-update-emergency-') &&
              f.endsWith('.bak') &&
              f !== path.basename(emergencyPath)
          )
          .sort()
          .reverse()

        for (const old of backups.slice(2)) {
          try {
            fs.unlinkSync(path.join(dbHome, old))
          } catch {
            void 0
          }
        }
      } catch {
        void 0
      }
    } catch (copyErr) {
      const message = copyErr instanceof Error ? copyErr.message : String(copyErr)

      rememberLog(`[updates] emergency state.db backup failed: ${message}`)
    }
  } catch (statErr) {
    const message = statErr instanceof Error ? statErr.message : String(statErr)

    rememberLog(`[updates] could not stat state.db before update: ${message}`)
  }
}

/**
 * Take an emergency snapshot of every `state.db` under this install — the
 * root home and each `profiles/<name>/` — and verify the live copies look
 * like SQLite before any update process mutates the tree (#68474, #97994).
 */
export function preflightStateDb(hermesHome: string, rememberLog: (message: string) => void): void {
  for (const dbHome of listStateDbHomes(hermesHome)) {
    preflightOneStateDb(dbHome, rememberLog)
  }
}
