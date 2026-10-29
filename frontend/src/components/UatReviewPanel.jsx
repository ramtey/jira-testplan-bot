/**
 * UAT review: the one human checkpoint of a runner's results.
 *
 * Shows the ticket's latest UAT run. While it awaits review the reviewer can
 * override any case (recorded with them as actor), pick the next step and
 * Approve & post — the only path from a run to Jira. The comment preview is
 * the same formatter the backend posts with.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { API_BASE_URL, getJiraTicketUrl } from '../config'
import { Alert, Btn, Chip, Coll, ErrNote, SectLbl } from './ui'
import Icon from './Icon'
import {
  NEXT_STEP_LABELS,
  NOT_RUN_LABELS,
  formatUatResultsAsJira,
  groupCases,
  nextStepLabel,
} from '../utils/uatResults'

const STEP_OPTIONS = [null, 'pass-to-uat', 'fail-to-in-progress', 'fail-to-todo']

const OUTCOME_TEXT = {
  attachments_failed: 'Attaching the video or failure frames failed',
  comment_failed: 'Writing the results comment failed',
  transition_failed: 'The comment landed, but moving the ticket failed',
  pending: 'Posting did not finish',
}

const errorText = async (res) => {
  try {
    const body = await res.json()
    const d = body?.detail
    if (d && typeof d === 'object') return d.message || d.error || JSON.stringify(d)
    return d || `HTTP ${res.status}`
  } catch {
    return `HTTP ${res.status}`
  }
}

const fmtDuration = (s) => {
  if (s == null) return null
  const m = Math.floor(s / 60)
  return m ? `${m}m ${s % 60}s` : `${s}s`
}

const fileName = (ref) => String(ref || '').split('/').pop()

function Evidence({ result }) {
  const ev = result?.evidence || []
  if (!ev.length) return null
  return (
    <ul className="uat-evidence">
      {ev.map((e, i) => (
        <li key={i}>
          <span className="uat-ev-kind">{e.kind}</span>
          <span>
            {e.text}
            {e.ref && <> <code title={e.ref}>{fileName(e.ref)}</code></>}
            {e.at_s != null && <span className="uat-muted"> at {e.at_s}s</span>}
          </span>
        </li>
      ))}
    </ul>
  )
}

function CaseRow({ c, open, onOverride, busy }) {
  const [failing, setFailing] = useState(false)
  const [note, setNote] = useState('')
  const r = c.result
  const actor = r?.actor?.kind === 'human' ? (r.actor.name || r.actor.email || 'you') : r?.actor?.kind === 'agent' ? 'runner' : null
  return (
    <div className="uat-case">
      <div className="uat-case-head">
        <span className="uat-case-title">{c.title || c.id}</span>
        <span className="uat-muted uat-mono">{c.id}</span>
        {r?.flaky && <Chip size="sm" dot dotColor="var(--warning)">flaky</Chip>}
        {actor && <span className="uat-muted">by {actor}{r?.source === 'review' ? ' (override)' : ''}</span>}
      </div>
      {r?.not_run?.detail && <div className="uat-detail">{r.not_run.detail}</div>}
      <Evidence result={r} />
      {open && (
        <div className="uat-case-actions">
          {!failing ? (
            <>
              {r?.status !== 'passed' && (
                <Btn size="sm" variant="success-soft" icon="check" disabled={busy}
                  onClick={() => onOverride(c.id, 'passed', null)}>Mark passed</Btn>
              )}
              <Btn size="sm" variant="danger-soft" icon="x" disabled={busy}
                onClick={() => setFailing(true)}>Mark failed…</Btn>
            </>
          ) : (
            <>
              <input className="uat-input" autoFocus placeholder="What failed?" value={note}
                onChange={(e) => setNote(e.target.value)} />
              <Btn size="sm" variant="danger" disabled={busy || !note.trim()}
                onClick={() => onOverride(c.id, 'failed', note.trim()).then(() => setFailing(false))}>Record failure</Btn>
              <Btn size="sm" variant="ghost" onClick={() => setFailing(false)}>Cancel</Btn>
            </>
          )}
        </div>
      )}
    </div>
  )
}

function Group({ title, tone, cases, open, onOverride, busy, defaultOpen }) {
  if (!cases.length) return null
  return (
    <Coll
      icon={tone === 'danger' ? 'x-circle' : tone === 'success' ? 'check-circle' : 'info'}
      title={`${title} (${cases.length})`}
      defaultOpen={defaultOpen}
    >
      {cases.map((c) => <CaseRow key={c.id} c={c} open={open} onOverride={onOverride} busy={busy} />)}
    </Coll>
  )
}

export default function UatReviewPanel({ ticketKey }) {
  const [runs, setRuns] = useState(null)
  const [view, setView] = useState(null)
  const [error, setError] = useState(null)
  const [actionError, setActionError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [nextStep, setNextStep] = useState(undefined)
  const [notes, setNotes] = useState('')
  const [rejecting, setRejecting] = useState(false)
  // Responses for a ticket the panel has since left are dropped.
  const keyRef = useRef(ticketKey)
  keyRef.current = ticketKey

  const load = useCallback(async () => {
    const key = ticketKey
    setError(null)
    try {
      const res = await fetch(`${API_BASE_URL}/tickets/${key}/uat-runs`)
      if (!res.ok) throw new Error(await errorText(res))
      const data = await res.json()
      if (keyRef.current !== key) return
      setRuns(data.runs || [])
      const latest = (data.runs || [])[0]
      if (!latest) { setView(null); return }
      const r2 = await fetch(`${API_BASE_URL}/uat-runs/${latest.run_id}/review`)
      if (!r2.ok) throw new Error(await errorText(r2))
      const v = await r2.json()
      if (keyRef.current !== key) return
      setView(v)
    } catch (e) {
      if (keyRef.current === key) setError(`Could not load UAT runs: ${e.message}`)
    }
  }, [ticketKey])

  useEffect(() => { load() }, [load])

  const groups = useMemo(() => (view ? groupCases(view) : null), [view])
  const step = nextStep === undefined ? view?.derived_next_step ?? null : nextStep
  const preview = useMemo(
    () => (view ? formatUatResultsAsJira(view, { nextStep: step, notes }) : ''),
    [view, step, notes],
  )

  if (error) return <ErrNote error={error} style={{ padding: 0, marginTop: 'var(--s-5)' }} />
  if (!runs || !view) return null

  const post = async (path, body) => {
    setBusy(true)
    setActionError(null)
    try {
      const res = await fetch(`${API_BASE_URL}/uat-runs/${view.run_id}/${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) throw new Error(await errorText(res))
      const v = await res.json()
      if (keyRef.current === ticketKey) setView(v)
    } catch (e) {
      setActionError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const onOverride = (caseId, status, note) => post('review/results', { case_id: caseId, status, note })

  const awaiting = view.status === 'awaiting_review'
  const tracker = view.tracker || {}
  const stale = !view.plan?.live
  const agentPasses = groups.passed.filter((c) => c.result?.actor?.kind === 'agent')
  const otherPasses = groups.passed.filter((c) => c.result?.actor?.kind !== 'agent')
  const flatten = (byCode) => Object.entries(byCode)
  const jiraUrl = getJiraTicketUrl(view.ticket_key)
  const commentUrl = jiraUrl && tracker.comment_id ? `${jiraUrl}?focusedCommentId=${tracker.comment_id}` : null
  const denials = Object.values(view.permission_denials || {}).reduce((n, d) => n + d.length, 0)

  let statusChip
  if (view.status === 'running') statusChip = <Chip size="sm" dot dotColor="var(--accent)" pulse>Running</Chip>
  else if (awaiting) statusChip = <Chip size="sm" dot dotColor="var(--warning)" pulse>Awaiting your review</Chip>
  else if (view.status === 'approved' && tracker.outcome === 'posted') statusChip = <Chip size="sm" dot dotColor="var(--success)">Approved · posted</Chip>
  else if (view.status === 'approved') statusChip = <Chip size="sm" dot dotColor="var(--danger)">Approved · post incomplete</Chip>
  else statusChip = <Chip size="sm" dot dotColor="var(--fg-faint)">{view.status}</Chip>

  return (
    <div className="card uat-review" style={{ marginTop: 'var(--s-5)' }}>
      <div className="uat-head">
        <Icon name="beaker" size={14} style={{ color: 'var(--accent)' }} />
        <span className="uat-title">UAT run {view.run_id}</span>
        {statusChip}
        <span className="uat-muted">
          plan v{view.plan_version} · {view.counts.passed} passed · {view.counts.failed} failed · {view.counts.not_run} not run
        </span>
        <span style={{ flex: 1 }} />
        <span className="uat-muted">
          {[view.cost_usd != null && `$${Number(view.cost_usd).toFixed(2)}`, fmtDuration(view.duration_s),
            denials ? `${denials} permission denial${denials === 1 ? '' : 's'}` : null].filter(Boolean).join(' · ')}
        </span>
        <Btn variant="ghost" size="sm" icon="refresh" onClick={load} title="Reload this run">Reload</Btn>
      </div>

      <div className="uat-body">
        {stale && (
          <Alert tone="warning" title="This run tested a plan that is no longer live">
            {view.plan?.live_plan_id
              ? `The live plan is v${view.plan.live_plan_version}. Posting is blocked; start a new run on the live plan.`
              : 'No plan of this ticket is live in Jira. Posting is blocked.'}
          </Alert>
        )}
        {view.status === 'running' && (
          <Alert tone="info" title="Still running">The review opens when every runner has finished. Reload to check.</Alert>
        )}

        {(view.builds || []).length > 0 && (
          <div className="uat-builds">
            {view.builds.map((b) => (
              <span key={b.repo} className="uat-build" data-status={b.status} title={b.detail || ''}>
                {b.repo}: {b.status}{b.env ? ` (${b.env})` : ''}
              </span>
            ))}
          </div>
        )}

        <Group title="Failed" tone="danger" cases={groups.failed} open={awaiting} onOverride={onOverride} busy={busy} defaultOpen />
        <Group title="Runner passes, pending your review" tone="success" cases={agentPasses} open={awaiting} onOverride={onOverride} busy={busy} defaultOpen />
        <Group title="Passed by a person" tone="success" cases={otherPasses} open={awaiting} onOverride={onOverride} busy={busy} />
        {flatten(groups.needsPerson).map(([code, cases]) => (
          <Group key={code} title={`Still needs a person: ${NOT_RUN_LABELS[code] || code}`} cases={cases} open={awaiting} onOverride={onOverride} busy={busy} />
        ))}
        {flatten(groups.blocked).map(([code, cases]) => (
          <Group key={code} title={`Not run: ${NOT_RUN_LABELS[code] || code}`} cases={cases} open={awaiting} onOverride={onOverride} busy={busy} />
        ))}
        <Group title="No result recorded" cases={groups.noResult} open={awaiting} onOverride={onOverride} busy={busy} />

        <div className="uat-media uat-muted">
          <Icon name="paperclip" size={12} />{' '}
          {view.media?.video
            ? `${view.media.video.name}${view.media.video.too_large ? ' (too large to attach)' : ' will be attached'}`
            : 'No walkthrough video in this run'}
          {(view.media?.fail_frames || []).length > 0 && ` · ${view.media.fail_frames.length} failure frame(s)`}
        </div>

        {awaiting && (
          <div className="uat-decision">
            <SectLbl>Next step</SectLbl>
            <div className="uat-steps" role="radiogroup">
              {STEP_OPTIONS.map((s) => (
                <label key={String(s)} className="uat-step" data-active={step === s ? 'true' : 'false'}>
                  <input type="radio" name="uat-next-step" checked={step === s} onChange={() => setNextStep(s)} />
                  {s ? NEXT_STEP_LABELS[s] : 'Stay in testing'}
                  {s === view.derived_next_step && <span className="uat-muted"> (suggested)</span>}
                </label>
              ))}
            </div>
            <textarea className="uat-input" rows={2} placeholder="Notes for the developer (optional)"
              value={notes} onChange={(e) => setNotes(e.target.value)} />
            <Coll icon="message" title="Comment preview" preview="exactly what will be posted">
              <pre className="uat-preview">{preview}</pre>
            </Coll>
            {actionError && <Alert tone="danger" title="Not done">{actionError}</Alert>}
            {!rejecting ? (
              <div className="uat-actions">
                <Btn variant="primary" icon="send" loading={busy} disabled={stale}
                  onClick={() => post('approve', { next_step: step, notes })}>
                  Approve & post{step ? ` · ${nextStepLabel(step)}` : ''}
                </Btn>
                <Btn variant="danger-soft" icon="x" disabled={busy} onClick={() => setRejecting(true)}>Reject…</Btn>
              </div>
            ) : (
              <div className="uat-actions">
                <span className="uat-muted">Nothing is posted; the runner's results never count.</span>
                <Btn variant="danger" loading={busy} onClick={() => post('reject', { notes })}>Reject run</Btn>
                <Btn variant="ghost" onClick={() => setRejecting(false)}>Cancel</Btn>
              </div>
            )}
          </div>
        )}

        {view.status === 'approved' && (
          tracker.outcome === 'posted' ? (
            <Alert tone="success" title="Posted to Jira">
              Results comment {tracker.comment_updated ? 'updated' : 'written'}
              {commentUrl && <> (<a href={commentUrl} target="_blank" rel="noreferrer">open</a>)</>}
              {(tracker.attachments || []).length > 0 && `; attached ${tracker.attachments.map((a) => a.name).join(', ')}`}
              {tracker.transition?.target_status
                ? `; moved to ${tracker.transition.target_status}, assigned to ${tracker.transition.assigned_to}.`
                : '; ticket left where it was.'}
            </Alert>
          ) : (
            <Alert tone="danger" title={OUTCOME_TEXT[tracker.outcome] || 'Posting did not finish'}
              action={<Btn variant="secondary" size="sm" icon="refresh" loading={busy}
                onClick={() => post('approve', { next_step: null })}>Retry post</Btn>}>
              {tracker.error || 'The approval is recorded. Retry resumes from the step that failed.'}
              {actionError && <div>{actionError}</div>}
            </Alert>
          )
        )}
        {view.status === 'rejected' && (
          <Alert tone="muted" title="Rejected">
            Nothing was posted.{view.review?.notes ? ` Notes: ${view.review.notes}` : ''}
          </Alert>
        )}
      </div>
    </div>
  )
}
