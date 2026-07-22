/**
 * "We've analyzed this ticket before" banner.
 */

import { useMemo, useState } from 'react'
import PlanDiffModal from './PlanDiffModal'
import { API_BASE_URL } from '../config'
import Icon from './Icon'
import { Btn, Alert, Chip } from './ui'

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

export default function RunHistoryBanner({ runs, ticketData, onViewPlan }) {
  const [expanded, setExpanded] = useState(false)
  const [diffTarget, setDiffTarget] = useState(null)
  const [loadingPlanId, setLoadingPlanId] = useState(null)
  const [error, setError] = useState(null)

  const latest = runs[0]
  const summary = useMemo(() => {
    if (!latest) return ''
    return `${formatRelative(latest.created_at)} · ${latest.case_count} cases`
  }, [latest])
  const latestIsLive = Boolean(latest?.jira_comment_id)

  if (!runs || runs.length === 0) return null

  const handleView = async (run) => {
    setLoadingPlanId(run.plan_id)
    setError(null)
    try {
      const res = await fetch(`${API_BASE_URL}/plans/${run.plan_id}`)
      if (!res.ok) throw new Error('Failed to load plan')
      const data = await res.json()
      let parsed
      try {
        parsed = JSON.parse(data.body)
      } catch {
        throw new Error('Stored plan is not in the expected format')
      }
      onViewPlan(parsed, {
        planId: run.plan_id,
        version: run.version,
        createdAt: run.created_at,
      })
    } catch (err) {
      setError(err.message)
    } finally {
      setLoadingPlanId(null)
    }
  }

  return (
    <div className="card" style={{ marginTop: 'var(--s-5)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-4)', padding: '10px var(--s-5)' }}>
        <Icon name="history" size={14} style={{ color: 'var(--accent)' }} />
        <span style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)' }}>
          v{latest.version}
        </span>
        <span style={{ color: 'var(--fg-muted)', fontSize: 'var(--t-sm)' }}>· {summary}</span>
        {latestIsLive && !expanded && (
          <span
            title={latest.posted_at ? `Posted ${new Date(latest.posted_at).toLocaleString()}` : 'Posted to Jira'}
            style={{ display: 'inline-flex' }}
          >
            <Chip size="sm" dot dotColor="var(--success)" pulse>
              Live in Jira
            </Chip>
          </span>
        )}
        <span style={{ flex: 1 }} />
        <Btn variant="ghost" size="sm" iconRight={expanded ? 'chevron-up' : 'chevron-down'} onClick={() => setExpanded((v) => !v)}>
          {runs.length} version{runs.length === 1 ? '' : 's'}
        </Btn>
      </div>

      {error && (
        <div style={{ padding: '0 var(--s-5) var(--s-4)' }}>
          <Alert tone="danger" title="Failed to load">{error}</Alert>
        </div>
      )}

      {expanded && (
        <div style={{ borderTop: '1px solid var(--divider)' }}>
          {runs.map((run, i) => {
            const isMulti = run.run_type === 'test_plan_multi'
            const otherKeys = (run.ticket_keys || []).filter((k) => k !== ticketData?.key)
            return (
              <div
                key={run.run_id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 'var(--s-4)',
                  padding: '10px var(--s-5)',
                  borderBottom: i < runs.length - 1 ? '1px solid var(--divider)' : 'none',
                }}
              >
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-sm)', color: 'var(--accent)', fontWeight: 600 }}>v{run.version}</span>
                <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>{formatRelative(run.created_at)}</span>
                <span style={{ color: 'var(--fg-muted)', fontSize: 'var(--t-xs)' }}>{run.case_count} cases</span>
                {isMulti && otherKeys.length > 0 && (
                  <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>multi: also {otherKeys.join(', ')}</span>
                )}
                {run.jira_comment_id && (
                  <span
                    title={run.posted_at ? `Posted ${new Date(run.posted_at).toLocaleString()}` : 'Posted to Jira'}
                    style={{ display: 'inline-flex' }}
                  >
                    <Chip size="sm" dot dotColor="var(--success)" pulse>Live in Jira</Chip>
                  </span>
                )}
                <span style={{ flex: 1 }} />
                <Btn
                  variant="ghost"
                  size="sm"
                  icon="eye"
                  onClick={() => handleView(run)}
                  disabled={loadingPlanId === run.plan_id}
                  loading={loadingPlanId === run.plan_id}
                >
                  View
                </Btn>
                <Btn
                  variant="ghost"
                  size="sm"
                  icon="split"
                  onClick={() => setDiffTarget(run)}
                  disabled={!run.previous_plan_id}
                  title={run.previous_plan_id ? 'Compare to previous version' : 'No earlier version'}
                >
                  Diff
                </Btn>
              </div>
            )
          })}
        </div>
      )}

      {diffTarget && (
        <PlanDiffModal
          key={diffTarget.plan_id}
          rightPlan={diffTarget}
          ticketData={ticketData}
          onClose={() => setDiffTarget(null)}
        />
      )}
    </div>
  )
}
