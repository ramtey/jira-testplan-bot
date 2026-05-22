/**
 * One row inside the Epic children list. Holds its own state for fetch /
 * generate / analyze and renders the result inline beneath the row.
 */

import { useEffect, useRef, useState } from 'react'
import { API_BASE_URL, getJiraTicketUrl } from '../config'
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

  const buildTestPlanPayload = (td) => ({
    ticket_key: td.key,
    summary: td.summary,
    description: td.description,
    issue_type: td.issue_type,
    testing_context: {},
    development_info: td.development_info,
    image_urls: td.attachments ? td.attachments.map((a) => a.url) : null,
    comments: td.comments || null,
    parent_info: td.parent || null,
    child_info: td.children || null,
    linked_info: td.linked_issues || null,
    bounce_history: td.bounce_history || null,
  })

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

  const handleGenerate = async () => {
    setBusy('generate')
    setError(null)
    setTestPlan(null)
    setBugAnalysis(null)
    setCollapsed(false)
    try {
      const td = await fetchTicketDetail()
      const res = await fetch(`${API_BASE_URL}/generate-test-plan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildTestPlanPayload(td)),
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

  const jiraUrl = getJiraTicketUrl(child.key)
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
            <Btn variant="ghost" size="sm" icon="beaker" onClick={handleGenerate} disabled={busy !== null} loading={busy === 'generate'}>
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
            <TestPlanDisplay
              testPlan={testPlan}
              ticketData={{ key: child.key, summary: child.summary }}
            />
          )}
          {bugAnalysis && <BugAnalysisDisplay analysis={bugAnalysis} />}
        </div>
      )}
    </div>
  )
}

export default EpicChildRow
