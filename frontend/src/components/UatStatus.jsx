import { useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'
import { uatPill } from '../utils/uatStatus'

const POLL_MS = 60 * 1000

// Header pill for unattended UAT runs: dispatcher stopped, or runs waiting
// for review. Clicking a ticket in the tooltip opens it.
const UatStatus = ({ onOpenTicket }) => {
  const [health, setHealth] = useState(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/health/uat`)
        const data = res.ok ? await res.json() : null
        if (!cancelled) setHealth(data)
      } catch {
        if (!cancelled) setHealth(null)
      }
    }
    load()
    const id = setInterval(load, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [])

  const pill = uatPill(health)
  if (!pill) return null
  return (
    <span className="tip" data-align="end">
      <span className="tokpill" data-state={pill.tone} tabIndex={0} aria-label={`${pill.label}. ${pill.detail}`}
        role={pill.tickets.length === 1 && onOpenTicket ? 'button' : undefined}
        onClick={pill.tickets.length === 1 && onOpenTicket ? () => onOpenTicket(pill.tickets[0]) : undefined}>
        <span className="dot" data-state={pill.tone} />
        {pill.label}
      </span>
      <span className="tip-body" style={{ whiteSpace: 'normal', width: 360 }}>{pill.detail}</span>
    </span>
  )
}

export default UatStatus
