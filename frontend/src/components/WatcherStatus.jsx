import { useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'
import { watcherPill } from '../utils/watcherStatus'

// Rechecked every two minutes: a laptop waking from sleep reads "late" until
// launchd's catch-up sweep lands, and that should clear without a reload.
const POLL_MS = 2 * 60 * 1000

const WatcherStatus = () => {
  const [health, setHealth] = useState(null)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/health/watcher`)
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

  const pill = watcherPill(health)
  if (!pill) return null
  return (
    <span className="tip" data-align="end">
      <span className="tokpill" data-state={pill.tone} tabIndex={0} aria-label={`${pill.label}. ${pill.detail}`}>
        <span className="dot" data-state={pill.tone} />
        {pill.label}
      </span>
      {/* Wraps: .tip-body is nowrap, sized for one-line labels. */}
      <span className="tip-body" style={{ whiteSpace: 'normal', width: 360 }}>{pill.detail}</span>
    </span>
  )
}

export default WatcherStatus
