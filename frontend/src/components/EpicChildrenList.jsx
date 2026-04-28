/**
 * Lists child tickets under an Epic. Each row hosts its own Generate / Analyze
 * actions and renders results inline beneath itself.
 */

import { useEffect, useState } from 'react'
import { API_BASE_URL } from '../config'
import EpicChildRow from './EpicChildRow'

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
      <div className="epic-children-section">
        <p className="epic-children-loading">
          <span className="spinner"></span> Loading child tickets…
        </p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="epic-children-section">
        <div className="alert alert-error">
          <strong>Error:</strong> {error}
        </div>
        <button type="button" className="btn-generate btn-small" onClick={load}>
          Retry
        </button>
      </div>
    )
  }

  if (!children || children.length === 0) {
    return (
      <div className="epic-children-section">
        <div className="alert alert-info">
          <strong>ℹ️ No child tickets found under {epicKey}.</strong>
        </div>
      </div>
    )
  }

  return (
    <div className="epic-children-section">
      <h3 className="epic-children-heading">
        Tickets under {epicKey} ({children.length})
      </h3>
      <div className="epic-children-list">
        {children.map((child) => (
          <EpicChildRow key={child.key} child={child} />
        ))}
      </div>
    </div>
  )
}

export default EpicChildrenList
