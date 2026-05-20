/**
 * Display generated test plan with export options.
 * Supports both single-ticket and multi-ticket posting.
 */

import { useState, useRef, useEffect, useMemo } from 'react'
import { formatTestPlanAsMarkdown, formatTestPlanAsJira } from '../utils/markdown'
import { API_BASE_URL } from '../config'

const API_BASE = API_BASE_URL

const PROGRESS_STORAGE_PREFIX = 'testplan-progress:'

const SECTION_KEYS = ['happy_path', 'edge_cases', 'integration_tests', 'regression_checklist']

function sectionLength(testPlan, key) {
  return Array.isArray(testPlan?.[key]) ? testPlan[key].length : 0
}

function buildStorageKey(testPlan, ticketKeys) {
  if (!ticketKeys || ticketKeys.length === 0) return null
  const fingerprint = SECTION_KEYS.map((k) => sectionLength(testPlan, k)).join('-')
  return `${PROGRESS_STORAGE_PREFIX}${ticketKeys.join('+')}:${fingerprint}`
}

function SectionProgress({ checked, total }) {
  if (total === 0) return null
  const pct = total === 0 ? 0 : (checked / total) * 100
  // Interpolate hue from red (0) → yellow (60) → green (120) as pct rises.
  // Saturation a touch lower so the bar doesn't scream at the user.
  const hue = Math.round((pct / 100) * 120)
  const fillColor = `hsl(${hue}, 70%, 45%)`
  return (
    <span className="section-progress">
      <span className="section-progress-bar">
        <span
          className="section-progress-fill"
          style={{ width: `${pct}%`, background: fillColor }}
        />
      </span>
      <span className="section-progress-count">
        {checked}/{total}
      </span>
    </span>
  )
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Render a string with backtick-delimited segments as inline <code> spans.
 * The LLM prompt produces backticks around LogQL queries, field names, and
 * configuration values; rendering them as <code> makes queries copyable and
 * lets long unbreakable tokens wrap inside the code span instead of pushing
 * the panel out of the frame.
 *
 * Unbalanced backticks fall through as literal text — the regex only matches
 * fully closed pairs.
 */
function renderInline(value) {
  if (typeof value !== 'string') return JSON.stringify(value)
  if (!value.includes('`')) return value
  const parts = []
  const re = /`([^`]+)`/g
  let cursor = 0
  let match
  let key = 0
  while ((match = re.exec(value)) !== null) {
    if (match.index > cursor) parts.push(value.slice(cursor, match.index))
    parts.push(
      <code key={key++} className="test-code">
        {match[1]}
      </code>
    )
    cursor = match.index + match[0].length
  }
  if (cursor < value.length) parts.push(value.slice(cursor))
  return parts
}

function renderTestCase(test, index, opts = {}) {
  const { showCategory = false, checked = false, onToggle, checkboxId } = opts
  const acIds = Array.isArray(test.covers_acs)
    ? test.covers_acs.filter((id) => typeof id === 'string' && id.trim())
    : []
  return (
    <div key={index} className={`test-case${checked ? ' test-case--checked' : ''}`}>
      <h5>
        {onToggle && (
          <input
            type="checkbox"
            className="test-case-checkbox"
            checked={checked}
            onChange={onToggle}
            id={checkboxId}
            aria-label="Mark test case complete"
          />
        )}
        <label htmlFor={checkboxId} className="test-case-title">
          {typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}
        </label>
        {test.priority && (
          <span className={`priority-badge priority-${test.priority}`}>
            {test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'}{' '}
            {test.priority}
          </span>
        )}
        {showCategory && test.category && (
          <span className="category-badge">{test.category}</span>
        )}
        {test.needs_manual_verification && (
          <span
            className="needs-verification-badge"
            title="The AC element referenced in this test could not be verified in the PR diff or testID reference. See the UI Grounding Warnings panel above for details."
          >
            ⚠️ needs manual verification
          </span>
        )}
      </h5>
      {test.preconditions && (
        <div className="test-preconditions">
          <strong>Preconditions:</strong> {renderInline(test.preconditions)}
        </div>
      )}
      {test.steps && Array.isArray(test.steps) && test.steps.length > 0 && (
        <div className="test-steps">
          <strong>Steps:</strong>
          <ol>
            {test.steps.map((step, i) => (
              <li key={i}>{renderInline(step)}</li>
            ))}
          </ol>
        </div>
      )}
      {test.expected && (
        <div className="test-expected">
          <strong>Expected:</strong> {renderInline(test.expected)}
        </div>
      )}
      {test.test_data && (
        <div className="test-data">
          <strong>Test Data:</strong> {renderInline(test.test_data)}
        </div>
      )}
      {acIds.length > 0 && (
        <div className="test-covers-acs">
          <strong>Covers:</strong>
          {acIds.map((id) => (
            <span key={id} className="ac-tag" data-ac-id={id}>
              {id}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * Coverage summary for multi-ticket plans. Shows X/Y per ticket and lists any
 * uncovered AC IDs so the user can spot gaps before posting.
 */
function AcCoveragePanel({ coverage }) {
  if (!coverage || !coverage.tickets) return null
  // Keep tickets where there's *something* to show: real ACs, or a supersede
  // that needs explaining (a ticket whose only AC was overridden still has 0
  // total but should still appear so the user sees why it dropped off).
  const entries = Object.entries(coverage.tickets).filter(
    ([, info]) => info && (info.total > 0 || (info.superseded?.length ?? 0) > 0)
  )
  if (entries.length === 0) return null

  const uncoveredTotal = coverage.uncovered_total ?? 0
  const invalidIds = Array.isArray(coverage.invalid_ids) ? coverage.invalid_ids : []
  const superseded = Array.isArray(coverage.superseded_acs) ? coverage.superseded_acs : []
  const status = uncoveredTotal === 0 && invalidIds.length === 0 ? 'complete' : 'gaps'

  return (
    <div className={`ac-coverage-panel ac-coverage-panel--${status}`}>
      <div className="ac-coverage-header">
        <strong>Acceptance criteria coverage</strong>
        <span className="ac-coverage-summary">
          {uncoveredTotal === 0
            ? '✅ All ACs covered'
            : `⚠️ ${uncoveredTotal} AC${uncoveredTotal === 1 ? '' : 's'} uncovered`}
        </span>
      </div>
      {invalidIds.length > 0 && (
        <div
          className="ac-coverage-invalid"
          title="The LLM tagged these IDs but they don't exist in any ticket. They were dropped from the test cases."
        >
          <strong>⚠️ Model invented {invalidIds.length} unknown AC ID
            {invalidIds.length === 1 ? '' : 's'}:</strong>{' '}
          {invalidIds.map((id) => (
            <span key={id} className="ac-tag ac-tag--invalid">{id}</span>
          ))}
        </div>
      )}
      {superseded.length > 0 && (
        <div
          className="ac-coverage-superseded"
          title="These ACs from older tickets were overridden by a newer ticket's AC about the same behaviour. The newer one is what the test plan verifies."
        >
          <strong>🔁 Newer ticket overrides {superseded.length} older AC
            {superseded.length === 1 ? '' : 's'}:</strong>
          <ul className="ac-coverage-superseded-list">
            {superseded.map((s) => (
              <li key={s.loser_id}>
                <span className="ac-tag ac-tag--superseded">{s.loser_id}</span>
                <span className="ac-coverage-supersede-arrow">→</span>
                <span className="ac-tag">{s.winner_id}</span>
                {s.reason && (
                  <span className="ac-coverage-supersede-reason" title={s.reason}>
                    {s.reason}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      <ul className="ac-coverage-tickets">
        {entries.map(([key, info]) => {
          const covered = info.covered?.length ?? 0
          const total = info.total ?? 0
          const allCovered = covered === total
          return (
            <li key={key} className="ac-coverage-ticket">
              <span className="ac-coverage-ticket-key">{key}</span>
              <span
                className={`ac-coverage-ratio ${allCovered ? 'ac-coverage-ratio--ok' : 'ac-coverage-ratio--gap'}`}
              >
                {covered}/{total} {allCovered ? '✅' : '⚠️'}
              </span>
              {info.uncovered && info.uncovered.length > 0 && (
                <ul className="ac-coverage-uncovered">
                  {info.uncovered.map((u) => (
                    <li key={u.id}>
                      <span className="ac-tag ac-tag--uncovered">{u.id}</span>
                      <span
                        className="ac-coverage-uncovered-text"
                        title={u.text}
                      >
                        {u.text}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function GroundingWarningsPanel({ warnings }) {
  if (!Array.isArray(warnings) || warnings.length === 0) return null
  return (
    <div
      className="grounding-warnings-panel"
      title="The model wrote tests for these UI elements but couldn't verify they exist in the PR diff or testID reference. Confirm before running the tests."
    >
      <strong>
        🔍 {warnings.length} UI element{warnings.length === 1 ? '' : 's'} not
        found in code — verify before testing:
      </strong>
      <ul className="grounding-warnings-list">
        {warnings.map((w, idx) => (
          <li key={`${w.ac_id}-${idx}`}>
            <span className="ac-tag ac-tag--grounding">{w.ac_id}</span>
            <span className="grounding-warning-element">{w.missing_element}</span>
            <span className="grounding-warning-explanation">{w.explanation}</span>
          </li>
        ))}
      </ul>
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

  // ── Per-test checkmark state (visual only, persisted to localStorage) ──────
  // Key format: `${section}:${index}` (e.g. "happy_path:0", "regression_checklist:2")
  const ticketKeysJoined = isMulti
    ? allKeys.join('+')
    : ticketData?.key || ''
  const storageKey = useMemo(() => {
    if (!ticketKeysJoined) return null
    return buildStorageKey(testPlan, ticketKeysJoined.split('+'))
  }, [testPlan, ticketKeysJoined])
  const [checkedTests, setCheckedTests] = useState(() => {
    if (!storageKey || typeof window === 'undefined') return new Set()
    try {
      const raw = window.localStorage.getItem(storageKey)
      if (!raw) return new Set()
      const arr = JSON.parse(raw)
      return Array.isArray(arr) ? new Set(arr) : new Set()
    } catch {
      return new Set()
    }
  })

  // Reload checks when the plan identity changes (e.g. user generates a new plan).
  useEffect(() => {
    if (!storageKey || typeof window === 'undefined') {
      setCheckedTests(new Set())
      return
    }
    try {
      const raw = window.localStorage.getItem(storageKey)
      setCheckedTests(raw ? new Set(JSON.parse(raw)) : new Set())
    } catch {
      setCheckedTests(new Set())
    }
  }, [storageKey])

  // Persist on every change.
  useEffect(() => {
    if (!storageKey || typeof window === 'undefined') return
    try {
      if (checkedTests.size === 0) {
        window.localStorage.removeItem(storageKey)
      } else {
        window.localStorage.setItem(storageKey, JSON.stringify([...checkedTests]))
      }
    } catch {
      // localStorage full or disabled — silently skip; UI still works in-memory
    }
  }, [storageKey, checkedTests])

  const toggleTest = (section, index) => {
    const id = `${section}:${index}`
    setCheckedTests((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const countSectionChecks = (section, total) => {
    let n = 0
    for (let i = 0; i < total; i++) {
      if (checkedTests.has(`${section}:${i}`)) n++
    }
    return n
  }

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

  const postToKey = async (issueKey, otherKeys = []) => {
    setPostingStates((prev) => ({ ...prev, [issueKey]: 'posting' }))
    try {
      let jiraText = formatTestPlanAsJira(testPlan)
      if (otherKeys.length > 0) {
        jiraText += `\n\n----\n_Also posted to: ${otherKeys.join(', ')}_`
      }
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
      const otherKeys = keys.filter((k) => k !== key)
      await postToKey(key, otherKeys)
    }
    // Show success if no errors were encountered
    setPostingStates((prev) => {
      const anyError = keys.some((k) => prev[k] === 'error')
      if (!anyError) {
        showNotification(setPostNotification, postTimerRef, 'success', `Posted to ${keys.join(', ')}`)
      }
      return prev
    })
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

      {isMulti && testPlan.ac_coverage && (
        <AcCoveragePanel coverage={testPlan.ac_coverage} />
      )}

      <GroundingWarningsPanel warnings={testPlan.grounding_warnings} />

      {(() => {
        const totals = SECTION_KEYS.map((k) => sectionLength(testPlan, k))
        const total = totals.reduce((a, b) => a + b, 0)
        if (total === 0) return null
        const checked = SECTION_KEYS.reduce(
          (acc, k, i) => acc + countSectionChecks(k, totals[i]),
          0
        )
        return (
          <div className="overall-progress-sticky">
            <SectionProgress checked={checked} total={total} />
          </div>
        )
      })()}

      {testPlan.happy_path &&
        Array.isArray(testPlan.happy_path) &&
        testPlan.happy_path.length > 0 && (() => {
          const total = testPlan.happy_path.length
          const checked = countSectionChecks('happy_path', total)
          return (
            <div className="test-plan-group">
              <h4>
                <span>✅ Happy Path Test Cases</span>
                <SectionProgress checked={checked} total={total} />
              </h4>
              {testPlan.happy_path.map((test, i) =>
                renderTestCase(test, i, {
                  checked: checkedTests.has(`happy_path:${i}`),
                  onToggle: () => toggleTest('happy_path', i),
                  checkboxId: `tc-happy_path-${i}`,
                })
              )}
            </div>
          )
        })()}

      {testPlan.edge_cases &&
        Array.isArray(testPlan.edge_cases) &&
        testPlan.edge_cases.length > 0 && (() => {
          const total = testPlan.edge_cases.length
          const checked = countSectionChecks('edge_cases', total)
          return (
            <div className="test-plan-group">
              <h4>
                <span>🔍 Edge Cases & Error Scenarios</span>
                <SectionProgress checked={checked} total={total} />
              </h4>
              {testPlan.edge_cases.map((test, i) =>
                renderTestCase(test, i, {
                  showCategory: true,
                  checked: checkedTests.has(`edge_cases:${i}`),
                  onToggle: () => toggleTest('edge_cases', i),
                  checkboxId: `tc-edge_cases-${i}`,
                })
              )}
            </div>
          )
        })()}

      {testPlan.integration_tests &&
        Array.isArray(testPlan.integration_tests) &&
        testPlan.integration_tests.length > 0 && (() => {
          const total = testPlan.integration_tests.length
          const checked = countSectionChecks('integration_tests', total)
          return (
            <div className="test-plan-group">
              <h4>
                <span>🔗 Integration & Backend Tests</span>
                <SectionProgress checked={checked} total={total} />
              </h4>
              {testPlan.integration_tests.map((test, i) =>
                renderTestCase(test, i, {
                  checked: checkedTests.has(`integration_tests:${i}`),
                  onToggle: () => toggleTest('integration_tests', i),
                  checkboxId: `tc-integration_tests-${i}`,
                })
              )}
            </div>
          )
        })()}

      {testPlan.regression_checklist &&
        Array.isArray(testPlan.regression_checklist) &&
        testPlan.regression_checklist.length > 0 && (() => {
          const total = testPlan.regression_checklist.length
          const checked = countSectionChecks('regression_checklist', total)
          return (
            <div className="test-plan-group">
              <h4>
                <span>🔄 Regression Checklist</span>
                <SectionProgress checked={checked} total={total} />
              </h4>
              <ul className="checklist">
                {testPlan.regression_checklist.map((item, i) => {
                  const isChecked = checkedTests.has(`regression_checklist:${i}`)
                  const cbId = `tc-regression_checklist-${i}`
                  return (
                    <li
                      key={i}
                      className={`checklist-item${isChecked ? ' checklist-item--checked' : ''}`}
                    >
                      <input
                        type="checkbox"
                        className="test-case-checkbox"
                        id={cbId}
                        checked={isChecked}
                        onChange={() => toggleTest('regression_checklist', i)}
                        aria-label="Mark regression item complete"
                      />
                      <label htmlFor={cbId} className="checklist-item-text">
                        {typeof item === 'string' ? item : JSON.stringify(item)}
                      </label>
                    </li>
                  )
                })}
              </ul>
            </div>
          )
        })()}

      <div className="test-plan-actions">
        {/* ── Jira post row ── */}
        <div className="actions-row actions-row--jira">
          {isMulti ? (
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
                  : postNotification?.type === 'success'
                  ? `✓ ${postNotification.message}`
                  : postNotification?.type === 'error'
                  ? `✗ ${postNotification.message}`
                  : `Post to Selected (${selectedKeys.size})`}
              </button>
            </div>
          ) : (
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
        </div>

        {/* ── Export row ── */}
        <div className="actions-row actions-row--export">
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
    </div>
  )
}

export default TestPlanDisplay
