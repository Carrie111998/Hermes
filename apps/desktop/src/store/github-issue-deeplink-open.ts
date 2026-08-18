import { requestComposerFocus, requestComposerInsert } from '@/app/chat/composer/focus'
import { translateNow } from '@/i18n'
import { GITHUB_ISSUE_DEEPLINK_ERROR_KEYS, parseGithubIssueDeepLink } from '@/lib/github-issue-deeplink'

import { notify } from './notifications'

/**
 * `hermes://github-issue/open` -> a reviewable investigation task in the main
 * composer, or a toast saying why the link was refused. Nothing is sent: the
 * user reads the task, edits it if they want, and presses Send.
 *
 * The task text is fixed copy plus the validated URL, so a link can never put
 * words of its own in the composer.
 */
export function requestGithubIssueTaskFromDeepLink(params: Record<string, string | undefined>): void {
  const result = parseGithubIssueDeepLink(params)

  if (!result.ok) {
    notify({
      kind: 'error',
      title: translateNow('composer.githubIssueDeepLink.errorTitle'),
      message: translateNow(`composer.githubIssueDeepLink.${GITHUB_ISSUE_DEEPLINK_ERROR_KEYS[result.error]}`)
    })

    return
  }

  requestComposerInsert(`${translateNow('composer.githubIssueDeepLink.task')}\n\n${result.url}`, {
    mode: 'block',
    target: 'main'
  })
  requestComposerFocus('main')
}
