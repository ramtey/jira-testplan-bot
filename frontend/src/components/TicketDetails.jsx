/**
 * Display Jira ticket details and quality analysis.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { API_BASE_URL, getJiraTicketUrl } from '../config'
import DevelopmentInfo from './DevelopmentInfo'
import WorkflowActions from './WorkflowActions'
import RowQuickAction from './RowQuickAction'
import Icon from './Icon'
import { ItChip, StatPill, Asn, Tag, Coll, Alert, ACTag, Chip } from './ui'

// Turn bare URLs into clickable <a> elements while preserving surrounding
// whitespace/linebreaks — the description is rendered inside <pre>, so we
// return a mixed array of strings and elements instead of an HTML string.
const URL_RE = /(https?:\/\/[^\s<>"')\]}]+)/g
const TRAILING_PUNCT_RE = /[.,;:!?]+$/
function linkifyText(text) {
  if (!text) return text
  const parts = text.split(URL_RE)
  return parts.map((part, i) => {
    if (i % 2 === 0) return part
    const trailing = (part.match(TRAILING_PUNCT_RE) || [''])[0]
    const href = trailing ? part.slice(0, -trailing.length) : part
    return (
      <span key={i}>
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          style={{ color: 'var(--accent)', textDecoration: 'underline', wordBreak: 'break-all' }}
        >
          {href}
        </a>
        {trailing}
      </span>
    )
  })
}

function statusCategory(status) {
  const s = (status || '').toLowerCase()
  if (/done|closed|complete|resolved/.test(s)) return 'done'
  if (/progress|review|qa|testing|uat/.test(s)) return 'inprogress'
  if (/block/.test(s)) return 'blocked'
  return 'todo'
}

function formatBounceTimestamp(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function formatRelativeTime(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const diffMs = Date.now() - d.getTime()
  const abs = Math.abs(diffMs)
  const mins = Math.round(abs / 60000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins} minute${mins === 1 ? '' : 's'} ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days} day${days === 1 ? '' : 's'} ago`
  const months = Math.round(days / 30)
  if (months < 12) return `${months} month${months === 1 ? '' : 's'} ago`
  const years = Math.round(months / 12)
  return `${years} year${years === 1 ? '' : 's'} ago`
}

// State shape shared between BounceSection (owner) and BounceCard (presenter):
//   { state: 'loading' | 'ready' | 'no-reason' | 'no-comment' | 'error', headline: string | null }
// Ownership sits at the section level so the collapsed preview can reflect the
// latest bounce's headline before the user expands the panel — the whole reason
// this refactor exists.

// The fix that answers a send-back is the earliest PR merged AFTER the bounce.
// Older bounces on the same ticket can then be paired with earlier fix PRs — a
// naive "latest merged PR" scoop would credit every bounce with the same fix.
function findResultingPr(bounce, pullRequests) {
  if (!bounce?.timestamp || !Array.isArray(pullRequests) || pullRequests.length === 0) return null
  const bounceMs = new Date(bounce.timestamp).getTime()
  if (Number.isNaN(bounceMs)) return null
  let best = null
  let bestMs = Infinity
  for (const pr of pullRequests) {
    if (!pr?.merged_at) continue
    const mergedMs = new Date(pr.merged_at).getTime()
    if (Number.isNaN(mergedMs) || mergedMs <= bounceMs) continue
    if (mergedMs < bestMs) {
      bestMs = mergedMs
      best = pr
    }
  }
  return best
}

function BounceCard({ bounce, headlineState, resultingPr }) {
  const [showFull, setShowFull] = useState(false)
  const state = headlineState?.state || (bounce.reason ? 'loading' : 'no-comment')
  const headline = headlineState?.headline || null

  const author = bounce.author || 'Someone'
  const relative = formatRelativeTime(bounce.timestamp)
  const absolute = formatBounceTimestamp(bounce.timestamp)
  // "Sent back to <to_status> from <from_status>" reads left-to-right in plain English —
  // the arrow form (X → Y) forced readers to translate "backward transition" every time.
  const transitionSentence = `${author} moved this back to ${bounce.to_status} from ${bounce.from_status}.`

  const headlineNode = (() => {
    if (state === 'loading') {
      return <span style={{ color: 'var(--fg-muted)', fontStyle: 'italic' }}>Reading the reviewer's comment…</span>
    }
    if (state === 'ready' && headline) {
      return <span style={{ color: 'var(--fg-strong)' }}>{headline}</span>
    }
    if (state === 'no-reason') {
      return <span style={{ color: 'var(--fg-muted)', fontStyle: 'italic' }}>The nearby comment didn't explain a clear reason — see the full comment for context.</span>
    }
    if (state === 'no-comment') {
      return <span style={{ color: 'var(--fg-muted)', fontStyle: 'italic' }}>No comment was posted near this transition.</span>
    }
    return <span style={{ color: 'var(--fg-muted)', fontStyle: 'italic' }}>Couldn't summarize the reviewer's comment — see below.</span>
  })()

  return (
    <Alert
      tone="warning"
      title={
        <span style={{ fontWeight: 600, fontSize: 'var(--t-md)', lineHeight: 1.4 }}>
          {headlineNode}
        </span>
      }
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-2)' }}>
        <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg-muted)' }}>
          {transitionSentence}
          {relative && (
            <>
              {' '}
              <span title={absolute}>{relative}</span>
              <span style={{ color: 'var(--fg-subtle)' }}> · {absolute}</span>
            </>
          )}
        </div>
        {bounce.reason && (
          <div>
            <button
              type="button"
              onClick={() => setShowFull((v) => !v)}
              className="btn"
              data-variant="ghost"
              data-size="sm"
              style={{ padding: '2px 8px', fontSize: 'var(--t-xs)' }}
            >
              {showFull ? 'Hide full comment' : 'Show full comment'}
            </button>
            {showFull && (
              <pre style={{
                margin: 'var(--s-3) 0 0',
                padding: 'var(--s-4)',
                background: 'var(--bg-input)',
                border: '1px solid var(--line)',
                borderRadius: 'var(--r-sm)',
                color: 'var(--fg)',
                fontFamily: 'var(--font-mono)',
                fontSize: '12px',
                lineHeight: '18px',
                whiteSpace: 'pre-wrap',
                wordWrap: 'break-word',
              }}>
                {bounce.reason}
              </pre>
            )}
          </div>
        )}
        {resultingPr && <ResultingPrChanges pr={resultingPr} />}
      </div>
    </Alert>
  )
}

function ResultingPrChanges({ pr }) {
  const [showAll, setShowAll] = useState(false)
  const files = Array.isArray(pr.files_changed) ? pr.files_changed : []
  const mergedRelative = formatRelativeTime(pr.merged_at)
  const mergedAbsolute = formatBounceTimestamp(pr.merged_at)
  const label = pr.title || 'the follow-up PR'
  const shown = showAll ? files : files.slice(0, 6)

  return (
    <div style={{
      marginTop: 'var(--s-2)',
      padding: 'var(--s-3)',
      background: 'var(--bg-input)',
      border: '1px solid var(--line)',
      borderRadius: 'var(--r-sm)',
    }}>
      <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)', marginBottom: 'var(--s-2)' }}>
        <strong>Changes merged after this send-back</strong>
        {mergedRelative && (
          <span style={{ color: 'var(--fg-muted)' }}>
            {' · '}
            <span title={mergedAbsolute}>{mergedRelative}</span>
          </span>
        )}
      </div>
      <div style={{ fontSize: 'var(--t-sm)', marginBottom: files.length ? 'var(--s-2)' : 0 }}>
        {pr.url
          ? <a href={pr.url} target="_blank" rel="noreferrer">{label}</a>
          : <span>{label}</span>}
      </div>
      {files.length > 0 && (
        <>
          <ul style={{
            margin: 0,
            paddingLeft: 'var(--s-5)',
            fontSize: 'var(--t-xs)',
            fontFamily: 'var(--font-mono)',
            color: 'var(--fg)',
            display: 'flex',
            flexDirection: 'column',
            gap: '2px',
          }}>
            {shown.map((f) => (
              <li key={f.filename}>
                <span style={{ color: 'var(--fg-muted)' }}>[{f.status}] </span>
                {f.filename}
                {(f.additions || f.deletions) ? (
                  <span style={{ color: 'var(--fg-muted)' }}>
                    {' '}(+{f.additions ?? 0} / −{f.deletions ?? 0})
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
          {files.length > shown.length && (
            <button
              type="button"
              onClick={() => setShowAll(true)}
              className="btn"
              data-variant="ghost"
              data-size="sm"
              style={{ padding: '2px 8px', fontSize: 'var(--t-xs)', marginTop: 'var(--s-2)' }}
            >
              Show {files.length - shown.length} more file{files.length - shown.length === 1 ? '' : 's'}
            </button>
          )}
        </>
      )}
    </div>
  )
}

function BounceSection({ events, pullRequests }) {
  const [open, setOpen] = useState(false)
  const [headlines, setHeadlines] = useState({})  // { [timestamp]: { state, headline } }

  // Reset the cache when the bounce set changes (e.g. new ticket loaded into this view).
  // Timestamps are ms-precision ISO strings, so their joined signature uniquely identifies
  // a set of bounces without needing to thread ticketKey down here.
  const signature = events.map((e) => e.timestamp).join('|')
  const prevSigRef = useRef(signature)
  if (prevSigRef.current !== signature) {
    prevSigRef.current = signature
    setHeadlines({})
  }

  const latest = events[events.length - 1]

  const fetchHeadline = useCallback(async (b) => {
    if (!b?.reason) {
      setHeadlines((prev) => (prev[b.timestamp] ? prev : { ...prev, [b.timestamp]: { state: 'no-comment', headline: null } }))
      return
    }
    let started = false
    setHeadlines((prev) => {
      if (prev[b.timestamp]) return prev
      started = true
      return { ...prev, [b.timestamp]: { state: 'loading', headline: null } }
    })
    if (!started) return
    try {
      const r = await fetch(`${API_BASE_URL}/bounce/summarize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          from_status: b.from_status,
          to_status: b.to_status,
          reason: b.reason,
        }),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      const data = await r.json()
      setHeadlines((prev) => ({
        ...prev,
        [b.timestamp]: data.headline
          ? { state: 'ready', headline: data.headline }
          : { state: 'no-reason', headline: null },
      }))
    } catch {
      setHeadlines((prev) => ({ ...prev, [b.timestamp]: { state: 'error', headline: null } }))
    }
  }, [])

  // Eagerly fetch the latest bounce's headline so the collapsed preview can carry
  // the actual reason. Older bounces stay lazy — only fetched when the panel opens.
  useEffect(() => {
    if (latest) fetchHeadline(latest)
    // signature covers the "events changed" case; latest.timestamp is derived from it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, fetchHeadline])

  useEffect(() => {
    if (!open) return
    for (const b of events) {
      if (b === latest) continue
      fetchHeadline(b)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, signature, fetchHeadline])

  const count = events.length
  const title = count === 1
    ? 'Sent back for more work · 1 time'
    : `Sent back for more work · ${count} times`

  const latestState = latest ? headlines[latest.timestamp] : null
  const latestRelative = latest ? formatRelativeTime(latest.timestamp) : ''
  const preview = latestState?.state === 'ready' && latestState.headline
    ? (latestRelative ? `${latestState.headline} · ${latestRelative}` : latestState.headline)
    : latestRelative

  return (
    <Coll
      icon="history"
      title={title}
      open={open}
      onToggle={setOpen}
      preview={preview}
      meta={<Chip>{count}</Chip>}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-3)' }}>
        {[...events].reverse().map((b, i) => (
          <BounceCard
            key={`${b.timestamp}-${i}`}
            bounce={b}
            headlineState={headlines[b.timestamp]}
            resultingPr={findResultingPr(b, pullRequests)}
          />
        ))}
      </div>
    </Coll>
  )
}

function TicketDetails({ ticketData, isDescriptionExpanded, onToggleDescription, onActionComplete, onRowAction, compact = false, videoChecklistSteps }) {
  const [isAttachmentsExpanded, setIsAttachmentsExpanded] = useState(false)
  const [isSummaryExpanded, setIsSummaryExpanded] = useState(false)
  const [plainSummary, setPlainSummary] = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [summaryError, setSummaryError] = useState(null)
  const [isDescriptionOpen, setIsDescriptionOpen] = useState(true)

  const handleToggleSummary = async () => {
    // Once a summary exists (or errored), the click is a normal expand/collapse toggle.
    if (plainSummary !== null || summaryError) {
      setIsSummaryExpanded(!isSummaryExpanded)
      return
    }

    // First click while empty: fetch but leave the panel collapsed — the preview
    // line carries the snippet, so the user can read it without expanding.
    if (summaryLoading) return

    setSummaryLoading(true)
    setSummaryError(null)
    try {
      const response = await fetch(`${API_BASE_URL}/issue/${ticketData.key}/summarize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          summary: ticketData.summary,
          description: ticketData.description,
        }),
      })
      if (!response.ok) {
        const err = await response.json()
        throw new Error(err.detail || 'Failed to generate summary')
      }
      const data = await response.json()
      setPlainSummary(data.summary)
    } catch (err) {
      setSummaryError(err.message)
    } finally {
      setSummaryLoading(false)
    }
  }

  const jiraTicketUrl = getJiraTicketUrl(ticketData.key)
  const cat = statusCategory(ticketData.status)
  const shouldTruncateDescription = (description) => description && description.length > 500
  const getDisplayDescription = (description) => {
    if (!description) return ''
    if (isDescriptionExpanded || !shouldTruncateDescription(description)) return description
    return description.substring(0, 500) + '...'
  }

  // ── Compact rendering for multi-ticket bundle ──────────────────────────
  if (compact) {
    return (
      <div className="card" style={{ padding: '10px var(--s-5)', display: 'flex', alignItems: 'center', gap: 'var(--s-4)' }}>
        <ItChip type={ticketData.issue_type} label={ticketData.issue_type} />
        {jiraTicketUrl ? (
          <a href={jiraTicketUrl} target="_blank" rel="noopener noreferrer" style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-sm)', color: 'var(--accent)', flexShrink: 0 }}>
            {ticketData.key}
          </a>
        ) : (
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-sm)', color: 'var(--fg)', flexShrink: 0 }}>{ticketData.key}</span>
        )}
        <span style={{ flex: 1, minWidth: 0, fontSize: 'var(--t-sm)', color: 'var(--fg-strong)', overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>
          {ticketData.summary}
        </span>
        {ticketData.status && <StatPill cat={cat}>{ticketData.status}</StatPill>}
        <RowQuickAction
          ticketKey={ticketData.key}
          currentStatus={ticketData.status}
          hasSubtasks={
            Array.isArray(ticketData.children) &&
            ticketData.children.some((c) => /sub-?task/i.test(c?.issue_type || ''))
          }
          onActionComplete={onRowAction}
        />
        {ticketData.assignee && <Asn name={ticketData.assignee} />}
      </div>
    )
  }

  // ── Full single-ticket card ────────────────────────────────────────────
  return (
    <div style={{ marginTop: 'var(--s-6)', display: 'flex', flexDirection: 'column', gap: 'var(--s-4)' }}>
      {/* Header card */}
      <div className="card" style={{ padding: 'var(--s-7)' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 'var(--s-4)', marginBottom: 'var(--s-4)' }}>
          {jiraTicketUrl ? (
            <a
              href={jiraTicketUrl}
              target="_blank"
              rel="noopener noreferrer"
              style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-md)', color: 'var(--accent)' }}
            >
              {ticketData.key}
              <Icon name="external" size={11} style={{ marginLeft: 4, verticalAlign: -1 }} />
            </a>
          ) : (
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-md)', color: 'var(--fg-strong)' }}>{ticketData.key}</span>
          )}
        </div>
        <h1 style={{ fontSize: 'var(--t-2xl)', lineHeight: 'var(--lh-2xl)', fontWeight: 600, letterSpacing: '-.005em', margin: '0 0 var(--s-6)', color: 'var(--fg-strong)' }}>
          {ticketData.summary}
        </h1>

        {/* Meta row */}
        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 'var(--s-3)' }}>
          {ticketData.issue_type && <ItChip type={ticketData.issue_type} label={ticketData.issue_type} />}
          {ticketData.status && <StatPill cat={cat}>{ticketData.status}</StatPill>}
          <span style={{ width: 1, height: 14, background: 'var(--line-strong)', margin: '0 var(--s-2)' }} />
          {ticketData.assignee_history && ticketData.assignee_history.length > 0 ? (
            <>
              <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '.04em', fontWeight: 600 }}>Assignee</span>
              {ticketData.assignee_history.map((name, i) => (
                <Asn key={i} name={name} muted={name !== ticketData.assignee} />
              ))}
            </>
          ) : ticketData.assignee ? (
            <>
              <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '.04em', fontWeight: 600 }}>Assignee</span>
              <Asn name={ticketData.assignee} />
            </>
          ) : null}
          {ticketData.labels && ticketData.labels.length > 0 && (
            <>
              <span style={{ width: 1, height: 14, background: 'var(--line-strong)', margin: '0 var(--s-2)' }} />
              {ticketData.labels.map((label, i) => <Tag key={i}>{label}</Tag>)}
            </>
          )}
        </div>

        <hr className="hrule" />

        <WorkflowActions
          ticketKey={ticketData.key}
          currentStatus={ticketData.status}
          description={ticketData.description}
          comments={ticketData.comments}
          assignee={ticketData.assignee}
          assigneeAccountId={ticketData.assignee_account_id}
          assigneeHistory={ticketData.assignee_history}
          assigneeHistoryAccountIds={ticketData.assignee_history_account_ids}
          currentUserAccountId={ticketData.current_user_account_id}
          childIssues={ticketData.children}
          onActionComplete={onActionComplete}
          videoChecklistSteps={videoChecklistSteps}
        />
      </div>

      {ticketData.bounce_history && ticketData.bounce_history.length > 0 && (
        <BounceSection
          events={ticketData.bounce_history}
          pullRequests={ticketData.development_info?.pull_requests || []}
        />
      )}

      {/* Summary */}
      <Coll
        icon="sparkles"
        title="Summary"
        open={isSummaryExpanded}
        onToggle={handleToggleSummary}
        preview={
          summaryLoading
            ? 'Generating summary…'
            : summaryError
              ? 'Failed to generate summary — click to see details'
              : plainSummary
                ? (plainSummary.length > 100 ? plainSummary.slice(0, 100) + '…' : plainSummary)
                : 'Click to generate plain-English explanation'
        }
      >
        {summaryLoading && <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', color: 'var(--fg-muted)' }}><span className="spin" />Generating summary…</div>}
        {summaryError && <div style={{ color: 'var(--danger)' }}>{summaryError}</div>}
        {plainSummary && <p style={{ margin: 0, color: 'var(--fg)' }}>{plainSummary}</p>}
      </Coll>

      {/* Description */}
      <Coll
        icon="file-text"
        title="Description"
        open={isDescriptionOpen}
        onToggle={setIsDescriptionOpen}
        meta={ticketData.description ? <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>{ticketData.description.length} chars</span> : null}
      >
        {ticketData.description ? (
          <div>
            <pre style={{
              margin: 0,
              padding: 'var(--s-5)',
              background: 'var(--bg-input)',
              border: '1px solid var(--line)',
              borderRadius: 'var(--r-sm)',
              color: 'var(--fg)',
              fontFamily: 'var(--font-mono)',
              fontSize: '12px',
              lineHeight: '18px',
              whiteSpace: 'pre-wrap',
              wordWrap: 'break-word',
            }}>
              {linkifyText(getDisplayDescription(ticketData.description))}
            </pre>
            {shouldTruncateDescription(ticketData.description) && (
              <button
                type="button"
                onClick={onToggleDescription}
                className="btn"
                data-variant="ghost"
                data-size="sm"
                style={{ marginTop: 'var(--s-3)' }}
              >
                {isDescriptionExpanded ? 'Show less' : 'Show more'}
              </button>
            )}
          </div>
        ) : (
          <p style={{ margin: 0, color: 'var(--fg-subtle)' }}>No description available</p>
        )}

        {ticketData.description_quality?.gaps?.length > 0 && (
          <div style={{ marginTop: 'var(--s-5)' }}>
            <Alert tone="warning" title="Description gaps">
              <ul style={{ margin: 'var(--s-2) 0 0', paddingLeft: 18 }}>
                {ticketData.description_quality.gaps.map((gap, i) => <li key={i}>{gap}</li>)}
              </ul>
            </Alert>
          </div>
        )}
      </Coll>

      {/* Attachments */}
      {ticketData.attachments && ticketData.attachments.length > 0 && (
        <Coll
          icon="paperclip"
          title="Image Attachments"
          open={isAttachmentsExpanded}
          onToggle={setIsAttachmentsExpanded}
          preview={`${ticketData.attachments.length} attachment${ticketData.attachments.length === 1 ? '' : 's'}`}
          meta={<Chip>{ticketData.attachments.length}</Chip>}
        >
          <p style={{ margin: '0 0 var(--s-4)', color: 'var(--fg-muted)', fontSize: 'var(--t-sm)' }}>
            Images will be analyzed by the LLM to generate UI-specific test cases.
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-2)' }}>
            {ticketData.attachments.map((att, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)' }}>
                <Icon name="image" size={14} style={{ color: 'var(--fg-muted)' }} />
                <a
                  href={att.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ color: 'var(--accent)', fontSize: 'var(--t-sm)' }}
                >
                  {att.filename}
                </a>
                <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>({Math.round(att.size / 1024)} KB)</span>
              </div>
            ))}
          </div>
        </Coll>
      )}

      <DevelopmentInfo developmentInfo={ticketData.development_info} />
    </div>
  )
}

export default TicketDetails
