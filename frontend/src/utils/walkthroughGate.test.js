import test from 'node:test'
import assert from 'node:assert/strict'

import { walkthroughPromptCopy } from './walkthroughGate.js'

test('manual-check plans are asked for evidence, not a video', () => {
  const copy = walkthroughPromptCopy('manual_checks_need_evidence')
  assert.match(copy.title, /evidence/)
  assert.match(copy.sub, /results file/)
})

test('UI plans keep the walkthrough wording', () => {
  const copy = walkthroughPromptCopy('ui_needs_walkthrough')
  assert.match(copy.title, /walkthrough/)
  assert.equal(walkthroughPromptCopy(undefined).title, copy.title)
})
