/**
 * Rules Q–Z as a reader sees them: the build check and shared setup written
 * once at the top, "how to verify" on each case, and every downgrade the
 * re-read made listed in the "Grounding re-read" line.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { formatTestPlanAsMarkdown, formatTestPlanAsJira, groundingRereadLine } from './markdown.js'

const TICKET = { key: 'SK-1', summary: 'Clear documents on sign-out' }

const plan = {
  happy_path: [{
    title: 'Sign-out clears the document cache',
    priority: 'high',
    preconditions: 'Precondition: Build check passed for docs-android (see Build check at the top of the plan). Precondition: S1 passed (top of plan).',
    steps: ['Sign out'],
    expected: 'The documents directory is empty.',
    how_to_verify: '`adb shell run-as com.acme.docs.debug ls files/documents` lists nothing.',
  }],
  edge_cases: [],
  regression_checklist: [],
  build_check: { step: 'Build check: acme/docs-android (Android app) — confirm the installed build contains merge commit abc1234.', summary: 'the installed docs-android app is built from a commit containing abc1234' },
  shared_preconditions: [{ id: 'S1', text: 'Signed in as the agent test account with two documents downloaded.' }],
  source_provenance: {
    grounding_verification: {
      checked: 5, downgraded: 1, unchecked: 0,
      downgraded_claims: [{ case_id: 'happy_path:0', title: 'Sign-out clears the document cache', kind: 'expected', reason: 'the cited line does not say it' }],
    },
    plan_recheck: {
      findings: [
        { section: 'edge_cases', title: 'Missing file shows a blank screen', rule: 'Q', action: 'downgraded', reason: 'expected result contradicts the traced error path: an error alert shows' },
        { section: 'happy_path', title: 'Theme toggles', rule: 'U', action: 'moved', reason: 'graded case on untouched code' },
        { section: 'shared_preconditions', title: 'Signed in…', rule: 'Z', action: 'fixed', reason: 'written once' },
      ],
    },
  },
}

test('the build check and shared setup are written once, at the top', () => {
  const md = formatTestPlanAsMarkdown(plan, TICKET)
  assert.match(md, /## ✅ Before Any Case \(do these once\)/)
  assert.match(md, /\*\*Build check\*\* — Build check: acme\/docs-android/)
  assert.match(md, /\*\*S1\*\* — Signed in as the agent test account/)
  assert.equal(md.split('confirm the installed build contains merge commit abc1234').length, 2, 'once only')
  const jira = formatTestPlanAsJira(plan, TICKET)
  assert.match(jira, /✅ BEFORE ANY CASE \(DO THESE ONCE\)/)
})

test('each case says how to verify it', () => {
  assert.match(formatTestPlanAsMarkdown(plan, TICKET), /\*\*How to verify:\*\* `adb shell run-as com\.acme\.docs\.debug/)
  assert.match(formatTestPlanAsJira(plan, TICKET), /How to verify: `adb shell run-as/)
})

test('the grounding re-read line lists every downgrade, not only a count', () => {
  const line = groundingRereadLine(plan)
  assert.match(line, /^Grounding re-read: 5 code claim\(s\) re-read against the cited files, 1 downgraded to unverified\./)
  assert.match(line, /Re-check against the code \(rules Q–AG\): 1 downgraded or marked, 1 moved out of the graded cases, 1 fixed in place\./)
  assert.match(line, /\(1\) “Sign-out clears the document cache” — expected: the cited line does not say it/)
  assert.match(line, /\(2\) “Missing file shows a blank screen” — downgraded: expected result contradicts the traced error path: an error alert shows \(rule Q\)/)
  assert.match(line, /\(3\) “Theme toggles” — moved out of the graded cases: graded case on untouched code \(rule U\)/)
  assert.doesNotMatch(line, /written once/, 'fixes are counted, not listed as downgrades')
})

test('a re-check that could not run says so', () => {
  const line = groundingRereadLine({ source_provenance: { plan_recheck: { findings: [], not_run: ['the error-path re-check could not run (RuntimeError)'] } } })
  assert.match(line, /⚠️ Not checked: the error-path re-check could not run/)
})

test('rule Q first rejecting check: the per-row trace is shown, and its downgrade is listed', () => {
  const traced = {
    happy_path: [],
    edge_cases: [{
      title: 'Outer copies are rejected', priority: 'high', steps: ['Repeat with method PUT'],
      expected: "Row 2 (PUT): \"HTTP method 'PUT' is not allowed\".", how_to_verify: 'Jest output',
      rejection_traces: [{ input: 'outer method PUT', traced_path: 'handler.js:2 → method-policy.js:3', first_rejecting_check: 'method-policy.js:3', message: "HTTP method 'PUT' is not allowed" }],
    }],
    regression_checklist: [],
    source_provenance: { plan_recheck: { findings: [{ section: 'edge_cases', title: 'IP literals', rule: 'Q', action: 'downgraded', reason: 'exact error text without a traced first rejecting check: [::1] is rejected first by url-encoding.js:3' }] } },
  }
  const md = formatTestPlanAsMarkdown(traced, TICKET)
  assert.match(md, /\*\*First rejecting check, traced from the entry point:\*\*\n- outer method PUT: "HTTP method 'PUT' is not allowed" at `method-policy\.js:3` \(handler\.js:2 → method-policy\.js:3\)/)
  assert.match(formatTestPlanAsJira(traced, TICKET), /First rejecting check, traced from the entry point:\n- outer method PUT/)
  assert.match(groundingRereadLine(traced), /“IP literals” — downgraded: exact error text without a traced first rejecting check: \[::1\] is rejected first by url-encoding\.js:3 \(rule Q\)/)
})
