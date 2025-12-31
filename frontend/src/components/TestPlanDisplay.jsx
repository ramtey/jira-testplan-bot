/**
 * Display generated test plan with export options
 */

import { formatTestPlanAsMarkdown } from '../utils/markdown'

function TestPlanDisplay({ testPlan, ticketData }) {
  const handleCopyMarkdown = () => {
    const markdown = formatTestPlanAsMarkdown(testPlan, ticketData)
    navigator.clipboard.writeText(markdown)
      .then(() => alert('Test plan copied to clipboard!'))
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

      {testPlan.happy_path && testPlan.happy_path.length > 0 && (
        <div className="test-plan-group">
          <h4>✅ Happy Path Test Cases</h4>
          {testPlan.happy_path.map((test, index) => (
            <div key={index} className="test-case">
              <h5>{test.title}</h5>
              {test.steps && test.steps.length > 0 && (
                <div className="test-steps">
                  <strong>Steps:</strong>
                  <ol>
                    {test.steps.map((step, stepIndex) => (
                      <li key={stepIndex}>{step}</li>
                    ))}
                  </ol>
                </div>
              )}
              {test.expected && (
                <div className="test-expected">
                  <strong>Expected:</strong> {test.expected}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {testPlan.edge_cases && testPlan.edge_cases.length > 0 && (
        <div className="test-plan-group">
          <h4>🔍 Edge Cases</h4>
          {testPlan.edge_cases.map((test, index) => (
            <div key={index} className="test-case">
              <h5>{test.title}</h5>
              {test.steps && test.steps.length > 0 && (
                <div className="test-steps">
                  <strong>Steps:</strong>
                  <ol>
                    {test.steps.map((step, stepIndex) => (
                      <li key={stepIndex}>{step}</li>
                    ))}
                  </ol>
                </div>
              )}
              {test.expected && (
                <div className="test-expected">
                  <strong>Expected:</strong> {test.expected}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {testPlan.regression_checklist && testPlan.regression_checklist.length > 0 && (
        <div className="test-plan-group">
          <h4>🔄 Regression Checklist</h4>
          <ul className="checklist">
            {testPlan.regression_checklist.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {testPlan.non_functional && testPlan.non_functional.length > 0 && (
        <div className="test-plan-group">
          <h4>⚡ Non-Functional Tests</h4>
          <ul className="checklist">
            {testPlan.non_functional.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {testPlan.assumptions && testPlan.assumptions.length > 0 && (
        <div className="test-plan-group">
          <h4>💡 Assumptions</h4>
          <ul className="assumptions-list">
            {testPlan.assumptions.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {testPlan.questions && testPlan.questions.length > 0 && (
        <div className="test-plan-group">
          <h4>❓ Questions for PM/Dev</h4>
          <ul className="questions-list">
            {testPlan.questions.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="test-plan-actions">
        <button
          type="button"
          onClick={handleCopyMarkdown}
          className="btn-copy"
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
