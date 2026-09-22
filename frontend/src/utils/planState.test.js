/**
 * The SK-2327 regression.
 *
 * The bug: the view rendered a leftover 14-case plan for a ticket whose stored
 * plan has 15 cases, derived the progress key from what it was rendering, polled
 * `SK-2327:3-4-0-7` — a key nothing had ever written — and showed 0%, while 11
 * cases sat marked under `SK-2327:4-5-0-6`.
 *
 * Two independent defects had to line up, and each gets a test here:
 *
 *   1. the plan cache carried no ticket identity, so it could be restored under
 *      any ticket; and
 *   2. the stored plan was merged in with `prev ?? stored`, so anything already
 *      in the slot — which, after a reload, is always the cache — shut it out
 *      permanently.
 *
 * Run with `npm test` (node's built-in runner; no dependencies).
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  EMPTY_PLAN_STATE,
  ORIGIN,
  nextPlanState,
  planOwner,
  readStoredPlan,
  writeStoredPlan,
} from './planState.js'

const plan = (n) => ({ happy_path: Array.from({ length: n }, (_, i) => ({ title: `h${i}` })) })

// ---------------------------------------------------------------------------
// Precedence
// ---------------------------------------------------------------------------

test('the stored plan replaces one restored from the cache', () => {
  // This is the bug. `prev ?? stored` kept the restored plan; the ticket's real
  // plan never reached the screen, and the progress key followed the phantom.
  const restored = { plan: plan(14), origin: ORIGIN.restored, ticketKeys: ['SK-2327'] }
  const stored = { plan: plan(15), origin: ORIGIN.stored, ticketKeys: ['SK-2327'] }
  assert.equal(nextPlanState(restored, stored).plan, stored.plan)
})

test('a plan generated in this session is not clobbered by a stored-plan read', () => {
  // The reason `prev ??` was there in the first place: an in-flight read of the
  // previous version must not overwrite what the tester just generated.
  const generated = { plan: plan(9), origin: ORIGIN.generated, ticketKeys: ['SK-2327'] }
  const stored = { plan: plan(15), origin: ORIGIN.stored, ticketKeys: ['SK-2327'] }
  assert.equal(nextPlanState(generated, stored).plan, generated.plan)
})

test('a cache read landing late cannot undo a generate', () => {
  const generated = { plan: plan(9), origin: ORIGIN.generated, ticketKeys: ['SK-2327'] }
  const restored = { plan: plan(14), origin: ORIGIN.restored, ticketKeys: ['SK-2327'] }
  assert.equal(nextPlanState(generated, restored).plan, generated.plan)
})

test('a stored plan does not replace a stored plan', () => {
  // Two hydrations of the same ticket must be idempotent, not a swap race.
  const first = { plan: plan(15), origin: ORIGIN.stored, ticketKeys: ['SK-2327'] }
  const second = { plan: plan(15), origin: ORIGIN.stored, ticketKeys: ['SK-2327'] }
  assert.equal(nextPlanState(first, second).plan, first.plan)
})

test('anything fills an empty slot', () => {
  const restored = { plan: plan(3), origin: ORIGIN.restored, ticketKeys: ['SK-1'] }
  assert.equal(nextPlanState(EMPTY_PLAN_STATE, restored).plan, restored.plan)
  assert.equal(nextPlanState(null, restored).plan, restored.plan)
})

test('an empty incoming state is ignored', () => {
  const stored = { plan: plan(15), origin: ORIGIN.stored, ticketKeys: ['SK-2327'] }
  assert.equal(nextPlanState(stored, null).plan, stored.plan)
  assert.equal(nextPlanState(stored, { plan: null, origin: ORIGIN.stored }).plan, stored.plan)
})

// ---------------------------------------------------------------------------
// Cache identity
// ---------------------------------------------------------------------------

test('a cached plan is restored for the ticket it was cached under', () => {
  const raw = writeStoredPlan({ plan: plan(15), origin: ORIGIN.stored, ticketKeys: ['SK-2327'] })
  const back = readStoredPlan(raw, ['SK-2327'])
  assert.deepEqual(back.plan, plan(15))
  assert.equal(back.origin, ORIGIN.restored, 'a rehydrated plan is `restored`, whatever it was when cached')
})

test("a cached plan is dropped when it belongs to another ticket", () => {
  const raw = writeStoredPlan({ plan: plan(15), origin: ORIGIN.stored, ticketKeys: ['SK-9999'] })
  assert.equal(readStoredPlan(raw, ['SK-2327']).plan, null)
})

test('a legacy bare-plan cache entry is dropped, not guessed at', () => {
  // What the old code wrote: the plan object itself, with no ticket anywhere.
  assert.equal(readStoredPlan(plan(14), ['SK-2327']).plan, null)
})

test('a plan is not restored when the load has no ticket to match against', () => {
  const raw = writeStoredPlan({ plan: plan(15), origin: ORIGIN.stored, ticketKeys: ['SK-2327'] })
  assert.equal(readStoredPlan(raw, []).plan, null)
})

test('ticket identity ignores case and surrounding space', () => {
  const raw = writeStoredPlan({ plan: plan(15), origin: ORIGIN.stored, ticketKeys: [' sk-2327 '] })
  assert.deepEqual(raw.ticketKeys, ['SK-2327'])
  assert.deepEqual(readStoredPlan(raw, ['SK-2327']).plan, plan(15))
})

test('a multi-ticket plan matches only the same bundle, in the same order', () => {
  const raw = writeStoredPlan({ plan: plan(20), origin: ORIGIN.stored, ticketKeys: ['SK-1', 'SK-2'] })
  assert.deepEqual(readStoredPlan(raw, ['SK-1', 'SK-2']).plan, plan(20))
  assert.equal(readStoredPlan(raw, ['SK-1']).plan, null)
  // Order is part of the progress key (`SK-1+SK-2:…`), so it is part of identity.
  assert.equal(readStoredPlan(raw, ['SK-2', 'SK-1']).plan, null)
})

test('an unownable plan is never cached', () => {
  assert.equal(writeStoredPlan({ plan: plan(3), origin: ORIGIN.generated, ticketKeys: [] }), null)
  assert.equal(writeStoredPlan(EMPTY_PLAN_STATE), null)
  assert.equal(writeStoredPlan(null), null)
})

test('corrupt cache entries degrade to empty rather than throwing', () => {
  for (const raw of [null, undefined, 'nonsense', 42, [], { ticketKeys: ['SK-2327'] }]) {
    assert.equal(readStoredPlan(raw, ['SK-2327']).plan, null)
  }
})

test('planOwner normalizes a bare string and drops blanks', () => {
  assert.deepEqual(planOwner('sk-2327'), ['SK-2327'])
  assert.deepEqual(planOwner(['SK-1', '', null, '  ']), ['SK-1'])
})
