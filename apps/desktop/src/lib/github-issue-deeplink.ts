/**
 * Parser/validator for the `hermes://github-issue/open?url=URL` deep link (an
 * "Investigate in Hermes" button on an issue page), mirroring the
 * `hermes://mcp/install` contract in `mcp-deeplink.ts`.
 *
 * Everything here is HOSTILE INPUT — any web page can open the link — so this
 * module only classifies. It never inserts and never sends: the caller composes
 * a task for the user to review, and the user presses Send.
 */

/**
 * The one URL shape this deep link may carry: a canonical GitHub issue.
 *
 * Anchored against the RAW string on purpose. `new URL()` normalizes away an
 * explicit default port (`https://github.com:443/...` parses with an empty
 * `port`), backslash separators, single-slash authorities and `%2e%2e`
 * segments, so a parsed-field check accepts links that never looked canonical.
 * Matching the whole string means anything decorating the authority (port,
 * userinfo, a lookalike host) or trailing it (query, fragment, extra path
 * segments) fails, and the value that survives IS the canonical URL — there is
 * nothing left to rebuild.
 */
export const GITHUB_ISSUE_URL_RE = /^https:\/\/github\.com\/[A-Za-z0-9-]+\/[A-Za-z0-9._-]+\/issues\/[1-9]\d*$/

/** Hard cap on the `url` param, far above any real issue URL. */
export const GITHUB_ISSUE_DEEPLINK_MAX_URL_LENGTH = 2048

export type GithubIssueDeepLinkErrorCode = 'invalid_url' | 'missing_url' | 'url_too_long'

export type GithubIssueDeepLinkParseResult =
  { ok: false; error: GithubIssueDeepLinkErrorCode } | { ok: true; url: string }

/** i18n key (under `composer.githubIssueDeepLink`) for each rejection, used by
 *  the toast. */
export const GITHUB_ISSUE_DEEPLINK_ERROR_KEYS: Record<GithubIssueDeepLinkErrorCode, string> = {
  invalid_url: 'errorUrl',
  missing_url: 'errorUrl',
  url_too_long: 'errorTooLarge'
}

/**
 * Classify the deep link's query params into a canonical issue URL or a
 * rejection code. Pure and side-effect free; composing the task and showing it
 * for review is the caller's responsibility.
 */
export function parseGithubIssueDeepLink(params: Record<string, string | undefined>): GithubIssueDeepLinkParseResult {
  const url = params.url ?? ''

  if (!url) {
    return { ok: false, error: 'missing_url' }
  }

  if (url.length > GITHUB_ISSUE_DEEPLINK_MAX_URL_LENGTH) {
    return { ok: false, error: 'url_too_long' }
  }

  if (!GITHUB_ISSUE_URL_RE.test(url)) {
    return { ok: false, error: 'invalid_url' }
  }

  return { ok: true, url }
}
