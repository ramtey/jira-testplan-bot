import { test } from 'node:test'
import assert from 'node:assert/strict'
import { formatClock, formatDuration } from './time.js'

test('formatClock reads like a stopwatch', () => {
  assert.equal(formatClock(0), '0:00')
  assert.equal(formatClock(7_400), '0:07')
  assert.equal(formatClock(252_000), '4:12')
  assert.equal(formatClock(3_725_000), '1:02:05')
  assert.equal(formatClock(-5), '0:00')
})

test('formatDuration reads as words', () => {
  assert.equal(formatDuration(7_400), '7s')
  assert.equal(formatDuration(252_000), '4m 12s')
  assert.equal(formatDuration(3_725_000), '1h 2m')
  assert.equal(formatDuration(null), '0s')
})
