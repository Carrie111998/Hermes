import { spawnSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { buildAppEnv, createSandbox } from './fixtures'
import { expect, test } from './test'

const REPO_ROOT = path.resolve(import.meta.dirname, '..', '..', '..')

function runKanban(env: Record<string, string>, title: string): void {
  const result = spawnSync(process.env.PYTHON ?? 'python', ['-c', `
from hermes_cli import kanban_db
kanban_db.init_db()
conn = kanban_db.connect()
kanban_db.create_task(conn, title=${JSON.stringify(title)}, body="fixture", created_by="desktop-e2e")
conn.commit()
conn.close()
`], { cwd: REPO_ROOT, encoding: 'utf8', env: { ...env, PYTHONPATH: REPO_ROOT } })
  if (result.status !== 0) {
    throw new Error(result.stderr)
  }
}

test('final app env rejects hostile Kanban selectors and mutates only the isolated board', () => {
  const sandbox = createSandbox('kanban-env')
  const canonicalRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-e2e-canonical-'))
  const canonicalHome = path.join(canonicalRoot, 'home')
  const canonicalDb = path.join(canonicalRoot, 'sentinel.db')
  const dbLink = path.join(canonicalRoot, 'sentinel-link.db')
  fs.mkdirSync(canonicalHome)

  try {
    runKanban({ ...process.env, HERMES_HOME: canonicalHome, HERMES_KANBAN_DB: canonicalDb } as Record<string, string>, 'canonical sentinel')
    fs.symlinkSync(canonicalDb, dbLink)
    const before = fs.readFileSync(canonicalDb)

    const env = buildAppEnv(sandbox, {
      HERMES_HOME: canonicalHome,
      HERMES_KANBAN_DB: dbLink,
      HERMES_KANBAN_BOARD: 'canonical',
      HERMES_KANBAN_TASK: 'hostile-task',
      HERMES_KANBAN_BOARD_HOME: canonicalRoot,
      hermes_kanban_db: canonicalDb,
      HeRmEs_KaNbAn_BoArD: 'mixed-case',
      HERMES_KANBAN_EMPTY: '',
      HERMES_KANBAN_UNDEFINED: undefined,
      E2E_UNRELATED_VALUE: 'preserved',
    })

    expect(env.HERMES_HOME).toBe(sandbox.hermesHome)
    expect(env.E2E_UNRELATED_VALUE).toBe('preserved')
    expect(Object.keys(env).filter(key => key.toUpperCase().startsWith('HERMES_KANBAN_'))).toEqual([])

    runKanban(env, 'isolated mutation')

    expect(fs.readFileSync(canonicalDb).equals(before)).toBe(true)
    expect(fs.realpathSync(dbLink)).toBe(fs.realpathSync(canonicalDb))
    expect(fs.existsSync(path.join(sandbox.hermesHome, 'kanban.db'))).toBe(true)
  } finally {
    sandbox.cleanup()
    fs.rmSync(canonicalRoot, { recursive: true, force: true })
  }
})
