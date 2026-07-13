/**
 * Development information (commits, PRs, branches) for a Jira ticket.
 */

import { useState } from 'react'
import Icon from './Icon'
import { Coll, Chip, StatPill, Alert } from './ui'

function isOpenPR(status) {
  return (status || '').toLowerCase() === 'open'
}

function formatMergedDate(iso) {
  if (!iso) return null
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return null
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function prStatusCat(status) {
  const s = (status || '').toLowerCase()
  if (s === 'merged') return 'done'
  if (s === 'open') return 'inprogress'
  if (s === 'declined' || s === 'closed') return 'blocked'
  return 'todo'
}

function DevRow({ icon, status, label, title, meta, url, iconColor }) {
  const content = (
    <>
      <Icon name={icon} size={14} style={{ color: iconColor, flexShrink: 0 }} />
      <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-sm)', color: 'var(--fg-strong)', flexShrink: 0 }}>{label}</span>
      <span style={{ flex: 1, minWidth: 0, color: 'var(--fg)', fontSize: 'var(--t-sm)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{title}</span>
      {meta && <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>{meta}</span>}
      {status && <StatPill cat={prStatusCat(status)}>{status}</StatPill>}
      {url && <Icon name="external" size={12} style={{ color: 'var(--fg-subtle)' }} />}
    </>
  )
  if (url) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className="card"
        data-interactive="true"
        style={{ padding: '10px var(--s-5)', display: 'flex', alignItems: 'center', gap: 'var(--s-4)', textDecoration: 'none', color: 'inherit' }}
      >
        {content}
      </a>
    )
  }
  return (
    <div className="card" style={{ padding: '10px var(--s-5)', display: 'flex', alignItems: 'center', gap: 'var(--s-4)' }}>
      {content}
    </div>
  )
}

function DevelopmentInfo({ developmentInfo }) {
  const [isOpen, setIsOpen] = useState(false)

  if (!developmentInfo) {
    return (
      <Alert tone="muted" title="No linked development activity" icon="git-pull">
        Test plan will be derived from the ticket description only.
      </Alert>
    )
  }

  const hasCommits = developmentInfo.commits && developmentInfo.commits.length > 0
  const hasPullRequests = developmentInfo.pull_requests && developmentInfo.pull_requests.length > 0
  const hasBranches = developmentInfo.branches && developmentInfo.branches.length > 0

  if (!hasCommits && !hasPullRequests && !hasBranches) {
    return (
      <Alert tone="muted" title="No development activity" icon="git-pull">
        Test plan will be derived from the ticket description only.
      </Alert>
    )
  }

  const openPRCount = hasPullRequests
    ? developmentInfo.pull_requests.filter((pr) => isOpenPR(pr.status)).length
    : 0
  const mergedPRCount = hasPullRequests
    ? developmentInfo.pull_requests.filter((pr) => (pr.status || '').toLowerCase() === 'merged').length
    : 0

  const summaryParts = []
  if (hasPullRequests) {
    const total = developmentInfo.pull_requests.length
    summaryParts.push(`${total} PR${total !== 1 ? 's' : ''}`)
  }
  if (hasCommits) summaryParts.push(`${developmentInfo.commits.length} commit${developmentInfo.commits.length !== 1 ? 's' : ''}`)
  if (hasBranches) summaryParts.push(`${developmentInfo.branches.length} branch${developmentInfo.branches.length !== 1 ? 'es' : ''}`)

  return (
    <Coll
      icon="git-pull"
      title="Development Activity"
      open={isOpen}
      onToggle={setIsOpen}
      preview={summaryParts.join(' · ')}
      meta={
        openPRCount > 0 ? (
          <Chip dot dotColor="var(--warning)">
            {openPRCount} open · {mergedPRCount} merged
          </Chip>
        ) : (
          <Chip>{summaryParts.join(' · ')}</Chip>
        )
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
        {hasPullRequests &&
          developmentInfo.pull_requests.map((pr, i) => {
            const isOpen = isOpenPR(pr.status)
            const isMerged = (pr.status || '').toLowerCase() === 'merged'
            const iconColor = isOpen ? 'var(--warning)' : isMerged ? '#a855f7' : 'var(--fg-muted)'
            const mergedOn = isMerged ? formatMergedDate(pr.merged_at) : null
            const metaParts = [
              pr.repository,
              pr.author && `@${pr.author}`,
              mergedOn && `merged ${mergedOn}`,
            ].filter(Boolean)
            return (
              <DevRow
                key={`pr-${i}`}
                icon="git-pull"
                status={pr.status}
                label={pr.number ? `#${pr.number}` : 'PR'}
                title={pr.title}
                meta={metaParts.length ? metaParts.join(' · ') : null}
                url={pr.url}
                iconColor={iconColor}
              />
            )
          })}

        {hasBranches && developmentInfo.branches.length > 0 && (
          <>
            {developmentInfo.branches.map((branch, i) => (
              <DevRow
                key={`branch-${i}`}
                icon="git-branch"
                label="branch"
                title={branch}
                iconColor="var(--fg-muted)"
              />
            ))}
          </>
        )}

        {hasCommits && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', color: 'var(--fg-muted)', fontSize: 'var(--t-sm)', padding: 'var(--s-3) var(--s-5)' }}>
            <Icon name="git-commit" size={13} />
            <span>{developmentInfo.commits.length} commit{developmentInfo.commits.length !== 1 ? 's' : ''} linked to this ticket</span>
          </div>
        )}
      </div>
    </Coll>
  )
}

export default DevelopmentInfo
