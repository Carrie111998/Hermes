import { describe, expect, it } from 'vitest'

import { group, split } from '@/components/pane-shell/tree/model'

import { shouldMigrateLegacyRightPanel } from './layout-migration'

const legacyTree = () =>
  split(
    'row',
    [
      group(['sessions'], { id: 'grp-sessions' }),
      group(['workspace'], { id: 'grp-main' }),
      split(
        'column',
        [
          split(
            'row',
            [
              group(['review'], { id: 'grp-review' }),
              group(['preview'], { id: 'grp-preview' }),
              group(['files'], { id: 'grp-files' })
            ],
            undefined,
            'spl-rail'
          ),
          group(['terminal'], { id: 'grp-terminal' })
        ],
        undefined,
        'spl-right'
      )
    ],
    undefined,
    'spl-root'
  )

describe('legacy right-panel layout migration', () => {
  it('recognizes the former stock default before and after Artifacts was added', () => {
    const beforeArtifacts = legacyTree()

    const afterArtifacts = split(
      'row',
      [
        beforeArtifacts.children[0],
        beforeArtifacts.children[1],
        group(['artifacts-pane']),
        beforeArtifacts.children[2]
      ],
      undefined,
      'spl-root'
    )

    expect(shouldMigrateLegacyRightPanel(beforeArtifacts, 'default', new Set())).toBe(true)
    expect(shouldMigrateLegacyRightPanel(afterArtifacts, 'default', new Set())).toBe(true)
  })

  it('never rewrites another preset or an explicitly placed right feature', () => {
    expect(shouldMigrateLegacyRightPanel(legacyTree(), 'quad', new Set())).toBe(false)
    expect(shouldMigrateLegacyRightPanel(legacyTree(), 'default', new Set(['preview']))).toBe(false)
  })

  it('leaves an adopted plugin pane in its existing user layout', () => {
    const current = legacyTree()
    current.children.push(group(['plugin:inspector']))
    current.weights.push(1)

    expect(shouldMigrateLegacyRightPanel(current, 'default', new Set())).toBe(false)
  })

  it('migrates the first unified group that incorrectly made Files a function tab', () => {
    const current = split(
      'row',
      [
        group(['sessions'], { id: 'grp-sessions' }),
        group(['workspace'], { id: 'grp-main' }),
        group(['files', 'review', 'artifacts-pane', 'preview', 'terminal'], { id: 'grp-right-tools' })
      ],
      undefined,
      'spl-root'
    )

    expect(shouldMigrateLegacyRightPanel(current, 'default', new Set())).toBe(true)
  })

  it('is idempotent once Files is the independent outer sidebar', () => {
    const current = split(
      'row',
      [
        group(['sessions'], { id: 'grp-sessions' }),
        group(['workspace'], { id: 'grp-main' }),
        group(['review', 'artifacts-pane', 'preview', 'terminal'], { id: 'grp-right-tools' }),
        group(['files'], { id: 'grp-files' })
      ],
      undefined,
      'spl-root'
    )

    expect(shouldMigrateLegacyRightPanel(current, 'default', new Set())).toBe(false)
  })

  it('repairs reversed Files/Preview order without discarding a custom session tile', () => {
    const current = split(
      'row',
      [
        group(['sessions'], { id: 'grp-sessions' }),
        group(['workspace', 'session-tile:active'], { id: 'grp-main' }),
        group(['files'], { id: 'grp-files' }),
        group(['review', 'artifacts-pane', 'preview', 'terminal'], { id: 'grp-right-tools' })
      ],
      undefined,
      'spl-root'
    )

    expect(shouldMigrateLegacyRightPanel(current, 'custom', new Set(['session-tile:active']))).toBe(true)
  })
})
