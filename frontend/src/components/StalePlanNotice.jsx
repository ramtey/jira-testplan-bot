/**
 * "This plan is older than the code" warning.
 *
 * Shown above a stored plan when a pull request merged after the plan was
 * written. The watcher never regenerates, so without this the tester opens a
 * ticket, finds a plan waiting, and has no way to tell that the diff moved
 * underneath it — a stale plan reads exactly as authoritative as a current one.
 */

import { Alert, Btn } from './ui'
import { formatRelativeTime } from '../utils/time'
import { mergesAfterPlan } from '../utils/planFreshness'

function StalePlanNotice({ planCreatedAt, pullRequests, onRegenerate, regenerating }) {
  const merges = mergesAfterPlan(planCreatedAt, pullRequests)
  if (merges.length === 0) return null

  const newest = merges[0]
  const others = merges.length - 1

  return (
    <div style={{ marginTop: 'var(--s-4)' }}>
      <Alert
        tone="warning"
        title="This plan predates the current code"
        action={
          onRegenerate ? (
            <Btn
              variant="secondary"
              icon="refresh"
              loading={regenerating}
              onClick={onRegenerate}
              title="Generate a new plan against the merged code"
            >
              Regenerate
            </Btn>
          ) : null
        }
      >
        Written {formatRelativeTime(planCreatedAt)}, then{' '}
        {newest.url ? (
          <a href={newest.url} target="_blank" rel="noopener noreferrer">
            {newest.title || 'a pull request'}
          </a>
        ) : (
          newest.title || 'a pull request'
        )}{' '}
        merged {formatRelativeTime(newest.merged_at)}
        {others > 0 && ` (and ${others} other${others === 1 ? '' : 's'} since)`}. Re-read the
        cases against the merged diff, or regenerate.
      </Alert>
    </div>
  )
}

export default StalePlanNotice
