/**
 * Which test plan the view should hold, and how it survives a reload.
 *
 * Three plans can want the same slot on a single page load:
 *
 *   restored  — the copy this tab cached in sessionStorage last time
 *   stored    — the plan the server actually has for this ticket
 *   generated — a plan the tester just produced in this session
 *
 * They used to be indistinguishable. `useTestPlan` cached the plan under one
 * global key with no ticket and no provenance, and `hydrateStoredArtifacts`
 * merged the server's plan with `prev ?? stored` — "whichever arrives first
 * wins". A rehydrated plan is always first, so on any reload where the ticket
 * key hadn't changed (which is every reload of the same ticket, because
 * `ticketsData` is cached too and the same-keys path skips `testPlan.reset()`),
 * the cached copy permanently shut out the stored one.
 *
 * SK-2327 is what that looks like: plan 533 (15 cases, 4-5-0-6) sat in the
 * database with 11 cases marked passed, while the browser rendered a leftover
 * 14-case plan, derived the progress key 3-4-0-7 from what it was rendering,
 * polled a key nothing had ever written, and showed 0%.
 *
 * So provenance is explicit and ranked. A cached copy is a paint-fast
 * convenience and yields to anything authoritative; nothing can silently
 * outrank the server's record of the plan again.
 */

export const ORIGIN = {
  restored: 'restored',
  stored: 'stored',
  generated: 'generated',
}

// Higher wins. `stored` beats `restored` because the server is the record of
// what this ticket's plan *is*; `generated` beats `stored` because a plan the
// tester just made is newer than the read that was already in flight when they
// made it.
const RANK = { restored: 0, stored: 1, generated: 2 }

export const EMPTY_PLAN_STATE = { plan: null, origin: null, ticketKeys: [] }

/** Normalized, order-preserving identity for the ticket(s) a plan belongs to. */
export function planOwner(ticketKeys) {
  const list = Array.isArray(ticketKeys) ? ticketKeys : [ticketKeys]
  return list
    .filter((k) => typeof k === 'string' && k.trim())
    .map((k) => k.trim().toUpperCase())
}

/**
 * Apply an incoming plan to the current state, or keep the current one.
 *
 * Returns the state to render. An incoming plan of equal or lower rank is
 * dropped, so a late-landing cache read can't undo a generate and a stored plan
 * can't be shut out by the cache.
 */
export function nextPlanState(current, incoming) {
  const cur = current || EMPTY_PLAN_STATE
  if (!incoming || !incoming.plan) return cur
  if (!cur.plan) return incoming
  if (RANK[incoming.origin] > RANK[cur.origin]) return incoming
  return cur
}

/**
 * Decode the sessionStorage envelope, dropping anything that isn't this
 * ticket's.
 *
 * `runHistory` learned this the hard way and guards it (see STORAGE_KEYS.
 * runHistory in App.jsx); the plan never did, so the cache was a bare plan
 * object with no way to tell whose it was. Legacy bare-object entries written
 * by the old code are discarded rather than guessed at — one reload and the
 * cache heals.
 */
export function readStoredPlan(raw, ticketKeys) {
  const want = planOwner(ticketKeys)
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return EMPTY_PLAN_STATE
  if (!raw.plan || typeof raw.plan !== 'object') return EMPTY_PLAN_STATE
  const had = planOwner(raw.ticketKeys)
  if (want.length === 0 || had.length === 0) return EMPTY_PLAN_STATE
  if (want.join('+') !== had.join('+')) return EMPTY_PLAN_STATE
  return { plan: raw.plan, origin: ORIGIN.restored, ticketKeys: had }
}

/** The envelope to persist, or null to clear the cache. */
export function writeStoredPlan(state) {
  if (!state || !state.plan) return null
  const owner = planOwner(state.ticketKeys)
  // A plan with no owner can't be safely restored, so don't cache it at all —
  // that is precisely the entry that would come back under the wrong ticket.
  if (owner.length === 0) return null
  return { ticketKeys: owner, plan: state.plan }
}

/**
 * Which stored run a ticket's view should open, from `/runs/by-ticket/<key>`.
 *
 * Newest first is right until a batch is involved. A batch plan sits beside a
 * ticket's own plan, never on top of it, but it is usually the newer of the
 * two — so "newest" would open the batch on its host and hide the plan written
 * for that ticket alone. Its own plan wins; a ticket that has only the batch
 * (a pointer-only ticket like SK-2332) opens the batch.
 *
 * Runs without a `plan_id` are skipped: Bug Lens runs share the table.
 */
export function pickStoredRun(key, runs) {
  const withPlan = (runs || []).filter((r) => r?.plan_id)
  const own = withPlan.find(
    (r) => Array.isArray(r.ticket_keys) && r.ticket_keys.length === 1 &&
      String(r.ticket_keys[0]).toUpperCase() === String(key).toUpperCase()
  )
  return own || withPlan[0] || null
}
