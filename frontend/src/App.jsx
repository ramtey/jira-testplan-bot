import { useState, useEffect } from 'react'
import './App.css'
import { API_BASE_URL, fetchConfig } from './config'
import { initialTestingContext, resetTestingContext } from './utils/stateHelpers'
import TicketForm from './components/TicketForm'
import TicketDetails from './components/TicketDetails'
import TestingContextForm from './components/TestingContextForm'
import TestPlanDisplay from './components/TestPlanDisplay'
import TokenStatus from './components/TokenStatus'

// Issue types that don't require test plans
const NON_TESTABLE_ISSUE_TYPES = new Set(['Epic', 'Spike', 'Sub-task'])

function App() {
  const [issueKey, setIssueKey] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [ticketData, setTicketData] = useState(null)
  const [isDescriptionExpanded, setIsDescriptionExpanded] = useState(false)

  // Testing context form state
  const [testingContext, setTestingContext] = useState(initialTestingContext)

  // Test plan generation state
  const [generatingPlan, setGeneratingPlan] = useState(false)
  const [testPlan, setTestPlan] = useState(null)
  const [planError, setPlanError] = useState(null)
  const [abortController, setAbortController] = useState(null)

  // Fetch config on mount to get Jira base URL
  useEffect(() => {
    fetchConfig()
  }, [])

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
    setTestingContext(resetTestingContext())
    setTestPlan(null)
    setPlanError(null)

    try {
      const response = await fetch(`${API_BASE_URL}/issue/${issueKey.trim()}`)

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
    setTestingContext(resetTestingContext())
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

  const handleGenerateTestPlan = async () => {
    if (!ticketData) return

    // Create new AbortController for this request
    const controller = new AbortController()
    setAbortController(controller)

    setGeneratingPlan(true)
    setPlanError(null)
    setTestPlan(null)

    try {
      // Extract image URLs from attachments if available
      const imageUrls = ticketData.attachments
        ? ticketData.attachments.map(att => att.url)
        : null

      const response = await fetch(`${API_BASE_URL}/generate-test-plan`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          ticket_key: ticketData.key,
          summary: ticketData.summary,
          description: ticketData.description,
          issue_type: ticketData.issue_type,
          testing_context: {
            acceptanceCriteria: testingContext.acceptanceCriteria,
            specialInstructions: testingContext.specialInstructions,
          },
          development_info: ticketData.development_info,
          image_urls: imageUrls,
        }),
        signal: controller.signal,
      })

      if (!response.ok) {
        const errorData = await response.json()
        throw new Error(errorData.detail || 'Failed to generate test plan')
      }

      const data = await response.json()
      setTestPlan(data)
    } catch (err) {
      if (err.name === 'AbortError') {
        setPlanError('Test plan generation was cancelled')
      } else {
        setPlanError(err.message)
      }
    } finally {
      setGeneratingPlan(false)
      setAbortController(null)
    }
  }

  const handleStopGeneration = () => {
    if (abortController) {
      abortController.abort()
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Jira Test Plan Bot</h1>
        <p>Fetch Jira tickets and analyze description quality</p>
      </header>

      <main className="app-main">
        <TokenStatus />

        <TicketForm
          issueKey={issueKey}
          setIssueKey={setIssueKey}
          loading={loading}
          onSubmit={handleFetchTicket}
          onClear={handleClear}
          hasTicketData={!!ticketData}
        />

        {error && (
          <div className="alert alert-error">
            <strong>Error:</strong> {error}
          </div>
        )}

        {ticketData && (
          <>
            <TicketDetails
              ticketData={ticketData}
              isDescriptionExpanded={isDescriptionExpanded}
              onToggleDescription={toggleDescription}
            />

            {NON_TESTABLE_ISSUE_TYPES.has(ticketData.issue_type) ? (
              <div className="ticket-section">
                <div className="alert alert-info">
                  <strong>ℹ️ Note:</strong> Test plans are not generated for {ticketData.issue_type} tickets.
                  <br />
                  Test plan generation is available for Story, Task, and Bug issues only.
                </div>
              </div>
            ) : (
              <>
                <TestingContextForm
                  ticketData={ticketData}
                  testingContext={testingContext}
                  onContextChange={handleContextChange}
                  onGenerateTestPlan={handleGenerateTestPlan}
                  onStopGeneration={handleStopGeneration}
                  generatingPlan={generatingPlan}
                />

                {planError && (
                  <div className="alert alert-error">
                    <strong>Error:</strong> {planError}
                  </div>
                )}

                {testPlan && (
                  <TestPlanDisplay
                    testPlan={testPlan}
                    ticketData={ticketData}
                  />
                )}
              </>
            )}
          </>
        )}
      </main>
    </div>
  )
}

export default App
