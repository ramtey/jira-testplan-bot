import test from 'node:test'
import assert from 'node:assert/strict'

import { describeAutoPost } from './autoPost.js'

test('a transition that does not auto-post adds nothing', () => {
  assert.equal(describeAutoPost(null), '')
})

test('a post says it posted', () => {
  assert.equal(describeAutoPost({ action: 'posted', detail: 'plan 7' }), ' · plan posted to Jira')
})

test('a failure is named with its reason, never dropped', () => {
  const text = describeAutoPost({ action: 'failed', reason: 'post_failed', detail: 'JiraConnectionError: down' })
  assert.match(text, /NOT posted/)
  assert.match(text, /JiraConnectionError/)
})

test('an unknown skip reason still shows up', () => {
  assert.match(describeAutoPost({ action: 'skipped', reason: 'something_new' }), /something_new/)
})

test('a queued post says it is on its way, not that it landed', () => {
  assert.equal(describeAutoPost({ action: 'queued' }), ' · posting the plan to Jira')
})

test('a regeneration that replaced the live comment says it updated', () => {
  assert.match(describeAutoPost({ action: 'posted', reason: 'updated', detail: 'plan 9 (v2) updated in place' }), /updated in Jira/)
})

test('a newer version held back names the version Jira does not show', () => {
  const text = describeAutoPost({ action: 'skipped', reason: 'already_live', detail: 'v2 not posted to Jira: plan 7 (v1) was posted after it was generated, so it was chosen' })
  assert.match(text, /v2 not posted to Jira/)
})
