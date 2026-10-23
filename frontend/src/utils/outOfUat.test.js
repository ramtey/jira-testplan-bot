/**
 * Plan shape v2: what a tester cannot grade is shown, but never as a case.
 *
 * From SK-2889 (plan 2): two cases the plan called unreachable and one it
 * called pre-existing were numbered beside runnable cases, and every
 * regression line read as an unverified guess. The generator now moves them
 * to `code_review_checks`, `pre_existing_behaviour` and
 * `regression_low_confidence` (src/app/case_disposition.py). These tests pin
 * that both surfaces a tester reads — the Jira comment and the markdown copy —
 * show those lists, with no case id, and that a shape-1 plan renders as before.
 *
 * Run with `npm test`.
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { formatTestPlanAsMarkdown, formatTestPlanAsJira } from './markdown.js'

const TICKET = { key: 'SK-2889', summary: 'Complete the move to the UIScene lifecycle' }

const base = {
  happy_path: [{ title: 'Cold-start deep link lands on the right tab', priority: 'critical',
    steps: ['Open the link'], expected: 'Lands on the tab.' }],
  edge_cases: [],
  regression_checklist: ['🔴 Sign-in still completes (Acme/Classes/AppDelegate.swift:13).'],
}

const v2 = {
  ...base,
  plan_shape: 2,
  code_review_checks: [{
    title: 'Sheet requested while the presenter is not yet in a window',
    origin_section: 'edge_cases',
    why_not_uat: 'Every presenter is on screen when its button is tapped.',
    what_to_verify: 'The height falls back to the screen when no window is passed.',
    suggested_test: 'Add a unit test for SSCollectionViewController that passes a nil window.',
    code_refs: ['Acme/Classes/ViewControllers/RootControllers/SSCollectionViewController.swift:80-106'],
  }],
  pre_existing_behaviour: [{
    title: 'Split editor max height does not update while open',
    origin_section: 'edge_cases',
    note: 'EditSplitView assigns the ratio in onAppear only.',
    code_refs: [],
  }],
  regression_low_confidence: [{ line: '🟢 Orientation behaviour is unchanged.', why: 'cites no file' }],
}

test('the markdown copy lists the three sections without case ids', () => {
  const md = formatTestPlanAsMarkdown(v2, TICKET)
  assert.match(md, /## 🔍 Verify by Code Review \/ Unit Test \(1\)/)
  assert.match(md, /Suggested test: Add a unit test for SSCollectionViewController/)
  assert.match(md, /## 📎 Pre-existing Behaviour — Notes \(1\)/)
  assert.match(md, /## 🌫️ Low-confidence Regression Ideas \(1\)/)
  assert.doesNotMatch(md, /code_review_checks:0|pre_existing_behaviour:0|regression_low_confidence:0/)
})

test('the Jira comment lists them too', () => {
  const jira = formatTestPlanAsJira(v2)
  assert.match(jira, /VERIFY BY CODE REVIEW \/ UNIT TEST \(1\)/)
  assert.match(jira, /Why not UAT: Every presenter is on screen/)
  assert.match(jira, /PRE-EXISTING BEHAVIOUR — NOTES \(1\)/)
  assert.match(jira, /LOW-CONFIDENCE REGRESSION IDEAS \(1\)/)
})

test('a shape-1 plan renders no new sections', () => {
  const md = formatTestPlanAsMarkdown(base, TICKET)
  const jira = formatTestPlanAsJira(base)
  for (const out of [md, jira]) {
    assert.doesNotMatch(out, /CODE REVIEW|Code Review|PRE-EXISTING|Pre-existing Behaviour|LOW-CONFIDENCE|Low-confidence/)
  }
})
