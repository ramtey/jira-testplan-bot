/**
 * Display generated test plan with export options
 */

import { formatTestPlanAsMarkdown, formatTestPlanAsJira } from '../utils/markdown'

function TestPlanDisplay({ testPlan, ticketData }) {
  // Add safety check and logging
  if (!testPlan) {
    return <div className="ticket-section">No test plan data available</div>
  }

  console.log('TestPlanDisplay - testPlan:', testPlan)
  console.log('TestPlanDisplay - ticketData:', ticketData)

  const handleCopyMarkdown = () => {
    const markdown = formatTestPlanAsMarkdown(testPlan, ticketData)
    navigator.clipboard.writeText(markdown)
      .then(() => alert('Test plan copied to clipboard!'))
      .catch(() => alert('Failed to copy to clipboard'))
  }

  const handleCopyJira = () => {
    const jira = formatTestPlanAsJira(testPlan, ticketData)
    navigator.clipboard.writeText(jira)
      .then(() => alert('Test plan copied to clipboard in Jira format!'))
      .catch(() => alert('Failed to copy to clipboard'))
  }

  const handleDownloadMarkdown = () => {
    const markdown = formatTestPlanAsMarkdown(testPlan, ticketData)
    const blob = new Blob([markdown], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `test-plan-${ticketData.key}.md`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  return (
    <div className="ticket-section test-plan-section">
      <h3>Generated Test Plan</h3>

      {testPlan.happy_path && Array.isArray(testPlan.happy_path) && testPlan.happy_path.length > 0 && (
        <div className="test-plan-group">
          <h4>✅ Happy Path Test Cases</h4>
          {testPlan.happy_path.map((test, index) => (
            <div key={index} className="test-case">
              <h5>{typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}</h5>
              {test.steps && Array.isArray(test.steps) && test.steps.length > 0 && (
                <div className="test-steps">
                  <strong>Steps:</strong>
                  <ol>
                    {test.steps.map((step, stepIndex) => (
                      <li key={stepIndex}>{typeof step === 'string' ? step : JSON.stringify(step)}</li>
                    ))}
                  </ol>
                </div>
              )}
              {test.expected && (
                <div className="test-expected">
                  <strong>Expected:</strong> {typeof test.expected === 'string' ? test.expected : JSON.stringify(test.expected)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {testPlan.edge_cases && Array.isArray(testPlan.edge_cases) && testPlan.edge_cases.length > 0 && (
        <div className="test-plan-group">
          <h4>🔍 Edge Cases</h4>
          {testPlan.edge_cases.map((test, index) => (
            <div key={index} className="test-case">
              <h5>{typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}</h5>
              {test.steps && Array.isArray(test.steps) && test.steps.length > 0 && (
                <div className="test-steps">
                  <strong>Steps:</strong>
                  <ol>
                    {test.steps.map((step, stepIndex) => (
                      <li key={stepIndex}>{typeof step === 'string' ? step : JSON.stringify(step)}</li>
                    ))}
                  </ol>
                </div>
              )}
              {test.expected && (
                <div className="test-expected">
                  <strong>Expected:</strong> {typeof test.expected === 'string' ? test.expected : JSON.stringify(test.expected)}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {testPlan.regression_checklist && Array.isArray(testPlan.regression_checklist) && testPlan.regression_checklist.length > 0 && (
        <div className="test-plan-group">
          <h4>🔄 Regression Checklist</h4>
          <ul className="checklist">
            {testPlan.regression_checklist.map((item, index) => (
              <li key={index}>{typeof item === 'string' ? item : JSON.stringify(item)}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="test-plan-actions">
        <button
          type="button"
          onClick={handleCopyJira}
          className="btn-copy"
        >
          Copy for Jira
        </button>
        <button
          type="button"
          onClick={handleCopyMarkdown}
          className="btn-copy-markdown"
        >
          Copy as Markdown
        </button>
        <button
          type="button"
          onClick={handleDownloadMarkdown}
          className="btn-download"
        >
          Download as .md
        </button>
      </div>
    </div>
  )
}

export default TestPlanDisplay
