import { useEffect, useRef, useState } from 'react'
import { API_BASE_URL, isWorkflowEnabledForTicket } from '../config'
import Icon from './Icon'
import { Btn, Cbx, Alert } from './ui'

// Match the CSS exit duration so the feedback element stays in the DOM long enough.
const MESSAGE_EXIT_MS = 220

const TESTING_STATUS = 'in testing'

const ENVIRONMENT_OPTIONS = ['Integ', 'Staging', 'Prod']
const DEFAULT_ENVIRONMENTS = ['Integ']

const ENV_PATTERNS = {
  Integ: /\binteg\b/i,
  Staging: /\bstaging\b/i,
  Prod: /\bprod(uction)?\b/i,
}

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
    variant: 'secondary',
    icon: 'arrow-down-right',
    showWhen: (status) => normalize(status) !== TESTING_STATUS,
  },
  {
    id: 'pass-to-uat',
    label: 'Pass to UAT',
    title: 'Move to UAT and reassign to the previous person',
    variant: 'success-soft',
    icon: 'check',
    showWhen: (status) => normalize(status) === TESTING_STATUS,
  },
  {
    id: 'fail-to-todo',
    label: 'Fail back to To Do',
    title: 'Move back to To Do and reassign to the previous person',
    variant: 'danger-soft',
    icon: 'arrow-left',
    showWhen: (status) => normalize(status) === TESTING_STATUS,
  },
]

function normalize(status) {
  return (status || '').trim().toLowerCase()
}

function buildMentionCandidates({
  assignee,
  assigneeAccountId,
  assigneeHistory,
  assigneeHistoryAccountIds,
  comments,
  currentUserAccountId,
}) {
  const seen = new Map()
  const skip = (id) => !id || (currentUserAccountId && id === currentUserAccountId)

  if (!skip(assigneeAccountId)) {
    seen.set(assigneeAccountId, {
      accountId: assigneeAccountId,
      name: assignee || 'Assignee',
      isAssignee: true,
    })
  }

  if (Array.isArray(assigneeHistory) && Array.isArray(assigneeHistoryAccountIds)) {
    for (let i = 0; i < assigneeHistory.length; i++) {
      const id = assigneeHistoryAccountIds[i]
      const name = assigneeHistory[i]
      if (skip(id) || seen.has(id)) continue
      seen.set(id, { accountId: id, name: name || id, isAssignee: false })
    }
  }

  if (Array.isArray(comments)) {
    for (const c of comments) {
      const id = c?.author_account_id
      if (skip(id) || seen.has(id)) continue
      seen.set(id, {
        accountId: id,
        name: c.author || id,
        isAssignee: false,
      })
    }
  }

  return Array.from(seen.values())
}

function EnvPill({ value, on, onToggle, disabled }) {
  return (
    <button
      type="button"
      className="env-pill"
      data-on={on ? 'true' : 'false'}
      disabled={disabled}
      onClick={onToggle}
      aria-pressed={on}
    >
      {on && <Icon name="check" size={11} />}
      {value}
    </button>
  )
}

function ImageDropzone({ files, onAdd, onRemove, disabled }) {
  const [isDragging, setIsDragging] = useState(false)
  const inputRef = useRef(null)

  // Paste support: when the form is focused, ctrl/cmd+V drops an image
  // from the clipboard straight into the upload list. Mirrors the
  // existing "paste a URL" muscle memory but lands a real attachment.
  useEffect(() => {
    if (disabled) return
    const handler = (e) => {
      const items = e.clipboardData?.items
      if (!items) return
      const imageItems = Array.from(items).filter(
        (it) => it.type.startsWith('image/') || it.type === 'application/pdf'
      )
      if (imageItems.length === 0) return
      e.preventDefault()
      const fileList = imageItems
        .map((it) => it.getAsFile())
        .filter(Boolean)
      onAdd(fileList)
    }
    window.addEventListener('paste', handler)
    return () => window.removeEventListener('paste', handler)
  }, [disabled, onAdd])

  const onDrop = (e) => {
    e.preventDefault()
    setIsDragging(false)
    if (disabled) return
    onAdd(e.dataTransfer.files)
  }

  return (
    <div>
      <div
        onClick={() => !disabled && inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault()
          if (!disabled) setIsDragging(true)
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={onDrop}
        style={{
          border: '1px dashed ' + (isDragging ? 'var(--accent)' : 'var(--border)'),
          background: isDragging ? 'rgba(59,130,246,.06)' : 'transparent',
          borderRadius: 'var(--r-md)',
          padding: 'var(--s-4) var(--s-5)',
          cursor: disabled ? 'not-allowed' : 'pointer',
          fontSize: 'var(--t-sm)',
          color: 'var(--fg-subtle)',
          transition: 'background 120ms, border-color 120ms',
        }}
      >
        <Icon name="image" size={13} style={{ marginRight: 6, verticalAlign: '-2px' }} />
        Click, drag, or paste files here. PNG / JPEG / GIF / WEBP / PDF, up to 10 MB each.
        <input
          ref={inputRef}
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp,application/pdf"
          multiple
          hidden
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => {
            onAdd(e.target.files)
            e.target.value = ''
          }}
        />
      </div>
      {files.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
          {files.map((f, i) => (
            <span
              key={i}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                background: 'var(--bg-subtle)',
                border: '1px solid var(--border)',
                borderRadius: 999,
                padding: '4px 10px',
                fontSize: 'var(--t-xs)',
              }}
              title={`${f.name} · ${(f.size / 1024).toFixed(0)} KB`}
            >
              <Icon name="image" size={11} />
              {f.name.length > 28 ? f.name.slice(0, 25) + '…' : f.name}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  onRemove(i)
                }}
                disabled={disabled}
                style={{
                  background: 'transparent',
                  border: 'none',
                  cursor: disabled ? 'not-allowed' : 'pointer',
                  color: 'var(--fg-subtle)',
                  padding: 0,
                  display: 'inline-flex',
                }}
                aria-label={`Remove ${f.name}`}
              >
                <Icon name="x" size={11} />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function WorkflowActions({
  ticketKey,
  currentStatus,
  description,
  comments,
  assignee,
  assigneeAccountId,
  assigneeHistory,
  assigneeHistoryAccountIds,
  currentUserAccountId,
  childIssues,
  onActionComplete,
}) {
  const [pendingAction, setPendingAction] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const [isLeaving, setIsLeaving] = useState(false)
  const [noteForAction, setNoteForAction] = useState(null)
  const [loomUrlsText, setLoomUrlsText] = useState('')
  const [summary, setSummary] = useState('')
  const [environments, setEnvironments] = useState(DEFAULT_ENVIRONMENTS)
  const [reason, setReason] = useState('')
  const [imageFiles, setImageFiles] = useState([])
  const [mentionAccountIds, setMentionAccountIds] = useState([])
  const [cascadeToSubtasks, setCascadeToSubtasks] = useState(false)

  useEffect(() => {
    if (!feedback || feedback.kind !== 'success') return
    const dismiss = setTimeout(() => setIsLeaving(true), 15000)
    return () => clearTimeout(dismiss)
  }, [feedback])

  useEffect(() => {
    if (!isLeaving) return
    const t = setTimeout(() => {
      setFeedback(null)
      setIsLeaving(false)
    }, MESSAGE_EXIT_MS)
    return () => clearTimeout(t)
  }, [isLeaving])

  if (!isWorkflowEnabledForTicket(ticketKey)) return null

  const visibleActions = ACTIONS.filter((a) => a.showWhen(currentStatus))
  if (visibleActions.length === 0) return null

  const hasSubtasks =
    Array.isArray(childIssues) &&
    childIssues.some((c) => /sub-?task/i.test(c?.issue_type || ''))

  const closeNoteForm = () => {
    setNoteForAction(null)
    setLoomUrlsText('')
    setSummary('')
    setEnvironments(DEFAULT_ENVIRONMENTS)
    setReason('')
    setImageFiles([])
    setMentionAccountIds([])
    setCascadeToSubtasks(false)
  }

  const addImageFiles = (files) => {
    const incoming = Array.from(files || []).filter(
      (f) => f && (f.type.startsWith('image/') || f.type === 'application/pdf')
    )
    if (incoming.length === 0) return
    setImageFiles((prev) => [...prev, ...incoming])
  }

  const removeImageFile = (idx) => {
    setImageFiles((prev) => prev.filter((_, i) => i !== idx))
  }

  const toggleEnvironment = (env) => {
    setEnvironments((prev) =>
      prev.includes(env) ? prev.filter((e) => e !== env) : [...prev, env]
    )
  }

  const toggleMention = (accountId) => {
    setMentionAccountIds((prev) =>
      prev.includes(accountId)
        ? prev.filter((id) => id !== accountId)
        : [...prev, accountId]
    )
  }

  const mentionCandidates = buildMentionCandidates({
    assignee,
    assigneeAccountId,
    assigneeHistory,
    assigneeHistoryAccountIds,
    comments,
    currentUserAccountId,
  })

  const runAction = async (action, body, files = []) => {
    setPendingAction(action.id)
    setFeedback(null)
    setIsLeaving(false)
    try {
      // The backend always reads multipart/form-data on this endpoint:
      // `payload` carries the JSON body and `images[]` carries any files.
      // We send an empty FormData (no payload, no files) for actions like
      // pull-to-testing that just transition.
      const form = new FormData()
      const effectiveBody = cascadeToSubtasks
        ? { ...(body || {}), cascade_to_subtasks: true }
        : body
      if (effectiveBody && Object.keys(effectiveBody).length > 0) {
        form.append('payload', JSON.stringify(effectiveBody))
      }
      for (const file of files) {
        form.append('images', file, file.name)
      }
      const response = await fetch(
        `${API_BASE_URL}/issue/${ticketKey}/workflow/${action.id}`,
        { method: 'POST', body: form }
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
      const parentText =
        data.parent_transitioned && data.parent_key
          ? ` · parent ${data.parent_key} also moved`
          : ''
      const cascadedCount = Array.isArray(data.cascaded_subtasks)
        ? data.cascaded_subtasks.length
        : 0
      const cascadeText = cascadedCount > 0
        ? ` · ${cascadedCount} subtask${cascadedCount === 1 ? '' : 's'} moved`
        : ''
      const lead =
        action.id === 'fail-to-todo'
          ? `Bounced back to ${data.target_status}`
          : action.id === 'pass-to-uat'
            ? `Passed to ${data.target_status}`
            : `Moved to ${data.target_status}`
      setFeedback({
        kind: 'success',
        actionId: action.id,
        text: `${lead} · ${assigneeText}${noteText}${parentText}${cascadeText}`,
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
    if (action.id === 'fail-to-todo') {
      setNoteForAction(action)
      setFeedback(null)
      return
    }
    runAction(action)
  }

  const onNoteSubmit = (e) => {
    e.preventDefault()
    if (!noteForAction) return
    const looms = loomUrlsText
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean)

    if (noteForAction.id === 'pass-to-uat') {
      const trimmedSummary = summary.trim()
      const hasAnyField =
        looms.length > 0 ||
        trimmedSummary ||
        environments.length > 0 ||
        imageFiles.length > 0
      const body = hasAnyField
        ? {
            loom_urls: looms.length > 0 ? looms : null,
            summary: trimmedSummary || null,
            environments: environments.length > 0 ? environments : null,
            mention_account_ids:
              mentionAccountIds.length > 0 ? mentionAccountIds : null,
          }
        : undefined
      runAction(noteForAction, body, imageFiles)
      return
    }

    if (noteForAction.id === 'fail-to-todo') {
      const trimmedReason = reason.trim()
      if (!trimmedReason) {
        setFeedback({ kind: 'error', text: 'Reason is required.' })
        return
      }
      const body = {
        reason: trimmedReason,
        loom_urls: looms.length > 0 ? looms : null,
        mention_account_ids:
          mentionAccountIds.length > 0 ? mentionAccountIds : null,
      }
      runAction(noteForAction, body, imageFiles)
    }
  }

  const isFail = noteForAction?.id === 'fail-to-todo'

  return (
    <div>
      {!noteForAction && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', flexWrap: 'wrap' }}>
          <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '.06em', fontWeight: 600, marginRight: 'var(--s-2)' }}>
            Workflow
          </span>
          {visibleActions.map((action) => (
            <Btn
              key={action.id}
              variant={action.variant}
              icon={action.icon}
              title={action.title}
              disabled={pendingAction !== null}
              loading={pendingAction === action.id}
              onClick={() => onActionClick(action)}
            >
              {action.label}
            </Btn>
          ))}
          <span style={{ flex: 1 }} />
          {hasSubtasks && (
            <Cbx
              checked={cascadeToSubtasks}
              onChange={setCascadeToSubtasks}
              label="Also move all subtasks"
            />
          )}
        </div>
      )}

      {noteForAction && (
        <form
          onSubmit={onNoteSubmit}
          style={{
            background: isFail ? 'rgba(239,68,68,.04)' : 'rgba(34,197,94,.03)',
            border: '1px solid ' + (isFail ? 'rgba(239,68,68,.25)' : 'rgba(34,197,94,.22)'),
            borderRadius: 'var(--r-md)',
            padding: 'var(--s-6)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: 'var(--s-5)' }}>
            <Icon
              name={isFail ? 'arrow-left' : 'check-circle'}
              size={14}
              style={{ color: isFail ? 'var(--danger)' : 'var(--success)' }}
            />
            <span style={{ fontWeight: 600, color: 'var(--fg-strong)' }}>
              {isFail ? 'Fail back to To Do' : 'Pass to UAT'}
            </span>
            <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-sm)' }}>
              {isFail ? 'Will be returned to development.' : 'Will be moved to UAT.'}
            </span>
            <button type="button" className="hbtn" style={{ marginLeft: 'auto' }} onClick={closeNoteForm} disabled={pendingAction !== null}>
              <Icon name="x" size={13} />
            </button>
          </div>

          {isFail && (
            <div style={{ marginBottom: 'var(--s-5)' }}>
              <span className="lbl req">Reason</span>
              <textarea
                className="inp"
                style={{ minHeight: 70, borderColor: 'rgba(239,68,68,.45)', boxShadow: '0 0 0 3px rgba(239,68,68,.10)' }}
                placeholder="Required. What broke, where, and how to reproduce…"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                disabled={pendingAction !== null}
                required
                autoFocus
              />
              <div style={{ marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--danger)' }}>
                This becomes the bounce-back history. Be specific.
              </div>
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '160px 1fr', gap: 'var(--s-6) var(--s-7)', alignItems: 'start' }}>
            {noteForAction.id === 'pass-to-uat' && (
              <>
                <span className="lbl">Tested on</span>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {ENVIRONMENT_OPTIONS.map((env) => (
                    <EnvPill
                      key={env}
                      value={env}
                      on={environments.includes(env)}
                      onToggle={() => toggleEnvironment(env)}
                      disabled={pendingAction !== null}
                    />
                  ))}
                </div>
              </>
            )}

            <span className="lbl">Loom URL{loomUrlsText.includes('\n') ? 's' : ''}</span>
            <textarea
              className="inp mono"
              rows={2}
              placeholder="https://www.loom.com/share/…"
              value={loomUrlsText}
              onChange={(e) => setLoomUrlsText(e.target.value)}
              disabled={pendingAction !== null}
              style={{ minHeight: 40 }}
            />

            {noteForAction.id === 'pass-to-uat' && (
              <>
                <span className="lbl">Notes</span>
                <textarea
                  className="inp"
                  rows={3}
                  placeholder="Optional. Markdown supported."
                  value={summary}
                  onChange={(e) => setSummary(e.target.value)}
                  disabled={pendingAction !== null}
                />
              </>
            )}

            <span className="lbl">Screenshots</span>
            <ImageDropzone
              files={imageFiles}
              onAdd={addImageFiles}
              onRemove={removeImageFile}
              disabled={pendingAction !== null}
            />

            {mentionCandidates.length > 0 && (
              <>
                <span className="lbl">Notify</span>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {mentionCandidates.map((person) => {
                    const selected = mentionAccountIds.includes(person.accountId)
                    return (
                      <EnvPill
                        key={person.accountId}
                        value={person.isAssignee ? `★ ${person.name}` : person.name}
                        on={selected}
                        onToggle={() => toggleMention(person.accountId)}
                        disabled={pendingAction !== null}
                      />
                    )
                  })}
                </div>
              </>
            )}
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginTop: 'var(--s-6)' }}>
            {hasSubtasks && (
              <Cbx
                checked={cascadeToSubtasks}
                onChange={setCascadeToSubtasks}
                label="Also move all subtasks"
              />
            )}
            <span style={{ flex: 1 }} />
            <Btn variant="ghost" onClick={closeNoteForm} disabled={pendingAction !== null}>Cancel</Btn>
            <Btn
              type="submit"
              variant={isFail ? 'danger' : 'primary'}
              icon={isFail ? 'arrow-left' : 'check'}
              disabled={pendingAction !== null}
              loading={pendingAction === noteForAction.id}
            >
              {noteForAction.label}
            </Btn>
          </div>
        </form>
      )}

      {feedback && (() => {
        const isError = feedback.kind === 'error'
        const isBounce = !isError && feedback.actionId === 'fail-to-todo'
        const tone = isError ? 'danger' : isBounce ? 'warning' : 'success'
        const title = isError
          ? 'Action failed'
          : isBounce
            ? 'Returned to dev'
            : 'Success'
        const icon = isBounce ? 'arrow-left' : undefined
        return (
          <div style={{ marginTop: 'var(--s-4)', opacity: isLeaving ? 0 : 1, transition: `opacity ${MESSAGE_EXIT_MS}ms` }}>
            <Alert tone={tone} title={title} icon={icon}>
              {feedback.text}
            </Alert>
          </div>
        )
      })()}
    </div>
  )
}

export default WorkflowActions
