/**
 * Display bug analysis results from Jira Bug Lens.
 */

import { formatBugAnalysisAsMarkdown } from '../utils/markdown'

function BugAnalysisDisplay({ analysis }) {
  const isMulti = Array.isArray(analysis.ticket_keys)

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

  const allKeys = isMulti ? analysis.ticket_keys : [analysis.ticket_key]

  const fixStatus = analysis.fix_status || (analysis.is_fixed ? 'fixed' : 'not_fixed')
  const fixStatusBadge = {
    fixed: { label: '✅ Fixed', className: 'priority-high' },
    in_testing: { label: '🧪 In testing — fix awaiting QA', className: 'priority-medium' },
    not_fixed: { label: '⚠️ Not yet fixed', className: 'priority-critical' },
  }[fixStatus] || { label: '⚠️ Not yet fixed', className: 'priority-critical' }

  return (
    <div className="test-plan-display">
      <div className="test-plan-header">
        <h2>
          Bug Lens Analysis
          <span className="multi-ticket-badge multi-ticket-badge--bug">
            {allKeys.join(' + ')}
          </span>
        </h2>
      </div>

      {/* Fix status badge */}
      <div className="ticket-section">
        <span
          className={`priority-badge ${fixStatusBadge.className}`}
          style={{ fontSize: '0.95rem', padding: '4px 12px' }}
        >
          {fixStatusBadge.label}
        </span>
      </div>

      {/* Regression badge */}
      {analysis.is_regression != null && (
        <div className="ticket-section">
          <span
            className={`priority-badge ${analysis.is_regression ? 'priority-critical' : 'priority-medium'}`}
            style={{ fontSize: '0.85rem', padding: '3px 10px' }}
          >
            {analysis.is_regression ? '🔁 Regression' : '🆕 Never worked'}
          </span>
          {analysis.is_regression && analysis.regression_introduced_by && (
            <span className="fix-complexity-reasoning" style={{ marginLeft: '8px' }}>
              Introduced by: {analysis.regression_introduced_by}
            </span>
          )}
        </div>
      )}

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

      {/* Affected Flow */}
      {analysis.affected_flow && analysis.affected_flow.length > 0 && (
        <div className="ticket-section">
          <h3>Affected Flow</h3>
          <p className="section-description">End-to-end path from user action to the bug:</p>
          <ol className="regression-list">
            {analysis.affected_flow.map((step, i) => (
              <li key={i}>{step.replace(/^\s*\d+[.)]\s+/, '')}</li>
            ))}
          </ol>
        </div>
      )}

      {/* Scope of Impact */}
      {analysis.scope_of_impact && analysis.scope_of_impact.length > 0 && (
        <div className="ticket-section">
          <h3>Scope of Impact</h3>
          <p className="section-description">Other features or callers affected by the same broken code:</p>
          <ul className="regression-list">
            {analysis.scope_of_impact.map((item, i) => (
              <li key={i}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Code Evidence — deterministic grep results for suspect symbols.
          Hide individual no-match entries; only show a fallback if ALL suspects missed. */}
      {analysis.code_evidence && analysis.code_evidence.length > 0 && (() => {
        const withHits = analysis.code_evidence.filter(e => e.usages && e.usages.length > 0)
        if (withHits.length === 0) {
          return (
            <div className="ticket-section">
              <h3>Code Evidence</h3>
              <p className="text-muted">
                Searched for the suspected symbols but none were found in the candidate repos — the bug may live in a different repo, or the suspects were off. Verify the repo mapping and the symbol names before acting.
              </p>
            </div>
          )
        }
        return (
          <div className="ticket-section">
            <h3>Code Evidence</h3>
            <p className="section-description">
              Places the suspected symbols actually appear in the repo — verify before acting.
            </p>
            {withHits.map((entry, i) => (
              <div key={i} className="code-evidence-entry">
                <h4 className="code-evidence-heading">
                  <code>{entry.suspect}</code>
                  <span className="code-evidence-repo"> in {entry.repo}</span>
                </h4>
                <ul className="regression-list">
                  {entry.usages.map((u, j) => (
                    <li key={j}>
                      <a
                        href={encodeURI(`https://github.com/${entry.repo}/blob/${u.ref}/${u.path}`) + `#L${u.line}`}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        <code>{u.path}:{u.line}</code>
                      </a>
                      {u.snippet && (
                        <pre className="code-evidence-snippet"><code>{u.snippet}</code></pre>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        )
      })()}

      {/* Why Tests Miss */}
      {analysis.why_tests_miss && (
        <div className="ticket-section">
          <h3>Why Tests Don't Catch This</h3>
          <p>{analysis.why_tests_miss}</p>
        </div>
      )}

      {/* Fix Explanation — shown when fix is in (fixed) or in flight (in_testing) */}
      {(fixStatus === 'fixed' || fixStatus === 'in_testing') && (
        <div className="ticket-section">
          <h3>Fix Explanation</h3>
          {analysis.fix_explanation ? (
            <p>{analysis.fix_explanation}</p>
          ) : (
            <p className="text-muted">No fix details available.</p>
          )}
          {fixStatus === 'in_testing' && (
            <p className="section-description">
              The code change is in but QA hasn't validated it yet — confirm the fix behaves correctly before closing the ticket.
            </p>
          )}
        </div>
      )}

      {/* Open Questions — surface ambiguity before the estimate */}
      {analysis.open_questions && analysis.open_questions.length > 0 && (
        <div className="ticket-section">
          <h3>Open Questions</h3>
          <p className="section-description">Resolve these before committing to an estimate or fix:</p>
          <ul className="regression-list">
            {analysis.open_questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Assumptions — inferences the analysis depends on */}
      {analysis.assumptions && analysis.assumptions.length > 0 && (
        <div className="ticket-section">
          <h3>Assumptions</h3>
          <p className="section-description">Inferences not directly grounded in the evidence — verify before acting:</p>
          <ul className="regression-list">
            {analysis.assumptions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        </div>
      )}

      {/* Fix Complexity — only when no code change is in yet */}
      {fixStatus === 'not_fixed' && analysis.fix_complexity && (
        <div className="ticket-section">
          <h3>Fix Complexity</h3>
          <div className="fix-complexity-row">
            <span className={`fix-complexity-badge fix-complexity-${analysis.fix_complexity}`}>
              {analysis.fix_complexity.charAt(0).toUpperCase() + analysis.fix_complexity.slice(1)}
            </span>
            {analysis.fix_effort_estimate && (
              <span className="fix-effort-estimate">{analysis.fix_effort_estimate}</span>
            )}
          </div>
          {analysis.fix_complexity_reasoning && (
            <p className="fix-complexity-reasoning">{analysis.fix_complexity_reasoning}</p>
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
