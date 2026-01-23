/**
 * Display Jira ticket details and quality analysis
 */

import { getJiraTicketUrl } from '../config'
import DevelopmentInfo from './DevelopmentInfo'

function TicketDetails({ ticketData, isDescriptionExpanded, onToggleDescription }) {
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

      <DevelopmentInfo developmentInfo={ticketData.development_info} />
    </div>
  )
}

export default TicketDetails
