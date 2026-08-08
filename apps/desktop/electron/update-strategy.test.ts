/**
 * Tests for electron/update-strategy.ts — the update-path ladder.
 *
 * The load-bearing invariant: electron-updater only claims a PACKAGED app
 * with a configured feed. Every existing install (source checkout, or
 * packaged with no feed) must fall through to the exact path it uses today,
 * so merging this changes nothing for current users. These tests pin that
 * precedence and the no-behavior-change guarantee.
 *
 * Run with: npx vitest run --project electron electron/update-strategy.test.ts
 */

import { describe, expect, it } from 'vitest'

import { feedConfiguration, resolveUpdateStrategy, usesElectronUpdater, type UpdateStrategyInput } from './update-strategy'

function input(overrides: Partial<UpdateStrategyInput>): UpdateStrategyInput {
  return {
    isPackaged: false,
    feedUrl: '',
    hasStagedUpdater: false,
    isWindows: false,
    ...overrides
  }
}

describe('resolveUpdateStrategy', () => {
  it('prefers electron-updater for a packaged app with a configured feed', () => {
    expect(
      resolveUpdateStrategy(input({ isPackaged: true, feedUrl: 'https://github.com/NousResearch/hermes-agent/releases' }))
    ).toBe('electron-updater')
  })

  it('electron-updater wins on every platform, including Windows', () => {
    // Even with a staged updater present (Windows), a configured feed takes
    // precedence — the packaged binary path needs no quit→hand-off dance.
    expect(
      resolveUpdateStrategy(
        input({ isPackaged: true, feedUrl: 'https://example.com/feed', hasStagedUpdater: true, isWindows: true })
      )
    ).toBe('electron-updater')
  })

  it('never selects electron-updater without a feed (packaged, no feed)', () => {
    // The no-behavior-change gate: a packaged app with no feed must keep its
    // existing path. On POSIX that is the in-app rebuild.
    expect(resolveUpdateStrategy(input({ isPackaged: true, feedUrl: '', isWindows: false }))).toBe('posix-in-app')
  })

  it('never selects electron-updater for a source checkout, even with a feed', () => {
    // A dev/source install (`electron .`, isPackaged=false) keeps its dev
    // path even if a feed string is somehow present.
    expect(
      resolveUpdateStrategy(input({ isPackaged: false, feedUrl: 'https://example.com/feed', isWindows: false }))
    ).toBe('posix-in-app')
  })

  it('trims a whitespace-only feed and treats it as unconfigured', () => {
    expect(resolveUpdateStrategy(input({ isPackaged: true, feedUrl: '   ', isWindows: false }))).toBe('posix-in-app')
  })

  it('selects staged-setup on Windows with a staged updater and no feed', () => {
    // Existing Windows CLI-install behavior, unchanged.
    expect(
      resolveUpdateStrategy(input({ isPackaged: true, feedUrl: '', hasStagedUpdater: true, isWindows: true }))
    ).toBe('staged-setup')
  })

  it('selects posix-in-app on POSIX regardless of staged updater', () => {
    // The POSIX resolver returns null by policy today; even if a staged
    // updater were somehow present, POSIX keeps the in-app rebuild path.
    expect(
      resolveUpdateStrategy(input({ isPackaged: true, feedUrl: '', hasStagedUpdater: true, isWindows: false }))
    ).toBe('posix-in-app')
  })

  it('selects manual on Windows with neither feed nor staged updater', () => {
    expect(
      resolveUpdateStrategy(input({ isPackaged: false, feedUrl: '', hasStagedUpdater: false, isWindows: true }))
    ).toBe('manual')
  })
})

describe('usesElectronUpdater', () => {
  it('is true exactly when the ladder selects electron-updater', () => {
    expect(usesElectronUpdater(input({ isPackaged: true, feedUrl: 'https://x' }))).toBe(true)
    expect(usesElectronUpdater(input({ isPackaged: true, feedUrl: '' }))).toBe(false)
    expect(usesElectronUpdater(input({ isPackaged: false, feedUrl: 'https://x' }))).toBe(false)
  })
})

describe('feedConfiguration', () => {
  it('uses the GitHub provider for a github.com/<owner>/<repo> URL', () => {
    expect(feedConfiguration('https://github.com/NousResearch/hermes-agent')).toEqual({
      provider: 'github',
      owner: 'NousResearch',
      repo: 'hermes-agent'
    })
  })

  it('uses the GitHub provider for a …/releases URL', () => {
    expect(feedConfiguration('https://github.com/NousResearch/hermes-agent/releases')).toEqual({
      provider: 'github',
      owner: 'NousResearch',
      repo: 'hermes-agent'
    })
  })

  it('strips a .git suffix from the repo name', () => {
    expect(feedConfiguration('https://github.com/NousResearch/hermes-agent.git')).toEqual({
      provider: 'github',
      owner: 'NousResearch',
      repo: 'hermes-agent'
    })
  })

  it('uses the generic provider for a non-GitHub (tenant / self-hosted / S3) feed', () => {
    expect(feedConfiguration('https://updates.tenant-x.example.com/hermes')).toEqual({
      provider: 'generic',
      url: 'https://updates.tenant-x.example.com/hermes'
    })
  })

  it('trims surrounding whitespace before matching', () => {
    expect(feedConfiguration('  https://github.com/NousResearch/hermes-agent  ')).toEqual({
      provider: 'github',
      owner: 'NousResearch',
      repo: 'hermes-agent'
    })
  })
})
