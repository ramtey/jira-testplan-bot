import { useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'

// Match the CSS exit duration for .workflow-message.is-leaving so the element
// stays in the DOM long enough to play its fade-out.
const MESSAGE_EXIT_MS = 220

const TESTING_STATUS = 'in testing'

// Environments QA can tag onto a Pass-to-UAT comment. Add new ones here —
// the backend accepts whatever strings are sent. Integ is preselected
// because it's the default target for most tickets.
const ENVIRONMENT_OPTIONS = ['Integ', 'Staging']
const DEFAULT_ENVIRONMENTS = ['Integ']

// Word-boundary keyword match per env. We deliberately don't match "stage"
// (too noisy: "early stage", "staged rollout") or "integration" (clashes
// with "integration test"); the bare "integ" / "staging" tokens almost
// always refer to the env in QA chatter.
const ENV_PATTERNS = {
  Integ: /\binteg\b/i,
  Staging: /\bstaging\b/i,
}

// Pick the most recent piece of ticket text that names an env and return
// the envs it mentions. Falls back to DEFAULT_ENVIRONMENTS when nothing
// matches, so QA still sees the usual preselect.
function detectEnvironments(description, comments) {
  const sources = []
  if (Array.isArray(comments)) {
    const sorted = comments
      .filter((c) => c && typeof c.body === 'string' && c.body.length > 0)
      .slice()
      .sort((a, b) => (b.created || '').localeCompare(a.created || ''))
    sources.push(...sorted.map((c) => c.body))
  }
  if (description) sources.push(description)

  for (const text of sources) {
    const matched = ENVIRONMENT_OPTIONS.filter((env) =>
      ENV_PATTERNS[env].test(text)
    )
    if (matched.length > 0) return matched
  }
  return DEFAULT_ENVIRONMENTS
}

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
    id: 'fail-to-todo',
    label: 'Fail back to To Do',
    title: 'Move back to To Do and reassign to the previous person',
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

function WorkflowActions({ ticketKey, currentStatus, description, comments, onActionComplete }) {
  const [pendingAction, setPendingAction] = useState(null)
  // {kind: 'success'|'error', text: string} — held during fade-out via isLeaving.
  const [feedback, setFeedback] = useState(null)
  const [isLeaving, setIsLeaving] = useState(false)
  // When set, the inline note form is showing instead of the buttons row.
  // Right now only `pass-to-uat` opens it; other actions still run on click.
  const [noteForAction, setNoteForAction] = useState(null)
  const [loomUrl, setLoomUrl] = useState('')
  const [summary, setSummary] = useState('')
  const [environments, setEnvironments] = useState(DEFAULT_ENVIRONMENTS)

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

  const closeNoteForm = () => {
    setNoteForAction(null)
    setLoomUrl('')
    setSummary('')
    setEnvironments(DEFAULT_ENVIRONMENTS)
  }

  const toggleEnvironment = (env) => {
    setEnvironments((prev) =>
      prev.includes(env) ? prev.filter((e) => e !== env) : [...prev, env]
    )
  }

  const runAction = async (action, body) => {
    setPendingAction(action.id)
    setFeedback(null)
    setIsLeaving(false)
    try {
      const init = { method: 'POST' }
      if (body) {
        init.headers = { 'Content-Type': 'application/json' }
        init.body = JSON.stringify(body)
      }
      const response = await fetch(
        `${API_BASE_URL}/issue/${ticketKey}/workflow/${action.id}`,
        init
      )
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(data.detail || `Action failed (${response.status})`)
      }
      const assigneeText =
        data.assigned_to === 'unassigned'
          ? 'unassigned'
          : `assigned to ${data.assigned_to}`
      const noteText = data.comment_posted ? ' · note posted' : ''
      setFeedback({
        kind: 'success',
        text: `Moved to ${data.target_status} · ${assigneeText}${noteText}`,
      })
      closeNoteForm()
      if (onActionComplete) onActionComplete(action.id)
    } catch (err) {
      setFeedback({ kind: 'error', text: err.message })
    } finally {
      setPendingAction(null)
    }
  }

  const onActionClick = (action) => {
    if (action.id === 'pass-to-uat') {
      setEnvironments(detectEnvironments(description, comments))
      setNoteForAction(action)
      setFeedback(null)
      return
    }
    runAction(action)
  }

  const onNoteSubmit = (e) => {
    e.preventDefault()
    if (!noteForAction) return
    const trimmedLoom = loomUrl.trim()
    const trimmedSummary = summary.trim()
    const hasAnyField =
      trimmedLoom || trimmedSummary || environments.length > 0
    const body = hasAnyField
      ? {
          loom_url: trimmedLoom || null,
          summary: trimmedSummary || null,
          environments: environments.length > 0 ? environments : null,
        }
      : undefined
    runAction(noteForAction, body)
  }

  return (
    <div className="workflow-actions">
      {noteForAction ? (
        <form className="workflow-note-form" onSubmit={onNoteSubmit}>
          <div className="workflow-note-form-header">
            Pass to UAT — optional note
          </div>
          <div className="workflow-note-field">
            <span>Tested in</span>
            <div className="workflow-env-chips" role="group" aria-label="Environments tested">
              {ENVIRONMENT_OPTIONS.map((env) => {
                const selected = environments.includes(env)
                return (
                  <button
                    key={env}
                    type="button"
                    className={`workflow-env-chip${selected ? ' is-selected' : ''}`}
                    aria-pressed={selected}
                    onClick={() => toggleEnvironment(env)}
                    disabled={pendingAction !== null}
                  >
                    {env}
                  </button>
                )
              })}
            </div>
          </div>
          <label className="workflow-note-field">
            <span>Loom URL</span>
            <input
              type="url"
              placeholder="https://www.loom.com/share/…"
              value={loomUrl}
              onChange={(e) => setLoomUrl(e.target.value)}
              disabled={pendingAction !== null}
            />
          </label>
          <label className="workflow-note-field">
            <span>Test summary (markdown supported)</span>
            <textarea
              rows={4}
              placeholder="Brief notes about what was tested…"
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              disabled={pendingAction !== null}
            />
          </label>
          <div className="workflow-note-form-actions">
            <button
              type="button"
              className="btn-workflow-link"
              onClick={closeNoteForm}
              disabled={pendingAction !== null}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn-workflow btn-workflow-success"
              disabled={pendingAction !== null}
            >
              {pendingAction === noteForAction.id ? 'Working…' : 'Pass to UAT'}
            </button>
          </div>
        </form>
      ) : (
        <div className="workflow-actions-buttons">
          {visibleActions.map((action) => (
            <button
              key={action.id}
              type="button"
              className={`btn-workflow btn-workflow-${action.intent}`}
              title={action.title}
              disabled={pendingAction !== null}
              onClick={() => onActionClick(action)}
            >
              {pendingAction === action.id ? 'Working…' : action.label}
            </button>
          ))}
        </div>
      )}
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
