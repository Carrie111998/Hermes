import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const workflow = readFileSync(new URL('../../../.github/workflows/desktop-release.yml', import.meta.url), 'utf8')
const autoReleaseWorkflow = readFileSync(
  new URL('../../../.github/workflows/desktop-auto-release.yml', import.meta.url),
  'utf8'
)

test('desktop release uses short-lived GitHub OIDC credentials only', () => {
  assert.match(workflow, /permissions:\s+contents: read\s+id-token: write/)
  assert.match(autoReleaseWorkflow, /permissions:\s+contents: write[^\n]*\s+id-token: write/)
  assert.match(workflow, /aws-actions\/configure-aws-credentials@v4/)
  assert.match(workflow, /arn:aws:iam::970547373533:role\/github-actions-desktop-release/)
  assert.doesNotMatch(workflow, /secrets\.AWS_ACCESS_KEY_ID|secrets\.AWS_SECRET_ACCESS_KEY/)
})

test('both artifact and download-page publishers assume the scoped role', () => {
  const roleUses = workflow.match(/role-to-assume: arn:aws:iam::970547373533:role\/github-actions-desktop-release/g) ?? []

  assert.equal(roleUses.length, 2)
  assert.match(workflow, /role-session-name: desktop-release-\$\{\{ matrix\.brand \}\}-\$\{\{ github\.run_id \}\}/)
  assert.match(workflow, /role-session-name: desktop-download-page-\$\{\{ github\.run_id \}\}/)
})
