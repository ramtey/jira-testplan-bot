/**
 * Display generated test plan with export options.
 * Supports both single-ticket and multi-ticket posting.
 */

import { useState, useRef, useEffect, useMemo } from 'react'
import { formatTestPlanAsMarkdown, formatTestPlanAsJira } from '../utils/markdown'
import { API_BASE_URL } from '../config'
import { useTicketWalkthrough } from '../hooks/useTicketWalkthrough'
import Icon from './Icon'
import MarkCarryOverModal from './MarkCarryOverModal'
import { Btn, Chip, ACTag, Pri, Cbx, Alert } from './ui'

const API_BASE = API_BASE_URL

const PROGRESS_STORAGE_PREFIX = 'testplan-progress:'
// Where the server's answer for a plan's key is remembered. Reusing a cached
// answer from the one producer is not a second producer; re-deriving the key
// locally would be, which is why the offline path reads this instead.
const PROGRESS_KEY_CACHE_PREFIX = 'testplan-progress-key:'

function rememberProgressKey(planId, key) {
  if (typeof window === 'undefined' || !planId || !key) return
  try {
    window.localStorage.setItem(`${PROGRESS_KEY_CACHE_PREFIX}${planId}`, key)
  } catch {
    /* storage full or disabled — the key is re-fetched next load */
  }
}

function recallProgressKey(planId) {
  if (typeof window === 'undefined' || !planId) return null
  try {
    return window.localStorage.getItem(`${PROGRESS_KEY_CACHE_PREFIX}${planId}`)
  } catch {
    return null
  }
}

// Post states that mean the plan reached the ticket, cleanly or with a caveat.
// A ticket in any of them stays locked so a second click can't re-post it.
const POSTED_STATES = new Set(['done', 'split', 'truncated', 'partial', 'stale', 'unrecorded'])

const SECTIONS = [
  { key: 'happy_path', label: 'Happy Path', icon: 'check-circle', renderer: 'card' },
  { key: 'edge_cases', label: 'Edge & Error', icon: 'alert', renderer: 'card', showCategory: true },
  { key: 'integration_tests', label: 'Integration & Backend', icon: 'circuit', renderer: 'card' },
  // API-level security negative cases. Present only when the ticket's DIFF
  // touched a risky surface — see src/app/security_surfaces.py — so most plans
  // render no such section at all.
  { key: 'security_negative_tests', label: 'Security Negative Tests', icon: 'alert', renderer: 'card', showSecurity: true },
  { key: 'regression_checklist', label: 'Regression Checklist', icon: 'history', renderer: 'checklist' },
]
const SECTION_KEYS = SECTIONS.map((s) => s.key)
// The four sections whose counts hold FIXED POSITIONS in the fingerprint.
// Deliberately not `SECTION_KEYS`: the security section is appended to the
// fingerprint, not positioned in it, because inserting a fifth positional
// count would change the key of every plan ever written and orphan its
// progress. `progress_key.SECTION_KEYS` is the same four, in the same order.
const FINGERPRINT_SECTION_KEYS = [
  'happy_path',
  'edge_cases',
  'integration_tests',
  'regression_checklist',
]
// Card sections whose cases the planner can flag as already covered by an
// existing unit test. Those cases are pulled into a separate list.
const COVERABLE_KEYS = ['happy_path', 'edge_cases', 'integration_tests', 'security_negative_tests']
// The namespace those lifted cases are addressed in. They are optional — they
// do not count towards the checklist — but they are not absent: a tester who
// verifies one live has an id to record it under. See `progress_key.py`.
const COVERED_KEY = 'covered_by_unit_test'

function sectionLength(testPlan, key) {
  return Array.isArray(testPlan?.[key]) ? testPlan[key].length : 0
}

/**
 * The canonical id of a case, rendered where the tester can read it.
 *
 * The visible grouping has never matched storage. This view shows Happy Path,
 * Edge & Error (with `error_handling` and `boundary` chips), Integration &
 * Backend (with a Cross-project chip) and Regression — but `edge_cases`
 * interleaves the two edge categories by plan position, so the second boundary
 * case on screen is `edge_cases:3`, not `edge_cases:1`. Anyone mapping from the
 * labels marks a different case than they meant to, silently and plausibly.
 * SK-2325 is where that was caught.
 *
 * Making the grouping match storage would mean splitting `edge_cases` into two
 * stored sections, which moves every index and orphans every recorded mark.
 * Printing the id instead costs one badge per card and removes the mapping step
 * entirely — the tester, the Jira comment and `mark-passed.sh` all quote the
 * same string.
 *
 * `display` overrides the badge text without touching that contract. Every
 * section passes the bare index, because the prefix is constant within a
 * section and printing it per case only crowds the row. What it cannot be is
 * *gone*: each section header states its namespace via `SectionIdHint`, the
 * tooltip here spells the id out in full, and the Jira comment prints it on
 * every case. The reader still never has to infer `edge_cases:3` from a label.
 */
function CaseIdBadge({ id, display }) {
  return (
    <code
      title={`Canonical id — quote this to mark the case (e.g. mark-passed.sh ${id}). The section headings above are display groupings and do not always match it.`}
      style={{
        height: 18,
        // A numeric badge is one or two digits wide. Padding alone would leave
        // the two-digit rows wider than the rest, jogging the text column, so
        // it gets a fixed box and centres inside it instead.
        padding: display === undefined ? '0 6px' : '0 2px',
        minWidth: display === undefined ? undefined : 24,
        background: 'var(--bg-input)',
        border: '1px solid var(--line)',
        color: 'var(--fg-subtle)',
        borderRadius: 3,
        fontSize: 10.5,
        fontFamily: 'var(--font-mono)',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        letterSpacing: 0,
      }}
    >
      {display ?? id}
    </code>
  )
}

/**
 * The section-size fingerprint, computed here for one purpose only: to check
 * the server's against it.
 *
 * The progress key itself is NOT built here any more. It has exactly one
 * producer — `src/app/services/progress_key.py`, deriving it from the *stored*
 * plan — and this component asks for it via `GET /plans/{id}/progress-key`.
 *
 * Building it locally is what let the key follow whatever the view happened to
 * be holding: a stale cached plan, or one generated but never recorded. SK-2327
 * polled `SK-2327:3-4-0-7` off a leftover 14-case render while its 15-case
 * stored plan had 11 cases passed under `SK-2327:4-5-0-6`, and showed 0%. The
 * same split, from the other side, is what `progress_key.py` was written to end
 * (mark-passed.sh vs this file; see its docstring).
 *
 * Keeping the local count as a *check* rather than a source means a future
 * divergence between this view's filtering and the server's counting rule
 * surfaces as a visible warning instead of silent zeros.
 */
function localFingerprint(displayPlan, coveredCount = 0) {
  const parts = FINGERPRINT_SECTION_KEYS.map((k) => sectionLength(displayPlan, k))
  const securityCount = sectionLength(displayPlan, 'security_negative_tests')
  // Trailing components exist only when they are non-zero, so a plan without
  // covered cases and without a security section keeps the exact key it had
  // before either existed — and its recorded progress with it. The one
  // exception: a plan with security cases and no covered ones writes the
  // covered count as an explicit 0, because otherwise a five-part key would
  // mean "covered" or "security" depending on which release wrote it.
  // `progress_key.fingerprint` does exactly this; these two must agree or the
  // drift warning fires on every plan.
  if (coveredCount > 0 || securityCount > 0) parts.push(coveredCount)
  if (securityCount > 0) parts.push(securityCount)
  return parts.join('-')
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

const SURFACE_LABELS = {
  backend_http: 'Backend / HTTP',
  web_ui: 'Web UI',
  mobile: 'Mobile',
  cli: 'CLI',
  manual_only: 'Manual only',
}

// Short chip text for the security section. The full meaning of each lives in
// the schema (`security_category` / `persona` in SUBMIT_TEST_PLAN_TOOL); these
// are the reader-facing names.
const SECURITY_CATEGORY_LABELS = {
  authz_matrix: 'Authz matrix',
  client_flag: 'Client flag',
  unauth_sweep: 'No auth',
  revoked_account: 'Deleted account',
  upload_abuse: 'Upload abuse',
  abuse_cost: 'Abuse / cost',
}

const PERSONA_LABELS = {
  anonymous: 'as anonymous',
  other_user: 'as another user',
  owner: 'as owner (control)',
  deleted_user: 'as deleted user',
  guest_capability: 'as link holder',
  service: 'as service',
}

// Three different asks to three different people: a record someone searches
// for, a payload someone mocks, and a shape no record can deliver to the code
// under test. See src/app/data_provisioning.py.
const PROVISIONING_LABELS = {
  real_record: 'real record — confirmed',
  real_record_unconfirmed: 'real record — UNCONFIRMED',
  stub: 'needs a stubbed payload (Engineering)',
  unreachable: 'not producible from live data',
}

const dataShapeParts = (test) => {
  const shape = test?.data_shape
  if (!shape || typeof shape !== 'object') return null
  const what = typeof shape.shape === 'string' ? shape.shape.trim() : ''
  const route = typeof shape.provisioning === 'string' ? shape.provisioning.trim() : ''
  const obtain = typeof shape.obtain === 'string' ? shape.obtain.trim() : ''
  const evidence = typeof shape.evidence === 'string' ? shape.evidence.trim() : ''
  if (!what && !route && !obtain) return null
  return { what, route: PROVISIONING_LABELS[route] || route, obtain, evidence }
}

// The concrete call a security case sends. Rendered as its own row rather
// than folded into the steps because the runner builds a curl/Postman request
// from exactly these fields and never opens the app.
function requestParts(test) {
  const req = test && test.request
  if (!req || typeof req !== 'object') return null
  const method = typeof req.method === 'string' ? req.method.trim() : ''
  const route = typeof req.route === 'string' ? req.route.trim() : ''
  if (!method && !route) return null
  const protocol = typeof req.protocol === 'string' ? req.protocol.trim() : ''
  return {
    call: [method, route].filter(Boolean).join(' ') + (protocol ? `  (${protocol})` : ''),
    params: typeof req.params === 'string' ? req.params.trim() : '',
    auth: typeof req.auth === 'string' ? req.auth.trim() : '',
  }
}

function formatSingleTestForClipboard(test) {
  const lines = []
  if (test.title) lines.push(typeof test.title === 'string' ? test.title : JSON.stringify(test.title))
  const req = requestParts(test)
  if (req) {
    lines.push('', `Request: ${req.call}`)
    if (req.params) lines.push(`Params: ${req.params}`)
    if (req.auth) lines.push(`Auth: ${req.auth}`)
  }
  if (test.preconditions) lines.push('', 'Preconditions:', String(test.preconditions))
  if (Array.isArray(test.steps) && test.steps.length > 0) {
    lines.push('', 'Steps:')
    test.steps.forEach((s, i) => lines.push(`${i + 1}. ${s}`))
  }
  if (test.expected) lines.push('', 'Expected:', String(test.expected))
  if (test.expected_verified === true) {
    lines.push('', `Expected verified against: ${test.expected_source || 'source not cited'}`)
    // The citation is a promise to stop checking. Say when its shape cannot
    // support that promise — this is "unchecked", not "wrong".
    if (test.expected_source_unconfirmed) {
      lines.push(`⚠️ Citation unconfirmed — ${test.expected_source_unconfirmed_reason}. Verify before treating a failure as a defect.`)
    }
  } else if (test.expected_verified === false) {
    lines.push('', 'Expected: UNVERIFIED — assumption, not read from the implementation.')
  }
  if (test.test_data) lines.push('', 'Test data:', String(test.test_data))
  const shapeParts = dataShapeParts(test)
  if (shapeParts) {
    lines.push('', `Data shape: ${[shapeParts.what, shapeParts.route].filter(Boolean).join(' — ')}`)
    if (shapeParts.obtain) lines.push(`How to get it: ${shapeParts.obtain}`)
    if (shapeParts.evidence) lines.push(`Evidence: ${shapeParts.evidence}`)
  }
  if (test.data_ask_unactionable) {
    lines.push('', `⚠️ Data ask is not actionable — ${test.data_ask_unactionable_reason || 'no provisioning route given'}.`)
  }
  const runsOn = [
    test.surface ? (SURFACE_LABELS[test.surface] || test.surface) : null,
    test.credentials ? `credentials: ${test.credentials}` : null,
    test.environment ? `environment: ${test.environment}` : null,
  ].filter(Boolean)
  if (runsOn.length > 0) lines.push('', 'Runs on:', runsOn.join(' · '))
  return lines.join('\n')
}

function TestCardCopyButton({ test }) {
  const [copied, setCopied] = useState(false)
  const timerRef = useRef(null)
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current) }, [])

  const handleCopy = (e) => {
    e.stopPropagation()
    const text = formatSingleTestForClipboard(test)
    navigator.clipboard.writeText(text).then(
      () => {
        setCopied(true)
        if (timerRef.current) clearTimeout(timerRef.current)
        timerRef.current = setTimeout(() => setCopied(false), 1500)
      },
      () => {
        /* clipboard blocked — swallow silently */
      }
    )
  }

  return (
    <button
      type="button"
      className="tc-copy-btn"
      data-copied={copied ? 'true' : 'false'}
      onClick={handleCopy}
      title={copied ? 'Copied' : 'Copy test to clipboard'}
      aria-label={copied ? 'Copied' : 'Copy test to clipboard'}
      style={{ position: 'absolute', top: 8, right: 8, width: 14, height: 14 }}
    >
      <Icon name={copied ? 'check' : 'copy'} size={14} />
    </button>
  )
}

function TestCard({ test, section, index, checked, onToggle, showCategory, planHasAcs, ticketKeys }) {
  const acIds = Array.isArray(test.covers_acs)
    ? test.covers_acs.filter((id) => typeof id === 'string' && id.trim())
    : []
  const groundedIn = Array.isArray(test.grounded_in)
    ? test.grounded_in.filter((s) => typeof s === 'string' && s.trim())
    : []
  const isUntraced = planHasAcs && acIds.length === 0 && groundedIn.length === 0
  const shapeParts = dataShapeParts(test)
  // `tc-happy_path-0` is the DOM anchor; `happy_path:0` is what gets stored.
  // Both spellings have been written into progress rows before, which is why
  // the badge below shows the stored one and mark-passed.sh rejects this one.
  const checkboxId = `tc-${section.key}-${index}`
  const storedId = `${section.key}:${index}`

  return (
    <div
      className="card"
      id={checkboxId}
      style={{
        position: 'relative',
        borderColor: checked ? 'rgba(34,197,94,.3)' : undefined,
        transition: 'border-color var(--d-fast)',
      }}
    >
      <TestCardCopyButton test={test} />
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 'var(--s-5)', padding: checked ? 'var(--s-5) var(--s-6)' : 'var(--s-6) var(--s-6) var(--s-5)' }}>
        <span onClick={() => onToggle && onToggle()} style={{ flexShrink: 0, marginTop: 1 }}>
          <span className="cbx" data-checked={checked ? 'true' : 'false'} role="checkbox" aria-checked={checked} />
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center', marginBottom: 6 }}>
            <CaseIdBadge id={storedId} display={String(index)} />
            {test.priority && <Pri level={test.priority} />}
            {acIds.map((id) => <ACTag key={id}>{shortAcId(id, ticketKeys)}</ACTag>)}
            {section.showSecurity && test.security_category && (
              <span
                title="Which security category this case belongs to. Only categories the ticket's diff actually triggered appear here."
                style={{ height: 18, padding: '0 6px', background: 'rgba(244,63,94,.10)', border: '1px solid rgba(244,63,94,.35)', color: '#fda4af', borderRadius: 3, fontSize: 10.5, fontWeight: 500, display: 'inline-flex', alignItems: 'center' }}
              >
                {SECURITY_CATEGORY_LABELS[test.security_category] || test.security_category}
              </span>
            )}
            {section.showSecurity && test.persona && (
              <span
                title="The one principal this case runs as. Each principal is its own case on purpose: they share steps and differ in the correct outcome, so a merged case would report one verdict for several independent controls."
                style={{ height: 18, padding: '0 6px', background: 'rgba(244,63,94,.10)', border: '1px solid rgba(244,63,94,.35)', color: '#fda4af', borderRadius: 3, fontSize: 10.5, fontWeight: 500, display: 'inline-flex', alignItems: 'center' }}
              >
                {PERSONA_LABELS[test.persona] || test.persona}
              </span>
            )}
            {showCategory && test.category && (
              <span style={{ height: 18, padding: '0 6px', background: 'rgba(255,255,255,.04)', color: 'var(--fg-muted)', borderRadius: 3, fontSize: 10.5, fontWeight: 500, display: 'inline-flex', alignItems: 'center' }}>
                {test.category}
              </span>
            )}
            {test.surface && (
              <span
                title={`Surface: what it takes to run this case.${test.credentials ? ` Credentials: ${test.credentials}.` : ''}${test.environment ? ` Environment: ${test.environment}.` : ''}`}
                style={{ height: 18, padding: '0 6px', background: 'rgba(99,102,241,.10)', border: '1px solid rgba(99,102,241,.35)', color: '#a5b4fc', borderRadius: 3, fontSize: 10.5, fontWeight: 500, display: 'inline-flex', alignItems: 'center', gap: 4 }}
              >
                {SURFACE_LABELS[test.surface] || test.surface}
              </span>
            )}
            {test.expected_verified === false && (
              <span
                title="The expected result here was not read from the implementation — it is an assumption from the AC text or convention. Confirm it against the code before treating a failure as a defect."
                style={{ height: 18, padding: '0 6px', background: 'rgba(245,158,11,.10)', border: '1px solid rgba(245,158,11,.35)', color: '#fcd34d', borderRadius: 3, fontSize: 10.5, fontWeight: 500, display: 'inline-flex', alignItems: 'center', gap: 4 }}
              >
                <Icon name="scan" size={10} />
                Unverified expectation
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
                Unverified UI
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
      <div style={{ display: 'grid', gridTemplateColumns: '140px minmax(0, 1fr)', gap: '6px var(--s-6)', padding: '0 var(--s-6) var(--s-6)', alignItems: 'start', overflowWrap: 'anywhere' }}>
        {requestParts(test) && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Request</span>
            <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>
              <code style={{ fontSize: 'var(--t-sm)' }}>{requestParts(test).call}</code>
              {requestParts(test).params && (
                <div style={{ color: 'var(--fg-muted)', marginTop: 2 }}>
                  params <code style={{ fontSize: 'var(--t-sm)' }}>{requestParts(test).params}</code>
                </div>
              )}
              {requestParts(test).auth && (
                <div style={{ color: 'var(--fg-muted)', marginTop: 2 }}>auth: {requestParts(test).auth}</div>
              )}
            </div>
          </>
        )}
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
        {shapeParts && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Data shape</span>
            <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>
              <div>
                {shapeParts.what}
                {shapeParts.route && (
                  <em style={{ color: 'var(--fg-faint)' }}>
                    {shapeParts.what ? ' — ' : ''}{shapeParts.route}
                  </em>
                )}
              </div>
              {shapeParts.obtain && <div>How to get it: {renderInline(shapeParts.obtain)}</div>}
              {shapeParts.evidence && <div>Evidence: {renderInline(shapeParts.evidence)}</div>}
            </div>
          </>
        )}
        {test.data_ask_unactionable && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Data ask</span>
            <div style={{ fontSize: 'var(--t-sm)', color: '#fcd34d' }}>
              ⚠️ Not actionable — {test.data_ask_unactionable_reason || 'no provisioning route given'}. Settle whether a real record can produce this shape before sending anyone to look for one.
            </div>
          </>
        )}
        {test.expected_verified === true && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Verified against</span>
            <div className="pathrow" style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>
              {test.expected_source
                ? <code style={{ fontSize: 11 }}>{test.expected_source}</code>
                : <span style={{ color: '#fcd34d' }}>marked verified but no source cited</span>}
            </div>
          </>
        )}
        {(test.credentials || test.environment) && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Runs on</span>
            <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>
              {[test.credentials && `credentials: ${test.credentials}`, test.environment && `environment: ${test.environment}`]
                .filter(Boolean)
                .join(' · ')}
            </div>
          </>
        )}
        {groundedIn.length > 0 && (
          <>
            <span className="lbl" style={{ marginTop: 2 }}>Grounded in</span>
            <div className="pathrow" style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
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
                    minWidth: 0,
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
        <span style={{ color: 'var(--fg-faint)', fontSize: 'var(--t-xs)' }}>·</span>
        <SectionIdHint prefix={section.key} />
      </header>
      <div className="card" style={{ padding: 'var(--s-5) var(--s-6)' }}>
        {items.map((item, i) => {
          const id = `${section.key}:${i}`
          const isChecked = checkedTests.has(id)
          return (
            <div key={i} style={{ display: 'flex', gap: 'var(--s-4)', padding: '6px 0', borderBottom: i < items.length - 1 ? '1px solid var(--divider)' : 'none', alignItems: 'center' }}>
              <Cbx checked={isChecked} onChange={() => onToggle(section.key, i)} />
              <CaseIdBadge id={id} display={String(i)} />
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

/**
 * The namespace the badges below are indices into.
 *
 * It belongs in the header rather than on every row: the prefix is constant
 * for a section, so repeating it per case is noise, but dropping it outright
 * would leave a bare `3` that nothing maps back to `edge_cases:3`. Stated once
 * here, spelled out in full in each badge's tooltip, and printed in the Jira
 * comment besides.
 */
function SectionIdHint({ prefix }) {
  return (
    <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>
      ids{' '}
      <code style={{ fontFamily: 'var(--font-mono)', color: 'var(--fg-muted)' }}>{prefix}:N</code>
    </span>
  )
}

function CardSection({ section, items, checkedTests, onToggle, planHasAcs, ticketKeys }) {
  const c = items.reduce((acc, _, i) => acc + (checkedTests.has(`${section.key}:${i}`) ? 1 : 0), 0)
  return (
    <section id={`sect-${section.key}`} style={{ marginTop: 'var(--s-8)' }}>
      <header style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: 'var(--s-4)' }}>
        <Icon name={section.icon} size={16} style={{ color: 'var(--accent)' }} />
        <h2 style={{ margin: 0, fontSize: 'var(--t-lg)', fontWeight: 600, letterSpacing: '-.005em', color: 'var(--fg-strong)' }}>{section.label}</h2>
        <SectionChip checked={c} total={items.length} />
        <span style={{ flex: 1 }} />
        <SectionIdHint prefix={section.key} />
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
            ticketKeys={ticketKeys}
          />
        ))}
      </div>
    </section>
  )
}

// AC IDs are ticket-scoped ("SK-2585-AC1"), but the surrounding UI often
// already names the ticket. Drop the "SK-XXXX-" prefix only when the
// reader can't confuse it with another ticket's ACs: single-ticket plans
// at panel level, or any per-ticket group whose header already names the
// key. In a multi-ticket panel-level list ("2 invented AC IDs") we keep
// the full id so a bare "AC1" can't ambiguously belong to either ticket.
function shortAcId(id, knownKeys) {
  if (!id || !knownKeys || knownKeys.size !== 1) return id
  const [key] = knownKeys
  return id.startsWith(`${key}-`) ? id.slice(key.length + 1) : id
}

function stripKeyPrefix(id, key) {
  if (!id || !key) return id
  return id.startsWith(`${key}-`) ? id.slice(key.length + 1) : id
}

function AcCoveragePanel({ coverage }) {
  if (!coverage || !coverage.tickets) return null
  const entries = Object.entries(coverage.tickets).filter(
    ([, info]) => info && (info.total > 0 || (info.superseded?.length ?? 0) > 0)
  )
  if (entries.length === 0) return null
  const ticketKeys = new Set(Object.keys(coverage.tickets))

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
            {invalidIds.map((id) => <span key={id} style={{ display: 'inline-block', marginRight: 6 }}><ACTag>{shortAcId(id, ticketKeys)}</ACTag></span>)}
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
                  <ACTag>{shortAcId(s.loser_id, ticketKeys)}</ACTag>
                  <Icon name="arrow-right" size={11} style={{ color: 'var(--fg-faint)' }} />
                  <ACTag>{shortAcId(s.winner_id, ticketKeys)}</ACTag>
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
                      <ACTag>{stripKeyPrefix(u.id, key)}</ACTag>
                      <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-muted)' }} title={u.text}>{u.text}</span>
                    </li>
                  ))}
                </ul>
              )}
              {info.under_covered && info.under_covered.length > 0 && (
                <ul style={{ margin: '6px 0 0 88px', padding: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 4 }}>
                  {info.under_covered.map((u) => (
                    <li key={u.id} style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', flexWrap: 'wrap' }}>
                      <ACTag>{stripKeyPrefix(u.id, key)}</ACTag>
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
function GroundingWarningsPanel({ warnings, ticketKeys }) {
  if (!Array.isArray(warnings) || warnings.length === 0) return null
  const warnItems = warnings.filter((w) => (w?.severity ?? 'warn') === 'warn')
  const infoItems = warnings.filter((w) => w?.severity === 'info')
  if (warnItems.length === 0 && infoItems.length === 0) return null
  return (
    <div style={{ marginBottom: 'var(--s-5)', display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
      {warnItems.length > 0 && (
        <CollapsibleWarningAlert
          tone="warning"
          title={`${warnItems.length} behaviour${warnItems.length === 1 ? '' : 's'} not confirmed in AC or code — verify before testing`}
          items={warnItems}
          rowPrefix="warn"
          ticketKeys={ticketKeys}
        />
      )}
      {infoItems.length > 0 && (
        <CollapsibleWarningAlert
          tone="info"
          title={`${infoItems.length} behaviour${infoItems.length === 1 ? '' : 's'} beyond the cited AC but present in code`}
          items={infoItems}
          rowPrefix="info"
          ticketKeys={ticketKeys}
        />
      )}
    </div>
  )
}

// Rule-8 output: gaps the planner noticed while reading the code that would
// break the ticket's stated goal, but that no test case covers. Rendered apart
// from the case lists on purpose — a gap is not something QA can mark pass or
// fail, and putting it in the checklist means it gets ticked and forgotten.
function RisksAndGapsPanel({ gaps }) {
  const items = Array.isArray(gaps) ? gaps.filter(Boolean) : []
  if (items.length === 0) return null
  return (
    <div
      className="card"
      style={{
        marginBottom: 'var(--s-5)',
        padding: 'var(--s-5) var(--s-6)',
        borderColor: 'rgba(245,158,11,.35)',
        background: 'rgba(245,158,11,.05)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-2)', marginBottom: 'var(--s-3)' }}>
        <Icon name="scan" size={12} />
        <span style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: '#fcd34d' }}>
          {items.length} risk{items.length === 1 ? '' : 's'} / gap{items.length === 1 ? '' : 's'} observed
        </span>
      </div>
      <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)', marginBottom: 'var(--s-4)' }}>
        Not test cases — gaps in the ticket itself that no case can mark pass or fail.
      </div>
      <ul style={{ margin: 0, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
        {items.map((g, i) => (
          <li key={i} style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>
            {typeof g === 'string' ? g : (
              <>
                <div>{g.gap}</div>
                {g.impact && (
                  <div style={{ color: 'var(--fg-muted)', marginTop: 2 }}>Impact: {g.impact}</div>
                )}
                {g.evidence && (
                  <div style={{ color: 'var(--fg-muted)', marginTop: 2 }}>
                    Evidence: <code style={{ fontSize: 11 }}>{g.evidence}</code>
                  </div>
                )}
              </>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

function CollapsibleWarningAlert({ tone, title, items, rowPrefix, ticketKeys }) {
  const [open, setOpen] = useState(false)
  const header = (
    <button
      type="button"
      onClick={() => setOpen((v) => !v)}
      aria-expanded={open}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--s-2)',
        background: 'none',
        border: 'none',
        padding: 0,
        margin: 0,
        color: 'inherit',
        font: 'inherit',
        fontWeight: 'var(--w-semi)',
        cursor: 'pointer',
        textAlign: 'left',
        width: '100%',
      }}
    >
      <Icon
        name="chevron-right"
        style={{
          width: 14,
          height: 14,
          transition: 'transform 120ms',
          transform: open ? 'rotate(90deg)' : 'rotate(0deg)',
          flexShrink: 0,
        }}
      />
      <span>{title}</span>
    </button>
  )
  return (
    <Alert tone={tone} title={header}>
      {open && (
        <ul style={{ margin: 4, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 4 }}>
          {items.map((w, idx) => (
            <GroundingWarningRow key={`${rowPrefix}-${w.ac_id}-${idx}`} warning={w} ticketKeys={ticketKeys} />
          ))}
        </ul>
      )}
    </Alert>
  )
}

function GroundingWarningRow({ warning, ticketKeys }) {
  const files = warning?.code_evidence?.files || []
  return (
    <li style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', flexWrap: 'wrap' }}>
      <ACTag>{shortAcId(warning.ac_id, ticketKeys)}</ACTag>
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

/**
 * Cases the planner flagged as already covered by an existing unit test.
 * Collapsed by default so QA's manual checklist stays lean — they're shown for
 * completeness (and to prove the coverage was considered, not forgotten).
 */
/**
 * Cases a unit test already asserts: optional, not absent.
 *
 * These are lifted out of the manual sections so the checklist QA actually runs
 * stays lean, and that was right. Removing them from the *id space* was not: a
 * covered case had no `section:index` at all, so a tester who verified one live
 * had nowhere to record it. SK-2325's plan 536 is the case — all four of its
 * integration cases were flagged covered, the section rendered as 0 of 0, two
 * were then verified live with real evidence, and `mark-passed.sh` answered
 * "'integration_tests:0' is out of range — integration_tests has 0 case(s),
 * valid indexes 0..-1".
 *
 * They are now checkable under `covered_by_unit_test:<n>`, and excluded from
 * the checklist denominator so ticking none of them still reads 100%. Open by
 * default once any of them is checked, so recorded evidence is never hidden
 * behind a collapsed header.
 */
function CoveredByUnitTestsSection({ cases, checkedTests, onToggle }) {
  const checkedCount = cases
    ? cases.reduce((acc, _, i) => acc + (checkedTests.has(`${COVERED_KEY}:${i}`) ? 1 : 0), 0)
    : 0
  const [open, setOpen] = useState(false)
  const openNow = open || checkedCount > 0
  if (!cases || cases.length === 0) return null
  return (
    <section id="sect-covered-by-unit-tests" style={{ marginTop: 'var(--s-8)' }}>
      <header
        onClick={() => setOpen((v) => !v)}
        style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: openNow ? 'var(--s-4)' : 0, cursor: 'pointer' }}
      >
        <Icon name={openNow ? 'chevron-down' : 'chevron-right'} size={14} style={{ color: 'var(--fg-muted)' }} />
        <Icon name="beaker" size={16} style={{ color: 'var(--fg-muted)' }} />
        <h2 style={{ margin: 0, fontSize: 'var(--t-lg)', fontWeight: 600, letterSpacing: '-.005em', color: 'var(--fg-muted)' }}>
          Already covered by unit tests
        </h2>
        <Chip size="sm">{cases.length}</Chip>
        {checkedCount > 0 && (
          <Chip
            size="sm"
            style={{
              background: 'rgba(34,197,94,.12)',
              borderColor: 'rgba(34,197,94,.3)',
              color: 'var(--success)',
            }}
          >
            {checkedCount} also verified in UAT
          </Chip>
        )}
        <span style={{ flex: 1 }} />
        <SectionIdHint prefix={COVERED_KEY} />
        <span style={{ color: 'var(--fg-faint)', fontSize: 'var(--t-xs)' }}>·</span>
        <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>
          Optional · not counted in progress
        </span>
      </header>
      {openNow && (
        <div className="card" style={{ padding: 'var(--s-5) var(--s-6)' }}>
          <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', paddingBottom: 'var(--s-4)' }}>
            QA can skip these — an automated test already asserts them. Tick one
            only if you verified it live anyway; it is recorded like any other
            case and left out of the checklist total. All {cases.length} are
            listed in the Jira comment too, last and marked optional: they used
            to be omitted by default, which left the UAT runner with no id to
            record against when one was verified live anyway.
          </div>
          {cases.map((test, i) => {
            const id = `${COVERED_KEY}:${i}`
            const isChecked = checkedTests.has(id)
            return (
              <div
                key={i}
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 'var(--s-4)',
                  padding: '8px 0',
                  borderBottom: i < cases.length - 1 ? '1px solid var(--divider)' : 'none',
                }}
              >
                <Cbx checked={isChecked} onChange={() => onToggle(COVERED_KEY, i)} />
                <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0, flex: 1 }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
                    <CaseIdBadge id={id} display={String(i)} />
                    <span
                      style={{
                        fontSize: 'var(--t-sm)',
                        color: isChecked ? 'var(--fg-muted)' : 'var(--fg)',
                        textDecoration: isChecked ? 'line-through' : 'none',
                        textDecorationColor: 'var(--fg-faint)',
                      }}
                    >
                      {typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}
                    </span>
                  </div>
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
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

const PR_STATE_LABELS = {
  merged: 'merged',
  open: 'open — not yet merged',
  closed_unmerged: 'closed without merging — not used',
  unknown: 'state unconfirmed',
}

/**
 * What the plan was derived from. Every case in the plan is only as strong as
 * the PR behind it: merged code shipped, an open PR is a proposal, and a
 * closed-unmerged PR is listed here purely so a reader can see it was found
 * and deliberately skipped rather than missed.
 */
function SourceProvenancePanel({ provenance }) {
  const entries = Array.isArray(provenance?.pull_requests) ? provenance.pull_requests : []
  const figmaUnavailable = !!provenance?.figma_unavailable
  const contextGaps = Array.isArray(provenance?.context_gaps) ? provenance.context_gaps : []
  const criticsUnavailable = Array.isArray(provenance?.critics_unavailable)
    ? provenance.critics_unavailable
    : []
  const copyOnly = provenance?.copy_only
  const unreachableVariants = Array.isArray(copyOnly?.unreachable_variants)
    ? copyOnly.unreachable_variants
    : []
  // An unreadable design is worth saying even on a ticket with no PRs — it is
  // the difference between "no visual checks were needed" and "no visual
  // checks could be written".
  if (entries.length === 0 && !figmaUnavailable && contextGaps.length === 0
      && criticsUnavailable.length === 0 && !copyOnly) return null
  const unmerged = !!provenance.grounded_on_unmerged
  return (
    <div
      className="card"
      style={{
        marginBottom: 'var(--s-5)',
        padding: 'var(--s-5) var(--s-6)',
        borderColor: unmerged ? 'rgba(245,158,11,.35)' : undefined,
        background: unmerged ? 'rgba(245,158,11,.05)' : undefined,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-2)', marginBottom: 'var(--s-3)' }}>
        <Icon name="link" size={12} />
        <span style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)' }}>
          Grounded in
        </span>
      </div>
      {unmerged && (
        <div style={{ fontSize: 'var(--t-sm)', color: '#fcd34d', marginBottom: 'var(--s-3)' }}>
          Part of this plan rests on code that has not merged — those cases describe
          proposed behaviour and can drift as the PR changes.
        </div>
      )}
      {contextGaps.length > 0 && (
        <div style={{ fontSize: 'var(--t-sm)', color: '#fcd34d', marginBottom: 'var(--s-3)' }}>
          This ticket links context that could not be read, so nothing here rests on it:
          <ul style={{ margin: 'var(--s-2) 0 0', paddingLeft: 'var(--s-5)' }}>
            {contextGaps.map((g) => <li key={g}>{g}</li>)}
          </ul>
        </div>
      )}
      {criticsUnavailable.length > 0 && (
        <div style={{ fontSize: 'var(--t-sm)', color: '#fcd34d', marginBottom: 'var(--s-3)' }}>
          Some checks that normally run over this plan did not:
          <ul style={{ margin: 'var(--s-2) 0 0', paddingLeft: 'var(--s-5)' }}>
            {criticsUnavailable.map((c) => <li key={c}>{c}</li>)}
          </ul>
          Absence of warnings below does not mean the plan was checked and found clean.
        </div>
      )}
      {copyOnly && (
        <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)', marginBottom: 'var(--s-3)' }}>
          Planned as a <strong>copy-only</strong> change
          {typeof copyOnly.variant_count === 'number' && (
            <> — {copyOnly.variant_count} distinct string variant{copyOnly.variant_count === 1 ? '' : 's'},
            {' '}{copyOnly.manual_cases} manual case{copyOnly.manual_cases === 1 ? '' : 's'} against a budget of {copyOnly.budget}</>
          )}.
          {copyOnly.rationale && (
            <div style={{ marginTop: 'var(--s-1)', color: 'var(--fg-subtle)' }}>{copyOnly.rationale}</div>
          )}
          {copyOnly.within_budget === false && (
            <div style={{ marginTop: 'var(--s-2)', color: '#fcd34d' }}>
              The plan is over its own copy-only budget. Nothing was cut for you — read
              the checklist for cases that re-test one string by a second route, or
              re-test wiring the PR's component tests already cover.
            </div>
          )}
          {unreachableVariants.length > 0 && (
            <div style={{ marginTop: 'var(--s-2)', color: '#fcd34d' }}>
              One account cannot reach every variant. These need a different account:
              <ul style={{ margin: 'var(--s-2) 0 0', paddingLeft: 'var(--s-5)' }}>
                {unreachableVariants.map((v) => <li key={v}>{v}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}
      {figmaUnavailable && (
        <div style={{ fontSize: 'var(--t-sm)', color: '#fcd34d', marginBottom: 'var(--s-3)' }}>
          This ticket links a design that could not be read, so nothing here checks
          the build against the mocks. Open the design yourself before signing off
          on anything visual.
        </div>
      )}
      <ul style={{ margin: 0, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 4 }}>
        {entries.map((e, i) => {
          const name = e.repository && e.number
            ? `${e.repository}#${e.number}`
            : (e.number ? `PR #${e.number}` : (e.url || e.title || 'unidentified PR'))
          const sha = typeof e.head_sha === 'string' && e.head_sha.length >= 7
            ? e.head_sha.slice(0, 7)
            : null
          return (
            <li
              key={i}
              style={{
                fontSize: 'var(--t-sm)',
                color: e.used_as_grounding ? 'var(--fg)' : 'var(--fg-subtle)',
              }}
            >
              {e.url ? (
                <a href={e.url} target="_blank" rel="noreferrer">{name}</a>
              ) : name}
              {' — '}
              {PR_STATE_LABELS[e.state] || e.state || 'unknown'}
              {sha && (
                <code style={{ fontSize: 11, marginLeft: 6, color: 'var(--fg-subtle)' }}>{sha}</code>
              )}
            </li>
          )
        })}
      </ul>
      {entries.some((e) => !e.used_as_grounding) && (
        <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', marginTop: 'var(--s-3)' }}>
          Closed-unmerged pull requests were deliberately not used as source.
          Code that was abandoned cannot be tested.
        </div>
      )}
    </div>
  )
}

/**
 * Cases the pipeline could not trace to any source. They are shown — nothing
 * is silently dropped — but deliberately outside the numbered sections and
 * without checkboxes: the whole point is that they are not gradeable. Before
 * this section existed they sat among the verified cases and testers marked
 * them pass/fail like everything else.
 */
function NeedsSpecSection({ cases }) {
  const [open, setOpen] = useState(false)
  if (!cases || cases.length === 0) return null
  return (
    <section id="sect-needs-spec" style={{ marginTop: 'var(--s-8)' }}>
      <header
        onClick={() => setOpen((v) => !v)}
        style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: open ? 'var(--s-4)' : 0, cursor: 'pointer' }}
      >
        <Icon name={open ? 'chevron-down' : 'chevron-right'} size={14} style={{ color: 'var(--fg-muted)' }} />
        <Icon name="scan" size={16} style={{ color: '#fcd34d' }} />
        <h2 style={{ margin: 0, fontSize: 'var(--t-lg)', fontWeight: 600, letterSpacing: '-.005em', color: 'var(--fg-muted)' }}>
          Needs spec — not verifiable from source
        </h2>
        <Chip size="sm">{cases.length}</Chip>
        <span style={{ flex: 1 }} />
        <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>Not gradeable</span>
      </header>
      {open && (
        <div className="card" style={{ padding: 'var(--s-5) var(--s-6)' }}>
          <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)', marginBottom: 'var(--s-4)' }}>
            Neither the UI these describe nor their expected results could be traced to
            the linked code, so they cannot be marked pass or fail. Confirm the intended
            behaviour, then regenerate.
          </div>
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
              {test.needs_spec_reason && (
                <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
                  {test.needs_spec_reason}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

/**
 * What replaces the plan when nothing grounded it. Names where the bot looked,
 * so a reader can tell a missing PR from a broken Jira-GitHub integration.
 */
function NoSourcePanel({ plan }) {
  const searched = Array.isArray(plan.searched) ? plan.searched : []
  return (
    <div style={{ marginTop: 'var(--s-6)' }}>
      <div
        className="card"
        style={{
          padding: 'var(--s-6)',
          borderColor: 'rgba(245,158,11,.35)',
          background: 'rgba(245,158,11,.05)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-2)', marginBottom: 'var(--s-3)' }}>
          <Icon name="alert" size={14} />
          <span style={{ fontSize: 'var(--t-md)', fontWeight: 600, color: '#fcd34d' }}>
            No implementation found — no plan generated
          </span>
        </div>
        <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)', marginBottom: 'var(--s-4)' }}>
          {plan.no_source_message}
        </div>
        {searched.length > 0 && (
          <>
            <div style={{ fontSize: 'var(--t-xs)', fontWeight: 600, color: 'var(--fg-muted)', marginBottom: 4 }}>
              Searched
            </div>
            <ul style={{ margin: 0, paddingLeft: 18, display: 'flex', flexDirection: 'column', gap: 2 }}>
              {searched.map((item, i) => (
                <li key={i} style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>{item}</li>
              ))}
            </ul>
          </>
        )}
      </div>
      <SourceProvenancePanel provenance={plan.source_provenance} />
    </div>
  )
}

/**
 * What is, and isn't, being recorded about this plan's QA progress.
 *
 * "0%" is the most expensive thing this view can render wrongly: it reads as
 * "nobody has tested this", and it looked identical whether the plan genuinely
 * had no marks, the key was pointing somewhere nothing writes, or eleven passed
 * cases were sitting under the previous plan's shape. Each of those now says
 * which one it is.
 */
function ProgressTrackingNotice({ status, shapeDrift, storedFingerprint, shownFingerprint, orphans, onCarryOver }) {
  if (shapeDrift) {
    return (
      <Alert tone="danger" title="The plan on screen isn't the stored plan">
        Progress is recorded against the stored plan ({storedFingerprint} cases per
        section), but this view is showing {shownFingerprint}. Ticking a box here
        would land on a different case. Reload the ticket to get the stored plan
        back, or regenerate to replace it.
      </Alert>
    )
  }
  if (status === 'untracked') {
    return (
      <Alert tone="warning" title="Progress isn't being shared">
        This plan has no stored run, so it has no progress key. Checks are kept in
        this browser only — nobody else sees them, and the UAT runner can't write
        to them. Regenerate the plan, or adopt it from its Jira comment, to start
        tracking.
      </Alert>
    )
  }
  if (status === 'unreachable') {
    return (
      <Alert tone="warning" title="Couldn't reach the server for this plan's progress key">
        Checks are kept in this browser until it answers. They are deliberately
        not being written under a guessed key.
      </Alert>
    )
  }
  if (orphans && orphans.length > 0) {
    const total = orphans.reduce((n, o) => n + o.checked_count, 0)
    const newest = orphans[0]
    const when = newest.updated_at ? new Date(newest.updated_at).toLocaleString() : 'an earlier run'
    return (
      <Alert tone="info" title="Progress exists under a previous plan shape">
        {total} case{total === 1 ? ' was' : 's were'} marked against an earlier
        version of this plan ({orphans.map((o) => o.fingerprint).join(', ')}; last
        updated {when}). Progress is keyed to a plan's section sizes, so
        regenerating starts it over — those marks are still recorded, they just
        don't map onto the cases below. This plan's own checklist is empty, not
        untested.
        {onCarryOver && (
          <div style={{ marginTop: 'var(--s-4)' }}>
            <Btn variant="ghost" icon="history" onClick={onCarryOver}>
              Review and carry over
            </Btn>
          </div>
        )}
      </Alert>
    )
  }
  return null
}


function TestPlanDisplay({ testPlan, ticketData, ticketsData, onPosted }) {
  const isMulti = !!(ticketsData && ticketsData.length > 1)

  const allKeys = isMulti ? ticketsData.map((t) => t.key) : []
  const [selectedKeys, setSelectedKeys] = useState(() => new Set(allKeys))
  const [postingStates, setPostingStates] = useState({})

  // Pull cases the planner flagged as already covered by a unit test out of the
  // manual QA sections into their own list, so the checklist QA actually runs
  // stays lean. `displayPlan` drives the four sections and the progress total;
  // checkbox indices there are relative to this filtered view. The lifted cases
  // are not discarded — they keep their own namespace (`covered_by_unit_test:n`,
  // in exactly this flat order) so evidence for one is recordable. Export/post
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

  const [isPosting, setIsPosting] = useState(false)

  // Walkthrough (Loom / screenshot / notes) is authored inline in the
  // Pass-to-UAT form (WorkflowActions). Here it's read-only — fetched purely
  // so the export/Jira formatters can embed it into the posted plan.
  const walkthroughKey = ticketData?.key || (ticketsData && ticketsData[0]?.key) || ''
  const { walkthrough } = useTicketWalkthrough(walkthroughKey)

  const ticketKeysJoined = isMulti
    ? allKeys.join('+')
    : ticketData?.key || ''
  // ---------------------------------------------------------------------
  // The progress key. Asked for, never derived — see `localFingerprint`.
  //
  // `pending`     the key request is in flight
  // `ready`       the server gave us the canonical key; progress is shared
  // `untracked`   this plan has no stored run, so it has no canonical key
  // `unreachable` the server didn't answer
  // ---------------------------------------------------------------------
  const planId = testPlan?.plan_id ?? null
  const [keyState, setKeyState] = useState({ status: 'pending', key: null, fingerprint: null })

  useEffect(() => {
    const untracked = { status: 'untracked', key: null, fingerprint: null }
    if (!ticketKeysJoined || !planId) {
      setKeyState(untracked)
      return
    }
    let cancelled = false
    setKeyState({ status: 'pending', key: null, fingerprint: null })
    fetch(`${API_BASE}/plans/${planId}/progress-key`)
      .then(async (r) => {
        if (cancelled) return
        // A plan the database lost, or one whose run went missing. Either way
        // there is no canonical key to write under.
        if (r.status === 404) {
          setKeyState(untracked)
          return
        }
        if (!r.ok) throw new Error(`progress-key ${r.status}`)
        const data = await r.json()
        if (cancelled) return
        rememberProgressKey(planId, data.progress_key)
        setKeyState({
          status: 'ready',
          key: data.progress_key,
          fingerprint: data.fingerprint ?? null,
        })
      })
      .catch(() => {
        if (cancelled) return
        // Never fall back to a locally-derived key: a wrong key writes QA
        // results somewhere nothing reads, which is the failure this whole
        // path exists to prevent. The last answer the server gave for *this
        // plan* is not a derivation, so reuse it — that keeps the offline
        // checks under the key they'll sync from, rather than stranding them
        // under a second one. With no cached answer, say the progress isn't
        // being tracked.
        const remembered = recallProgressKey(planId)
        setKeyState(
          remembered
            ? { status: 'ready', key: remembered, fingerprint: null }
            : { status: 'unreachable', key: null, fingerprint: null }
        )
      })
    return () => {
      cancelled = true
    }
  }, [planId, ticketKeysJoined])

  // Only a `ready` key may touch the server.
  const serverKey = keyState.status === 'ready' ? keyState.key : null

  // The stored plan and the plan on screen disagree about their shape. The key
  // still follows the stored plan (one producer), but checkbox indices are
  // relative to what is rendered, so the two no longer line up and the tester
  // has to know before ticking anything.
  const shapeDrift =
    keyState.status === 'ready' &&
    keyState.fingerprint !== null &&
    keyState.fingerprint !== localFingerprint(displayPlan, coveredCases.length)

  // localStorage mirror. Follows the canonical key when there is one; otherwise
  // a `local:` key that is never sent anywhere, so an unrecorded plan still has
  // working checkboxes without inventing a server key nothing will read.
  const storageKey = useMemo(() => {
    if (serverKey) return `${PROGRESS_STORAGE_PREFIX}${serverKey}`
    if (!ticketKeysJoined || keyState.status === 'pending') return null
    return `${PROGRESS_STORAGE_PREFIX}local:${ticketKeysJoined}:${localFingerprint(displayPlan, coveredCases.length)}`
  }, [serverKey, keyState.status, ticketKeysJoined, displayPlan, coveredCases.length])

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

  // Which key `checkedTests` was loaded for. The canonical key now arrives
  // asynchronously, so `storageKey` changes mid-session (null while the key
  // request is in flight, then the real key). Without this the mirror effect
  // below would fire on the new key while `checkedTests` still held the old
  // key's value — writing one key's checks under another, or, when the old
  // value was the empty starting set, deleting the cache the loader had just
  // read. The mirror waits until the two agree.
  const hydratedForKey = useRef(null)

  useEffect(() => {
    if (!storageKey || typeof window === 'undefined') {
      hydratedForKey.current = storageKey
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
    hydratedForKey.current = storageKey
    setCheckedTests(local)
  }, [storageKey])

  useEffect(() => {
    // Authoritative: the shared per-ticket progress lives on the server, so the
    // whole QA team converges on the same checked set. Falls back to the local
    // optimistic state if the server is unreachable. Without a canonical key
    // there is nothing to sync with — the local mirror above is all there is.
    if (!serverKey) return
    let cancelled = false
    let lastUpdatedAt = null

    const syncFromServer = () => {
      // Skip while a local edit is still queued or mid-PUT — otherwise the poll
      // could land with pre-save data and undo the checkbox the user just toggled.
      if (progressSaveTimer.current) return
      fetch(`${API_BASE}/test-plan-progress/${encodeURIComponent(serverKey)}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (cancelled || !data || !Array.isArray(data.checked_ids)) return
          if (data.updated_at && data.updated_at === lastUpdatedAt) return
          lastUpdatedAt = data.updated_at || lastUpdatedAt
          setCheckedTests(new Set(data.checked_ids))
        })
        .catch(() => {
          /* offline / server down — keep the local optimistic state */
        })
    }

    syncFromServer()
    // Poll so external writers (the UAT runner script marking cases as it goes)
    // show up without a manual page refresh. Paused when the tab is hidden.
    const pollId = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      syncFromServer()
    }, 3000)

    return () => {
      cancelled = true
      clearInterval(pollId)
    }
  }, [serverKey])

  // Progress recorded for this ticket under a *different* plan shape.
  //
  // A 404 on the current key renders exactly like a plan nobody has touched —
  // 0%, every box empty. After a regeneration that is usually wrong: the marks
  // exist, filed under the shape they were made against, because the key
  // encodes section sizes. Asked once per key; the server never migrates
  // anything, since which checks still apply to a changed plan is a judgement
  // only a tester can make.
  const [orphanedProgress, setOrphanedProgress] = useState(null)
  const [carryOverOpen, setCarryOverOpen] = useState(false)
  useEffect(() => {
    setOrphanedProgress(null)
    if (!serverKey) return
    let cancelled = false
    const encoded = encodeURIComponent(serverKey)
    fetch(`${API_BASE}/test-plan-progress/${encoded}`)
      .then((r) =>
        r.status === 404
          ? fetch(`${API_BASE}/test-plan-progress/${encoded}/other-shapes`)
          : null
      )
      .then((r) => (r && r.ok ? r.json() : null))
      .then((data) => {
        if (cancelled || !data) return
        const others = (data.others || []).filter((o) => o.checked_count > 0)
        if (others.length > 0) setOrphanedProgress(others)
      })
      .catch(() => {
        /* offline — the notice is an extra, not a requirement */
      })
    return () => {
      cancelled = true
    }
  }, [serverKey])

  // Mirror every change to localStorage as an offline cache + optimistic source
  // for the next load. The server remains the shared source of truth.
  useEffect(() => {
    if (!storageKey || typeof window === 'undefined') return
    if (hydratedForKey.current !== storageKey) return
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
    // No canonical key means no shared write. Deriving one here to have
    // something to PUT is exactly how progress ends up under a key nothing
    // reads; the checks stay in this browser and the notice says so.
    if (!serverKey || typeof window === 'undefined') return
    const payload = [...nextSet]
    if (progressSaveTimer.current) clearTimeout(progressSaveTimer.current)
    progressSaveTimer.current = setTimeout(() => {
      fetch(`${API_BASE}/test-plan-progress/${encodeURIComponent(serverKey)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ checked_ids: payload }),
      })
        .catch(() => {
          /* offline — localStorage holds it; resyncs on the next successful save */
        })
        .finally(() => {
          // Clear so the poll loop knows no local write is in flight anymore.
          progressSaveTimer.current = null
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
  // Per-ticket post outcomes, so the batch summary can name what actually
  // happened to each ticket instead of collapsing everything into "posted".
  const postOutcomesRef = useRef({})

  // Success clears fast — it only has to confirm. A failure or a truncated post
  // is something the tester has to act on, so it stays up long enough to read
  // and (for truncation) to register that the Jira comment is not the whole plan.
  const NOTIFICATION_MS = { success: 3000, error: 8000, warning: 12000 }

  const showNotification = (setter, timerRef, type, message) => {
    if (timerRef.current) clearTimeout(timerRef.current)
    setter({ type, message })
    timerRef.current = setTimeout(() => setter(null), NOTIFICATION_MS[type] ?? 6000)
  }

  // A plan too big for one Jira comment is now split across consecutive
  // comments instead of being cut off. That is a success, but the tester still
  // has to know the plan is spread over several comments — and a post that
  // stopped partway has to say how far it got rather than read as clean.
  const describePostResult = (result, target, action) => {
    const parts = result.parts ?? 1
    const posted = result.posted_parts ?? parts
    const spread = parts > 1 ? ` across ${parts} comments` : ''
    // The comment can land while the app fails to record it as the live
    // version. That is not an alternative to the outcomes below — a post can
    // be partial *and* unrecorded — so it reads as a suffix on whichever one
    // applies rather than a branch competing with them. `recorded` is null
    // when there was no stored plan to record, which UntrackedPlanNotice
    // already explains; only an explicit false is worth saying here.
    const unrecorded = result.recorded === false
    const recordNote = unrecorded
      ? ` It was not recorded as the live version${
          result.record_error ? ` (${result.record_error})` : ''
        } — the version badge will still read "Not live in Jira", and posting again will not change that until the cause is fixed.`
      : ''
    if (posted < parts) {
      const why = result.part_error ? ` — ${result.part_error}` : ''
      return {
        type: 'warning',
        message: `Test plan ${action} on ${target}, but only ${posted} of ${parts} comments landed${why}. The complete plan is here in the app.${recordNote}`,
        state: 'partial',
      }
    }
    if (result.truncated) {
      return {
        type: 'warning',
        message: `Test plan ${action} on ${target}${spread}, but it was still too long for Jira and got cut short. The complete plan is here in the app.${recordNote}`,
        state: 'truncated',
      }
    }
    if (result.stale_parts_left > 0) {
      return {
        type: 'warning',
        message: `Test plan ${action} on ${target}${spread}, but ${result.stale_parts_left} comment(s) from the previous plan could not be removed — the ticket still shows cases this plan dropped.${recordNote}`,
        state: 'stale',
      }
    }
    if (unrecorded) {
      return {
        type: 'warning',
        message: `Test plan ${action} on ${target}${spread}.${recordNote}`,
        state: 'unrecorded',
      }
    }
    return {
      type: 'success',
      message: `Test plan ${action} on ${target}${spread}`,
      state: parts > 1 ? 'split' : 'done',
    }
  }

  if (!testPlan) return null

  const primaryTicketData = ticketData || (ticketsData && ticketsData[0])

  const planHasAcs = ['happy_path', 'edge_cases', 'integration_tests', 'security_negative_tests'].some((key) => {
    const items = displayPlan[key]
    if (!Array.isArray(items)) return false
    return items.some(
      (t) =>
        Array.isArray(t?.covers_acs) &&
        t.covers_acs.some((id) => typeof id === 'string' && id.trim())
    )
  })
  // Ticket keys drive AC-id shortening on both the coverage panel and
  // per-test-case chips: a single-ticket plan drops the redundant
  // "SK-XXXX-" prefix so "SK-2585-AC1" reads as just "AC1".
  const ticketKeys = testPlan.ac_coverage?.tickets
    ? new Set(Object.keys(testPlan.ac_coverage.tickets))
    : undefined

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

  const handlePostToJira = async () => {
    setIsPosting(true)
    try {
      const jiraText = formatTestPlanAsJira(testPlan, walkthrough)
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
      if (onPosted) onPosted({ ticketKey: ticketData.key, planId: testPlan?.plan_id ?? null })
      const outcome = describePostResult(result, ticketData.key, action)
      showNotification(setPostNotification, postTimerRef, outcome.type, outcome.message)
    } catch (error) {
      showNotification(setPostNotification, postTimerRef, 'error', error.message)
    } finally {
      setIsPosting(false)
    }
  }

  const postToKey = async (issueKey, otherKeys = []) => {
    setPostingStates((prev) => ({ ...prev, [issueKey]: 'posting' }))
    try {
      let jiraText = formatTestPlanAsJira(testPlan, walkthrough)
      if (otherKeys.length > 0) {
        jiraText += `\n\n----\n_Also posted to: ${otherKeys.join(', ')}_`
      }
      const response = await fetch(`${API_BASE}/jira/post-comment`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          issue_key: issueKey,
          comment_text: jiraText,
          plan_id: testPlan?.plan_id ?? null,
        }),
      })

      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.detail || 'Failed to post to Jira')
      }

      const result = await response.json()
      const outcome = describePostResult(result, issueKey, result.updated ? 'updated' : 'posted')
      postOutcomesRef.current[issueKey] = outcome
      setPostingStates((prev) => ({ ...prev, [issueKey]: outcome.state }))
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
      if (anyError) {
        // Per-ticket errors already raised their own notification in postToKey.
        return prev
      }
      const flagged = keys.filter((k) => postOutcomesRef.current[k]?.type === 'warning')
      if (flagged.length > 0) {
        // One line per flagged ticket: "cut short" and "only 1 of 3 landed" are
        // different problems and a lumped summary hides which ticket has which.
        const detail = flagged
          .map((k) => postOutcomesRef.current[k].message)
          .join(' ')
        showNotification(
          setPostNotification,
          postTimerRef,
          'warning',
          `Posted to ${keys.join(', ')}. ${detail}`
        )
      } else {
        const split = keys.filter((k) => prev[k] === 'split')
        showNotification(
          setPostNotification,
          postTimerRef,
          'success',
          split.length > 0
            ? `Posted to ${keys.join(', ')} — ${split.join(', ')} needed more than one Jira comment.`
            : `Posted to ${keys.join(', ')}`
        )
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

  // No source, no plan. Rendering the usual empty scaffolding here would read
  // as "the bot produced nothing", which is the wrong story: it found no
  // implementation and declined to guess. Placed after every hook so the
  // early return doesn't change hook order.
  if (testPlan?.no_source) {
    return <NoSourcePanel plan={testPlan} />
  }

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
                Test plan
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
            <div className="tp-section-chips" style={{ display: 'flex', gap: 6 }}>
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
                // The button has to carry every outcome, not just success. It is
                // the post control most plans go through, and the only other
                // report lives at the bottom of the page, where a reader scrolled
                // anywhere else never sees it.
                const postTone = postNotification?.type
                const postIcon =
                  { success: 'check', warning: 'alert', error: 'alert-circle' }[postTone] || 'send'
                const postLabel = postNotification
                  ? postNotification.message
                  : isMulti
                    ? `Post to selected (${selectedKeys.size})`
                    : 'Post to Jira'
                // Said here because this is now the only Post control most
                // plans render — the bar at the foot of the page that used to
                // carry the sentence is gone for the single-ticket case.
                const postHint = postNotification
                  ? null
                  : 'Updates the existing bot comment instead of duplicating.'
                const postStyle =
                  postTone === 'error'
                    ? { ...base, background: 'var(--danger-soft)', borderColor: 'rgba(239,68,68,.5)', color: 'var(--danger)' }
                    : postTone === 'warning'
                      ? { ...base, background: 'var(--warning-soft)', borderColor: 'rgba(245,158,11,.5)', color: 'var(--warning)' }
                      : primary
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
                          ...postStyle,
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
                          <Icon name={postIcon} size={12} />
                        )}
                      </button>
                      <span className="tip-body">
                        {postLabel}
                        {postHint && (
                          <span style={{ display: 'block', marginTop: 3, color: 'var(--fg-subtle)' }}>
                            {postHint}
                          </span>
                        )}
                      </span>
                    </span>
                  </>
                )
              })()}
            </div>
          </div>

          {/* Post result, reported where the post button is. This row lives
              inside the sticky container on purpose: the bar follows the
              viewport, so anything rendered below it in normal flow is off
              screen for a reader who is not at the bottom of the plan. */}
          {postNotification && (
            <div
              role="status"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 'var(--s-3)',
                marginTop: 8,
                padding: '6px 10px',
                borderRadius: 'var(--r-md)',
                border: '1px solid',
                fontSize: 'var(--t-xs)',
                fontWeight: 500,
                ...(postNotification.type === 'error'
                  ? { background: 'var(--danger-soft)', borderColor: 'rgba(239,68,68,.4)', color: 'var(--danger)' }
                  : postNotification.type === 'warning'
                    ? { background: 'var(--warning-soft)', borderColor: 'rgba(245,158,11,.35)', color: 'var(--warning)' }
                    : { background: 'var(--success-soft)', borderColor: 'rgba(34,197,94,.35)', color: 'var(--success)' }),
              }}
            >
              <Icon
                name={
                  postNotification.type === 'error'
                    ? 'alert-circle'
                    : postNotification.type === 'warning'
                      ? 'alert'
                      : 'check-circle'
                }
                size={13}
                style={{ flexShrink: 0 }}
              />
              <span>
                {postNotification.type === 'error' && (
                  <strong style={{ fontWeight: 600 }}>Post failed — </strong>
                )}
                {postNotification.message}
              </span>
            </div>
          )}
        </div>
      )}

      <ProgressTrackingNotice
        status={keyState.status}
        shapeDrift={shapeDrift}
        storedFingerprint={keyState.fingerprint}
        shownFingerprint={localFingerprint(displayPlan, coveredCases.length)}
        orphans={orphanedProgress}
        onCarryOver={planId ? () => setCarryOverOpen(true) : null}
      />

      {carryOverOpen && planId && (
        <MarkCarryOverModal
          planId={planId}
          onClose={() => setCarryOverOpen(false)}
          onCarried={(ids) => {
            // The server has the authoritative set now. Adopt it wholesale
            // rather than merging locally — a second producer of this set is
            // how checks end up recorded where nothing renders them.
            setCheckedTests(new Set(ids))
            setOrphanedProgress(null)
          }}
        />
      )}

      <SourceProvenancePanel provenance={testPlan.source_provenance} />

      {testPlan.ac_coverage && (
        <AcCoveragePanel coverage={testPlan.ac_coverage} />
      )}

      <GroundingWarningsPanel
        warnings={testPlan.grounding_warnings}
        ticketKeys={ticketKeys}
      />

      <RisksAndGapsPanel gaps={testPlan.risks_and_gaps} />

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
            ticketKeys={ticketKeys}
          />
        )
      })}

      <NeedsSpecSection cases={testPlan.needs_spec_cases} />

      <CoveredByUnitTestsSection
        cases={coveredCases}
        checkedTests={checkedTests}
        onToggle={toggleTest}
      />

      {/* Export & post bar.

          It used to render for every plan, and for the ordinary single-ticket
          one it was three buttons the sticky bar already carries — duplicated
          at the foot of a page you have to scroll to reach. What is left here
          is what the sticky bar cannot show: the per-ticket selector a
          multi-ticket post is driven from, and a full-size set of controls for
          a plan with no cases, which renders no sticky bar at all. */}
      {(isMulti || totalAll === 0) && (
      <div className="card" style={{ marginTop: 'var(--s-9)', padding: 'var(--s-6)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-5)', flexWrap: 'wrap' }}>
          <Icon name="upload" size={14} style={{ color: 'var(--fg-muted)' }} />
          <div style={{ flex: 1, minWidth: 200 }}>
            <div style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)' }}>Export this plan</div>
            <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
              Posting to Jira updates the existing bot comment instead of duplicating.
            </div>
          </div>
          {totalAll === 0 && (
            <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
              <Btn variant="ghost" icon="copy" onClick={handleCopyMarkdown}>
                {copyNotification?.type === 'success' ? `✓ ${copyNotification.message}` : 'Copy markdown'}
              </Btn>
              <Btn variant="ghost" icon="download" onClick={handleDownloadMarkdown}>
                Download .md
              </Btn>
              {!isMulti && (
                <>
                  <span style={{ width: 1, height: 18, background: 'var(--line-strong)', alignSelf: 'center', margin: '0 4px' }} />
                  <Btn
                    variant="primary"
                    icon="send"
                    onClick={handlePostToJira}
                    disabled={isPosting}
                    loading={isPosting}
                  >
                    {isPosting
                      ? 'Posting…'
                      : postNotification?.type === 'success'
                      ? `✓ ${postNotification.message}`
                      : 'Post to Jira'}
                  </Btn>
                </>
              )}
            </div>
          )}
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
                      onClick={() =>
                        !isAnyPosting &&
                        !POSTED_STATES.has(state) &&
                        toggleKeySelection(td.key)
                      }
                    />
                    <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-sm)', color: 'var(--fg)' }}>{td.key}</span>
                    {state === 'posting' && <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>Posting…</span>}
                    {state === 'done' && <Chip size="sm" dot dotColor="var(--success)">Posted</Chip>}
                    {state === 'split' && (
                      <span className="tip">
                        <Chip size="sm" dot dotColor="var(--success)">Posted in parts</Chip>
                        <span className="tip-body">
                          Too long for one Jira comment — the whole plan is on the ticket, split across consecutive comments.
                        </span>
                      </span>
                    )}
                    {state === 'truncated' && (
                      <span className="tip">
                        <Chip size="sm" dot dotColor="var(--warning)">Posted, truncated</Chip>
                        <span className="tip-body">
                          Too long even split across several Jira comments — the last one was cut short. The complete plan is here in the app.
                        </span>
                      </span>
                    )}
                    {state === 'partial' && (
                      <span className="tip">
                        <Chip size="sm" dot dotColor="var(--warning)">Posted in part</Chip>
                        <span className="tip-body">
                          Posting stopped partway: some of the plan is on the ticket and some is not. The complete plan is here in the app.
                        </span>
                      </span>
                    )}
                    {state === 'stale' && (
                      <span className="tip">
                        <Chip size="sm" dot dotColor="var(--warning)">Posted, stale parts</Chip>
                        <span className="tip-body">
                          The new plan is posted, but comments from the previous plan could not be removed — the ticket still shows cases this plan dropped.
                        </span>
                      </span>
                    )}
                    {state === 'unrecorded' && (
                      <span className="tip">
                        <Chip size="sm" dot dotColor="var(--warning)">Posted, not recorded</Chip>
                        <span className="tip-body">
                          The plan is on the ticket, but the app could not record it as the live version — the version badge will still read "Not live in Jira". Posting again will not change that until the cause is fixed.
                        </span>
                      </span>
                    )}
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

        {/* Fallback report for a plan with no test cases, which renders no
            sticky bar and so has no status strip to report into. Every other
            plan gets the strip, which stays on screen wherever the reader is. */}
        {totalAll === 0 && postNotification && postNotification.type !== 'success' && (
          <div style={{ marginTop: 'var(--s-4)' }}>
            <Alert
              tone={postNotification.type === 'error' ? 'danger' : 'warning'}
              title={postNotification.type === 'error' ? 'Post failed' : 'Posted, with a caveat'}
            >
              {postNotification.message}
            </Alert>
          </div>
        )}
      </div>
      )}
    </div>
  )
}

export default TestPlanDisplay
