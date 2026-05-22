/**
 * Modal that renders a unified line diff between two test plans.
 */

import { useEffect, useMemo, useState } from 'react'
import { diffLines } from 'diff'
import { formatTestPlanAsMarkdown } from '../utils/markdown'
import { API_BASE_URL } from '../config'
import Icon from './Icon'
import { Alert } from './ui'

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
      <div
        className="modal"
        style={{ width: 'min(1040px, calc(100vw - 64px))' }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="modal-head">
          <Icon name="split" size={14} style={{ color: 'var(--accent)' }} />
          <div style={{ flex: 1 }}>
            <div className="ttl">
              Diff · v{rightPlan.version}
              {rightPlan.previous_plan_id ? ` vs v${rightPlan.version - 1}` : ' (no prior version)'}
            </div>
          </div>
          <button type="button" className="hbtn" onClick={onClose} aria-label="Close">
            <Icon name="x" />
          </button>
        </div>

        <div className="modal-body">
          {loading && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', color: 'var(--fg-muted)' }}>
              <span className="spin" />Loading…
            </div>
          )}
          {error && <Alert tone="danger" title="Failed to load diff">{error}</Alert>}
          {!loading && !error && !rightPlan.previous_plan_id && (
            <div style={{ marginBottom: 'var(--s-4)' }}>
              <Alert tone="info" title="No prior version">
                This is the first version for this ticket. Showing the full plan as added content.
              </Alert>
            </div>
          )}
          {!loading && !error && diffParts && (
            <pre
              style={{
                margin: 0,
                fontFamily: 'var(--font-mono)',
                fontSize: 11.5,
                lineHeight: '17px',
                color: 'var(--fg)',
                background: 'var(--bg-input)',
                border: '1px solid var(--line)',
                borderRadius: 'var(--r-sm)',
                padding: 'var(--s-4) var(--s-5)',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                overflowX: 'auto',
              }}
            >
              {diffParts.map((part, i) => {
                const lines = part.value.split('\n')
                if (lines[lines.length - 1] === '') lines.pop()
                return lines.map((line, j) => (
                  <span
                    key={`${i}-${j}`}
                    style={{
                      display: 'block',
                      background: part.added
                        ? 'rgba(34,197,94,.10)'
                        : part.removed
                        ? 'rgba(239,68,68,.10)'
                        : 'transparent',
                      color: part.added
                        ? 'var(--success)'
                        : part.removed
                        ? 'var(--danger)'
                        : 'var(--fg)',
                    }}
                  >
                    {part.added ? '+ ' : part.removed ? '- ' : '  '}{line}
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
