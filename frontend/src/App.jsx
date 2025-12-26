import { useState } from 'react'
import './App.css'

function App() {
  const [issueKey, setIssueKey] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [ticketData, setTicketData] = useState(null)
  const [isDescriptionExpanded, setIsDescriptionExpanded] = useState(false)

  // Testing context form state
  const [testingContext, setTestingContext] = useState({
    acceptanceCriteria: '',
    testDataNotes: '',
    environments: '',
    rolesPermissions: '',
    outOfScope: '',
    riskAreas: ''
  })

  // Test plan generation state
  const [generatingPlan, setGeneratingPlan] = useState(false)
  const [testPlan, setTestPlan] = useState(null)
  const [planError, setPlanError] = useState(null)

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
    setTestingContext({
      acceptanceCriteria: '',
      testDataNotes: '',
      environments: '',
      rolesPermissions: '',
      outOfScope: '',
      riskAreas: ''
    })

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
    setTestingContext({
      acceptanceCriteria: '',
      testDataNotes: '',
      environments: '',
      rolesPermissions: '',
      outOfScope: '',
      riskAreas: ''
    })
    setTestPlan(null)
    setPlanError(null)
  }

  const handleContextChange = (field, value) => {
    setTestingContext(prev => ({
      ...prev,
      [field]: value
    }))
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

  const handleGenerateTestPlan = async () => {
    if (!ticketData) return

    setGeneratingPlan(true)
    setPlanError(null)
    setTestPlan(null)

    try {
      const response = await fetch('http://localhost:8000/generate-test-plan', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          ticket_key: ticketData.key,
          summary: ticketData.summary,
          description: ticketData.description,
          testing_context: {
            acceptanceCriteria: testingContext.acceptanceCriteria,
            testDataNotes: testingContext.testDataNotes,
            environments: testingContext.environments,
            rolesPermissions: testingContext.rolesPermissions,
            outOfScope: testingContext.outOfScope,
            riskAreas: testingContext.riskAreas,
          },
        }),
      })

      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.detail || 'Failed to generate test plan')
      }

      const data = await response.json()
      setTestPlan(data)
    } catch (err) {
      setPlanError(err.message)
    } finally {
      setGeneratingPlan(false)
    }
  }

  const formatTestPlanAsMarkdown = (plan) => {
    let markdown = `# Test Plan: ${ticketData.key}\n\n`
    markdown += `## ${ticketData.summary}\n\n`

    if (plan.happy_path && plan.happy_path.length > 0) {
      markdown += '## ✅ Happy Path Test Cases\n\n'
      plan.happy_path.forEach((test, index) => {
        markdown += `### ${index + 1}. ${test.title}\n\n`
        if (test.steps && test.steps.length > 0) {
          markdown += '**Steps:**\n'
          test.steps.forEach((step, stepIndex) => {
            markdown += `${stepIndex + 1}. ${step}\n`
          })
          markdown += '\n'
        }
        if (test.expected) {
          markdown += `**Expected Result:** ${test.expected}\n\n`
        }
      })
    }

    if (plan.edge_cases && plan.edge_cases.length > 0) {
      markdown += '## 🔍 Edge Cases\n\n'
      plan.edge_cases.forEach((test, index) => {
        markdown += `### ${index + 1}. ${test.title}\n\n`
        if (test.steps && test.steps.length > 0) {
          markdown += '**Steps:**\n'
          test.steps.forEach((step, stepIndex) => {
            markdown += `${stepIndex + 1}. ${step}\n`
          })
          markdown += '\n'
        }
        if (test.expected) {
          markdown += `**Expected Result:** ${test.expected}\n\n`
        }
      })
    }

    if (plan.regression_checklist && plan.regression_checklist.length > 0) {
      markdown += '## 🔄 Regression Checklist\n\n'
      plan.regression_checklist.forEach(item => {
        markdown += `- ${item}\n`
      })
      markdown += '\n'
    }

    if (plan.non_functional && plan.non_functional.length > 0) {
      markdown += '## ⚡ Non-Functional Tests\n\n'
      plan.non_functional.forEach(item => {
        markdown += `- ${item}\n`
      })
      markdown += '\n'
    }

    if (plan.assumptions && plan.assumptions.length > 0) {
      markdown += '## 💡 Assumptions\n\n'
      plan.assumptions.forEach(item => {
        markdown += `- ${item}\n`
      })
      markdown += '\n'
    }

    if (plan.questions && plan.questions.length > 0) {
      markdown += '## ❓ Questions for PM/Dev\n\n'
      plan.questions.forEach(item => {
        markdown += `- ${item}\n`
      })
      markdown += '\n'
    }

    return markdown
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

            <div className="ticket-section">
              <h3>Additional Testing Context</h3>
              <p className="section-description">
                Provide supplemental information to improve test plan quality (all fields optional)
              </p>

              <div className="context-form">
                <div className="form-field">
                  <label htmlFor="acceptanceCriteria">
                    Acceptance Criteria
                    {(!ticketData.description || ticketData.description_quality.is_weak) && (
                      <span className="field-suggested"> (Recommended)</span>
                    )}
                  </label>
                  <textarea
                    id="acceptanceCriteria"
                    placeholder="e.g., Given a user clicks 'Forgot Password', when they enter their email, then they receive a reset link"
                    value={testingContext.acceptanceCriteria}
                    onChange={(e) => handleContextChange('acceptanceCriteria', e.target.value)}
                    rows="3"
                  />
                </div>

                <div className="form-field">
                  <label htmlFor="testDataNotes">Test Data Notes</label>
                  <textarea
                    id="testDataNotes"
                    placeholder="e.g., Test accounts, roles, sample data needed"
                    value={testingContext.testDataNotes}
                    onChange={(e) => handleContextChange('testDataNotes', e.target.value)}
                    rows="3"
                  />
                </div>

                <div className="form-field">
                  <label htmlFor="environments">Environments</label>
                  <textarea
                    id="environments"
                    placeholder="e.g., Staging/prod flags, feature flags, configuration notes"
                    value={testingContext.environments}
                    onChange={(e) => handleContextChange('environments', e.target.value)}
                    rows="2"
                  />
                </div>

                <div className="form-field">
                  <label htmlFor="rolesPermissions">Roles/Permissions</label>
                  <textarea
                    id="rolesPermissions"
                    placeholder="e.g., Admin, user, guest - which roles need testing?"
                    value={testingContext.rolesPermissions}
                    onChange={(e) => handleContextChange('rolesPermissions', e.target.value)}
                    rows="2"
                  />
                </div>

                <div className="form-field">
                  <label htmlFor="outOfScope">Out of Scope / Assumptions</label>
                  <textarea
                    id="outOfScope"
                    placeholder="e.g., What's explicitly not included in this change?"
                    value={testingContext.outOfScope}
                    onChange={(e) => handleContextChange('outOfScope', e.target.value)}
                    rows="2"
                  />
                </div>

                <div className="form-field">
                  <label htmlFor="riskAreas">Known Risk Areas / Impacted Modules</label>
                  <textarea
                    id="riskAreas"
                    placeholder="e.g., Authentication flow, payment processing, data migration"
                    value={testingContext.riskAreas}
                    onChange={(e) => handleContextChange('riskAreas', e.target.value)}
                    rows="2"
                  />
                </div>
              </div>

              <div className="generate-section">
                <button
                  type="button"
                  onClick={handleGenerateTestPlan}
                  disabled={generatingPlan}
                  className="btn-generate"
                >
                  {generatingPlan ? (
                    <>
                      <span className="spinner"></span>
                      Generating Test Plan<span className="dots"></span>
                    </>
                  ) : (
                    'Generate Test Plan'
                  )}
                </button>
                {generatingPlan && (
                  <p className="generation-message">
                    This may take 30-120 seconds. Please wait...
                  </p>
                )}
              </div>
            </div>

            {planError && (
              <div className="alert alert-error">
                <strong>Error:</strong> {planError}
              </div>
            )}

            {testPlan && (
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
                    onClick={() => {
                      const markdown = formatTestPlanAsMarkdown(testPlan)
                      navigator.clipboard.writeText(markdown)
                        .then(() => alert('Test plan copied to clipboard!'))
                        .catch(() => alert('Failed to copy to clipboard'))
                    }}
                    className="btn-copy"
                  >
                    Copy as Markdown
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  )
}

export default App
