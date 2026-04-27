/**
 * "We've analyzed this ticket before" banner.
 *
 * Shows a collapsed summary when there are prior test-plan runs for the current
 * ticket. Expands to a list of versions with View (load into the active plan)
 * and Diff (open PlanDiffModal) actions.
 */

import { useMemo, useState } from 'react'
import PlanDiffModal from './PlanDiffModal'
import { API_BASE_URL } from '../config'

function formatRelative(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const diffMs = Date.now() - d.getTime()
  const mins = Math.round(diffMs / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return d.toLocaleDateString()
}

export default function RunHistoryBanner({ runs, ticketData, onViewPlan }) {
  const [expanded, setExpanded] = useState(false)
  const [diffTarget, setDiffTarget] = useState(null)
  const [loadingPlanId, setLoadingPlanId] = useState(null)
  const [error, setError] = useState(null)

  const latest = runs[0]
  const summary = useMemo(() => {
    if (!latest) return ''
    return `Last test plan generated ${formatRelative(latest.created_at)} (v${latest.version})`
  }, [latest])

  if (!runs || runs.length === 0) return null

  const handleView = async (run) => {
    setLoadingPlanId(run.plan_id)
    setError(null)
    try {
      const res = await fetch(`${API_BASE_URL}/plans/${run.plan_id}`)
      if (!res.ok) throw new Error('Failed to load plan')
      const data = await res.json()
      let parsed
      try {
        parsed = JSON.parse(data.body)
      } catch {
        throw new Error('Stored plan is not in the expected format')
      }
      onViewPlan(parsed, { planId: run.plan_id, version: run.version })
    } catch (err) {
      setError(err.message)
    } finally {
      setLoadingPlanId(null)
    }
  }

  return (
    <div className="run-history-banner">
      <div className="run-history-summary">
        <span className="run-history-icon" aria-hidden="true">🕘</span>
        <span className="run-history-text">
          <strong>{runs.length}</strong> prior run{runs.length === 1 ? '' : 's'} for this ticket — {summary}
        </span>
        <button
          type="button"
          className="run-history-toggle"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? 'Hide' : 'Show history'}
        </button>
      </div>

      {error && <div className="alert alert-error">{error}</div>}

      {expanded && (
        <ul className="run-history-list">
          {runs.map((run) => {
            const isMulti = run.run_type === 'test_plan_multi'
            const otherKeys = (run.ticket_keys || []).filter((k) => k !== ticketData?.key)
            return (
              <li key={run.run_id} className="run-history-item">
                <div className="run-history-meta">
                  <span className="run-history-version">v{run.version}</span>
                  <span className="run-history-when">{formatRelative(run.created_at)}</span>
                  <span className="run-history-cases">{run.case_count} cases</span>
                  <span className="run-history-model">{run.model}</span>
                  {isMulti && otherKeys.length > 0 && (
                    <span className="run-history-multi">
                      multi: also {otherKeys.join(', ')}
                    </span>
                  )}
                </div>
                <div className="run-history-actions">
                  <button
                    type="button"
                    onClick={() => handleView(run)}
                    disabled={loadingPlanId === run.plan_id}
                  >
                    {loadingPlanId === run.plan_id ? 'Loading…' : 'View'}
                  </button>
                  <button
                    type="button"
                    onClick={() => setDiffTarget(run)}
                    disabled={!run.previous_plan_id}
                    title={
                      run.previous_plan_id
                        ? 'Compare this version with the one before it'
                        : 'No earlier version to diff against'
                    }
                  >
                    Diff
                  </button>
                </div>
              </li>
            )
          })}
        </ul>
      )}

      {diffTarget && (
        <PlanDiffModal
          rightPlan={diffTarget}
          ticketData={ticketData}
          onClose={() => setDiffTarget(null)}
        />
      )}
    </div>
  )
}
