/**
 * Display bug analysis results from Jira Bug Lens.
 */

import { formatBugAnalysisAsMarkdown } from '../utils/markdown'

function BugAnalysisDisplay({ analysis }) {
  const isMulti = Array.isArray(analysis.ticket_keys)
  const ticketLabel = isMulti
    ? analysis.ticket_keys.join(', ')
    : analysis.ticket_key

  const handleDownloadMarkdown = () => {
    const markdown = formatBugAnalysisAsMarkdown(analysis)
    const blob = new Blob([markdown], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = isMulti
      ? `bug-analysis-${analysis.ticket_keys.join('-')}.md`
      : `bug-analysis-${analysis.ticket_key}.md`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  return (
    <div className="test-plan-display">
      <div className="test-plan-header">
        <h2>Bug Lens Analysis</h2>
        <p className="ticket-key-label">{ticketLabel}</p>
      </div>

      {/* Fix status badge */}
      <div className="ticket-section">
        <span
          className={`priority-badge ${analysis.is_fixed ? 'priority-high' : 'priority-critical'}`}
          style={{ fontSize: '0.95rem', padding: '4px 12px' }}
        >
          {analysis.is_fixed ? '✅ Fixed' : '⚠️ Not yet fixed'}
        </span>
      </div>

      {/* Bug Summary */}
      <div className="ticket-section">
        <h3>Bug Summary</h3>
        <p>{analysis.bug_summary}</p>
      </div>

      {/* Root Cause */}
      <div className="ticket-section">
        <h3>Root Cause</h3>
        {analysis.root_cause ? (
          <p>{analysis.root_cause}</p>
        ) : (
          <p className="text-muted">No code diff available — root cause derived from ticket description only.</p>
        )}
      </div>

      {/* Fix Explanation */}
      {analysis.is_fixed && (
        <div className="ticket-section">
          <h3>Fix Explanation</h3>
          {analysis.fix_explanation ? (
            <p>{analysis.fix_explanation}</p>
          ) : (
            <p className="text-muted">No fix details available.</p>
          )}
        </div>
      )}

      {/* Regression Tests */}
      {analysis.regression_tests && analysis.regression_tests.length > 0 && (
        <div className="ticket-section">
          <h3>Regression Tests</h3>
          <p className="section-description">
            Run these to confirm the bug does not recur:
          </p>
          <ul className="regression-list">
            {analysis.regression_tests.map((test, i) => (
              <li key={i}>{test}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Similar Bug Patterns */}
      {analysis.similar_patterns && analysis.similar_patterns.length > 0 && (
        <div className="ticket-section">
          <h3>Similar Bug Patterns to Watch</h3>
          <p className="section-description">
            Related classes of bugs that may exist elsewhere in the codebase:
          </p>
          <ul className="regression-list">
            {analysis.similar_patterns.map((pattern, i) => (
              <li key={i}>{pattern}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="test-plan-actions">
        <button type="button" onClick={handleDownloadMarkdown} className="btn-download">
          Download as .md
        </button>
      </div>
    </div>
  )
}

export default BugAnalysisDisplay
