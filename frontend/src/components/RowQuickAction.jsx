/**
 * Inline single-action button for the compact multi-ticket row.
 *
 * Scoped intentionally to `pull-to-testing` — the only fire-and-forget
 * workflow move. Pass-to-UAT and Fail-back-to-To-Do require a form
 * (Loom URLs, screenshots, reasons) and live in the single-ticket view.
 *
 * On success the row's status flips to "In Testing", which makes this
 * button hide itself on the next refresh — no separate "moved" state
 * to clear.
 */

import { useState } from 'react'
import { API_BASE_URL, isWorkflowEnabledForTicket } from '../config'
import Icon from './Icon'

const TESTING_STATUS = 'in testing'

function RowQuickAction({ ticketKey, currentStatus, hasSubtasks, onActionComplete }) {
  const [pending, setPending] = useState(false)
  const [error, setError] = useState(null)

  if (!isWorkflowEnabledForTicket(ticketKey)) return null
  if ((currentStatus || '').trim().toLowerCase() === TESTING_STATUS) return null

  const onClick = async (e) => {
    e.preventDefault()
    e.stopPropagation()
    if (pending) return
    setPending(true)
    setError(null)
    try {
      const form = new FormData()
      // Row-level Pull is fire-and-forget (no checkbox UI), so match the
      // single-ticket view's default: when the parent has subtasks, cascade.
      // The backend only moves siblings that share the parent's pre-transition
      // status, so this is safe when subtasks are in unrelated columns.
      if (hasSubtasks) {
        form.append('payload', JSON.stringify({ cascade_to_subtasks: true }))
      }
      const response = await fetch(
        `${API_BASE_URL}/issue/${ticketKey}/workflow/pull-to-testing`,
        { method: 'POST', body: form },
      )
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(data.detail || `Action failed (${response.status})`)
      }
      if (onActionComplete) onActionComplete(ticketKey)
    } catch (err) {
      setError(err.message)
    } finally {
      setPending(false)
    }
  }

  if (error) {
    return (
      <button
        type="button"
        onClick={onClick}
        title={`${error} — click to retry`}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 4,
          height: 22,
          padding: '0 8px',
          fontSize: 'var(--t-xs)',
          fontWeight: 600,
          background: 'transparent',
          color: 'var(--danger)',
          border: '1px solid var(--danger)',
          borderRadius: 'var(--r-sm)',
          cursor: 'pointer',
          flexShrink: 0,
        }}
      >
        <Icon name="x" size={11} />
        Retry
      </button>
    )
  }

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending}
      title={
        hasSubtasks
          ? 'Move to In Testing, pull matching subtasks along, and assign to you'
          : 'Move to In Testing and assign to you'
      }
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        height: 22,
        padding: '0 8px',
        fontSize: 'var(--t-xs)',
        fontWeight: 600,
        background: pending ? 'var(--bg-surface)' : 'transparent',
        color: 'var(--accent)',
        border: '1px solid var(--accent)',
        borderRadius: 'var(--r-sm)',
        cursor: pending ? 'wait' : 'pointer',
        flexShrink: 0,
      }}
    >
      {pending ? <span className="spin" /> : <Icon name="arrow-down-right" size={11} />}
      {pending ? 'Moving…' : 'Pull'}
    </button>
  )
}

export default RowQuickAction
