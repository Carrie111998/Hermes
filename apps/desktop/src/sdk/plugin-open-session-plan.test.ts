import assert from 'node:assert/strict'
import test from 'node:test'

import { planPluginOpenSession } from './plugin-open-session-plan.ts'

test('keepAllProfilesScope defaults to navigation: dial worker, do not steal chrome', () => {
  const plan = planPluginOpenSession({
    profile: 'worker',
    activeProfile: 'default'
  })

  assert.equal(plan.switchWorkspace, null)
  assert.equal(plan.dialWithoutSwitching, 'worker')
  assert.equal(plan.showAllProfiles, true)
  assert.equal(plan.requireActiveProfileForHydration, false)
})

test('keepAllProfilesScope:true does not steal chrome even when the flag is explicit', () => {
  const plan = planPluginOpenSession({
    profile: 'worker',
    activeProfile: 'default',
    keepAllProfilesScope: true
  })

  assert.equal(plan.switchWorkspace, null)
  assert.equal(plan.dialWithoutSwitching, 'worker')
  assert.equal(plan.showAllProfiles, true)
  assert.equal(plan.requireActiveProfileForHydration, false)
})

test('keepAllProfilesScope:true does not re-dial when chrome is already on that profile', () => {
  const plan = planPluginOpenSession({
    profile: 'worker',
    activeProfile: 'worker',
    keepAllProfilesScope: true
  })

  assert.equal(plan.switchWorkspace, null)
  assert.equal(plan.dialWithoutSwitching, null)
  assert.equal(plan.showAllProfiles, true)
})

test('keepAllProfilesScope:false is an explicit workspace switch and collapses the sidebar', () => {
  const plan = planPluginOpenSession({
    profile: 'worker',
    activeProfile: 'default',
    keepAllProfilesScope: false
  })

  assert.equal(plan.switchWorkspace, 'worker')
  assert.equal(plan.dialWithoutSwitching, null)
  assert.equal(plan.showAllProfiles, false)
  assert.equal(plan.requireActiveProfileForHydration, true)
})

test('an empty profile is neither a dial nor a workspace switch', () => {
  const plan = planPluginOpenSession({
    profile: '  ',
    activeProfile: 'default',
    keepAllProfilesScope: true
  })

  assert.equal(plan.switchWorkspace, null)
  assert.equal(plan.dialWithoutSwitching, null)
  assert.equal(plan.showAllProfiles, null)
})
