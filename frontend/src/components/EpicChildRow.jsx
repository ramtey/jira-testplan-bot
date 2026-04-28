/**
 * One row inside the Epic children list. Holds its own state for fetch /
 * generate / analyze and renders the result inline beneath the row.
 */

import { useEffect, useRef, useState } from 'react'
import { API_BASE_URL, getJiraTicketUrl } from '../config'
import TestPlanDisplay from './TestPlanDisplay'
import BugAnalysisDisplay from './BugAnalysisDisplay'

const NON_TESTABLE_ISSUE_TYPES = new Set(['Epic', 'Spike', 'Sub-task'])

function StatusBadge({ status, statusCategory }) {
  if (!status) return null
  const cat = (statusCategory || '').toLowerCase()
  return (
    <span className={`epic-child-status epic-child-status-${cat || 'unknown'}`}>
      {status}
    </span>
  )
}

function EpicChildRow({ child }) {
  const [busy, setBusy] = useState(null) // 'generate' | 'analyze' | null
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
    linked_info: td.linked_issues || null,
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

  return (
    <div className="epic-child-row">
      <div className="epic-child-row-header">
        <div className="epic-child-row-meta">
          {jiraUrl ? (
            <a
              href={jiraUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="epic-child-key"
            >
              {child.key}
            </a>
          ) : (
            <span className="epic-child-key">{child.key}</span>
          )}
          <span className="epic-child-type">{child.issue_type}</span>
          <StatusBadge status={child.status} statusCategory={child.status_category} />
          <span className="epic-child-summary">{child.summary}</span>
        </div>
        <div className="epic-child-row-actions">
          {isTestable && (
            <button
              type="button"
              className="btn-generate btn-small"
              onClick={handleGenerate}
              disabled={busy !== null}
            >
              {busy === 'generate' ? (
                <>
                  <span className="spinner"></span>Generating
                </>
              ) : (
                'Generate'
              )}
            </button>
          )}
          {isBug && (
            <button
              type="button"
              className="btn-generate btn-bug-lens btn-small"
              onClick={handleAnalyze}
              disabled={busy !== null}
            >
              {busy === 'analyze' ? (
                <>
                  <span className="spinner"></span>Analyzing
                </>
              ) : (
                'Analyze'
              )}
            </button>
          )}
          {hasResult && (
            <button
              type="button"
              className="btn-collapse"
              onClick={() => setCollapsed((c) => !c)}
            >
              {collapsed ? 'Show result' : 'Collapse'}
            </button>
          )}
        </div>
      </div>

      {error && (
        <div className="alert alert-error epic-child-row-error">
          <strong>Error:</strong> {error}
        </div>
      )}

      {hasResult && !collapsed && (
        <div className="epic-child-row-result" ref={resultRef}>
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
