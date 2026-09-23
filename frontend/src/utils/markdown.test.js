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
