import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { onComposerInsertRequest, onComposerSubmitRequest } from '@/app/chat/composer/focus'

import { requestGithubIssueTaskFromDeepLink } from './github-issue-deeplink-open'
import { $notifications } from './notifications'

const ISSUE_URL = 'https://github.com/NousResearch/hermes-agent/issues/63169'

describe('requestGithubIssueTaskFromDeepLink', () => {
  let inserted: { target: string; text: string }[]
  let submitted: string[]
  let stopListening: () => void

  // The composer bus dispatches on a macrotask so click/keydown handlers finish
  // first, so every assertion waits one turn.
  const flushComposerBus = () => new Promise(resolve => window.setTimeout(resolve, 0))

  const taskFor = async (url: string) => {
    requestGithubIssueTaskFromDeepLink({ url })
    await flushComposerBus()

    return inserted.at(-1)?.text ?? ''
  }

  beforeEach(() => {
    inserted = []
    submitted = []
    $notifications.set([])

    const offInsert = onComposerInsertRequest(({ target, text }) => inserted.push({ target, text }))
    const offSubmit = onComposerSubmitRequest(({ text }) => submitted.push(text))

    stopListening = () => {
      offInsert()
      offSubmit()
    }
  })

  afterEach(() => {
    stopListening()
    $notifications.set([])
  })

  it('fills the main composer with a task carrying the issue URL', async () => {
    requestGithubIssueTaskFromDeepLink({ url: ISSUE_URL })
    await flushComposerBus()

    expect(inserted).toHaveLength(1)
    expect(inserted[0].target).toBe('main')
    expect(inserted[0].text).toContain(ISSUE_URL)
    expect($notifications.get()).toEqual([])
  })

  it('never sends on the user behalf', async () => {
    requestGithubIssueTaskFromDeepLink({ url: ISSUE_URL })
    await flushComposerBus()

    expect(submitted).toEqual([])
  })

  // The security contract, asserted as an invariant rather than a snapshot of
  // the copy: the ONLY part of the composer text a link can influence is the
  // validated URL. Two accepted links must produce byte-identical text once
  // their URLs are masked out.
  it('lets a link contribute nothing but the validated URL', async () => {
    const other = 'https://github.com/owner/repo/issues/7'
    const first = await taskFor(ISSUE_URL)
    const second = await taskFor(other)

    expect(first.replace(ISSUE_URL, '<URL>')).toBe(second.replace(other, '<URL>'))
    expect(first.endsWith(`\n\n${ISSUE_URL}`)).toBe(true)
  })

  it('toasts instead of inserting when the URL is refused', async () => {
    requestGithubIssueTaskFromDeepLink({ url: 'https://github.com:443/owner/repo/issues/42' })
    await flushComposerBus()

    expect(inserted).toEqual([])
    expect($notifications.get()).toHaveLength(1)
    expect($notifications.get()[0].kind).toBe('error')
  })

  it('toasts when the url param is missing entirely', async () => {
    requestGithubIssueTaskFromDeepLink({})
    await flushComposerBus()

    expect(inserted).toEqual([])
    expect($notifications.get()).toHaveLength(1)
  })

  // translateNow falls back to `en` for an unknown key, returning the key
  // itself; a task that still reads like a dot-path means the catalog entry is
  // missing from every locale.
  it('resolves its copy from the i18n catalog', async () => {
    const text = await taskFor(ISSUE_URL)

    expect(text).not.toContain('composer.githubIssueDeepLink')
  })
})
