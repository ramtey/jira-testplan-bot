/**
 * "This plan's Jira status isn't being tracked" notice.
 *
 * The live/not-live-in-Jira chip is part of RunHistoryBanner, which renders
 * nothing when a ticket has no run history. A plan generated without a stored
 * run therefore showed no Jira status line at all, which reads the same as a
 * plan that simply hasn't been posted — and posting it doesn't change that,
 * because /jira/post-comment only records the live version when it is given a
 * plan_id. This states the gap instead of leaving a blank where the chip goes.
 */

import { Chip } from './ui'
import Icon from './Icon'

export default function UntrackedPlanNotice({ available }) {
  const reason = available
    ? 'This plan was not recorded, so the app cannot tell whether it is the version live in Jira. Regenerate it to start tracking.'
    : 'Run history is unavailable, so the app cannot tell whether this plan is the version live in Jira.'

  return (
    <div className="card" style={{ marginTop: 'var(--s-5)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-4)', padding: '10px var(--s-5)' }}>
        <Icon name="history" size={14} style={{ color: 'var(--fg-muted)' }} />
        <span title={reason} style={{ display: 'inline-flex' }}>
          <Chip size="sm" dot dotColor="var(--fg-muted)">Jira status not tracked</Chip>
        </span>
        <span style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>{reason}</span>
      </div>
    </div>
  )
}
