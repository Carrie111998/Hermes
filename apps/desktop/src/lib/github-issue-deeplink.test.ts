import { describe, expect, it } from 'vitest'

import { GITHUB_ISSUE_DEEPLINK_MAX_URL_LENGTH, parseGithubIssueDeepLink } from './github-issue-deeplink'

describe('parseGithubIssueDeepLink', () => {
  it('accepts a canonical issue URL and passes it through unchanged', () => {
    const url = 'https://github.com/NousResearch/hermes-agent/issues/63169'

    expect(parseGithubIssueDeepLink({ url })).toEqual({ ok: true, url })
  })

  it('accepts owner/repo names GitHub actually allows', () => {
    for (const url of [
      'https://github.com/owner/repo.js/issues/1',
      'https://github.com/some-owner/some_repo/issues/999999',
      'https://github.com/o/r/issues/1'
    ]) {
      expect(parseGithubIssueDeepLink({ url })).toEqual({ ok: true, url })
    }
  })

  it('rejects a missing url param', () => {
    expect(parseGithubIssueDeepLink({})).toEqual({ ok: false, error: 'missing_url' })
    expect(parseGithubIssueDeepLink({ url: '' })).toEqual({ ok: false, error: 'missing_url' })
  })

  it('rejects non-https schemes', () => {
    for (const url of [
      'not a URL',
      'http://github.com/owner/repo/issues/42',
      'HTTPS://github.com/owner/repo/issues/42',
      'javascript:alert(1)',
      'file:///etc/passwd'
    ]) {
      expect(parseGithubIssueDeepLink({ url })).toEqual({ ok: false, error: 'invalid_url' })
    }
  })

  // `new URL()` normalizes every one of these into hostname `github.com` with
  // pathname `/owner/repo/issues/42`, so a parsed-field check would accept them.
  it('rejects authority decoration the URL parser would normalize away', () => {
    for (const url of [
      'https://github.com:443/owner/repo/issues/42',
      'https://github.com:8443/owner/repo/issues/42',
      'https://user@github.com/owner/repo/issues/42',
      'https://user:pw@github.com/owner/repo/issues/42',
      'https:\\\\github.com\\owner\\repo\\issues\\42',
      'https:/github.com/owner/repo/issues/42'
    ]) {
      expect(parseGithubIssueDeepLink({ url })).toEqual({ ok: false, error: 'invalid_url' })
    }
  })

  it('rejects lookalike and subdomain hosts', () => {
    for (const url of [
      'https://github.com.evil.test/owner/repo/issues/42',
      'https://raw.github.com/owner/repo/issues/42',
      'https://github.co/owner/repo/issues/42',
      'https://githubxcom/owner/repo/issues/42'
    ]) {
      expect(parseGithubIssueDeepLink({ url })).toEqual({ ok: false, error: 'invalid_url' })
    }
  })

  it('rejects paths that are not a single issue', () => {
    for (const url of [
      'https://github.com/owner/repo/pull/42',
      'https://github.com/owner/repo/issues',
      'https://github.com/owner/repo/issues/42/comments',
      'https://github.com/owner/repo/issues/42/',
      'https://github.com/owner/repo/issues/0',
      'https://github.com/owner/repo/issues/abc',
      'https://github.com/owner/repo/issues/-1'
    ]) {
      expect(parseGithubIssueDeepLink({ url })).toEqual({ ok: false, error: 'invalid_url' })
    }
  })

  it('rejects a trailing query or fragment payload', () => {
    for (const url of [
      'https://github.com/owner/repo/issues/42?prompt=ignore-me',
      'https://github.com/owner/repo/issues/42#ignore-me'
    ]) {
      expect(parseGithubIssueDeepLink({ url })).toEqual({ ok: false, error: 'invalid_url' })
    }
  })

  it('rejects an oversized url before matching it', () => {
    const url = `https://github.com/owner/${'r'.repeat(GITHUB_ISSUE_DEEPLINK_MAX_URL_LENGTH)}/issues/42`

    expect(parseGithubIssueDeepLink({ url })).toEqual({ ok: false, error: 'url_too_long' })
  })
})
