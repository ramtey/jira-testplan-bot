import { useCallback, useEffect, useRef, useState } from 'react'
import { API_BASE_URL } from '../config'

// The canonical hold reasons, mirroring HOLD_REASONS in
// src/app/db/models/ticket_hold.py. The backend owns the codes (it validates
// them); the wording lives here so copy changes don't need a migration.
export const HOLD_REASONS = [
  { code: 'code-review', label: 'Waiting for code review' },
  { code: 'dependency', label: 'Waiting on other work' },
  { code: 'design-pm', label: 'Waiting on design / PM' },
  { code: 'environment', label: 'Environment down' },
  { code: 'other', label: 'Other' },
]

export const holdReasonLabel = (code) =>
  HOLD_REASONS.find((r) => r.code === code)?.label || 'On hold'

// Owns a ticket's shared QA hold — "testing is parked, and here's why".
// Lifted to App level (not TicketDetails) because the browser tab title needs
// it too: with one ticket per tab, the ⏸ marker beside the key is how you spot
// the parked tabs without clicking through them.
//
// Holds are shared server-side and not keyed by user, so a hold set by one
// tester shows up for everyone. Multi-ticket bundles are skipped: a bundle has
// no single ticket to park.
export function useTicketHold(ticketKey) {
  const [hold, setHold] = useState(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  // Guards against a slow response for a previous ticket landing after the
  // user has already switched — same last-selection-wins rule as the fetches
  // in App.
  const tokenRef = useRef({ cancelled: false })

  useEffect(() => {
    tokenRef.current.cancelled = true
    const token = { cancelled: false }
    tokenRef.current = token
    setError(null)
    if (!ticketKey) {
      setHold(null)
      return undefined
    }
    ;(async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/tickets/${ticketKey}/hold`)
        if (!res.ok) return
        const data = await res.json()
        if (!token.cancelled) setHold(data)
      } catch {
        // A missing hold is indistinguishable from no hold as far as the UI is
        // concerned, so a failed read stays silent rather than throwing an
        // error banner over a ticket that loaded fine.
      }
    })()
    return () => {
      token.cancelled = true
    }
  }, [ticketKey])

  const setHoldOn = useCallback(
    async (reason, note) => {
      if (!ticketKey) return null
      setSaving(true)
      setError(null)
      try {
        const res = await fetch(`${API_BASE_URL}/tickets/${ticketKey}/hold`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason, note: note || null }),
        })
        if (!res.ok) {
          const data = await res.json().catch(() => ({}))
          throw new Error(data.detail || 'Failed to put the ticket on hold')
        }
        const data = await res.json()
        setHold(data)
        return data
      } catch (err) {
        setError(err.message)
        return null
      } finally {
        setSaving(false)
      }
    },
    [ticketKey]
  )

  const resume = useCallback(async () => {
    if (!ticketKey) return null
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE_URL}/tickets/${ticketKey}/hold`, {
        method: 'DELETE',
      })
      if (!res.ok) throw new Error('Failed to resume the ticket')
      const data = await res.json()
      setHold(data)
      return data
    } catch (err) {
      setError(err.message)
      return null
    } finally {
      setSaving(false)
    }
  }, [ticketKey])

  return {
    held: hold?.held ?? false,
    reason: hold?.reason ?? null,
    note: hold?.note ?? null,
    heldSince: hold?.held_since ?? null,
    saving,
    error,
    setHoldOn,
    resume,
  }
}
