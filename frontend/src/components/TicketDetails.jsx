/**
 * Display Jira ticket details and quality analysis
 */

import { useState } from 'react'
import { API_BASE_URL, getJiraTicketUrl } from '../config'
import DevelopmentInfo from './DevelopmentInfo'

function TicketDetails({ ticketData, isDescriptionExpanded, onToggleDescription }) {
  const [isAttachmentsExpanded, setIsAttachmentsExpanded] = useState(false)
  const [isSummaryExpanded, setIsSummaryExpanded] = useState(false)
  const [plainSummary, setPlainSummary] = useState(null)
  const [summaryLoading, setSummaryLoading] = useState(false)
  const [summaryError, setSummaryError] = useState(null)

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

  const getIssueTypeClass = (issueType) => {
    if (!issueType) return ''
    return issueType.toLowerCase()
  }

  const shouldTruncateDescription = (description) => {
    return description && description.length > 500
  }

  const getDisplayDescription = (description) => {
    if (!description) return ''
    if (isDescriptionExpanded || !shouldTruncateDescription(description)) {
      return description
    }
    return description.substring(0, 500) + '...'
  }

  return (
    <div className="ticket-details">
      <div className="ticket-header">
        <h2>
          {jiraTicketUrl ? (
            <a
              href={jiraTicketUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="ticket-key-link"
            >
              {ticketData.key}
            </a>
          ) : (
            <span className="ticket-key">{ticketData.key}</span>
          )}
          {ticketData.summary}
        </h2>
        <div className="ticket-meta">
          <span className={`issue-type ${getIssueTypeClass(ticketData.issue_type)}`}>
            {ticketData.issue_type}
          </span>
          {ticketData.assignee_history && ticketData.assignee_history.length > 0 ? (
            <div className="assignee-history">
              {ticketData.assignee_history.map((name, index) => (
                <span
                  key={index}
                  className={`assignee-tag${name === ticketData.assignee ? ' current' : ''}`}
                  title={name === ticketData.assignee ? 'Current assignee' : 'Previously assigned'}
                >
                  👤 {name}
                </span>
              ))}
            </div>
          ) : ticketData.assignee ? (
            <span className="assignee-tag current" title="Current assignee">👤 {ticketData.assignee}</span>
          ) : null}
          {ticketData.labels && ticketData.labels.length > 0 && (
            <div className="labels">
              {ticketData.labels.map((label, index) => (
                <span key={index} className="label">{label}</span>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="ticket-section">
        <div
          className="collapsible-header"
          onClick={handleToggleSummary}
          style={{ cursor: 'pointer' }}
        >
          <h3>
            <span className={`collapse-icon ${isSummaryExpanded ? 'expanded' : ''}`}>▶</span>
            Plain-language Summary
          </h3>
          {!isSummaryExpanded && (
            <span className="collapse-summary">
              {plainSummary
                ? plainSummary.length > 100
                  ? plainSummary.slice(0, 100) + '…'
                  : plainSummary
                : 'Click to generate'}
            </span>
          )}
        </div>
        {isSummaryExpanded && (
          <div className="collapsible-content">
            {summaryLoading && <p className="loading-text">Generating summary...</p>}
            {summaryError && <p className="error-text">{summaryError}</p>}
            {plainSummary && <p className="plain-summary-text">{plainSummary}</p>}
          </div>
        )}
      </div>

      <div className="ticket-section">
        <h3>Description</h3>
        {ticketData.description ? (
          <div>
            <pre className="description-text">
              {getDisplayDescription(ticketData.description)}
            </pre>
            {shouldTruncateDescription(ticketData.description) && (
              <button
                type="button"
                onClick={onToggleDescription}
                className="btn-expand"
              >
                {isDescriptionExpanded ? 'Show less' : 'Show more'}
              </button>
            )}
          </div>
        ) : (
          <p className="no-data">No description available</p>
        )}
      </div>

      <div className="ticket-section">
        <h3>Description Quality Analysis</h3>
        <div className="quality-metrics">
          <div className="metric">
            <span className="metric-label">Has Description:</span>
            <span className={`metric-value ${ticketData.description_quality.has_description ? 'success' : 'warning'}`}>
              {ticketData.description_quality.has_description ? '✓ Yes' : '✗ No'}
            </span>
          </div>
          <div className="metric">
            <span className="metric-label">Quality:</span>
            <span className={`metric-value ${ticketData.description_quality.is_weak ? 'warning' : 'success'}`}>
              {ticketData.description_quality.is_weak ? 'Weak' : 'Good'}
            </span>
          </div>
          <div className="metric">
            <span className="metric-label">Characters:</span>
            <span className="metric-value">{ticketData.description_quality.char_count}</span>
          </div>
          <div className="metric">
            <span className="metric-label">Words:</span>
            <span className="metric-value">{ticketData.description_quality.word_count}</span>
          </div>
        </div>

        {ticketData.description_quality.warnings.length > 0 && (
          <div className="warnings">
            <h4>⚠️ Warnings</h4>
            <ul>
              {ticketData.description_quality.warnings.map((warning, index) => (
                <li key={index}>{warning}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {ticketData.attachments && ticketData.attachments.length > 0 && (
        <div className="ticket-section">
          <div
            className="collapsible-header"
            onClick={() => setIsAttachmentsExpanded(!isAttachmentsExpanded)}
          >
            <h3>
              <span className={`collapse-icon ${isAttachmentsExpanded ? 'expanded' : ''}`}>▶</span>
              📎 Image Attachments ({ticketData.attachments.length})
            </h3>
            {!isAttachmentsExpanded && (
              <span className="collapse-summary">Click to expand</span>
            )}
          </div>

          {isAttachmentsExpanded && (
            <div className="collapsible-content">
              <div className="attachments-info">
                <p className="info-note">
                  🖼️ Images will be analyzed by the LLM to generate UI-specific test cases
                </p>
                <div className="attachments-list">
                  {ticketData.attachments.map((attachment, index) => (
                    <div key={index} className="attachment-item">
                      <span className="attachment-icon">🖼️</span>
                      <a
                        href={attachment.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="attachment-link"
                      >
                        {attachment.filename}
                      </a>
                      <span className="attachment-size">
                        ({Math.round(attachment.size / 1024)} KB)
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      <DevelopmentInfo developmentInfo={ticketData.development_info} />
    </div>
  )
}

export default TicketDetails
