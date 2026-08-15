import assert from 'node:assert/strict'

import { test } from 'vitest'

import { isProfileCollectionPath, profileNameFromRequestPath, requestPathname } from './profile-request-path'

test('profileNameFromRequestPath extracts and normalizes dynamic profile authority', () => {
  assert.equal(profileNameFromRequestPath('/api/profiles/Worker/soul?source=desktop'), 'worker')
  assert.equal(profileNameFromRequestPath('/api/profiles/my%2Dprofile'), 'my-profile')
})

test('profileNameFromRequestPath ignores static profile collection resources', () => {
  assert.equal(profileNameFromRequestPath('/api/profiles/active'), null)
  assert.equal(profileNameFromRequestPath('/api/profiles/import'), null)
  assert.equal(profileNameFromRequestPath('/api/profiles/projects/tree'), null)
  assert.equal(profileNameFromRequestPath('/api/profiles/sessions/sidebar'), null)
})

test('profileNameFromRequestPath fails closed on malformed encoding', () => {
  assert.equal(profileNameFromRequestPath('/api/profiles/%E0%A4%A'), null)
})

test('profile collection and pathname parsing ignore query authority', () => {
  assert.equal(isProfileCollectionPath('/api/profiles?source=desktop'), true)
  assert.equal(isProfileCollectionPath('/api/profiles/worker?source=desktop'), false)
  assert.equal(requestPathname('/api/profiles/worker?source=desktop'), '/api/profiles/worker')
})
