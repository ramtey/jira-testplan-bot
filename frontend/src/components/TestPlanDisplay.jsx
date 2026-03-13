/**
 * Display generated test plan with export options.
 * Supports both single-ticket and multi-ticket posting.
 */

import { useState, useRef } from 'react'
import { formatTestPlanAsMarkdown, formatTestPlanAsJira } from '../utils/markdown'
import { API_BASE_URL } from '../config'

const API_BASE = API_BASE_URL

// ── Helpers ───────────────────────────────────────────────────────────────────

function renderTestCase(test, index, showCategory = false) {
  return (
    <div key={index} className="test-case">
      <h5>
        {typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}
        {test.priority && (
          <span className={`priority-badge priority-${test.priority}`}>
            {test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'}{' '}
            {test.priority}
          </span>
        )}
        {showCategory && test.category && (
          <span className="category-badge">{test.category}</span>
        )}
      </h5>
      {test.steps && Array.isArray(test.steps) && test.steps.length > 0 && (
        <div className="test-steps">
          <strong>Steps:</strong>
          <ol>
            {test.steps.map((step, i) => (
              <li key={i}>{typeof step === 'string' ? step : JSON.stringify(step)}</li>
            ))}
          </ol>
        </div>
      )}
      {test.expected && (
        <div className="test-expected">
          <strong>Expected:</strong>{' '}
          {typeof test.expected === 'string' ? test.expected : JSON.stringify(test.expected)}
        </div>
      )}
      {test.test_data && (
        <div className="test-data">
          <strong>Test Data:</strong>{' '}
          {typeof test.test_data === 'string' ? test.test_data : JSON.stringify(test.test_data)}
        </div>
      )}
    </div>
  )
}

// ── Component ─────────────────────────────────────────────────────────────────

function TestPlanDisplay({ testPlan, ticketData, ticketsData }) {
  const isMulti = !!(ticketsData && ticketsData.length > 1)

  // Multi-ticket: track selected tickets and per-ticket posting state
  const allKeys = isMulti ? ticketsData.map((t) => t.key) : []
  const [selectedKeys, setSelectedKeys] = useState(() => new Set(allKeys))
  const [postingStates, setPostingStates] = useState({}) // key → 'posting' | 'done' | 'error'

  // Single-ticket posting state (backward compat)
  const [isPosting, setIsPosting] = useState(false)

  // Inline notifications (replaces alert())
  const [postNotification, setPostNotification] = useState(null) // { type: 'success'|'error', message }
  const [copyNotification, setCopyNotification] = useState(null)
  const postTimerRef = useRef(null)
  const copyTimerRef = useRef(null)

  const showNotification = (setter, timerRef, type, message) => {
    if (timerRef.current) clearTimeout(timerRef.current)
    setter({ type, message })
    timerRef.current = setTimeout(() => setter(null), type === 'success' ? 3000 : 6000)
  }

  if (!testPlan) {
    return <div className="ticket-section">No test plan data available</div>
  }

  console.log('TestPlanDisplay - testPlan:', testPlan)

  // ── Export helpers ──────────────────────────────────────────────────────────

  // For markdown/download: use first available ticket context
  const primaryTicketData = ticketData || (ticketsData && ticketsData[0])

  const handleCopyMarkdown = () => {
    const markdown = formatTestPlanAsMarkdown(testPlan, primaryTicketData)
    navigator.clipboard
      .writeText(markdown)
      .then(() => showNotification(setCopyNotification, copyTimerRef, 'success', 'Copied!'))
      .catch(() => showNotification(setCopyNotification, copyTimerRef, 'error', 'Failed to copy'))
  }

  const handleDownloadMarkdown = () => {
    const markdown = formatTestPlanAsMarkdown(testPlan, primaryTicketData)
    const blob = new Blob([markdown], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const filename = isMulti
      ? `test-plan-${allKeys.join('-')}.md`
      : `test-plan-${primaryTicketData.key}.md`
    a.download = filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  // ── Single-ticket post (unchanged behaviour) ────────────────────────────────

  const handlePostToJira = async () => {
    setIsPosting(true)
    try {
      const jiraText = formatTestPlanAsJira(testPlan)
      const response = await fetch(`${API_BASE}/jira/post-comment`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          issue_key: ticketData.key,
          comment_text: jiraText,
        }),
      })

      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.detail || 'Failed to post to Jira')
      }

      const result = await response.json()
      const action = result.updated ? 'updated' : 'posted'
      showNotification(setPostNotification, postTimerRef, 'success', `Test plan ${action} on ${ticketData.key}`)
      console.log('Comment ID:', result.comment_id, 'Updated:', result.updated)
    } catch (error) {
      console.error('Error posting to Jira:', error)
      showNotification(setPostNotification, postTimerRef, 'error', error.message)
    } finally {
      setIsPosting(false)
    }
  }

  // ── Multi-ticket: post to a single key ─────────────────────────────────────

  const postToKey = async (issueKey) => {
    setPostingStates((prev) => ({ ...prev, [issueKey]: 'posting' }))
    try {
      const jiraText = formatTestPlanAsJira(testPlan)
      const response = await fetch(`${API_BASE}/jira/post-comment`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ issue_key: issueKey, comment_text: jiraText }),
      })

      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.detail || 'Failed to post to Jira')
      }

      const result = await response.json()
      const action = result.updated ? 'updated' : 'posted'
      console.log(`Comment ${action} on ${issueKey}, ID:`, result.comment_id)
      setPostingStates((prev) => ({ ...prev, [issueKey]: 'done' }))
    } catch (error) {
      console.error(`Error posting to ${issueKey}:`, error)
      setPostingStates((prev) => ({ ...prev, [issueKey]: 'error' }))
      showNotification(setPostNotification, postTimerRef, 'error', `${issueKey}: ${error.message}`)
    }
  }

  const handlePostSelected = async () => {
    const keys = [...selectedKeys]
    if (keys.length === 0) {
      showNotification(setPostNotification, postTimerRef, 'error', 'Select at least one ticket')
      return
    }
    for (const key of keys) {
      await postToKey(key)
    }
  }

  const toggleKeySelection = (key) => {
    setSelectedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) {
        next.delete(key)
      } else {
        next.add(key)
      }
      return next
    })
  }

  const isAnyPosting = Object.values(postingStates).includes('posting')

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="ticket-section test-plan-section">
      <h3>
        Generated Test Plan
        {isMulti && (
          <span className="multi-ticket-badge">
            {allKeys.join(' + ')}
          </span>
        )}
      </h3>

      {testPlan.happy_path &&
        Array.isArray(testPlan.happy_path) &&
        testPlan.happy_path.length > 0 && (
          <div className="test-plan-group">
            <h4>✅ Happy Path Test Cases</h4>
            {testPlan.happy_path.map((test, i) => renderTestCase(test, i))}
          </div>
        )}

      {testPlan.edge_cases &&
        Array.isArray(testPlan.edge_cases) &&
        testPlan.edge_cases.length > 0 && (
          <div className="test-plan-group">
            <h4>🔍 Edge Cases & Error Scenarios</h4>
            {testPlan.edge_cases.map((test, i) => renderTestCase(test, i, true))}
          </div>
        )}

      {testPlan.integration_tests &&
        Array.isArray(testPlan.integration_tests) &&
        testPlan.integration_tests.length > 0 && (
          <div className="test-plan-group">
            <h4>🔗 Integration & Backend Tests</h4>
            {testPlan.integration_tests.map((test, i) => renderTestCase(test, i))}
          </div>
        )}

      {testPlan.regression_checklist &&
        Array.isArray(testPlan.regression_checklist) &&
        testPlan.regression_checklist.length > 0 && (
          <div className="test-plan-group">
            <h4>🔄 Regression Checklist</h4>
            <ul className="checklist">
              {testPlan.regression_checklist.map((item, i) => (
                <li key={i}>{typeof item === 'string' ? item : JSON.stringify(item)}</li>
              ))}
            </ul>
          </div>
        )}

      <div className="test-plan-actions">
        {isMulti ? (
          // ── Multi-ticket posting panel ──────────────────────────────────
          <div className="multi-post-panel">
            <span className="multi-post-label">Post to Jira:</span>
            <div className="multi-post-options">
              {ticketsData.map((td) => {
                const state = postingStates[td.key]
                const checked = selectedKeys.has(td.key)
                return (
                  <label key={td.key} className="multi-post-option">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleKeySelection(td.key)}
                      disabled={isAnyPosting || state === 'done'}
                    />
                    <span className="multi-post-key">{td.key}</span>
                    {state === 'posting' && <span className="post-state posting">Posting…</span>}
                    {state === 'done' && <span className="post-state done">✅ Posted</span>}
                    {state === 'error' && <span className="post-state error">❌ Failed</span>}
                  </label>
                )
              })}
            </div>
            <button
              type="button"
              onClick={handlePostSelected}
              className={`btn-post-jira${postNotification ? ` btn-feedback--${postNotification.type}` : ''}`}
              disabled={isAnyPosting || selectedKeys.size === 0}
            >
              {isAnyPosting
                ? 'Posting…'
                : postNotification?.type === 'error'
                ? `✗ ${postNotification.message}`
                : `Post to Selected (${selectedKeys.size})`}
            </button>
          </div>
        ) : (
          // ── Single-ticket post button ────────────────────────────────────
          <button
            type="button"
            onClick={handlePostToJira}
            className={`btn-post-jira${postNotification ? ` btn-feedback--${postNotification.type}` : ''}`}
            disabled={isPosting}
          >
            {isPosting
              ? 'Posting...'
              : postNotification?.type === 'success'
              ? `✓ ${postNotification.message}`
              : postNotification?.type === 'error'
              ? `✗ ${postNotification.message}`
              : 'Post to Jira'}
          </button>
        )}

        <button
          type="button"
          onClick={handleCopyMarkdown}
          className={`btn-copy-markdown${copyNotification ? ` btn-feedback--${copyNotification.type}` : ''}`}
        >
          {copyNotification?.type === 'success'
            ? `✓ ${copyNotification.message}`
            : copyNotification?.type === 'error'
            ? `✗ ${copyNotification.message}`
            : 'Copy as Markdown'}
        </button>
        <button type="button" onClick={handleDownloadMarkdown} className="btn-download">
          Download as .md
        </button>
      </div>
    </div>
  )
}

export default TestPlanDisplay
