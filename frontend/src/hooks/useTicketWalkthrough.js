import { useCallback, useEffect, useRef, useState } from 'react'
import { API_BASE_URL } from '../config'

// Owns the ticket's UAT walkthrough (Loom / screenshots / notes) and its
// UAT complexity flag. Both the walkthrough card (author + save) and the
// Pass-to-UAT workflow gate (read-only material check) consume this — keeping
// the fetch, save, and shape in one place stops the two panels from drifting.
export function useTicketWalkthrough(ticketKey) {
  const [walkthrough, setWalkthrough] = useState(null)
  const [saving, setSaving] = useState(false)
  const cancelRef = useRef({ cancelled: false })

  const fetchOnce = useCallback(async () => {
    if (!ticketKey) return null
    try {
      const r = await fetch(`${API_BASE_URL}/tickets/${ticketKey}/walkthrough`)
      if (!r.ok) return null
      return await r.json()
    } catch {
      return null
    }
  }, [ticketKey])

  useEffect(() => {
    if (!ticketKey) {
      setWalkthrough(null)
      return
    }
    const token = { cancelled: false }
    cancelRef.current = token
    fetchOnce().then((data) => {
      if (token.cancelled || !data) return
      setWalkthrough(data)
    })
    return () => {
      token.cancelled = true
    }
  }, [ticketKey, fetchOnce])

  // Callable refetch — the Pass-to-UAT gate uses this because the walkthrough
  // card may have saved after the workflow panel first mounted. Returns the
  // fresh data (or null on failure) so callers can decide without waiting for
  // React state to settle.
  const refresh = useCallback(async () => {
    const data = await fetchOnce()
    if (data && !cancelRef.current.cancelled) setWalkthrough(data)
    return data
  }, [fetchOnce])

  const save = useCallback(
    async (payload) => {
      if (!ticketKey) return null
      setSaving(true)
      try {
        const form = new FormData()
        const jsonPayload = {
          loom_url: payload.loom_url || null,
          notes: payload.notes || null,
          existing_screenshots: (payload.existing_screenshots || []).map((s) => ({
            url: s.url,
            filename: s.filename || null,
            media_id: s.media_id || null,
          })),
        }
        form.append('payload', JSON.stringify(jsonPayload))
        for (const file of payload.new_files || []) {
          form.append('screenshots', file, file.name)
        }
        const res = await fetch(
          `${API_BASE_URL}/tickets/${ticketKey}/walkthrough`,
          { method: 'PUT', body: form }
        )
        if (!res.ok) {
          const data = await res.json().catch(() => ({}))
          throw new Error(data.detail || 'Failed to save walkthrough')
        }
        const data = await res.json()
        setWalkthrough(data)
        return data
      } finally {
        setSaving(false)
      }
    },
    [ticketKey]
  )

  return {
    walkthrough,
    uatComplexity: walkthrough?.uat_complexity ?? null,
    // Server-computed readiness signal. Both the walkthrough card ("does the
    // saved walkthrough have material?") and the Pass-to-UAT gate ("does this
    // high-complexity ticket still need one?") read from these instead of
    // re-deriving the rule locally.
    walkthroughPresent: walkthrough?.walkthrough_present ?? false,
    walkthroughSources: walkthrough?.walkthrough_sources ?? [],
    needsWalkthrough: walkthrough?.needs_walkthrough ?? false,
    saving,
    refresh,
    save,
  }
}
