import test from 'node:test'
import assert from 'node:assert/strict'

import { watcherPill } from './watcherStatus.js'

test('a healthy watcher, or one never set up, shows nothing', () => {
  assert.equal(watcherPill({ state: 'ok', minutes_since: 2 }), null)
  assert.equal(watcherPill({ state: 'never' }), null)
  assert.equal(watcherPill(null), null)
})

test('a late watcher is loud and says how to restart it', () => {
  const pill = watcherPill({ state: 'late', minutes_since: 47 })
  assert.equal(pill.tone, 'danger')
  assert.equal(pill.label, 'Watcher stopped · 47m ago')
  assert.match(pill.detail, /launchctl kickstart/)
})

test('long outages read in hours and days', () => {
  assert.equal(watcherPill({ state: 'late', minutes_since: 185 }).label, 'Watcher stopped · 3h ago')
  assert.equal(watcherPill({ state: 'late', minutes_since: 60 * 72 }).label, 'Watcher stopped · 3d ago')
})

test('a sweep that cannot read Jira names the error', () => {
  const pill = watcherPill({ state: 'error', queue_error: 'JiraAuthError: expired' })
  assert.equal(pill.tone, 'warn')
  assert.match(pill.detail, /JiraAuthError/)
})
