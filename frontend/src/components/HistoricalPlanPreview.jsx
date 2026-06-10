/**
 * Renders a previously-stored test plan beside the live one for comparison.
 */

import TestPlanDisplay from './TestPlanDisplay'
import Icon from './Icon'
import { Btn } from './ui'

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

export default function HistoricalPlanPreview({
  plan,
  version,
  createdAt,
  ticketData,
  onClose,
}) {
  return (
    <section
      aria-label="Historical test plan preview"
      style={{
        marginTop: 'var(--s-7)',
        background: 'var(--bg-surface)',
        border: '1px solid var(--line)',
        borderRadius: 'var(--r-md)',
        opacity: 0.85,
      }}
    >
      <header
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 'var(--s-3)',
          padding: 'var(--s-4) var(--s-5)',
          background: 'var(--bg-panel)',
          borderBottom: '1px solid var(--line)',
          borderRadius: 'var(--r-md) var(--r-md) 0 0',
        }}
      >
        <Icon name="history" size={14} style={{ color: 'var(--fg-muted)' }} />
        <span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>
          History preview · <strong style={{ color: 'var(--fg)' }}>v{version}</strong> · {formatRelative(createdAt)}
        </span>
        <span style={{ flex: 1 }} />
        <Btn variant="ghost" size="sm" icon="x" onClick={onClose}>Close preview</Btn>
      </header>

      <div style={{ padding: 'var(--s-5)' }}>
        <TestPlanDisplay testPlan={plan} ticketData={ticketData} />
      </div>
    </section>
  )
}
