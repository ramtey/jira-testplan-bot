import { useEffect, useRef, useState } from 'react'
import { API_BASE_URL } from '../config'
import { loadStored, saveStored } from '../utils/sessionStorage'
import {
  EMPTY_PLAN_STATE,
  ORIGIN,
  nextPlanState,
  planOwner,
  readStoredPlan,
  writeStoredPlan,
} from '../utils/planState'

const STORAGE_KEY = 'jtb.testPlan'

// Both single- and multi-ticket payloads share this per-ticket shape. The
// backend ignores extra fields, so this is also forward-compatible.
function buildTicketPayload(td) {
  return {
    ticket_key: td.key,
    summary: td.summary,
    description: td.description,
    issue_type: td.issue_type,
    testing_context: {},
    development_info: td.development_info,
    // Jira's dev-status endpoint didn't answer for this ticket, so an absent
    // development_info means "unknown", not "no PR". The server needs that
    // distinction before it can refuse a plan for lack of an implementation.
    dev_status_unavailable: !!td.dev_status_unavailable,
    // The ticket linked a design we couldn't read (expired token, rate limit,
    // no access). Absent design context would otherwise be indistinguishable
    // from a ticket that never had a design, and the plan would quietly skip
    // visual checks instead of telling the tester it couldn't make them.
    figma_unavailable: !!td.figma_unavailable,
    comments_unavailable: !!td.comments_unavailable,
    image_urls: td.attachments ? td.attachments.map((a) => a.url) : null,
    comments: td.comments || null,
    parent_info: td.parent || null,
    child_info: td.children || null,
    linked_info: td.linked_issues || null,
    bounce_history: td.bounce_history || null,
  }
}

/**
 * Owns test-plan generation state: the plan itself, the in-flight controller,
 * and error/loading flags. Caller passes `ticketsData` at generate-time so the
 * hook doesn't have to subscribe to the upstream ticket store.
 *
 * `restoringKeys` is the ticket(s) this page load is opening, needed at mount:
 * the sessionStorage cache is scoped to its ticket, so a cached plan belonging
 * to anything else is dropped instead of rendered under the wrong ticket.
 *
 * The plan is held with its provenance (see `utils/planState`) so a cached copy
 * can never outrank the plan the server has — the bug that rendered SK-2327's
 * 15-case stored plan as a leftover 14-case one, and with it a progress key
 * nothing had ever written.
 *
 * generate() resolves to the plan on success and null when aborted. Errors are
 * captured in `error` and also thrown so callers can short-circuit if needed.
 */
export function useTestPlan(restoringKeys = []) {
  const [generating, setGenerating] = useState(false)
  const [state, setState] = useState(() =>
    readStoredPlan(loadStored(STORAGE_KEY, null), restoringKeys)
  )
  const [error, setError] = useState(null)
  const [controller, setController] = useState(null)

  useEffect(() => saveStored(STORAGE_KEY, writeStoredPlan(state)), [state])

  // Kept in a ref as well so the async generate/adopt paths can apply their
  // result against the state at the moment it lands, not the one they closed
  // over when they started.
  const stateRef = useRef(state)
  stateRef.current = state

  const apply = (incoming) => {
    const next = nextPlanState(stateRef.current, incoming)
    stateRef.current = next
    setState(next)
    return next.plan
  }

  const reset = () => {
    stateRef.current = EMPTY_PLAN_STATE
    setState(EMPTY_PLAN_STATE)
    setError(null)
  }

  const stop = () => {
    if (controller) controller.abort()
  }

  /**
   * Show the plan the server has for this ticket.
   *
   * Outranks a plan restored from this tab's cache — that's the whole point —
   * and yields to one generated in this session, which is newer than any read
   * that was already in flight.
   */
  const adoptStored = (ticketKeys, plan) =>
    apply({ plan, origin: ORIGIN.stored, ticketKeys: planOwner(ticketKeys) })

  /** Replace the plan outright. Used by callers that own the plan's identity. */
  const setPlan = (plan, ticketKeys) =>
    plan === null
      ? reset()
      : apply({ plan, origin: ORIGIN.generated, ticketKeys: planOwner(ticketKeys) })

  const generate = async (ticketsData) => {
    if (!ticketsData || ticketsData.length === 0) return null

    const abort = new AbortController()
    setController(abort)
    setGenerating(true)
    setError(null)
    reset()

    const owner = planOwner(ticketsData.map((t) => t.key))

    try {
      const isMulti = ticketsData.length > 1
      const url = isMulti
        ? `${API_BASE_URL}/generate-test-plan/multi`
        : `${API_BASE_URL}/generate-test-plan`
      const body = isMulti
        ? { tickets: ticketsData.map(buildTicketPayload) }
        : buildTicketPayload(ticketsData[0])

      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: abort.signal,
      })

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}))
        throw new Error(errorData.detail || 'Failed to generate test plan')
      }

      const data = await response.json()
      apply({ plan: data, origin: ORIGIN.generated, ticketKeys: owner })
      return data
    } catch (err) {
      if (err.name === 'AbortError') {
        setError('Test plan generation was cancelled')
      } else {
        setError(err.message)
      }
      return null
    } finally {
      setGenerating(false)
      setController(null)
    }
  }

  return {
    generating,
    plan: state.plan,
    planOrigin: state.origin,
    error,
    setPlan,
    adoptStored,
    generate,
    stop,
    reset,
  }
}
