/**
 * Carry QA marks across a regeneration, case by case.
 *
 * Regenerating a plan whose shape changed moves progress to a fresh key, so
 * every mark made against the previous plan stops applying. That is deliberate
 * — a stale check landing on a *different* case is the worst thing this app can
 * do — but until now the only remedy was a human re-mapping the two plans by
 * hand. SK-2325 regenerated from `4-6-2-10` to `5-8-0-9-4` and stranded sixteen
 * marks; some cases had moved section, some had been reworded, two had no
 * equivalent at all, and the tester worked it out across two Jira comments in
 * two tabs.
 *
 * The server proposes; this dialog is where the tester decides. Nothing is
 * carried that isn't ticked here, reworded cases open unticked with both
 * wordings shown side by side, and a case with no equivalent is listed as one
 * so it reads as "you tested something that is gone", not as an oversight.
 */

import { useEffect, useMemo, useState } from 'react'
import { API_BASE_URL } from '../config'
import Icon from './Icon'
import { Btn, Cbx, Alert } from './ui'

const STATUS_LABEL = {
  same: 'Same case',
  reworded: 'Wording changed',
  gone: 'No equivalent',
  unresolved: 'Unreadable',
}

const STATUS_TONE = {
  same: { color: 'var(--success)', border: 'rgba(34,197,94,.3)', bg: 'rgba(34,197,94,.12)' },
  reworded: { color: '#fcd34d', border: 'rgba(245,158,11,.35)', bg: 'rgba(245,158,11,.10)' },
  gone: { color: '#fca5a5', border: 'rgba(239,68,68,.35)', bg: 'rgba(239,68,68,.10)' },
  unresolved: { color: 'var(--fg-muted)', border: 'var(--line)', bg: 'var(--bg-input)' },
}

function StatusChip({ status }) {
  const tone = STATUS_TONE[status] || STATUS_TONE.unresolved
  return (
    <span
      style={{
        height: 18,
        padding: '0 6px',
        borderRadius: 3,
        background: tone.bg,
        border: `1px solid ${tone.border}`,
        color: tone.color,
        fontSize: 10.5,
        fontWeight: 500,
        display: 'inline-flex',
        alignItems: 'center',
        whiteSpace: 'nowrap',
      }}
    >
      {STATUS_LABEL[status] || status}
    </span>
  )
}

function CaseRef({ id, title }) {
  return (
    <div style={{ display: 'flex', gap: 6, alignItems: 'baseline', minWidth: 0 }}>
      <code
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10.5,
          padding: '1px 5px',
          background: 'var(--bg-input)',
          border: '1px solid var(--line)',
          borderRadius: 3,
          color: 'var(--fg-subtle)',
          whiteSpace: 'nowrap',
        }}
      >
        {id}
      </code>
      <span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)', overflowWrap: 'anywhere' }}>
        {title || <em style={{ color: 'var(--fg-subtle)' }}>title not recoverable</em>}
      </span>
    </div>
  )
}

export default function MarkCarryOverModal({ planId, onClose, onCarried }) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState(() => new Set())
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let cancelled = false
    fetch(`${API_BASE_URL}/plans/${planId}/mark-carryover`)
      .then(async (r) => {
        if (!r.ok) throw new Error(`carry-over ${r.status}`)
        return r.json()
      })
      .then((body) => {
        if (cancelled) return
        setData(body)
        // Only exact matches are pre-selected. A reworded case is the one thing
        // here a tester must actually read — pre-ticking it would carry a check
        // onto a case whose meaning may have moved, which is the failure this
        // dialog exists to avoid rather than automate.
        setSelected(
          new Set(
            (body.marks || [])
              .filter((m) => m.status === 'same' && m.to)
              .map((m) => m.to)
          )
        )
      })
      .catch((e) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false))
    return () => {
      cancelled = true
    }
  }, [planId])

  const marks = useMemo(() => data?.marks || [], [data])
  const carriable = useMemo(() => marks.filter((m) => m.to), [marks])
  const stranded = useMemo(() => marks.filter((m) => !m.to), [marks])

  const toggle = (id) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const handleCarry = async () => {
    setSaving(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE_URL}/plans/${planId}/mark-carryover`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: [...selected] }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        throw new Error(body?.detail || `carry-over failed (${res.status})`)
      }
      const body = await res.json()
      onCarried?.(body.checked_ids || [])
      onClose()
    } catch (e) {
      // The marks were not carried. Saying so is the point — a dialog that
      // closed on a failed write would read as a successful migration.
      setError(e.message)
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        style={{ width: 'min(880px, calc(100vw - 64px))' }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Carry over QA marks"
      >
        <header
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 'var(--s-3)',
            padding: 'var(--s-6)',
            borderBottom: '1px solid var(--line)',
          }}
        >
          <Icon name="history" size={16} style={{ color: 'var(--accent)' }} />
          <h2 style={{ margin: 0, fontSize: 'var(--t-lg)', fontWeight: 600, color: 'var(--fg-strong)' }}>
            Carry over marks from the previous plan
          </h2>
          <span style={{ flex: 1 }} />
          <button
            onClick={onClose}
            aria-label="Close"
            style={{ background: 'none', border: 'none', color: 'var(--fg-muted)', cursor: 'pointer' }}
          >
            <Icon name="x" size={16} />
          </button>
        </header>

        <div style={{ padding: 'var(--s-6)', maxHeight: '60vh', overflowY: 'auto' }}>
          {loading && <div style={{ color: 'var(--fg-muted)', fontSize: 'var(--t-sm)' }}>Loading…</div>}

          {error && <Alert tone="danger" title="Nothing was carried over">{error}</Alert>}

          {!loading && data && !data.source && (
            <Alert tone="info" title="No earlier marks to carry">
              This ticket has no QA progress recorded under any other plan shape.
            </Alert>
          )}

          {!loading && data?.source && !data.source.resolvable && (
            <Alert tone="warning" title="The previous plan is no longer stored">
              {data.source.checked_count} mark
              {data.source.checked_count === 1 ? ' was' : 's were'} recorded under{' '}
              <code>{data.source.fingerprint}</code>, but the plan that produced that
              shape is gone, so there is no way to say which case each one named.
              They are listed below by id only.
            </Alert>
          )}

          {!loading && data?.source && (
            <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', marginBottom: 'var(--s-5)' }}>
              {data.source.checked_count} mark{data.source.checked_count === 1 ? '' : 's'} under{' '}
              <code>{data.source.fingerprint}</code>
              {data.source.plan_id ? ` (plan ${data.source.plan_id})` : ''}
              {data.source.updated_at
                ? `, last updated ${new Date(data.source.updated_at).toLocaleString()}`
                : ''}
              . Ticking a row records the mark against the case named on the right.
            </div>
          )}

          {carriable.map((mark) => (
            <div
              key={mark.from}
              style={{
                display: 'flex',
                gap: 'var(--s-4)',
                alignItems: 'flex-start',
                padding: 'var(--s-4) 0',
                borderBottom: '1px solid var(--divider)',
              }}
            >
              <div style={{ paddingTop: 2 }}>
                <Cbx checked={selected.has(mark.to)} onChange={() => toggle(mark.to)} />
              </div>
              <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
                <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                  <StatusChip status={mark.status} />
                  {mark.to_optional && (
                    <span style={{ fontSize: 10.5, color: 'var(--fg-subtle)' }}>
                      optional — already unit-tested
                    </span>
                  )}
                  {mark.from_section !== mark.to_section && (
                    <span style={{ fontSize: 10.5, color: 'var(--fg-subtle)' }}>
                      moved {mark.from_section} → {mark.to_section}
                    </span>
                  )}
                </div>
                <CaseRef id={mark.from} title={mark.from_title} />
                {mark.status === 'reworded' && (
                  <div style={{ display: 'flex', gap: 6, alignItems: 'center', paddingLeft: 2 }}>
                    <Icon name="chevron-down" size={12} style={{ color: 'var(--fg-subtle)' }} />
                    <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
                      now reads
                    </span>
                  </div>
                )}
                {mark.to_title !== mark.from_title && <CaseRef id={mark.to} title={mark.to_title} />}
                {mark.to_title === mark.from_title && (
                  <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
                    lands on <code>{mark.to}</code>
                  </div>
                )}
              </div>
            </div>
          ))}

          {stranded.length > 0 && (
            <div style={{ marginTop: 'var(--s-6)' }}>
              <div
                style={{
                  fontSize: 'var(--t-xs)',
                  textTransform: 'uppercase',
                  letterSpacing: '.04em',
                  fontWeight: 600,
                  color: 'var(--fg-subtle)',
                  marginBottom: 'var(--s-3)',
                }}
              >
                Cannot be carried ({stranded.length})
              </div>
              <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', marginBottom: 'var(--s-4)' }}>
                You tested these; the regenerated plan has nothing close enough to
                be the same case. Worth reading before you accept the new plan —
                a dropped case is either a case that stopped mattering or one the
                planner lost.
              </div>
              {stranded.map((mark) => (
                <div
                  key={mark.from}
                  style={{
                    display: 'flex',
                    gap: 'var(--s-4)',
                    alignItems: 'flex-start',
                    padding: 'var(--s-3) 0',
                    borderBottom: '1px solid var(--divider)',
                  }}
                >
                  <StatusChip status={mark.status} />
                  <CaseRef id={mark.from} title={mark.from_title} />
                </div>
              ))}
            </div>
          )}
        </div>

        <footer
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 'var(--s-4)',
            padding: 'var(--s-6)',
            borderTop: '1px solid var(--line)',
          }}
        >
          <span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>
            {selected.size} of {carriable.length} selected
          </span>
          <span style={{ flex: 1 }} />
          <Btn variant="ghost" onClick={onClose}>Cancel</Btn>
          <Btn
            variant="primary"
            icon="check"
            onClick={handleCarry}
            disabled={saving || selected.size === 0}
            loading={saving}
          >
            {saving ? 'Carrying over…' : `Carry over ${selected.size}`}
          </Btn>
        </footer>
      </div>
    </div>
  )
}
