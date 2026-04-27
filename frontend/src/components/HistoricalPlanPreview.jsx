/**
 * Renders a previously-stored test plan beside the live one for comparison.
 *
 * Wraps TestPlanDisplay in a muted (gray) container with a header that
 * identifies the version and a Close button. Used when the user clicks "View"
 * on a row in RunHistoryBanner — the live plan is preserved, this one appears
 * below it for side-by-side reading.
 */

import TestPlanDisplay from './TestPlanDisplay'

function formatRelative(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const diffMs = Date.now() - d.getTime()
  const mins = Math.round(diffMs / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return d.toLocaleDateString()
}

export default function HistoricalPlanPreview({ plan, version, createdAt, ticketData, onClose }) {
  return (
    <section className="history-preview" aria-label="Historical test plan preview">
      <header className="history-preview-header">
        <span className="history-preview-label">
          History preview · <strong>v{version}</strong> · {formatRelative(createdAt)}
        </span>
        <button type="button" className="history-preview-close" onClick={onClose}>
          Close preview
        </button>
      </header>

      <div className="history-preview-body">
        <TestPlanDisplay testPlan={plan} ticketData={ticketData} />
      </div>
    </section>
  )
}
