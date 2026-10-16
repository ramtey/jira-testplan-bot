import test from 'node:test'
import assert from 'node:assert/strict'

import { planPostState } from './postState.js'

const state = (over = {}) => ({
  ticketKey: 'SK-1',
  live_own_plan: null,
  last_auto_post: null,
  auto_post_status: 'In Testing',
  queue_status: 'Ready to Test',
  ...over,
})
const plan = { plan_id: 9, version: 3 }
const runs = [{ plan_id: 9, ticket_keys: ['SK-1'] }]
const at = (over) => planPostState({ plan, ticketKey: 'sk-1', ticketStatus: 'In Testing', postState: state(over), runs })

test('the live version is quiet, with a chip instead of a button to press', () => {
  const s = at({ live_own_plan: { plan_id: 9, version: 3 } })
  assert.equal(s.mode, 'live')
  assert.equal(s.prominent, false)
  assert.equal(s.chip, 'Live in Jira · v3')
})

test('a regeneration behind a live plan asks to update, naming both versions', () => {
  const s = at({ live_own_plan: { plan_id: 4, version: 2 } })
  assert.equal(s.mode, 'outdated')
  assert.equal(s.prominent, true)
  assert.equal(s.buttonText, 'Update Jira · v2 → v3')
})

test('a failed auto-post of this plan is loud and says why', () => {
  const s = at({ last_auto_post: { action: 'failed', reason: 'post_failed', detail: 'JiraConnectionError: down', plan_id: 9 } })
  assert.equal(s.mode, 'auto_missed')
  assert.equal(s.prominent, true)
  assert.match(s.hint, /JiraConnectionError/)
})

test('an outcome recorded for another plan is not this plan’s explanation', () => {
  const s = at({ last_auto_post: { action: 'skipped', reason: 'empty_plan', plan_id: 4 } })
  assert.equal(s.mode, 'unposted')
})

test('a no-plan-yet skip says nothing about the plan that exists now', () => {
  const s = at({ last_auto_post: { action: 'skipped', reason: 'no_own_plan', plan_id: null } })
  assert.equal(s.mode, 'unposted')
})

test('auto-post is promised only on a ticket waiting in the queue', () => {
  const queued = planPostState({ plan, ticketKey: 'SK-1', ticketStatus: 'Ready to Test', postState: state(), runs })
  assert.equal(queued.mode, 'auto_pending')
  assert.equal(queued.prominent, false)
  // Past testing (or anywhere else) nothing is coming, so no promise.
  const done = planPostState({ plan, ticketKey: 'SK-1', ticketStatus: 'Ready for UAT', postState: state(), runs })
  assert.equal(done.mode, 'unposted')
  assert.equal(done.prominent, true)
})

test('no promise when auto-post is switched off', () => {
  const s = planPostState({ plan, ticketKey: 'SK-1', ticketStatus: 'Ready to Test', postState: state({ auto_post_status: null }), runs })
  assert.equal(s.mode, 'unposted')
})

test('another ticket’s state is never used', () => {
  // The plan is SK-2's own, so only the ticket tag on postState can stop it.
  const ownRuns = [{ plan_id: 9, ticket_keys: ['SK-2'] }]
  const s = planPostState({ plan, ticketKey: 'SK-2', ticketStatus: 'In Testing', postState: state({ live_own_plan: { plan_id: 9, version: 3 } }), runs: ownRuns })
  assert.equal(s.mode, 'unknown')
})

test('unknown server state falls back to the plain button, not "not live"', () => {
  const s = planPostState({ plan, ticketKey: 'SK-1', ticketStatus: 'In Testing', postState: null })
  assert.equal(s.mode, 'unknown')
  assert.equal(s.prominent, true)
})

test('an unsaved plan falls back to the plain button', () => {
  const s = planPostState({ plan: { happy_path: [] }, ticketKey: 'SK-1', ticketStatus: 'In Testing', postState: state(), runs })
  assert.equal(s.mode, 'unknown')
})

test('a batch plan shown on one ticket never gets the smart states', () => {
  // Posting it from here would put the batch plan in the ticket's own slot.
  const batchRuns = [{ plan_id: 9, ticket_keys: ['SK-1', 'SK-2'] }]
  const s = planPostState({ plan, ticketKey: 'SK-1', ticketStatus: 'In Testing', postState: state({ live_own_plan: { plan_id: 4, version: 2 } }), runs: batchRuns })
  assert.equal(s.mode, 'unknown')
})

test('a plan missing from the run history is not assumed to be the ticket’s own', () => {
  const s = planPostState({ plan, ticketKey: 'SK-1', ticketStatus: 'In Testing', postState: state({ live_own_plan: { plan_id: 9, version: 3 } }), runs: [] })
  assert.equal(s.mode, 'unknown')
})

test('an auto-post that stopped partway is not shown as cleanly live', () => {
  const s = at({
    live_own_plan: { plan_id: 9, version: 3, posted_at: '2026-10-16T10:00:00+00:00' },
    last_auto_post: { action: 'failed', reason: 'partial', detail: 'only 1 of 3 comments landed', plan_id: 9, at: '2026-10-16T10:00:01+00:00' },
  })
  assert.equal(s.mode, 'live_incomplete')
  assert.equal(s.prominent, true)
  assert.match(s.hint, /1 of 3/)
})

test('a re-post after the incomplete auto-post clears the warning', () => {
  const s = at({
    live_own_plan: { plan_id: 9, version: 3, posted_at: '2026-10-16T11:00:00+00:00' },
    last_auto_post: { action: 'failed', reason: 'partial', detail: 'x', plan_id: 9, at: '2026-10-16T10:00:01+00:00' },
  })
  assert.equal(s.mode, 'live')
})

test('a stale plan says why and offers both ways out', () => {
  const s = at({ last_auto_post: { action: 'skipped', reason: 'stale_plan', detail: 'pr/2 merged after the plan was written', plan_id: 9 } })
  assert.equal(s.mode, 'auto_missed')
  assert.match(s.hint, /merged after/)
  assert.match(s.hint, /Regenerate/)
})

test('a post the app did not record says the plan is on the ticket', () => {
  const s = at({ last_auto_post: { action: 'failed', reason: 'not_recorded', detail: 'posted, but db down', plan_id: 9 } })
  assert.equal(s.warning, 'Not recorded')
  assert.match(s.hint, /on the ticket/)
})
