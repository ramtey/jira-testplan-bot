import { useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'

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
  const [message, setMessage] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!message) return
    const t = setTimeout(() => setMessage(null), 15000)
    return () => clearTimeout(t)
  }, [message])

  if (!isSKProject(ticketKey)) return null

  const visibleActions = ACTIONS.filter((a) => a.showWhen(currentStatus))
  if (visibleActions.length === 0) return null

  const runAction = async (action) => {
    setPendingAction(action.id)
    setMessage(null)
    setError(null)
    try {
      const response = await fetch(
        `${API_BASE_URL}/issue/${ticketKey}/workflow/${action.id}`,
        { method: 'POST' }
      )
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(data.detail || `Action failed (${response.status})`)
      }
      setMessage(`Moved to ${data.target_status} · assigned to ${data.assigned_to}`)
      if (onActionComplete) onActionComplete()
    } catch (err) {
      setError(err.message)
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
      {message && <div className="workflow-message success">{message}</div>}
      {error && <div className="workflow-message error">{error}</div>}
    </div>
  )
}

export default WorkflowActions
