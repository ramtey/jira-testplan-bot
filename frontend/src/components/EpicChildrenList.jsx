/**
 * Lists child tickets under an Epic. Each row hosts its own Generate / Analyze
 * actions and renders results inline beneath itself.
 */

import { useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'
import EpicChildRow from './EpicChildRow'
import Icon from './Icon'
import { Btn, Alert, SectLbl } from './ui'

function EpicChildrenList({ epicKey }) {
  const [children, setChildren] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API_BASE_URL}/issue/${epicKey}/children`)
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to load child tickets')
      }
      const data = await res.json()
      setChildren(data.children || [])
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [epicKey])

  if (loading) {
    return (
      <div style={{ marginTop: 'var(--s-7)', display: 'flex', alignItems: 'center', gap: 'var(--s-3)', color: 'var(--fg-muted)' }}>
        <span className="spin" style={{ color: 'var(--accent)' }} />
        Loading child tickets…
      </div>
    )
  }

  if (error) {
    return (
      <div style={{ marginTop: 'var(--s-7)' }}>
        <Alert tone="danger" title="Failed to load children" action={<Btn variant="ghost" size="sm" onClick={load} icon="refresh">Retry</Btn>}>
          {error}
        </Alert>
      </div>
    )
  }

  if (!children || children.length === 0) {
    return (
      <div style={{ marginTop: 'var(--s-7)' }}>
        <Alert tone="muted" title={`No child tickets under ${epicKey}`}>
          This Epic has no linked sub-tasks or stories yet.
        </Alert>
      </div>
    )
  }

  return (
    <div style={{ marginTop: 'var(--s-7)' }}>
      <SectLbl
        action={
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--s-3)' }}>
            <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', fontFamily: 'var(--font-mono)' }}>{children.length} children</span>
            <Btn variant="ghost" size="sm" icon="refresh" onClick={load}>Refresh</Btn>
          </span>
        }
      >
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--s-3)' }}>
          <Icon name="layers" size={12} style={{ color: 'var(--accent)' }} />
          Tickets under {epicKey}
        </span>
      </SectLbl>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
        {children.map((child) => (
          <EpicChildRow key={child.key} child={child} />
        ))}
      </div>
    </div>
  )
}

export default EpicChildrenList
