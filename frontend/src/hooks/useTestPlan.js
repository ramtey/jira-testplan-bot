import { useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'
import { loadStored, saveStored } from '../utils/sessionStorage'

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
 * generate() resolves to the plan on success and null when aborted. Errors are
 * captured in `error` and also thrown so callers can short-circuit if needed.
 */
export function useTestPlan() {
  const [generating, setGenerating] = useState(false)
  const [plan, setPlan] = useState(() => loadStored(STORAGE_KEY, null))
  const [error, setError] = useState(null)
  const [controller, setController] = useState(null)

  useEffect(() => saveStored(STORAGE_KEY, plan), [plan])

  const reset = () => {
    setPlan(null)
    setError(null)
  }

  const stop = () => {
    if (controller) controller.abort()
  }

  const generate = async (ticketsData) => {
    if (!ticketsData || ticketsData.length === 0) return null

    const abort = new AbortController()
    setController(abort)
    setGenerating(true)
    setError(null)
    setPlan(null)

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
      setPlan(data)
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

  return { generating, plan, error, setPlan, generate, stop, reset }
}
