/**
 * Display generated test plan with export options.
 * Supports both single-ticket and multi-ticket posting.
 */

import { useState, useRef, useEffect, useMemo } from 'react'
import { formatTestPlanAsMarkdown, formatTestPlanAsJira } from '../utils/markdown'
import { API_BASE_URL } from '../config'
import Icon from './Icon'
import { Btn, Chip, ACTag, Pri, Cbx, Alert } from './ui'

const API_BASE = API_BASE_URL

const PROGRESS_STORAGE_PREFIX = 'testplan-progress:'

const SECTIONS = [
  { key: 'happy_path', label: 'Happy Path', icon: 'check-circle', renderer: 'card' },
  { key: 'edge_cases', label: 'Edge & Error', icon: 'alert', renderer: 'card', showCategory: true },
  { key: 'integration_tests', label: 'Integration & Backend', icon: 'circuit', renderer: 'card' },
  { key: 'regression_checklist', label: 'Regression Checklist', icon: 'history', renderer: 'checklist' },
]
const SECTION_KEYS = SECTIONS.map((s) => s.key)

function sectionLength(testPlan, key) {
  return Array.isArray(testPlan?.[key]) ? testPlan[key].length : 0
}

function buildStorageKey(testPlan, ticketKeys) {
  if (!ticketKeys || ticketKeys.length === 0) return null
  const fingerprint = SECTION_KEYS.map((k) => sectionLength(testPlan, k)).join('-')
  return `${PROGRESS_STORAGE_PREFIX}${ticketKeys.join('+')}:${fingerprint}`
}

/**
 * Render a string with backtick-delimited segments as inline <code> spans.
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
      <code
        key={key++}
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 11.5,
          padding: '1px 5px',
          background: 'var(--bg-input)',
          border: '1px solid var(--line)',
          borderRadius: 3,
          color: 'var(--fg)',
        }}
      >
        {match[1]}
      </code>
    )
    cursor = match.index + match[0].length
  }
  if (cursor < value.length) parts.push(value.slice(cursor))
  return parts
}

function SectionChip({ checked, total }) {
  if (total === 0) return null
  const allDone = checked === total
  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        height: 20,
        padding: '0 8px',
        borderRadius: 'var(--r-pill)',
        background: allDone ? 'rgba(34,197,94,.12)' : 'var(--bg-surface)',
        border: '1px solid',
        borderColor: allDone ? 'rgba(34,197,94,.3)' : 'var(--line)',
        color: allDone ? 'var(--success)' : 'var(--fg-muted)',
        fontSize: 'var(--t-xs)',
        fontWeight: 500,
        fontVariantNumeric: 'tabular-nums',
      }}
    >
      {allDone && <Icon name="check" size={10} />}
      {checked} / {total}
    </span>
  )
}

function TestCard({ test, section, index, checked, onToggle, showCategory }) {
  const acIds = Array.isArray(test.covers_acs)
    ? test.covers_acs.filter((id) => typeof id === 'string' && id.trim())
    : []
  const checkboxId = `tc-${section.key}-${index}`

  return (
    <div
      className="card"
      id={checkboxId}
      style={{
        borderColor: checked ? 'rgba(34,197,94,.3)' : undefined,
        transition: 'border-color var(--d-fast)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 'var(--s-5)', padding: 'var(--s-6) var(--s-6) var(--s-5)' }}>
        <span onClick={() => onToggle && onToggle()} style={{ flexShrink: 0, marginTop: 1 }}>
          <span className="cbx" data-checked={checked ? 'true' : 'false'} role="checkbox" aria-checked={checked} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center', marginBottom: 6 }}>
            {test.priority && <Pri level={test.priority} />}
            {acIds.map((id) => <ACTag key={id}>{id}</ACTag>)}
            {showCategory && test.category && (
              <span style={{ height: 18, padding: '0 6px', background: 'rgba(255,255,255,.04)', color: 'var(--fg-muted)', borderRadius: 3, fontSize: 10.5, fontWeight: 500, display: 'inline-flex', alignItems: 'center' }}>
                {test.category}
              </span>
            )}
            {test.needs_manual_verification && (
              <span
                title="The AC element referenced in this test could not be verified in the PR diff or testID reference."
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 4,
                  height: 18,
                  padding: '0 6px',
                  background: 'rgba(245,158,11,.10)',
                  border: '1px solid rgba(245,158,11,.35)',
                  color: '#fcd34d',
                  borderRadius: 3,
                  fontSize: 10.5,
                  fontWeight: 500,
                }}
              >
                <Icon name="scan" size={10} />
                Ungrounded UI ref
              </span>
            )}
            {test.cross_project && (
              <span
                title={
                  test.seam?.verified === false
                    ? 'Cross-project seam (unverified — one side not visible in the diffs).'
                    : 'Cross-project seam: this test exercises behaviour spanning multiple repositories.'
                }
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 4,
                  height: 18,
                  padding: '0 6px',
                  background:
                    test.seam?.verified === false
                      ? 'rgba(245,158,11,.10)'
                      : 'rgba(99,102,241,.12)',
                  border:
                    '1px solid ' +
                    (test.seam?.verified === false
                      ? 'rgba(245,158,11,.35)'
                      : 'rgba(99,102,241,.35)'),
                  color: test.seam?.verified === false ? '#fcd34d' : '#a5b4fc',
                  borderRadius: 3,
                  fontSize: 10.5,
                  fontWeight: 500,
                }}
              >
                <Icon name="circuit" size={10} />
                Cross-project
              </span>
            )}
          </div>
          {test.cross_project && test.seam && (
            <div
              style={{
                fontSize: 11,
                color: 'var(--fg-muted)',
                fontFamily: 'var(--font-mono)',
                marginTop: 2,
                marginBottom: 4,
              }}
            >
              {test.seam.producer_repo || '?'} → {test.seam.consumer_repo || '?'}
              {test.seam.identifier && (
                <>
                  {' · '}
                  <code
                    style={{
                      fontSize: 11,
                      padding: '0 4px',
                      background: 'var(--bg-input)',
                      border: '1px solid var(--line)',
                      borderRadius: 3,
                    }}
                  >
                    {test.seam.identifier}
                  </code>
                </>
              )}
            </div>
          )}
          <label
            htmlFor={checkboxId}
            style={{
              fontSize: 'var(--t-md)',
              fontWeight: 600,
              color: checked ? 'var(--fg-muted)' : 'var(--fg-strong)',
              textDecoration: checked ? 'line-through' : 'none',
              textDecorationColor: 'var(--fg-faint)',
              cursor: 'pointer',
              display: 'block',
            }}
          >
            {typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}
          </label>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '140px 1fr', gap: '6px var(--s-6)', padding: '0 var(--s-6) var(--s-6)', alignItems: 'start' }}>
        {test.preconditions && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Preconditions</span>
            <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>{renderInline(test.preconditions)}</div>
          </>
        )}
        {test.steps && Array.isArray(test.steps) && test.steps.length > 0 && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Steps</span>
            <ol style={{ margin: 0, paddingLeft: 18, fontSize: 'var(--t-sm)', lineHeight: '20px', color: 'var(--fg)' }}>
              {test.steps.map((s, i) => <li key={i}>{renderInline(s)}</li>)}
            </ol>
          </>
        )}
        {test.expected && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Expected</span>
            <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>{renderInline(test.expected)}</div>
          </>
        )}
        {test.test_data && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Test data</span>
            <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>{renderInline(test.test_data)}</div>
          </>
        )}
      </div>
    </div>
  )
}

function ChecklistSection({ section, items, checkedTests, onToggle }) {
  const c = items.reduce((acc, _, i) => acc + (checkedTests.has(`${section.key}:${i}`) ? 1 : 0), 0)
  return (
    <section id={`sect-${section.key}`} style={{ marginTop: 'var(--s-8)' }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: 'var(--s-4)' }}>
        <Icon name={section.icon} size={16} style={{ color: 'var(--accent)' }} />
        <h2 style={{ margin: 0, fontSize: 'var(--t-lg)', fontWeight: 600, letterSpacing: '-.005em', color: 'var(--fg-strong)' }}>{section.label}</h2>
        <SectionChip checked={c} total={items.length} />
        <span style={{ flex: 1 }} />
        <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>Plain checklist · no metadata</span>
      </header>
      <div className="card" style={{ padding: 'var(--s-5) var(--s-6)' }}>
        {items.map((item, i) => {
          const id = `${section.key}:${i}`
          const isChecked = checkedTests.has(id)
          return (
            <div key={i} style={{ display: 'flex', gap: 'var(--s-4)', padding: '6px 0', borderBottom: i < items.length - 1 ? '1px solid var(--divider)' : 'none', alignItems: 'center' }}>
              <Cbx checked={isChecked} onChange={() => onToggle(section.key, i)} />
              <span
                style={{
                  flex: 1,
                  fontSize: 'var(--t-sm)',
                  color: isChecked ? 'var(--fg-muted)' : 'var(--fg)',
                  textDecoration: isChecked ? 'line-through' : 'none',
                  textDecorationColor: 'var(--fg-faint)',
                }}
              >
                {typeof item === 'string' ? item : JSON.stringify(item)}
              </span>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function CardSection({ section, items, checkedTests, onToggle }) {
  const c = items.reduce((acc, _, i) => acc + (checkedTests.has(`${section.key}:${i}`) ? 1 : 0), 0)
  return (
    <section id={`sect-${section.key}`} style={{ marginTop: 'var(--s-8)' }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: 'var(--s-4)' }}>
        <Icon name={section.icon} size={16} style={{ color: 'var(--accent)' }} />
        <h2 style={{ margin: 0, fontSize: 'var(--t-lg)', fontWeight: 600, letterSpacing: '-.005em', color: 'var(--fg-strong)' }}>{section.label}</h2>
        <SectionChip checked={c} total={items.length} />
      </header>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
        {items.map((test, i) => (
          <TestCard
            key={i}
            test={test}
            section={section}
            index={i}
            checked={checkedTests.has(`${section.key}:${i}`)}
            onToggle={() => onToggle(section.key, i)}
            showCategory={section.showCategory}
          />
        ))}
      </div>
    </section>
  )
}

function AcCoveragePanel({ coverage }) {
  if (!coverage || !coverage.tickets) return null
  const entries = Object.entries(coverage.tickets).filter(
    ([, info]) => info && (info.total > 0 || (info.superseded?.length ?? 0) > 0)
  )
  if (entries.length === 0) return null

  const uncoveredTotal = coverage.uncovered_total ?? 0
  const invalidIds = Array.isArray(coverage.invalid_ids) ? coverage.invalid_ids : []
  const superseded = Array.isArray(coverage.superseded_acs) ? coverage.superseded_acs : []

  return (
    <div className="card" style={{ padding: 'var(--s-5) var(--s-6)', marginBottom: 'var(--s-5)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: 'var(--s-4)' }}>
        <Icon name="shield" size={14} style={{ color: 'var(--accent)' }} />
        <span style={{ fontSize: 'var(--t-md)', fontWeight: 600, color: 'var(--fg-strong)' }}>Acceptance criteria coverage</span>
        <span style={{ flex: 1 }} />
        {uncoveredTotal === 0 ? (
          <Chip dot dotColor="var(--success)">All ACs covered</Chip>
        ) : (
          <Chip dot dotColor="var(--warning)">{uncoveredTotal} AC{uncoveredTotal === 1 ? '' : 's'} uncovered</Chip>
        )}
      </div>

      {invalidIds.length > 0 && (
        <div style={{ marginBottom: 'var(--s-4)' }}>
          <Alert tone="danger" title={`Model invented ${invalidIds.length} unknown AC ID${invalidIds.length === 1 ? '' : 's'}`}>
            {invalidIds.map((id) => <span key={id} style={{ display: 'inline-block', marginRight: 6 }}><ACTag>{id}</ACTag></span>)}
            <div style={{ marginTop: 4, fontSize: 'var(--t-xs)' }}>These were dropped from the test cases.</div>
          </Alert>
        </div>
      )}

      {superseded.length > 0 && (
        <div style={{ marginBottom: 'var(--s-4)' }}>
          <Alert tone="info" title={`Newer ticket overrides ${superseded.length} older AC${superseded.length === 1 ? '' : 's'}`}>
            <ul style={{ margin: 4, paddingLeft: 18 }}>
              {superseded.map((s) => (
                <li key={s.loser_id} style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                  <ACTag>{s.loser_id}</ACTag>
                  <Icon name="arrow-right" size={11} style={{ color: 'var(--fg-faint)' }} />
                  <ACTag>{s.winner_id}</ACTag>
                  {s.reason && <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>{s.reason}</span>}
                </li>
              ))}
            </ul>
          </Alert>
        </div>
      )}

      <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
        {entries.map(([key, info]) => {
          const covered = info.covered?.length ?? 0
          const total = info.total ?? 0
          const allCovered = covered === total
          return (
            <li key={key}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)' }}>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-sm)', color: 'var(--accent)', minWidth: 80 }}>{key}</span>
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-xs)', color: allCovered ? 'var(--success)' : 'var(--warning)' }}>
                  {covered}/{total}
                </span>
              </div>
              {info.uncovered && info.uncovered.length > 0 && (
                <ul style={{ margin: '6px 0 0 88px', padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {info.uncovered.map((u) => (
                    <li key={u.id} style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)' }}>
                      <ACTag>{u.id}</ACTag>
                      <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-muted)' }} title={u.text}>{u.text}</span>
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
    <div style={{ marginBottom: 'var(--s-5)' }}>
      <Alert tone="warning" title={`${warnings.length} UI element${warnings.length === 1 ? '' : 's'} not found in code — verify before testing`}>
        <ul style={{ margin: 4, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {warnings.map((w, idx) => (
            <li key={`${w.ac_id}-${idx}`} style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', flexWrap: 'wrap' }}>
              <ACTag>{w.ac_id}</ACTag>
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-xs)', color: 'var(--fg)' }}>{w.missing_element}</span>
              <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-muted)' }}>{w.explanation}</span>
            </li>
          ))}
        </ul>
      </Alert>
    </div>
  )
}

function TestPlanDisplay({ testPlan, ticketData, ticketsData }) {
  const isMulti = !!(ticketsData && ticketsData.length > 1)

  const allKeys = isMulti ? ticketsData.map((t) => t.key) : []
  const [selectedKeys, setSelectedKeys] = useState(() => new Set(allKeys))
  const [postingStates, setPostingStates] = useState({})

  const [isPosting, setIsPosting] = useState(false)

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

  useEffect(() => {
    if (!storageKey || typeof window === 'undefined') return
    try {
      if (checkedTests.size === 0) {
        window.localStorage.removeItem(storageKey)
      } else {
        window.localStorage.setItem(storageKey, JSON.stringify([...checkedTests]))
      }
    } catch {
      // localStorage full or disabled — silently skip
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

  const [postNotification, setPostNotification] = useState(null)
  const [copyNotification, setCopyNotification] = useState(null)
  const postTimerRef = useRef(null)
  const copyTimerRef = useRef(null)

  const showNotification = (setter, timerRef, type, message) => {
    if (timerRef.current) clearTimeout(timerRef.current)
    setter({ type, message })
    timerRef.current = setTimeout(() => setter(null), type === 'success' ? 3000 : 6000)
  }

  if (!testPlan) return null

  const primaryTicketData = ticketData || (ticketsData && ticketsData[0])

  const handleCopyMarkdown = () => {
    const markdown = formatTestPlanAsMarkdown(testPlan, primaryTicketData)
    navigator.clipboard
      .writeText(markdown)
      .then(() => showNotification(setCopyNotification, copyTimerRef, 'success', 'Copied'))
      .catch(() => showNotification(setCopyNotification, copyTimerRef, 'error', 'Failed to copy'))
  }

  const handleDownloadMarkdown = () => {
    const markdown = formatTestPlanAsMarkdown(testPlan, primaryTicketData)
    const blob = new Blob([markdown], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = isMulti
      ? `test-plan-${allKeys.join('-')}.md`
      : `test-plan-${primaryTicketData.key}.md`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

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
    } catch (error) {
      showNotification(setPostNotification, postTimerRef, 'error', error.message)
    } finally {
      setIsPosting(false)
    }
  }

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

      setPostingStates((prev) => ({ ...prev, [issueKey]: 'done' }))
    } catch (error) {
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
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const isAnyPosting = Object.values(postingStates).includes('posting')

  // Overall progress
  const totals = SECTION_KEYS.map((k) => sectionLength(testPlan, k))
  const totalAll = totals.reduce((a, b) => a + b, 0)
  const checkedAll = SECTION_KEYS.reduce(
    (acc, k, i) => acc + countSectionChecks(k, totals[i]),
    0
  )
  const pctAll = totalAll === 0 ? 0 : Math.round((checkedAll / totalAll) * 100)

  return (
    <div style={{ marginTop: 'var(--s-7)' }}>
      {/* Sticky progress */}
      {totalAll > 0 && (
        <div
          style={{
            position: 'sticky',
            top: 0,
            zIndex: 8,
            background: 'rgba(8,9,11,.85)',
            backdropFilter: 'blur(10px)',
            WebkitBackdropFilter: 'blur(10px)',
            borderBottom: '1px solid var(--line)',
            margin: '0 calc(-1 * var(--s-8))',
            padding: '10px var(--s-8)',
            marginBottom: 'var(--s-5)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-5)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)' }}>
              <Icon name="beaker" size={14} style={{ color: 'var(--accent)' }} />
              <span style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)' }}>
                Test plan{isMulti ? '' : ` · ${ticketData?.key || ''}`}
              </span>
              {isMulti && (
                <Chip size="sm">{allKeys.join(' + ')}</Chip>
              )}
            </div>
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 'var(--s-4)' }}>
              <div style={{ display: 'flex', gap: 2, flex: 1, height: 6, borderRadius: 999, overflow: 'hidden', background: 'var(--bg-input)' }}>
                {SECTIONS.map((s, i) => {
                  const t = totals[i]
                  if (t === 0) return null
                  const c = countSectionChecks(s.key, t)
                  const segPct = (c / t) * 100
                  const widthPct = (t / totalAll) * 100
                  return (
                    <div key={s.key} style={{ width: widthPct + '%', height: '100%', background: 'var(--bg-input)' }} title={`${s.label} ${c}/${t}`}>
                      <div style={{ width: segPct + '%', height: '100%', background: 'var(--accent)', transition: 'width var(--d-base) var(--ease-out)' }} />
                    </div>
                  )
                })}
              </div>
              <span className="tnum" style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)', minWidth: 56, textAlign: 'right' }}>
                {checkedAll} / {totalAll}
              </span>
              <span className="tnum" style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-subtle)', minWidth: 32, textAlign: 'right' }}>{pctAll}%</span>
            </div>
            <div style={{ display: 'flex', gap: 6 }}>
              {SECTIONS.map((s, i) => {
                const t = totals[i]
                if (t === 0) return null
                const c = countSectionChecks(s.key, t)
                const ok = c === t
                return (
                  <a
                    key={s.key}
                    href={`#sect-${s.key}`}
                    style={{
                      display: 'inline-flex',
                      alignItems: 'center',
                      gap: 4,
                      padding: '2px 8px',
                      borderRadius: 'var(--r-pill)',
                      background: ok ? 'rgba(34,197,94,.12)' : 'var(--bg-surface)',
                      border: '1px solid',
                      borderColor: ok ? 'rgba(34,197,94,.3)' : 'var(--line)',
                      color: ok ? 'var(--success)' : 'var(--fg-muted)',
                      fontSize: 'var(--t-xs)',
                      fontWeight: 500,
                      textDecoration: 'none',
                    }}
                  >
                    <span className="tnum">{c}/{t}</span>
                    <span>{s.label}</span>
                  </a>
                )
              })}
            </div>
          </div>
        </div>
      )}

      {/* Plan banner */}
      <div className="card" style={{ padding: 'var(--s-5) var(--s-6)', marginBottom: 'var(--s-6)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-4)' }}>
          <div style={{ width: 32, height: 32, borderRadius: 'var(--r-md)', background: 'rgba(59,130,246,.12)', display: 'grid', placeItems: 'center', flexShrink: 0 }}>
            <Icon name="beaker" size={16} style={{ color: 'var(--accent)' }} />
          </div>
          <div style={{ flex: 1 }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--s-3)' }}>
              <span style={{ fontSize: 'var(--t-md)', fontWeight: 600, color: 'var(--fg-strong)' }}>Generated test plan</span>
              {isMulti && <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>· {allKeys.join(' + ')}</span>}
            </div>
            <div style={{ marginTop: 2, fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
              {totalAll} test cases
            </div>
          </div>
        </div>
      </div>

      {isMulti && testPlan.ac_coverage && (
        <AcCoveragePanel coverage={testPlan.ac_coverage} />
      )}

      <GroundingWarningsPanel warnings={testPlan.grounding_warnings} />

      {SECTIONS.map((section) => {
        const items = testPlan[section.key]
        if (!Array.isArray(items) || items.length === 0) return null
        if (section.renderer === 'checklist') {
          return (
            <ChecklistSection
              key={section.key}
              section={section}
              items={items}
              checkedTests={checkedTests}
              onToggle={toggleTest}
            />
          )
        }
        return (
          <CardSection
            key={section.key}
            section={section}
            items={items}
            checkedTests={checkedTests}
            onToggle={toggleTest}
          />
        )
      })}

      {/* Export & post bar */}
      <div className="card" style={{ marginTop: 'var(--s-9)', padding: 'var(--s-6)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-5)', flexWrap: 'wrap' }}>
          <Icon name="upload" size={14} style={{ color: 'var(--fg-muted)' }} />
          <div style={{ flex: 1, minWidth: 200 }}>
            <div style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)' }}>Export this plan</div>
            <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
              Posting to Jira updates the existing bot comment instead of duplicating.
            </div>
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <Btn variant="ghost" icon="copy" onClick={handleCopyMarkdown}>
              {copyNotification?.type === 'success' ? `✓ ${copyNotification.message}` : 'Copy markdown'}
            </Btn>
            <Btn variant="ghost" icon="download" onClick={handleDownloadMarkdown}>
              Download .md
            </Btn>
            <span style={{ width: 1, height: 18, background: 'var(--line-strong)', alignSelf: 'center', margin: '0 4px' }} />
            {!isMulti && (
              <Btn
                variant="primary"
                icon="send"
                onClick={handlePostToJira}
                disabled={isPosting}
                loading={isPosting}
              >
                {postNotification?.type === 'success'
                  ? `✓ ${postNotification.message}`
                  : isPosting
                  ? 'Posting…'
                  : 'Post to Jira'}
              </Btn>
            )}
          </div>
        </div>

        {isMulti && (
          <div style={{ marginTop: 'var(--s-5)', paddingTop: 'var(--s-5)', borderTop: '1px solid var(--divider)' }}>
            <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '.04em', fontWeight: 600, marginBottom: 'var(--s-3)' }}>
              Post to selected tickets
            </div>
            <div style={{ display: 'flex', gap: 'var(--s-4)', flexWrap: 'wrap', alignItems: 'center' }}>
              {ticketsData.map((td) => {
                const state = postingStates[td.key]
                const checked = selectedKeys.has(td.key)
                return (
                  <label key={td.key} style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--s-3)', cursor: 'pointer' }}>
                    <span
                      className="cbx"
                      data-checked={checked ? 'true' : 'false'}
                      role="checkbox"
                      aria-checked={checked}
                      onClick={() => !isAnyPosting && state !== 'done' && toggleKeySelection(td.key)}
                    />
                    <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>{td.key}</span>
                    {state === 'posting' && <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>Posting…</span>}
                    {state === 'done' && <Chip size="sm" dot dotColor="var(--success)">Posted</Chip>}
                    {state === 'error' && <Chip size="sm" dot dotColor="var(--danger)">Failed</Chip>}
                  </label>
                )
              })}
              <span style={{ flex: 1 }} />
              <Btn
                variant="primary"
                icon="send"
                onClick={handlePostSelected}
                disabled={isAnyPosting || selectedKeys.size === 0}
                loading={isAnyPosting}
              >
                {isAnyPosting ? 'Posting…' : `Post to selected (${selectedKeys.size})`}
              </Btn>
            </div>
          </div>
        )}

        {postNotification && postNotification.type === 'error' && (
          <div style={{ marginTop: 'var(--s-4)' }}>
            <Alert tone="danger" title="Post failed">{postNotification.message}</Alert>
          </div>
        )}
      </div>
    </div>
  )
}

export default TestPlanDisplay
