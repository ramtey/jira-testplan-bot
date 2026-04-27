/**
 * Modal that renders a unified line diff between two test plans.
 *
 * Both plans are formatted as markdown via formatTestPlanAsMarkdown so the diff
 * is human-readable rather than a JSON blob. Inputs are the *response shape*
 * (happy_path / edge_cases / integration_tests / regression_checklist) — not
 * the persisted JSON-string body.
 */

import { useEffect, useMemo, useState } from 'react'
import { diffLines } from 'diff'
import { formatTestPlanAsMarkdown } from '../utils/markdown'
import { API_BASE_URL } from '../config'

function parsePlanBody(rawBody) {
  if (!rawBody) return null
  try {
    return JSON.parse(rawBody)
  } catch {
    return null
  }
}

async function fetchPlan(planId) {
  const res = await fetch(`${API_BASE_URL}/plans/${planId}`)
  if (!res.ok) throw new Error(`Failed to load plan ${planId}`)
  return res.json()
}

export default function PlanDiffModal({ rightPlan, ticketData, onClose }) {
  // rightPlan is the historical row being inspected: { plan_id, version, previous_plan_id, ... }
  // The parent re-mounts this component (via `key`) when rightPlan changes, so
  // initial state below is correct on every open and the effect only fires once.
  const [leftRaw, setLeftRaw] = useState(null)
  const [rightRaw, setRightRaw] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false

    const tasks = [fetchPlan(rightPlan.plan_id)]
    if (rightPlan.previous_plan_id) {
      tasks.push(fetchPlan(rightPlan.previous_plan_id))
    }

    Promise.all(tasks)
      .then((results) => {
        if (cancelled) return
        const [right, left] = results
        setRightRaw(right)
        setLeftRaw(left || null)
      })
      .catch((err) => {
        if (!cancelled) setError(err.message)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [rightPlan.plan_id, rightPlan.previous_plan_id])

  const diffParts = useMemo(() => {
    if (!rightRaw) return null
    const rightPlanObj = parsePlanBody(rightRaw.body)
    const leftPlanObj = leftRaw ? parsePlanBody(leftRaw.body) : null
    if (!rightPlanObj) return null

    const rightMd = formatTestPlanAsMarkdown(rightPlanObj, ticketData || { key: '', summary: '' })
    const leftMd = leftPlanObj
      ? formatTestPlanAsMarkdown(leftPlanObj, ticketData || { key: '', summary: '' })
      : ''

    return diffLines(leftMd, rightMd)
  }, [leftRaw, rightRaw, ticketData])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-panel" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h3>
            Diff — v{rightPlan.version}
            {rightPlan.previous_plan_id ? ` vs v${rightPlan.version - 1}` : ' (no prior version)'}
          </h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        <div className="modal-body">
          {loading && <p>Loading…</p>}
          {error && <p className="alert alert-error">{error}</p>}
          {!loading && !error && !rightPlan.previous_plan_id && (
            <p className="modal-note">
              This is the first version for this ticket — there's no earlier plan to diff against.
              Showing the full plan as added content.
            </p>
          )}
          {!loading && !error && diffParts && (
            <pre className="diff-pre">
              {diffParts.map((part, i) => {
                const cls = part.added
                  ? 'diff-added'
                  : part.removed
                  ? 'diff-removed'
                  : 'diff-context'
                const prefix = part.added ? '+ ' : part.removed ? '- ' : '  '
                const lines = part.value.split('\n')
                if (lines[lines.length - 1] === '') lines.pop()
                return lines.map((line, j) => (
                  <span key={`${i}-${j}`} className={cls}>
                    {prefix}
                    {line}
                    {'\n'}
                  </span>
                ))
              })}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}
