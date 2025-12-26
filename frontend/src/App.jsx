import { useState } from 'react'
import './App.css'

function App() {
  const [issueKey, setIssueKey] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [ticketData, setTicketData] = useState(null)
  const [isDescriptionExpanded, setIsDescriptionExpanded] = useState(false)

  const handleFetchTicket = async (e) => {
    e.preventDefault()

    if (!issueKey.trim()) {
      setError('Please enter a Jira issue key')
      return
    }

    setLoading(true)
    setError(null)
    setTicketData(null)
    setIsDescriptionExpanded(false)

    try {
      const response = await fetch(`http://localhost:8000/issue/${issueKey.trim()}`)

      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.detail || 'Failed to fetch ticket')
      }

      const data = await response.json()
      setTicketData(data)
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const handleClear = () => {
    setIssueKey('')
    setTicketData(null)
    setError(null)
    setIsDescriptionExpanded(false)
  }

  const toggleDescription = () => {
    setIsDescriptionExpanded(!isDescriptionExpanded)
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

  const getIssueTypeClass = (issueType) => {
    if (!issueType) return ''
    const type = issueType.toLowerCase()
    return type
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Jira Test Plan Bot</h1>
        <p>Fetch Jira tickets and analyze description quality</p>
      </header>

      <main className="app-main">
        <form onSubmit={handleFetchTicket} className="ticket-form">
          <div className="form-group">
            <label htmlFor="issueKey">Jira Issue Key</label>
            <div className="input-group">
              <input
                id="issueKey"
                type="text"
                placeholder="e.g., PROJ-123"
                value={issueKey}
                onChange={(e) => setIssueKey(e.target.value)}
                disabled={loading}
                autoComplete="off"
              />
              <button type="submit" disabled={loading}>
                {loading ? 'Fetching...' : 'Fetch Ticket'}
              </button>
              {ticketData && (
                <button type="button" onClick={handleClear} className="btn-clear">
                  Clear
                </button>
              )}
            </div>
          </div>
        </form>

        {error && (
          <div className="alert alert-error">
            <strong>Error:</strong> {error}
          </div>
        )}

        {ticketData && (
          <div className="ticket-details">
            <div className="ticket-header">
              <h2>
                <span className="ticket-key">{ticketData.key}</span>
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
                      onClick={toggleDescription}
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
          </div>
        )}
      </main>
    </div>
  )
}

export default App
