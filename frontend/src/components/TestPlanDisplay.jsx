/**
 * Display generated test plan with export options.
 * Supports both single-ticket and multi-ticket posting.
 */

import { useState, useRef, useEffect, useMemo } from 'react'
import { formatTestPlanAsMarkdown, formatTestPlanAsJira } from '../utils/markdown'
import { extractPrMedia } from '../utils/prMedia'
import {
  API_BASE_URL,
  isWalkthroughCardCtaEnabled,
  OPEN_PASS_TO_UAT_EVENT,
} from '../config'
import { useTicketWalkthrough } from '../hooks/useTicketWalkthrough'
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
// Card sections whose cases the planner can flag as already covered by an
// existing unit test. Those cases are pulled into a separate collapsed list.
const COVERABLE_KEYS = ['happy_path', 'edge_cases', 'integration_tests']

function sectionLength(testPlan, key) {
  return Array.isArray(testPlan?.[key]) ? testPlan[key].length : 0
}

function formatRelativeTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const mins = Math.round((Date.now() - d.getTime()) / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d ago`
  return d.toLocaleDateString()
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

function TestCard({ test, section, index, checked, onToggle, showCategory, planHasAcs }) {
  const acIds = Array.isArray(test.covers_acs)
    ? test.covers_acs.filter((id) => typeof id === 'string' && id.trim())
    : []
  const groundedIn = Array.isArray(test.grounded_in)
    ? test.grounded_in.filter((s) => typeof s === 'string' && s.trim())
    : []
  const isUntraced = planHasAcs && acIds.length === 0 && groundedIn.length === 0
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
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 'var(--s-5)', padding: checked ? 'var(--s-5) var(--s-6)' : 'var(--s-6) var(--s-6) var(--s-5)' }}>
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
                title="This test isn't fully grounded in the AC text or PR diff — either the referenced UI element wasn't in the diff/testID reference, or the cited AC doesn't describe the behaviour being tested. Verify the AC actually requires this before running."
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
            {isUntraced && (
              <span
                title="No AC coverage and no grounded_in source. Verify any specific numbers, strings, or symbols in this test before running it."
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 4,
                  height: 18,
                  padding: '0 6px',
                  background: 'rgba(239,68,68,.10)',
                  border: '1px solid rgba(239,68,68,.35)',
                  color: '#fca5a5',
                  borderRadius: 3,
                  fontSize: 10.5,
                  fontWeight: 500,
                }}
              >
                <Icon name="scan" size={10} />
                Untraced
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

      {!checked && (
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
        {groundedIn.length > 0 && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Grounded in</span>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
              {groundedIn.map((src, i) => (
                <code
                  key={i}
                  style={{
                    fontSize: 10.5,
                    padding: '1px 6px',
                    background: 'var(--bg-input)',
                    border: '1px solid var(--line)',
                    borderRadius: 3,
                    color: 'var(--fg-muted)',
                    fontFamily: 'var(--font-mono)',
                  }}
                >
                  {src}
                </code>
              ))}
            </div>
          </>
        )}
      </div>
      )}
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

function CardSection({ section, items, checkedTests, onToggle, planHasAcs }) {
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
            planHasAcs={planHasAcs}
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
  const underCoveredTotal = coverage.under_covered_total ?? 0
  const invalidIds = Array.isArray(coverage.invalid_ids) ? coverage.invalid_ids : []
  const superseded = Array.isArray(coverage.superseded_acs) ? coverage.superseded_acs : []

  return (
    <div className="card" style={{ padding: 'var(--s-5) var(--s-6)', marginBottom: 'var(--s-5)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: 'var(--s-4)' }}>
        <Icon name="shield" size={14} style={{ color: 'var(--accent)' }} />
        <span style={{ fontSize: 'var(--t-md)', fontWeight: 600, color: 'var(--fg-strong)' }}>Acceptance criteria coverage</span>
        <span style={{ flex: 1 }} />
        {uncoveredTotal === 0 && underCoveredTotal === 0 ? (
          <Chip dot dotColor="var(--success)">All ACs covered</Chip>
        ) : (
          <>
            {uncoveredTotal > 0 && (
              <Chip dot dotColor="var(--warning)">{uncoveredTotal} AC{uncoveredTotal === 1 ? '' : 's'} uncovered</Chip>
            )}
            {underCoveredTotal > 0 && (
              <Chip dot dotColor="var(--warning)">{underCoveredTotal} AC{underCoveredTotal === 1 ? '' : 's'} partially covered</Chip>
            )}
          </>
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
              {info.under_covered && info.under_covered.length > 0 && (
                <ul style={{ margin: '6px 0 0 88px', padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {info.under_covered.map((u) => (
                    <li key={u.id} style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', flexWrap: 'wrap' }}>
                      <ACTag>{u.id}</ACTag>
                      <span style={{ fontSize: 'var(--t-xs)', color: 'var(--warning)' }}>
                        missing: {(u.missing_actions || []).join(', ')}
                      </span>
                      <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-muted)' }} title={u.text}>
                        — enumerates {(u.actions || []).join(' · ')}
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

// The backend emits two flavours of warning in this list:
//   - severity "warn" (default): the test asserts behaviour we can't
//     confirm — either the AC text doesn't cover it OR the code doesn't
//     appear to implement it. QA should verify before testing.
//   - severity "info": the AC critic flagged it as "not in the AC" but
//     a follow-up code-grounding pass found the behaviour implemented in
//     the linked repo. Kept here as an informational trail rather than
//     silently dropped, so QA can spot-check the code_evidence link.
function GroundingWarningsPanel({ warnings }) {
  if (!Array.isArray(warnings) || warnings.length === 0) return null
  const warnItems = warnings.filter((w) => (w?.severity ?? 'warn') === 'warn')
  const infoItems = warnings.filter((w) => w?.severity === 'info')
  if (warnItems.length === 0 && infoItems.length === 0) return null
  return (
    <div style={{ marginBottom: 'var(--s-5)', display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
      {warnItems.length > 0 && (
        <Alert
          tone="warning"
          title={`${warnItems.length} behaviour${warnItems.length === 1 ? '' : 's'} not confirmed in AC or code — verify before testing`}
        >
          <ul style={{ margin: 4, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {warnItems.map((w, idx) => (
              <GroundingWarningRow key={`warn-${w.ac_id}-${idx}`} warning={w} />
            ))}
          </ul>
        </Alert>
      )}
      {infoItems.length > 0 && (
        <Alert
          tone="info"
          title={`${infoItems.length} behaviour${infoItems.length === 1 ? '' : 's'} beyond the cited AC but present in code`}
        >
          <ul style={{ margin: 4, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 4 }}>
            {infoItems.map((w, idx) => (
              <GroundingWarningRow key={`info-${w.ac_id}-${idx}`} warning={w} />
            ))}
          </ul>
        </Alert>
      )}
    </div>
  )
}

function GroundingWarningRow({ warning }) {
  const files = warning?.code_evidence?.files || []
  return (
    <li style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', flexWrap: 'wrap' }}>
      <ACTag>{warning.ac_id}</ACTag>
      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-xs)', color: 'var(--fg)' }}>{warning.missing_element}</span>
      <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-muted)' }}>{warning.explanation}</span>
      {files.length > 0 && (
        <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-muted)', fontFamily: 'var(--font-mono)' }}>
          ({files.map((f) => f.path).filter(Boolean).join(', ')})
        </span>
      )}
    </li>
  )
}

// Visual treatment per UAT-complexity rating. `high` gets a prominent amber
// banner because that's the tester who'd otherwise be lost without help; lower
// ratings stay quiet so the banner keeps its signal.
const UAT_COMPLEXITY_META = {
  high: {
    label: 'Hard to UAT',
    icon: 'alert',
    accent: '#fbbf24',
    border: 'rgba(245,158,11,.40)',
    bg: 'rgba(245,158,11,.08)',
    iconBg: 'rgba(245,158,11,.14)',
    heading: 'Needs a walkthrough before testing',
  },
  medium: {
    label: 'Some setup',
    icon: 'info',
    accent: '#93c5fd',
    border: 'var(--line)',
    bg: 'var(--bg-surface)',
    iconBg: 'rgba(59,130,246,.12)',
    heading: 'How to see this',
  },
  low: {
    label: 'Easy to UAT',
    icon: 'eye',
    accent: 'var(--success)',
    border: 'var(--line)',
    bg: 'var(--bg-surface)',
    iconBg: 'rgba(34,197,94,.12)',
    heading: 'How to see this',
  },
}

function UatComplexityBadge({ complexity }) {
  const meta = UAT_COMPLEXITY_META[complexity]
  if (!meta) return null
  return (
    <span
      title={`UAT complexity: ${complexity}`}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 4,
        height: 20,
        padding: '0 8px',
        borderRadius: 'var(--r-pill)',
        background: meta.iconBg,
        border: `1px solid ${meta.border}`,
        color: meta.accent,
        fontSize: 'var(--t-xs)',
        fontWeight: 600,
      }}
    >
      <Icon name={meta.icon} size={11} />
      {meta.label}
    </span>
  )
}

const PR_MEDIA_ICON = { video: 'play', image: 'image', attachment: 'paperclip' }
const PR_MEDIA_VERB = { video: 'Watch', image: 'View', attachment: 'Open' }

/**
 * Multi-file screenshot picker for the walkthrough form. Mirrors the
 * click / drag / paste UX of the Pass-to-UAT dropzone. Chips distinguish
 * "existing" (already uploaded to the Jira ticket — click X to drop from
 * the walkthrough on the next save; the attachment itself stays on Jira)
 * from "new" (a File staged locally that uploads on save).
 */
function ScreenshotPicker({ files, existing, onAddFiles, onRemoveFile, onRemoveExisting, disabled }) {
  const [isDragging, setIsDragging] = useState(false)
  const inputRef = useRef(null)

  useEffect(() => {
    if (disabled) return
    const handler = (e) => {
      const items = e.clipboardData?.items
      if (!items) return
      const imageItems = Array.from(items).filter(
        (it) => it.type.startsWith('image/') || it.type === 'application/pdf'
      )
      if (imageItems.length === 0) return
      e.preventDefault()
      const fileList = imageItems.map((it) => it.getAsFile()).filter(Boolean)
      if (fileList.length > 0) onAddFiles(fileList)
    }
    window.addEventListener('paste', handler)
    return () => window.removeEventListener('paste', handler)
  }, [disabled, onAddFiles])

  const pickFiles = (fileList) => {
    const arr = Array.from(fileList || []).filter(
      (f) => f && (f.type.startsWith('image/') || f.type === 'application/pdf')
    )
    if (arr.length > 0) onAddFiles(arr)
  }

  const chipBase = {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    background: 'var(--bg-subtle)',
    border: '1px solid var(--border)',
    borderRadius: 999,
    padding: '4px 10px',
    fontSize: 'var(--t-xs)',
  }
  const clip = (name, limit = 34) => (name.length > limit ? name.slice(0, limit - 3) + '…' : name)
  const removeBtnStyle = {
    background: 'transparent',
    border: 'none',
    cursor: disabled ? 'not-allowed' : 'pointer',
    color: 'var(--fg-subtle)',
    padding: 0,
    display: 'inline-flex',
  }

  const hasAny = (existing?.length || 0) + (files?.length || 0) > 0

  return (
    <div>
      <div
        onClick={() => !disabled && inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault()
          if (!disabled) setIsDragging(true)
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setIsDragging(false)
          if (disabled) return
          pickFiles(e.dataTransfer.files)
        }}
        style={{
          border: '1px dashed ' + (isDragging ? 'var(--accent)' : 'var(--border)'),
          background: isDragging ? 'rgba(59,130,246,.06)' : 'transparent',
          borderRadius: 'var(--r-md)',
          padding: 'var(--s-3) var(--s-4)',
          cursor: disabled ? 'not-allowed' : 'pointer',
          fontSize: 'var(--t-sm)',
          color: 'var(--fg-subtle)',
          transition: 'background 120ms, border-color 120ms',
        }}
      >
        <Icon name="image" size={13} style={{ marginRight: 6, verticalAlign: '-2px' }} />
        Click, drag, or paste files here. PNG / JPEG / GIF / WEBP / PDF, up to 10 MB each.
        <input
          ref={inputRef}
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp,application/pdf"
          multiple
          hidden
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => {
            pickFiles(e.target.files)
            e.target.value = ''
          }}
        />
      </div>
      {hasAny && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
          {(existing || []).map((shot, i) => (
            <span
              key={`existing-${shot.url}`}
              style={chipBase}
              title={shot.filename || 'Attached screenshot'}
            >
              <Icon name="image" size={11} />
              {clip(shot.filename || 'Screenshot')}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  onRemoveExisting(i)
                }}
                disabled={disabled}
                style={removeBtnStyle}
                aria-label={`Remove ${shot.filename || 'screenshot'}`}
              >
                <Icon name="x" size={11} />
              </button>
            </span>
          ))}
          {(files || []).map((f, i) => (
            <span
              key={`new-${i}-${f.name}`}
              style={chipBase}
              title={`${f.name} · ${(f.size / 1024).toFixed(0)} KB · uploads on save`}
            >
              <Icon name="image" size={11} />
              {clip(f.name)}
              <span style={{ color: 'var(--fg-subtle)', fontStyle: 'italic' }}>· new</span>
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  onRemoveFile(i)
                }}
                disabled={disabled}
                style={removeBtnStyle}
                aria-label={`Remove ${f.name}`}
              >
                <Icon name="x" size={11} />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * Human-authored walkthrough: a Loom link, a screenshot uploaded to the Jira
 * ticket, and free-text setup/repro notes. The thing the LLM can't produce —
 * the planner records it once and it persists across regenerations. Read mode
 * shows links/notes; edit mode (controlled by the parent so the post-gate can
 * open it) shows the form.
 */
function WalkthroughSection({ walkthrough, prMedia, editing, onEditingChange, onSave, saving, accent, ticketStatus }) {
  const wt = walkthrough || {}
  const savedScreenshots = Array.isArray(wt.screenshots) ? wt.screenshots : []
  const prMediaList = Array.isArray(prMedia) ? prMedia : []
  const [loom, setLoom] = useState(wt.loom_url || '')
  const [notes, setNotes] = useState(wt.notes || '')
  const [existingScreenshots, setExistingScreenshots] = useState(savedScreenshots)
  const [newFiles, setNewFiles] = useState([])

  // Feature-flagged second entry point for the Pass-to-UAT flow: when the
  // ticket is "In Testing", surface the action here on the walkthrough card
  // itself (in addition to the button in the workflow header). Click dispatches
  // a CustomEvent that WorkflowActions listens for and opens its existing form.
  const isTesting =
    typeof ticketStatus === 'string' &&
    ticketStatus.trim().toLowerCase() === 'in testing'
  const showCardCta = isTesting && isWalkthroughCardCtaEnabled()
  const cardCta = showCardCta ? (
    <div style={{ marginTop: 'var(--s-3)' }}>
      <Btn
        variant="success-soft"
        icon="check"
        title="Pass this ticket to UAT"
        onClick={() =>
          window.dispatchEvent(new CustomEvent(OPEN_PASS_TO_UAT_EVENT))
        }
      >
        Pass to UAT
      </Btn>
    </div>
  ) : null

  // Re-seed the form whenever the saved values change or we (re)enter edit mode,
  // so opening the editor always starts from the persisted state.
  useEffect(() => {
    setLoom(wt.loom_url || '')
    setNotes(wt.notes || '')
    setExistingScreenshots(Array.isArray(wt.screenshots) ? wt.screenshots : [])
    setNewFiles([])
  }, [wt.loom_url, wt.screenshots, wt.notes, editing])

  const divider = { marginTop: 'var(--s-4)', paddingTop: 'var(--s-4)', borderTop: '1px solid var(--divider)' }

  if (editing) {
    return (
      <div style={divider}>
        <div style={{ display: 'grid', gap: 'var(--s-4)' }}>
          <div>
            <span className="lbl">Loom / video link</span>
            <input
              className="inp"
              type="url"
              placeholder="https://www.loom.com/share/…"
              value={loom}
              onChange={(e) => setLoom(e.target.value)}
              disabled={saving}
            />
          </div>
          <div>
            <span className="lbl">Screenshots</span>
            <ScreenshotPicker
              files={newFiles}
              existing={existingScreenshots}
              onAddFiles={(added) => setNewFiles((prev) => [...prev, ...added])}
              onRemoveFile={(idx) =>
                setNewFiles((prev) => prev.filter((_, i) => i !== idx))
              }
              onRemoveExisting={(idx) =>
                setExistingScreenshots((prev) => prev.filter((_, i) => i !== idx))
              }
              disabled={saving}
            />
          </div>
          <div>
            <span className="lbl">Setup / repro notes</span>
            <textarea
              className="inp"
              style={{ minHeight: 70 }}
              placeholder="Prerequisites, feature flags, accounts, or steps to reproduce…"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              disabled={saving}
            />
          </div>
        </div>
        <div style={{ display: 'flex', gap: 6, marginTop: 'var(--s-4)' }}>
          <Btn
            variant="primary"
            icon="check"
            loading={saving}
            disabled={saving}
            onClick={() =>
              onSave({
                loom_url: loom.trim(),
                notes: notes.trim(),
                existing_screenshots: existingScreenshots,
                new_files: newFiles,
              })
            }
          >
            Save walkthrough
          </Btn>
          <Btn variant="ghost" onClick={() => onEditingChange(false)} disabled={saving}>
            Cancel
          </Btn>
        </div>
      </div>
    )
  }

  const present = wt.walkthrough_present === true
  const anyPrMedia = prMediaList.length > 0
  if (!present && !anyPrMedia) {
    return (
      <div style={divider}>
        <button
          type="button"
          onClick={() => onEditingChange(true)}
          style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 'var(--t-sm)', color: accent, fontWeight: 600 }}
        >
          <Icon name="plus" size={13} /> Add a walkthrough (Loom, screenshots, or notes)
        </button>
        {cardCta}
      </div>
    )
  }

  return (
    <div style={divider}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {prMediaList.map((m) => {
          const iconName = PR_MEDIA_ICON[m.kind] || 'paperclip'
          const verb = PR_MEDIA_VERB[m.kind] || 'Open'
          const label = m.filename || `${m.kind === 'video' ? 'video' : m.kind === 'image' ? 'screenshot' : 'attachment'}`
          const prLabel = m.source?.pr_label || 'PR'
          return (
            <a
              key={m.url}
              href={m.url}
              target="_blank"
              rel="noopener noreferrer"
              title={m.source?.pr_title || undefined}
              style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 'var(--t-sm)', color: accent, fontWeight: 600 }}
            >
              <Icon name={iconName} size={13} /> {verb} {label}
              <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', fontWeight: 500 }}>
                from {prLabel}
              </span>
            </a>
          )
        })}
        {wt.loom_url && (
          <a href={wt.loom_url} target="_blank" rel="noopener noreferrer" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 'var(--t-sm)', color: accent, fontWeight: 600 }}>
            <Icon name="play" size={13} /> Watch walkthrough
          </a>
        )}
        {savedScreenshots.map((shot) => (
          <a
            key={shot.url}
            href={shot.url}
            target="_blank"
            rel="noopener noreferrer"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 'var(--t-sm)', color: accent, fontWeight: 600 }}
          >
            <Icon name="image" size={13} /> {shot.filename ? `View ${shot.filename}` : 'View screenshot'}
          </a>
        ))}
        {wt.notes && wt.notes.trim() && (
          <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)', whiteSpace: 'pre-wrap', lineHeight: '20px' }}>
            {renderInline(wt.notes)}
          </div>
        )}
        <button
          type="button"
          onClick={() => onEditingChange(true)}
          style={{ alignSelf: 'flex-start', marginTop: 2, background: 'none', border: 'none', padding: 0, cursor: 'pointer', fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', textDecoration: 'underline' }}
        >
          {present ? 'Edit walkthrough' : 'Add a walkthrough'}
        </button>
        {cardCta}
      </div>
    </div>
  )
}

/**
 * "How to see this" orientation for a UAT tester who won't read the full plan.
 * Driven by the LLM's uat_complexity + how_to_see_it. High-complexity tickets
 * get a prominent amber treatment; low/medium stay quiet. When `enableWalkthrough`
 * is set, the planner's editable walkthrough (Loom/screenshot/notes) is appended.
 */
function UatGuideCard({
  complexity,
  howToSeeIt,
  enableWalkthrough,
  walkthrough,
  prMedia,
  editingWalkthrough,
  onEditingChange,
  onSaveWalkthrough,
  savingWalkthrough,
  ticketStatus,
}) {
  const summary = typeof howToSeeIt?.summary === 'string' ? howToSeeIt.summary.trim() : ''
  const reason = typeof howToSeeIt?.reason === 'string' ? howToSeeIt.reason.trim() : ''
  // Nothing useful to show and no walkthrough to author — bail rather than
  // render an empty shell (e.g. an old plan generated before these fields).
  if (!summary && !reason && !enableWalkthrough) return null
  const meta = UAT_COMPLEXITY_META[complexity] || UAT_COMPLEXITY_META.medium

  return (
    <div
      id="uat-guide-card"
      className="card"
      style={{
        padding: 'var(--s-5) var(--s-6)',
        marginBottom: 'var(--s-5)',
        background: meta.bg,
        borderColor: meta.border,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 'var(--s-4)' }}>
        <div style={{ width: 32, height: 32, borderRadius: 'var(--r-md)', background: meta.iconBg, display: 'grid', placeItems: 'center', flexShrink: 0 }}>
          <Icon name={meta.icon} size={16} style={{ color: meta.accent }} />
        </div>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', flexWrap: 'wrap', marginBottom: summary || reason ? 6 : 0 }}>
            <span style={{ fontSize: 'var(--t-md)', fontWeight: 600, color: 'var(--fg-strong)' }}>{meta.heading}</span>
            <UatComplexityBadge complexity={complexity} />
          </div>
          {summary && (
            <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)', lineHeight: '20px' }}>
              {renderInline(summary)}
            </div>
          )}
          {reason && (
            <div style={{ marginTop: summary ? 4 : 0, fontSize: 'var(--t-xs)', color: 'var(--fg-muted)' }}>
              Why: {renderInline(reason)}
            </div>
          )}
          {enableWalkthrough && (
            <WalkthroughSection
              walkthrough={walkthrough}
              prMedia={prMedia}
              editing={editingWalkthrough}
              onEditingChange={onEditingChange}
              onSave={onSaveWalkthrough}
              saving={savingWalkthrough}
              accent={meta.accent}
              ticketStatus={ticketStatus}
            />
          )}
        </div>
      </div>
    </div>
  )
}

/**
 * Cases the planner flagged as already covered by an existing unit test.
 * Collapsed by default so QA's manual checklist stays lean — they're shown for
 * completeness (and to prove the coverage was considered, not forgotten).
 */
function CoveredByUnitTestsSection({ cases }) {
  const [open, setOpen] = useState(false)
  if (!cases || cases.length === 0) return null
  return (
    <section id="sect-covered-by-unit-tests" style={{ marginTop: 'var(--s-8)' }}>
      <header
        onClick={() => setOpen((v) => !v)}
        style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: open ? 'var(--s-4)' : 0, cursor: 'pointer' }}
      >
        <Icon name={open ? 'chevron-down' : 'chevron-right'} size={14} style={{ color: 'var(--fg-muted)' }} />
        <Icon name="beaker" size={16} style={{ color: 'var(--fg-muted)' }} />
        <h2 style={{ margin: 0, fontSize: 'var(--t-lg)', fontWeight: 600, letterSpacing: '-.005em', color: 'var(--fg-muted)' }}>
          Already covered by unit tests
        </h2>
        <Chip size="sm">{cases.length}</Chip>
        <span style={{ flex: 1 }} />
        <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>Automated · QA can skip</span>
      </header>
      {open && (
        <div className="card" style={{ padding: 'var(--s-5) var(--s-6)' }}>
          {cases.map((test, i) => (
            <div
              key={i}
              style={{
                display: 'flex',
                flexDirection: 'column',
                gap: 2,
                padding: '8px 0',
                borderBottom: i < cases.length - 1 ? '1px solid var(--divider)' : 'none',
              }}
            >
              <span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>
                {typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}
              </span>
              {test.unit_test_ref && (
                <code
                  style={{
                    fontSize: 10.5,
                    color: 'var(--fg-subtle)',
                    fontFamily: 'var(--font-mono)',
                  }}
                >
                  {test.unit_test_ref}
                </code>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

function TestPlanDisplay({ testPlan, ticketData, ticketsData, onPosted }) {
  const isMulti = !!(ticketsData && ticketsData.length > 1)

  const allKeys = isMulti ? ticketsData.map((t) => t.key) : []
  const [selectedKeys, setSelectedKeys] = useState(() => new Set(allKeys))
  const [postingStates, setPostingStates] = useState({})

  // Pull cases the planner flagged as already covered by a unit test out of the
  // manual QA sections into a separate collapsed list, so the checklist QA
  // actually runs stays lean. `displayPlan` drives all section rendering and
  // progress; checkbox indices are relative to this filtered view. Export/post
  // helpers receive the full `testPlan` — the formatters filter internally.
  const { displayPlan, coveredCases } = useMemo(() => {
    const covered = []
    const dp = { ...testPlan }
    COVERABLE_KEYS.forEach((key) => {
      const items = Array.isArray(testPlan?.[key]) ? testPlan[key] : null
      if (!items) return
      const keep = []
      items.forEach((t) => {
        if (t && t.covered_by_unit_test) covered.push(t)
        else keep.push(t)
      })
      dp[key] = keep
    })
    return { displayPlan: dp, coveredCases: covered }
  }, [testPlan])

  // Whether the "already covered by unit tests" cases are included in the Jira
  // comment. Off by default — QA wants a lean checklist; on writes the full
  // record. Resets when the plan identity changes.
  const [includeCovered, setIncludeCovered] = useState(false)

  const [isPosting, setIsPosting] = useState(false)
  // Local "we just posted this version" timestamp. The parent re-fetches
  // history asynchronously via onPosted; this lets the badge appear
  // immediately without waiting for that round-trip. Cleared when the plan
  // identity changes (regeneration produces a new plan_id).
  const [localPostedAt, setLocalPostedAt] = useState(null)
  useEffect(() => {
    setLocalPostedAt(null)
    setIncludeCovered(false)
  }, [testPlan?.plan_id])
  const postedAt = testPlan?.posted_at || localPostedAt

  // Human-authored walkthrough (Loom / screenshot / notes), keyed to the
  // primary ticket so it persists across regenerations. Editing is enabled for
  // single-ticket plans (the walkthrough belongs to one ticket).
  const walkthroughKey = ticketData?.key || (ticketsData && ticketsData[0]?.key) || ''
  const {
    walkthrough,
    saving: savingWalkthrough,
    save: saveWalkthrough,
  } = useTicketWalkthrough(walkthroughKey)
  const [editingWalkthrough, setEditingWalkthrough] = useState(false)

  // Images/videos the developer already uploaded to the PR. These count as
  // walkthrough material on their own — surfacing them here saves the tester a
  // click into GitHub. Read-only: they live on GitHub, not our DB.
  const prMedia = useMemo(
    () => extractPrMedia(ticketData?.development_info?.pull_requests),
    [ticketData?.development_info?.pull_requests]
  )

  // Close the walkthrough editor when the ticket changes — the fetched
  // walkthrough belongs to a different key and stale edit state would leak.
  useEffect(() => {
    setEditingWalkthrough(false)
  }, [walkthroughKey])

  const ticketKeysJoined = isMulti
    ? allKeys.join('+')
    : ticketData?.key || ''
  const storageKey = useMemo(() => {
    if (!ticketKeysJoined) return null
    return buildStorageKey(displayPlan, ticketKeysJoined.split('+'))
  }, [displayPlan, ticketKeysJoined])

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
    // Optimistic: render the last-known local state instantly so toggles never
    // flicker while the shared, server-side state is in flight.
    let local = new Set()
    try {
      const raw = window.localStorage.getItem(storageKey)
      if (raw) {
        const arr = JSON.parse(raw)
        if (Array.isArray(arr)) local = new Set(arr)
      }
    } catch {
      /* ignore corrupt cache */
    }
    setCheckedTests(local)

    // Authoritative: the shared per-ticket progress lives on the server, so the
    // whole QA team converges on the same checked set. Falls back to the local
    // optimistic state if the server is unreachable.
    const serverKey = storageKey.slice(PROGRESS_STORAGE_PREFIX.length)
    let cancelled = false
    fetch(`${API_BASE}/test-plan-progress/${encodeURIComponent(serverKey)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (cancelled || !data || !Array.isArray(data.checked_ids)) return
        setCheckedTests(new Set(data.checked_ids))
      })
      .catch(() => {
        /* offline / server down — keep the local optimistic state */
      })
    return () => {
      cancelled = true
    }
  }, [storageKey])

  // Mirror every change to localStorage as an offline cache + optimistic source
  // for the next load. The server remains the shared source of truth.
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

  // Debounced save of user edits to the shared server-side progress. Only fires
  // on an actual toggle (not on hydration), so loading never echoes back a write.
  const progressSaveTimer = useRef(null)
  useEffect(
    () => () => {
      if (progressSaveTimer.current) clearTimeout(progressSaveTimer.current)
    },
    []
  )
  const scheduleProgressSave = (nextSet) => {
    if (!storageKey || typeof window === 'undefined') return
    const serverKey = storageKey.slice(PROGRESS_STORAGE_PREFIX.length)
    const payload = [...nextSet]
    if (progressSaveTimer.current) clearTimeout(progressSaveTimer.current)
    progressSaveTimer.current = setTimeout(() => {
      fetch(`${API_BASE}/test-plan-progress/${encodeURIComponent(serverKey)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ checked_ids: payload }),
      }).catch(() => {
        /* offline — localStorage holds it; resyncs on the next successful save */
      })
    }, 600)
  }

  const toggleTest = (section, index) => {
    const id = `${section}:${index}`
    const next = new Set(checkedTests)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    setCheckedTests(next)
    scheduleProgressSave(next)
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

  const planHasAcs = ['happy_path', 'edge_cases', 'integration_tests'].some((key) => {
    const items = displayPlan[key]
    if (!Array.isArray(items)) return false
    return items.some(
      (t) =>
        Array.isArray(t?.covers_acs) &&
        t.covers_acs.some((id) => typeof id === 'string' && id.trim())
    )
  })

  const handleCopyMarkdown = () => {
    const markdown = formatTestPlanAsMarkdown(testPlan, primaryTicketData, walkthrough)
    navigator.clipboard
      .writeText(markdown)
      .then(() => showNotification(setCopyNotification, copyTimerRef, 'success', 'Copied'))
      .catch(() => showNotification(setCopyNotification, copyTimerRef, 'error', 'Failed to copy'))
  }

  const handleDownloadMarkdown = () => {
    const markdown = formatTestPlanAsMarkdown(testPlan, primaryTicketData, walkthrough)
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

  const handleSaveWalkthrough = async (payload) => {
    if (!walkthroughKey) return
    try {
      await saveWalkthrough(payload)
      setEditingWalkthrough(false)
    } catch (err) {
      showNotification(
        setPostNotification,
        postTimerRef,
        'error',
        err?.message || 'Failed to save walkthrough'
      )
    }
  }

  const handlePostToJira = async () => {
    setIsPosting(true)
    try {
      const jiraText = formatTestPlanAsJira(testPlan, walkthrough, { includeCovered })
      const response = await fetch(`${API_BASE}/jira/post-comment`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          issue_key: ticketData.key,
          comment_text: jiraText,
          plan_id: testPlan?.plan_id ?? null,
        }),
      })

      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.detail || 'Failed to post to Jira')
      }

      const result = await response.json()
      const action = result.updated ? 'updated' : 'posted'
      setLocalPostedAt(result.posted_at || new Date().toISOString())
      if (onPosted) onPosted({ ticketKey: ticketData.key, planId: testPlan?.plan_id ?? null })
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
      let jiraText = formatTestPlanAsJira(testPlan, walkthrough, { includeCovered })
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
  const totals = SECTION_KEYS.map((k) => sectionLength(displayPlan, k))
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
                <span className="tip">
                  <Chip size="sm">
                    {allKeys[0]}
                    {allKeys.length > 1 ? ` +${allKeys.length - 1} more` : ''}
                  </Chip>
                  <span className="tip-body">{allKeys.join(', ')}</span>
                </span>
              )}
            </div>
            <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 'var(--s-4)' }}>
              <div style={{ position: 'relative', flex: 1, height: 18, borderRadius: 999, overflow: 'hidden', background: 'var(--bg-input)' }}>
                <div style={{ display: 'flex', gap: 2, width: '100%', height: '100%' }}>
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
                <span
                  className="tnum"
                  style={{
                    position: 'absolute',
                    inset: 0,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    fontSize: 'var(--t-xs)',
                    fontWeight: 600,
                    color: 'var(--fg-strong)',
                    textShadow: '0 1px 2px rgba(0,0,0,.45)',
                    pointerEvents: 'none',
                  }}
                >
                  {pctAll}%
                </span>
              </div>
              <span className="tnum" style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)', minWidth: 56, textAlign: 'right' }}>
                {checkedAll} / {totalAll}
              </span>
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
            <span style={{ width: 1, height: 14, background: 'var(--line-strong)', alignSelf: 'center', flexShrink: 0 }} />
            <div style={{ display: 'inline-flex', gap: 4, flexShrink: 0 }}>
              {(() => {
                const base = {
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  padding: '2px 8px',
                  borderRadius: 'var(--r-pill)',
                  border: '1px solid var(--line)',
                  background: 'var(--bg-surface)',
                  color: 'var(--fg-muted)',
                  fontSize: 'var(--t-xs)',
                  fontWeight: 500,
                  textDecoration: 'none',
                  cursor: 'pointer',
                  lineHeight: 1,
                  height: 22,
                  minWidth: 0,
                  flexShrink: 0,
                }
                const primary = {
                  ...base,
                  background: 'var(--accent)',
                  borderColor: 'var(--accent)',
                  color: 'var(--accent-ink)',
                }
                const isPostBusy = isMulti ? isAnyPosting : isPosting
                const postDisabled = isMulti
                  ? isAnyPosting || selectedKeys.size === 0
                  : isPosting
                const copyLabel =
                  copyNotification?.type === 'success' ? 'Copied' : 'Copy markdown'
                const postLabel = isMulti
                  ? `Post to selected (${selectedKeys.size})`
                  : postNotification?.type === 'success'
                    ? postNotification.message
                    : 'Post to Jira'
                return (
                  <>
                    <span className="tip">
                      <button
                        type="button"
                        style={base}
                        aria-label={copyLabel}
                        onClick={handleCopyMarkdown}
                      >
                        <Icon
                          name={copyNotification?.type === 'success' ? 'check' : 'copy'}
                          size={12}
                        />
                      </button>
                      <span className="tip-body">{copyLabel}</span>
                    </span>
                    <span className="tip">
                      <button
                        type="button"
                        style={base}
                        aria-label="Download .md"
                        onClick={handleDownloadMarkdown}
                      >
                        <Icon name="download" size={12} />
                      </button>
                      <span className="tip-body">Download .md</span>
                    </span>
                    <span className="tip" data-align="end">
                      <button
                        type="button"
                        style={{
                          ...primary,
                          opacity: postDisabled ? 0.5 : 1,
                          cursor: postDisabled ? 'not-allowed' : 'pointer',
                        }}
                        aria-label={postLabel}
                        onClick={isMulti ? handlePostSelected : handlePostToJira}
                        disabled={postDisabled}
                      >
                        {isPostBusy ? (
                          <span className="spin" />
                        ) : (
                          <Icon
                            name={postNotification?.type === 'success' ? 'check' : 'send'}
                            size={12}
                          />
                        )}
                      </button>
                      <span className="tip-body">{postLabel}</span>
                    </span>
                  </>
                )
              })()}
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
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', flexWrap: 'wrap' }}>
              <span style={{ fontSize: 'var(--t-md)', fontWeight: 600, color: 'var(--fg-strong)' }}>Generated test plan</span>
              {isMulti && <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>· {allKeys.join(' + ')}</span>}
              {!isMulti && postedAt && (
                <span title={`Posted ${new Date(postedAt).toLocaleString()}`} style={{ display: 'inline-flex' }}>
                  <Chip size="sm" dot dotColor="var(--success)">
                    Live in Jira · {formatRelativeTime(postedAt)}
                  </Chip>
                </span>
              )}
            </div>
            <div style={{ marginTop: 2, fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
              {totalAll} test cases
            </div>
          </div>
        </div>
      </div>

      <UatGuideCard
        complexity={testPlan.uat_complexity}
        howToSeeIt={testPlan.how_to_see_it}
        enableWalkthrough={!isMulti && !!walkthroughKey}
        walkthrough={walkthrough}
        prMedia={prMedia}
        editingWalkthrough={editingWalkthrough}
        onEditingChange={setEditingWalkthrough}
        onSaveWalkthrough={handleSaveWalkthrough}
        savingWalkthrough={savingWalkthrough}
        ticketStatus={primaryTicketData?.status}
      />

      {testPlan.ac_coverage && (
        <AcCoveragePanel coverage={testPlan.ac_coverage} />
      )}

      <GroundingWarningsPanel warnings={testPlan.grounding_warnings} />

      {SECTIONS.map((section) => {
        const items = displayPlan[section.key]
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
            planHasAcs={planHasAcs}
          />
        )
      })}

      <CoveredByUnitTestsSection cases={coveredCases} />

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

        {coveredCases.length > 0 && (
          <div style={{ marginTop: 'var(--s-4)', paddingTop: 'var(--s-4)', borderTop: '1px solid var(--divider)' }}>
            <label style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--s-3)', cursor: 'pointer' }}>
              <Cbx checked={includeCovered} onChange={() => setIncludeCovered((v) => !v)} />
              <span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>
                Include the {coveredCases.length} unit-tested case{coveredCases.length === 1 ? '' : 's'} in the Jira comment
              </span>
            </label>
            <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', marginTop: 4, marginLeft: 26 }}>
              Off by default — these are already automated, so the posted checklist stays lean. Turn on to keep the full record.
            </div>
          </div>
        )}

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
