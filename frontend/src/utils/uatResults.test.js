import test from 'node:test'
import assert from 'node:assert/strict'

import { UAT_RESULTS_MARKER, formatUatResultsAsJira, groupCases } from './uatResults.js'

const view = (over = {}) => ({
  run_id: 6,
  plan_version: 2,
  counts: { passed: 1, failed: 1, not_run: 3 },
  needs_person_codes: ['missing_fixture', 'needs_client_token', 'not_automatable'],
  builds: [{ repo: 'acme/api', status: 'included', env: 'integ', deployed_commit: '70811d7abc' }],
  cases: [
    { id: 'happy_path:0', title: 'Saves an order', repos: ['acme/api'],
      result: { status: 'passed', actor: { kind: 'agent' }, evidence: [{ kind: 'assertion', text: '201 Created' }] } },
    { id: 'happy_path:1', title: 'Shows a toast', repos: ['acme/app'],
      result: { status: 'failed', actor: { kind: 'agent' },
        evidence: [{ kind: 'assertion', text: 'no toast after save' },
          { kind: 'screenshot', ref: '/e/03-FAIL-no-toast.png' }, { kind: 'video_ts', ref: 'v', at_s: 83 }] } },
    { id: 'edge_cases:0', title: 'Empty basket', result: { status: 'not_run', not_run: { code: 'missing_fixture' } } },
    { id: 'edge_cases:1', title: 'Old phone', result: { status: 'not_run', not_run: { code: 'not_automatable' } } },
    { id: 'edge_cases:2', title: 'In CI', result: { status: 'not_run', not_run: { code: 'deferred_to_ci' } } },
    { id: 'edge_cases:3', title: 'Never reached', result: null },
  ],
  media: { video: { name: 'walkthrough-final.mp4', parts: ['07-walkthrough-narrated.mp4'], host: 'jira' },
    published: { host: 'jira', name: 'walkthrough-final.mp4', attachment_id: '9' }, fail_frames: [] },
  how_to_see_it: 'Open Orders and save one.',
  review: null,
  ...over,
})

test('opens with the marker the backend finds the comment by', () => {
  assert.ok(formatUatResultsAsJira(view()).startsWith(`${UAT_RESULTS_MARKER} — plan v2 · run 6`))
})

test('failures carry their assertion and evidence names', () => {
  const text = formatUatResultsAsJira(view())
  assert.match(text, /- \*\*Shows a toast\*\* \[acme\/app\] — no toast after save \(03-FAIL-no-toast\.png, video 1:23\)/)
})

test('not run splits into what a person must do and what is blocked', () => {
  const g = groupCases(view())
  assert.deepEqual(Object.keys(g.needsPerson).sort(), ['missing_fixture', 'not_automatable'])
  assert.deepEqual(Object.keys(g.blocked), ['deferred_to_ci'])
  assert.equal(g.noResult.length, 1)
  const text = formatUatResultsAsJira(view())
  assert.match(text, /### Still needs a person \(2\)/)
  assert.match(text, /### No result recorded \(1\)/)
})

test('preview options win until a decision is recorded', () => {
  const preview = formatUatResultsAsJira(view(), { nextStep: 'fail-to-in-progress', notes: 'toast', reviewer: 'Sam' })
  assert.match(preview, /reviewed by Sam/)
  assert.match(preview, /\*\*Next step:\*\* Fail back to In Progress/)
  assert.match(preview, /\*\*Reviewer notes:\*\* toast/)
  const posted = formatUatResultsAsJira(view({ review: { next_step: null, notes: null, actor: { name: 'Sam' } } }))
  assert.match(posted, /\*\*Next step:\*\* Stay in testing/)
})

test('the video is named and described; a missing one is said so', () => {
  assert.match(formatUatResultsAsJira(view()), /📎 walkthrough-final\.mp4 \(attached[^\n]*\nWhat it shows: Open Orders and save one\./)
  assert.match(formatUatResultsAsJira(view({ media: { video: null, fail_frames: [] } })), /No walkthrough was recorded/)
})

test('a hosted video is a link; a fallback, a failure and a pending post each say so', () => {
  const media = (published) => ({ media: { video: { name: 'walkthrough-final.mp4', parts: ['a.mp4', 'b.mp4'], host: 'drive_folder' }, published, fail_frames: [] } })
  const link = 'https://drive.google.com/file/d/abc/view'
  assert.match(formatUatResultsAsJira(view(media({ host: 'drive_folder', link }))), /📹 \[Watch the walkthrough\]\(https:\/\/drive\.google\.com\/file\/d\/abc\/view\)\nWhat it shows/)
  const fell = formatUatResultsAsJira(view(media({ host: 'jira', name: 'walkthrough-final.mp4', attachment_id: '3', fallback_reason: 'Drive folder not found' })))
  assert.match(fell, /📎 walkthrough-final\.mp4 \(attached/)
  assert.match(fell, /because the video host failed: Drive folder not found/)
  assert.match(formatUatResultsAsJira(view(media({ host: 'jira', error: 'ffmpeg is not installed' }))), /could not be made: ffmpeg is not installed/)
  assert.match(formatUatResultsAsJira(view(media(null))), /walkthrough \(2 recordings\) is linked here when the results are posted/)
})
