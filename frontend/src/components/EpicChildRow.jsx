/**
 * One row inside the Epic children list. Holds its own state for fetch /
 * generate / analyze and renders the result inline beneath the row.
 */

import { useEffect, useRef, useState } from 'react'
import { API_BASE_URL, useJiraTicketUrl } from '../config'
import { formatRelativeTime } from '../utils/time'
import TestPlanDisplay from './TestPlanDisplay'
import BugAnalysisDisplay from './BugAnalysisDisplay'
import Icon from './Icon'
import { Btn, ItChip, StatPill, Alert } from './ui'

const NON_TESTABLE_ISSUE_TYPES = new Set(['Epic', 'Spike'])

function statusCat(s) {
  const v = (s || '').toLowerCase()
  if (v === 'done' || v === 'complete') return 'done'
  if (v === 'indeterminate' || v === 'inprogress' || v === 'in progress') return 'inprogress'
  if (v === 'blocked') return 'blocked'
  return 'todo'
}

function EpicChildRow({ child }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)
  const [testPlan, setTestPlan] = useState(null)
  const [bugAnalysis, setBugAnalysis] = useState(null)
  const [collapsed, setCollapsed] = useState(false)
  // Set when the plan on screen came from storage rather than this click, so
  // the row can say how old it is. The full predates-the-merge check lives on
  // the ticket view, which already has the PR merge times loaded; buying them
  // here would mean the very ticket fetch this change removes.
  const [storedAt, setStoredAt] = useState(null)
  const resultRef = useRef(null)

  const isTestable = !NON_TESTABLE_ISSUE_TYPES.has(child.issue_type)
  const isBug = child.issue_type === 'Bug'
  const hasResult = testPlan || bugAnalysis

  useEffect(() => {
    if (hasResult && !collapsed && resultRef.current) {
      resultRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  }, [testPlan, bugAnalysis, collapsed, hasResult])

  const fetchTicketDetail = async () => {
    const res = await fetch(`${API_BASE_URL}/issue/${child.key}`)
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || `Failed to fetch ${child.key}`)
    }
    return res.json()
  }

  const buildBugLensPayload = (td) => ({
    ticket_key: td.key,
    summary: td.summary,
    description: td.description,
    issue_type: td.issue_type,
    development_info: td.development_info,
    comments: td.comments || null,
    parent_info: td.parent || null,
    linked_info: td.linked_issues || null,
    status: td.status || null,
    status_category: td.status_category || null,
  })

  // A plan already on disk — the watcher's, or one from an earlier session —
  // is *the* plan, not history. This row used to regenerate on every click and
  // re-pay for it.
  const loadStoredPlan = async () => {
    const res = await fetch(`${API_BASE_URL}/runs/by-ticket/${child.key}`)
    if (!res.ok) return null
    const data = await res.json()
    const runs = Array.isArray(data.runs) ? data.runs : []
    // Newest first, and Bug Lens runs share the table without a plan_id.
    const latest = runs.find((r) => r.plan_id)
    if (!latest) return null
    const planRes = await fetch(`${API_BASE_URL}/plans/${latest.plan_id}`)
    if (!planRes.ok) return null
    const stored = await planRes.json()
    try {
      return {
        ...JSON.parse(stored.body),
        plan_id: latest.plan_id,
        version: latest.version,
        created_at: latest.created_at,
      }
    } catch {
      // Unparseable stored body — fall through and generate rather than
      // leaving the row with nothing and no explanation.
      return null
    }
  }

  const handleGenerate = async ({ force = false } = {}) => {
    setBusy('generate')
    setError(null)
    setTestPlan(null)
    setBugAnalysis(null)
    setCollapsed(false)
    setStoredAt(null)
    try {
      if (!force) {
        const stored = await loadStoredPlan()
        if (stored) {
          setTestPlan(stored)
          setStoredAt(stored.created_at || '')
          return
        }
      }
      // The key-only endpoint assembles the payload server-side through
      // plan_service, so this row can't drift from what the ticket view sends
      // — the hand-maintained copy that used to live here already had.
      const res = await fetch(`${API_BASE_URL}/tickets/${child.key}/plan`, {
        method: 'POST',
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to generate test plan')
      }
      setTestPlan(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  const handleAnalyze = async () => {
    setBusy('analyze')
    setError(null)
    setTestPlan(null)
    setBugAnalysis(null)
    setCollapsed(false)
    try {
      const td = await fetchTicketDetail()
      const res = await fetch(`${API_BASE_URL}/bug-lens/analyze`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildBugLensPayload(td)),
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to analyze bug')
      }
      setBugAnalysis(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  const jiraUrl = useJiraTicketUrl(child.key)
  const cat = statusCat(child.status_category || child.status)

  return (
    <div>
      <div className="card" data-interactive="true" style={{ padding: '10px var(--s-5)', display: 'flex', alignItems: 'center', gap: 'var(--s-4)' }}>
        {child.issue_type && <ItChip type={child.issue_type} label={child.issue_type} />}
        {jiraUrl ? (
          <a href={jiraUrl} target="_blank" rel="noopener noreferrer" style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-sm)', color: 'var(--accent)', flexShrink: 0 }}>
            {child.key}
          </a>
        ) : (
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-sm)', color: 'var(--fg)', flexShrink: 0 }}>{child.key}</span>
        )}
        <span style={{ flex: 1, minWidth: 0, fontSize: 'var(--t-sm)', color: 'var(--fg)', overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>
          {child.summary}
        </span>
        {child.status && <StatPill cat={cat}>{child.status}</StatPill>}
        <span style={{ display: 'flex', gap: 4 }}>
          {isTestable && (
            <Btn variant="ghost" size="sm" icon="beaker" onClick={() => handleGenerate()} disabled={busy !== null} loading={busy === 'generate'}>
              Generate
            </Btn>
          )}
          {isBug && (
            <Btn variant="ghost" size="sm" icon="scan" onClick={handleAnalyze} disabled={busy !== null} loading={busy === 'analyze'}>
              Analyze
            </Btn>
          )}
          {hasResult && (
            <Btn variant="ghost" size="sm" icon={collapsed ? 'chevron-down' : 'chevron-up'} onClick={() => setCollapsed((c) => !c)}>
              {collapsed ? 'Show' : 'Hide'}
            </Btn>
          )}
        </span>
      </div>

      {error && (
        <div style={{ marginTop: 'var(--s-3)', marginLeft: 24, paddingLeft: 'var(--s-5)', borderLeft: '2px solid var(--line-strong)' }}>
          <Alert tone="danger" title="Failed">{error}</Alert>
        </div>
      )}

      {hasResult && !collapsed && (
        <div
          ref={resultRef}
          style={{ marginTop: 'var(--s-3)', marginLeft: 24, paddingLeft: 'var(--s-5)', borderLeft: '2px solid var(--line-strong)' }}
        >
          {testPlan && (
            <>
              {storedAt !== null && (
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 'var(--s-3)',
                    marginBottom: 'var(--s-3)',
                    fontSize: 'var(--t-xs)',
                    color: 'var(--fg-subtle)',
                  }}
                >
                  <Icon name="clock" size={12} />
                  <span>
                    Existing plan
                    {testPlan.version ? ` v${testPlan.version}` : ''}
                    {storedAt ? `, written ${formatRelativeTime(storedAt)}` : ''} — reused
                    rather than regenerated
                  </span>
                  <Btn
                    variant="ghost"
                    size="sm"
                    icon="refresh"
                    onClick={() => handleGenerate({ force: true })}
                    disabled={busy !== null}
                    loading={busy === 'generate'}
                    title="Ignore the stored plan and generate a new one"
                  >
                    Regenerate
                  </Btn>
                </div>
              )}
              <TestPlanDisplay
                testPlan={testPlan}
                ticketData={{ key: child.key, summary: child.summary }}
              />
            </>
          )}
          {bugAnalysis && <BugAnalysisDisplay analysis={bugAnalysis} />}
        </div>
      )}
    </div>
  )
}

export default EpicChildRow
