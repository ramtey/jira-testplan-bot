import { useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'

// Match the CSS exit duration for .workflow-message.is-leaving so the element
// stays in the DOM long enough to play its fade-out.
const MESSAGE_EXIT_MS = 220

const TESTING_STATUS = 'in testing'

const ACTIONS = [
  {
    id: 'pull-to-testing',
    label: 'Pull to Testing',
    title: 'Move to In Testing and assign to me',
    intent: 'primary',
    showWhen: (status) => normalize(status) !== TESTING_STATUS,
  },
  {
    id: 'pass-to-uat',
    label: 'Pass to UAT',
    title: 'Move to UAT and reassign to the previous person',
    intent: 'success',
    showWhen: (status) => normalize(status) === TESTING_STATUS,
  },
  {
    id: 'fail-to-in-progress',
    label: 'Fail back to In Progress',
    title: 'Move back to In Progress and reassign to the previous person',
    intent: 'warn',
    showWhen: (status) => normalize(status) === TESTING_STATUS,
  },
]

function normalize(status) {
  return (status || '').trim().toLowerCase()
}

function isSKProject(ticketKey) {
  return (ticketKey || '').toUpperCase().startsWith('SK-')
}

function WorkflowActions({ ticketKey, currentStatus, onActionComplete }) {
  const [pendingAction, setPendingAction] = useState(null)
  // {kind: 'success'|'error', text: string} — held during fade-out via isLeaving.
  const [feedback, setFeedback] = useState(null)
  const [isLeaving, setIsLeaving] = useState(false)

  // Auto-dismiss success messages after 15s; errors stay until the next action.
  useEffect(() => {
    if (!feedback || feedback.kind !== 'success') return
    const dismiss = setTimeout(() => setIsLeaving(true), 15000)
    return () => clearTimeout(dismiss)
  }, [feedback])

  // Once isLeaving flips on, wait for the CSS exit transition then unmount.
  useEffect(() => {
    if (!isLeaving) return
    const t = setTimeout(() => {
      setFeedback(null)
      setIsLeaving(false)
    }, MESSAGE_EXIT_MS)
    return () => clearTimeout(t)
  }, [isLeaving])

  if (!isSKProject(ticketKey)) return null

  const visibleActions = ACTIONS.filter((a) => a.showWhen(currentStatus))
  if (visibleActions.length === 0) return null

  const runAction = async (action) => {
    setPendingAction(action.id)
    setFeedback(null)
    setIsLeaving(false)
    try {
      const response = await fetch(
        `${API_BASE_URL}/issue/${ticketKey}/workflow/${action.id}`,
        { method: 'POST' }
      )
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(data.detail || `Action failed (${response.status})`)
      }
      const assigneeText =
        data.assigned_to === 'unassigned'
          ? 'unassigned'
          : `assigned to ${data.assigned_to}`
      setFeedback({
        kind: 'success',
        text: `Moved to ${data.target_status} · ${assigneeText}`,
      })
      if (onActionComplete) onActionComplete()
    } catch (err) {
      setFeedback({ kind: 'error', text: err.message })
    } finally {
      setPendingAction(null)
    }
  }

  return (
    <div className="workflow-actions">
      <div className="workflow-actions-buttons">
        {visibleActions.map((action) => (
          <button
            key={action.id}
            type="button"
            className={`btn-workflow btn-workflow-${action.intent}`}
            title={action.title}
            disabled={pendingAction !== null}
            onClick={() => runAction(action)}
          >
            {pendingAction === action.id ? 'Working…' : action.label}
          </button>
        ))}
      </div>
      {feedback && (
        <div
          className={`workflow-message ${feedback.kind} ${isLeaving ? 'is-leaving' : 'is-shown'}`}
          role={feedback.kind === 'error' ? 'alert' : 'status'}
        >
          {feedback.text}
        </div>
      )}
    </div>
  )
}

export default WorkflowActions
