import test from 'node:test'
import assert from 'node:assert/strict'

import { uatPill } from './uatStatus.js'

test('nothing to say for a healthy idle dispatcher or none at all', () => {
  assert.equal(uatPill(null), null)
  assert.equal(uatPill({ state: 'never', awaiting_review: [], running: [] }), null)
  assert.equal(uatPill({ state: 'ok', awaiting_review: [], running: [] }), null)
})

test('a stopped dispatcher wins over runs to review', () => {
  const p = uatPill({ state: 'late', minutes_since: 90, awaiting_review: [{ run_id: 1, ticket_key: 'PROJ-1' }] })
  assert.equal(p.tone, 'danger')
  assert.match(p.label, /stopped · 1h ago/)
})

test('runs awaiting review are counted and named once per ticket', () => {
  const p = uatPill({ state: 'ok', awaiting_review: [
    { run_id: 3, ticket_key: 'PROJ-2' }, { run_id: 2, ticket_key: 'PROJ-1' }, { run_id: 1, ticket_key: 'PROJ-1' }] })
  assert.equal(p.label, '3 UAT runs to review')
  assert.deepEqual(p.tickets, ['PROJ-2', 'PROJ-1'])
})
