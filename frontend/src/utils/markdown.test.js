/**
 * What a case's data ask looks like once it reaches a reader.
 *
 * From SK-2325: three cases asked Engineering to "supply a property" for shapes
 * live provider data cannot produce — one excluded by the importer's
 * latest-list-date selector, two absent from ~86 sampled parcels. The fix is
 * that the plan now carries the *route* ("someone finds a record" vs "someone
 * mocks a payload" vs "nothing can produce this"), and these tests pin that the
 * route survives into both surfaces a tester actually reads: the Jira comment
 * and the markdown copy.
 *
 * Run with `npm test` (node's built-in runner; no dependencies).
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { formatTestPlanAsMarkdown, formatTestPlanAsJira } from './markdown.js'

const TICKET = { key: 'SK-2325', summary: 'Import acreage from the assessment' }

const planWith = (overrides) => ({
  happy_path: [{
    title: 'Import a parcel with a non-numeric acreage',
    priority: 'high',
    steps: ['Run an import'],
    expected: 'The importer keeps the previous value',
    ...overrides,
  }],
  edge_cases: [],
  regression_checklist: [],
})

const STUB_SHAPE = {
  shape: 'An import payload whose acreage field holds a non-numeric string',
  provisioning: 'stub',
  evidence: 'No such value appears in any of the ~86 sampled parcels',
  obtain: 'Engineering stubs the provider response at the import client',
}

test('the markdown names the shape, the route, and who does what', () => {
  const md = formatTestPlanAsMarkdown(planWith({ data_shape: STUB_SHAPE }), TICKET)

  assert.match(md, /\*\*Data shape:\*\*/)
  assert.match(md, /non-numeric string/)
  assert.match(md, /needs a stubbed payload \(Engineering\)/)
  assert.match(md, /How to get it: Engineering stubs the provider response/)
  assert.match(md, /Evidence: No such value appears/)
})

test('the Jira comment carries the same three parts', () => {
  const jira = formatTestPlanAsJira(planWith({ data_shape: STUB_SHAPE }))

  assert.match(jira, /Data shape: .*non-numeric string.* — needs a stubbed payload \(Engineering\)/)
  assert.match(jira, /How to get it: /)
  assert.match(jira, /Evidence: /)
})

test('an unconfirmed real record says so in the label', () => {
  const md = formatTestPlanAsMarkdown(planWith({
    data_shape: {
      shape: 'A parcel with lot square footage but no acreage',
      provisioning: 'real_record_unconfirmed',
      obtain: 'Search for one; if every parcel supplies both or neither, this is a stub case',
    },
  }), TICKET)

  assert.match(md, /real record — UNCONFIRMED/)
  assert.match(md, /if every parcel supplies both or neither/)
})

test('an unactionable ask is called out rather than left to read as a data hunt', () => {
  const plan = planWith({
    steps: ['Ask Engineering to supply a parcel with a non-numeric acreage'],
    data_ask_unactionable: true,
    data_ask_unactionable_reason: 'names no provisioning route',
  })

  assert.match(formatTestPlanAsMarkdown(plan, TICKET), /\*\*Data ask is not actionable\*\* — names no provisioning route/)
  assert.match(formatTestPlanAsJira(plan), /⚠️ Data ask is not actionable/)
})

test('a case that runs on ordinary data renders nothing extra', () => {
  const md = formatTestPlanAsMarkdown(planWith({}), TICKET)
  const jira = formatTestPlanAsJira(planWith({}))

  assert.ok(!md.includes('Data shape'))
  assert.ok(!jira.includes('Data shape'))
})

test('an empty data_shape object is treated as absent', () => {
  const md = formatTestPlanAsMarkdown(planWith({ data_shape: {} }), TICKET)
  assert.ok(!md.includes('Data shape'))
})

/**
 * The UAT video plan, as it reaches a reader.
 *
 * The PM watches the recording and does not read the plan, so the comment has
 * to say what the video covers. Two things are load-bearing and pinned here:
 * the chapter markers (a chapter starts where the precondition changes — only
 * 47 of 297 plans share one setup across every happy case, so an unmarked
 * state reset reads as a glitch), and the truncation notice (silently dropping
 * the tail would leave a reader thinking the video covered the whole flow).
 *
 * The selection itself is the server's (src/app/video_walkthrough.py); these
 * tests cover the rendering only.
 */

const videoPlan = (overrides = {}) => ({
  happy_path: [], edge_cases: [], regression_checklist: [],
  video_walkthrough: {
    chapters: [
      { case_id: 'happy_path:0', title: 'Agent upgrades', steps: 3, starts_chapter: true },
      { case_id: 'happy_path:1', title: 'Receipt updates', steps: 2, starts_chapter: false },
      { case_id: 'happy_path:2', title: 'Broker sees it', steps: 3, starts_chapter: true },
    ],
    estimated_seconds: 20,
    truncated: false,
    ...overrides,
  },
})

test('the Jira comment lists what the video should cover', () => {
  const jira = formatTestPlanAsJira(videoPlan())
  assert.ok(jira.includes('VIDEO SHOULD COVER'))
  assert.ok(jira.includes('Agent upgrades'))
  assert.ok(jira.includes('Broker sees it'))
})

test('a precondition change is marked as a new chapter, a continuation is not', () => {
  const jira = formatTestPlanAsJira(videoPlan())
  assert.ok(jira.includes('▸ Agent upgrades'))
  assert.ok(jira.includes('▸ Broker sees it'))
  // The middle case shares the previous setup, so it must not claim a reset.
  assert.ok(!jira.includes('▸ Receipt updates'))
  assert.ok(jira.includes('Receipt updates'))
})

test('truncation is stated rather than left silent', () => {
  const jira = formatTestPlanAsJira(videoPlan({ truncated: true }))
  assert.ok(jira.includes('past the recording budget'))
  assert.ok(!formatTestPlanAsJira(videoPlan()).includes('past the recording budget'))
})

test('the markdown export carries the same plan', () => {
  const md = formatTestPlanAsMarkdown(videoPlan(), TICKET)
  assert.ok(md.includes('Video should cover'))
  assert.ok(md.includes('▸ Agent upgrades'))
})

test('a plan with no walkthrough renders no video section', () => {
  const bare = { happy_path: [], edge_cases: [], regression_checklist: [] }
  assert.ok(!formatTestPlanAsJira(bare).includes('VIDEO SHOULD COVER'))
})

test('an empty chapter list renders no video section', () => {
  const jira = formatTestPlanAsJira(videoPlan({ chapters: [] }))
  assert.ok(!jira.includes('VIDEO SHOULD COVER'))
})

test('a malformed walkthrough does not break the comment', () => {
  for (const wt of [null, 'nope', 42, { chapters: 'not a list' }, { chapters: [null, {}] }]) {
    const jira = formatTestPlanAsJira({
      happy_path: [], edge_cases: [], regression_checklist: [], video_walkthrough: wt,
    })
    assert.ok(!jira.includes('VIDEO SHOULD COVER'))
  }
})

// SK-2627: a plan written from ACs because the ticket has no PR of its own
// must say so where the tester reads it, and attribute borrowed PRs.
const SPEC_ONLY_PLAN = {
  happy_path: [{ title: 'Check It Out lands inside the shared file', steps: ['Click it'], expected: 'Lands in the file' }],
  edge_cases: [],
  regression_checklist: [],
  source_provenance: {
    pull_requests: [{
      number: 49, repository: 'acme/user-service', state: 'merged',
      used_as_grounding: true, borrowed: true, ticket_key: 'SK-2630',
    }],
    spec_only: { mode: 'spec_only', message: 'This ticket has no merged or open pull request of its own.' },
  },
}

test('a spec-only plan says so in the Jira comment and names the sibling', () => {
  const jira = formatTestPlanAsJira(SPEC_ONLY_PLAN)
  assert.match(jira, /WRITTEN FROM ACCEPTANCE CRITERIA\. This ticket has no merged or open pull request/)
  assert.match(jira, /acme\/user-service#49 — merged — from SK-2630/)
})

test('a spec-only plan says so in the markdown copy, even with no PRs at all', () => {
  const plan = { ...SPEC_ONLY_PLAN, source_provenance: { ...SPEC_ONLY_PLAN.source_provenance, pull_requests: [] } }
  const md = formatTestPlanAsMarkdown(plan, { key: 'SK-2627', summary: 'Email invitation' })
  assert.match(md, /Written from acceptance criteria\.\*\* This ticket has no merged/)
})

test('a PR shared by several tickets in a batch is listed once', () => {
  // SK-2687's batch comment named digisign.sender.envelope#92 three times.
  const pr = {
    number: 92, repository: 'skyslope/digisign.sender.envelope', state: 'merged',
    head_sha: '6d9a3550000', url: 'https://github.com/skyslope/digisign.sender.envelope/pull/92',
    used_as_grounding: true,
  }
  const open = { ...pr, number: 96, state: 'open', url: 'https://github.com/skyslope/digisign.sender.envelope/pull/96' }
  const plan = { happy_path: [], source_provenance: { pull_requests: [pr, open, { ...pr }, { ...pr }] } }
  const jira = formatTestPlanAsJira(plan)
  assert.equal(jira.match(/envelope#92/g).length, 1)
  assert.equal(jira.match(/envelope#96/g).length, 1)
})
