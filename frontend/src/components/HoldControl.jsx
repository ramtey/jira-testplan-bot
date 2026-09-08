/**
 * The QA hold: "testing is parked, and here's why."
 *
 * Deliberately *not* a Jira status. Jira's status says where the ticket sits in
 * the workflow and its blocked-by links say which tickets gate it; neither says
 * "we haven't started testing because the PR is still in review". That gap is
 * what this fills, and it's shared across everyone testing the ticket.
 *
 * Split across two surfaces on purpose:
 *   - HoldChip lives in the header meta row next to the status pill, because
 *     that's where you look to learn the ticket's state.
 *   - HoldControl (the button) lives on the action row next to the workflow
 *     buttons, because that's where you look for something to click. It sits
 *     outside WorkflowActions so projects without the workflow feature still
 *     get a hold.
 */

import { useEffect, useRef, useState } from 'react'
import Icon from './Icon'
import { HOLD_REASONS, holdReasonLabel } from '../hooks/useTicketHold'
import { formatRelativeTime } from '../utils/time'

const DEFAULT_REASON = 'code-review'

// Passive state readout for the meta row. Renders nothing when the ticket is
// running normally, so an un-held ticket's header looks exactly as it did.
export function HoldChip({ hold }) {
  if (!hold?.held) return null
  const since = hold.heldSince ? formatRelativeTime(hold.heldSince) : ''
  return (
    <span
      className="hold-chip"
      title={[holdReasonLabel(hold.reason), hold.note, since && `On hold since ${since}`]
        .filter(Boolean)
        .join(' — ')}
    >
      <Icon name="flag" size={11} />
      {holdReasonLabel(hold.reason)}
      {since && <span className="hold-since">{since}</span>}
    </span>
  )
}

export default function HoldControl({ hold }) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState(DEFAULT_REASON)
  const [note, setNote] = useState('')
  const wrapRef = useRef(null)
  const noteRef = useRef(null)

  const openForm = () => {
    // Prefill from the live hold so reopening is an edit, not a re-entry.
    setReason(hold.reason || DEFAULT_REASON)
    setNote(hold.note || '')
    setOpen(true)
  }

  // Dismiss on outside click or Escape — the form is a popover over the card,
  // so leaving it open while the user moves on would cover ticket content.
  useEffect(() => {
    if (!open) return undefined
    const onDown = (e) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false)
    }
    const onKey = (e) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  // 'other' carries no meaning without the note, so focus it when picked.
  useEffect(() => {
    if (open && reason === 'other' && noteRef.current) noteRef.current.focus()
  }, [open, reason])

  const save = async () => {
    const saved = await hold.setHoldOn(reason, note)
    if (saved) setOpen(false)
  }

  const resume = async () => {
    setOpen(false)
    await hold.resume()
  }

  const noteRequired = reason === 'other' && !note.trim()

  return (
    <span className="hold-wrap" ref={wrapRef}>
      {hold.held ? (
        <>
          <button
            type="button"
            className="btn hold-btn-on"
            data-size="sm"
            onClick={openForm}
            disabled={hold.saving}
            title={`On hold: ${holdReasonLabel(hold.reason)}${
              hold.note ? ` — ${hold.note}` : ''
            }\nClick to change the reason`}
          >
            <Icon name="flag" className="ic" />
            On hold
          </button>
          <button
            type="button"
            className="btn"
            data-variant="secondary"
            data-size="sm"
            onClick={resume}
            disabled={hold.saving}
            title="Clear the hold — testing can continue"
          >
            {hold.saving ? <span className="spin" /> : <Icon name="play" className="ic" />}
            Resume
          </button>
        </>
      ) : (
        <button
          type="button"
          className="btn"
          data-variant="secondary"
          data-size="sm"
          onClick={openForm}
          disabled={hold.saving}
          title="Park this ticket with a reason — shows in the tab title and to the rest of QA"
        >
          <Icon name="clock" className="ic" />
          Hold
        </button>
      )}

      {open && (
        <div className="hold-pop" role="dialog" aria-label="Put ticket on hold">
          <div className="hold-pop-head">
            <span className="lbl" style={{ margin: 0 }}>
              {hold.held ? 'Edit hold' : 'Put on hold'}
            </span>
            <button
              type="button"
              className="tc-copy-btn"
              onClick={() => setOpen(false)}
              aria-label="Close"
              style={{ width: 14, height: 14, opacity: 1, marginLeft: 'auto' }}
            >
              <Icon name="x" size={13} />
            </button>
          </div>

          <div className="hold-reasons">
            {HOLD_REASONS.map((r) => (
              <label key={r.code} className="hold-reason" data-on={reason === r.code}>
                <input
                  type="radio"
                  name="hold-reason"
                  value={r.code}
                  checked={reason === r.code}
                  onChange={() => setReason(r.code)}
                />
                {r.label}
              </label>
            ))}
          </div>

          <textarea
            ref={noteRef}
            className="inp"
            rows={2}
            style={{ marginTop: 'var(--s-4)', minHeight: 52, resize: 'vertical' }}
            placeholder={
              reason === 'other'
                ? 'Required for "Other" — what are we waiting on?'
                : 'Optional detail, e.g. "waiting on the migration in SK-1180"'
            }
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />

          {hold.error && (
            <div style={{ marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--danger)' }}>
              {hold.error}
            </div>
          )}

          <p className="hold-pop-hint">
            Everyone testing this ticket sees the hold.
          </p>

          <div className="hold-pop-foot">
            <button
              type="button"
              className="btn"
              data-variant="secondary"
              data-size="sm"
              onClick={() => setOpen(false)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="btn"
              data-variant="primary"
              data-size="sm"
              onClick={save}
              disabled={hold.saving || noteRequired}
              title={noteRequired ? 'Add a note to explain "Other"' : undefined}
            >
              {hold.saving ? <span className="spin" /> : null}
              {hold.held ? 'Update hold' : 'Hold'}
            </button>
          </div>
        </div>
      )}
    </span>
  )
}
