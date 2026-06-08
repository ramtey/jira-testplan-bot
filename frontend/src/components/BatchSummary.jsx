/**
 * One-call summary across a multi-ticket fetch.
 *
 * Hits /issues/summarize-batch once for the whole bundle and renders:
 *   - an overall "what does this batch deliver" paragraph
 *   - a one-liner per ticket (KEY — blurb)
 *
 * Lives above the compact row list as a collapsible row, mirroring the
 * single-ticket "Summary" section. First expand triggers the LLM call;
 * collapsing keeps the result so re-expanding is instant.
 */

import { useState } from 'react'
import { API_BASE_URL } from '../config'
import { Coll, Btn } from './ui'

function BatchSummary({ tickets }) {
  const [open, setOpen] = useState(false)
  const [summary, setSummary] = useState(null) // { overview, per_ticket: [{key, blurb}] }
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  if (!tickets || tickets.length < 2) return null

  const buildPayload = () =>
    tickets.map((t) => ({
      key: t.key,
      summary: t.summary,
      description: t.description,
    }))

  const fetchSummary = async () => {
    setLoading(true)
    setError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/issues/summarize-batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tickets: buildPayload() }),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(data.detail || `Failed (${response.status})`)
      }
      setSummary({
        overview: (data.overview || '').toString(),
        per_ticket: Array.isArray(data.per_ticket) ? data.per_ticket : [],
      })
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const handleToggle = async (nextOpen) => {
    setOpen(nextOpen)
    if (nextOpen && summary === null && !loading) {
      await fetchSummary()
    }
  }

  const previewText = summary?.overview
    ? summary.overview.length > 100
      ? summary.overview.slice(0, 100) + '…'
      : summary.overview
    : `Click to summarize ${tickets.length} tickets together`

  return (
    <div style={{ marginTop: 'var(--s-6)' }}>
      <Coll icon="sparkles" title="Batch summary" open={open} onToggle={handleToggle} preview={previewText}>
        {loading && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', color: 'var(--fg-muted)' }}>
            <span className="spin" />
            Reading {tickets.length} tickets and summarizing…
          </div>
        )}

        {error && !loading && (
          <div style={{ color: 'var(--danger)', fontSize: 'var(--t-sm)' }}>{error}</div>
        )}

        {summary && !loading && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-4)' }}>
            {summary.overview && (
              <p style={{ margin: 0, color: 'var(--fg)', lineHeight: 'var(--lh-md)' }}>
                {summary.overview}
              </p>
            )}
            {summary.per_ticket.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-2)' }}>
                {summary.per_ticket.map((row) => (
                  <div
                    key={row.key}
                    style={{
                      display: 'flex',
                      gap: 'var(--s-3)',
                      fontSize: 'var(--t-sm)',
                      color: 'var(--fg)',
                      lineHeight: 'var(--lh-md)',
                    }}
                  >
                    <span
                      style={{
                        fontFamily: 'var(--font-mono)',
                        color: 'var(--accent)',
                        flexShrink: 0,
                        minWidth: 70,
                      }}
                    >
                      {row.key}
                    </span>
                    <span style={{ color: row.blurb ? 'var(--fg)' : 'var(--fg-subtle)', fontStyle: row.blurb ? 'normal' : 'italic' }}>
                      {row.blurb || 'No summary returned for this ticket.'}
                    </span>
                  </div>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 'var(--s-2)' }}>
              <Btn variant="ghost" size="sm" icon="refresh" onClick={fetchSummary} disabled={loading}>
                Regenerate
              </Btn>
            </div>
          </div>
        )}
      </Coll>
    </div>
  )
}

export default BatchSummary
