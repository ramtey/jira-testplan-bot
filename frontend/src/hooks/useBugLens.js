import { useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'
import { loadStored, saveStored } from '../utils/sessionStorage'

const STORAGE_KEY = 'jtb.bugAnalysis'

function buildTicketPayload(td) {
  return {
    ticket_key: td.key,
    summary: td.summary,
    description: td.description,
    issue_type: td.issue_type,
    development_info: td.development_info,
    comments: td.comments || null,
    parent_info: td.parent || null,
    child_info: td.children || null,
    linked_info: td.linked_issues || null,
    status: td.status || null,
    status_category: td.status_category || null,
  }
}

/**
 * Owns Bug Lens analysis state. Mirrors useTestPlan so the App layer can treat
 * both flows the same way: kick off, abort, clear.
 */
export function useBugLens() {
  const [analyzing, setAnalyzing] = useState(false)
  const [analysis, setAnalysis] = useState(() => loadStored(STORAGE_KEY, null))
  const [error, setError] = useState(null)
  const [controller, setController] = useState(null)

  useEffect(() => saveStored(STORAGE_KEY, analysis), [analysis])

  const reset = () => {
    setAnalysis(null)
    setError(null)
  }

  const stop = () => {
    if (controller) controller.abort()
  }

  const analyze = async (ticketsData) => {
    if (!ticketsData || ticketsData.length === 0) return null

    const abort = new AbortController()
    setController(abort)
    setAnalyzing(true)
    setError(null)
    setAnalysis(null)

    const isMulti = ticketsData.length > 1
    const url = isMulti
      ? `${API_BASE_URL}/bug-lens/analyze/multi`
      : `${API_BASE_URL}/bug-lens/analyze`
    const body = isMulti
      ? { tickets: ticketsData.map(buildTicketPayload) }
      : buildTicketPayload(ticketsData[0])

    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: abort.signal,
      })

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}))
        throw new Error(errorData.detail || 'Failed to analyze bug')
      }

      const data = await response.json()
      setAnalysis(data)
      return data
    } catch (err) {
      if (err.name === 'AbortError') {
        setError('Bug analysis was cancelled')
      } else {
        setError(err.message)
      }
      return null
    } finally {
      setAnalyzing(false)
      setController(null)
    }
  }

  return { analyzing, analysis, error, setAnalysis, analyze, stop, reset }
}
