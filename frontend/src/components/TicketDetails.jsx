/**
 * Display Jira ticket details and quality analysis.
 */

import { useState } from 'react'
import { API_BASE_URL, getJiraTicketUrl } from '../config'
import DevelopmentInfo from './DevelopmentInfo'
import WorkflowActions from './WorkflowActions'
import Icon from './Icon'
import { ItChip, StatPill, Asn, Tag, Coll, Alert, ACTag, Chip } from './ui'

function statusCategory(status) {
  const s = (status || '').toLowerCase()
  if (/done|closed|complete|resolved/.test(s)) return 'done'
  if (/progress|review|qa|testing|uat/.test(s)) return 'inprogress'
  if (/block/.test(s)) return 'blocked'
  return 'todo'
}

function TicketDetails({ ticketData, isDescriptionExpanded, onToggleDescription, onActionComplete, compact = false }) {
  const [isAttachmentsExpanded, setIsAttachmentsExpanded] = useState(false)
  const [isSummaryExpanded, setIsSummaryExpanded] = useState(false)
  const [plainSummary, setPlainSummary] = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [summaryError, setSummaryError] = useState(null)
  const [isDescriptionOpen, setIsDescriptionOpen] = useState(true)

  const handleToggleSummary = async () => {
    if (isSummaryExpanded) {
      setIsSummaryExpanded(false)
      return
    }

    setIsSummaryExpanded(true)

    if (plainSummary === null && !summaryLoading) {
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
        />
      </div>

      {/* Summary */}
      <Coll
        icon="sparkles"
        title="Summary"
        open={isSummaryExpanded}
        onToggle={handleToggleSummary}
        preview={plainSummary ? (plainSummary.length > 100 ? plainSummary.slice(0, 100) + '…' : plainSummary) : 'Click to generate plain-English explanation'}
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
              {getDisplayDescription(ticketData.description)}
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
