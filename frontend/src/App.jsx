import { useState, useEffect } from 'react'
import './App.css'
import { API_BASE_URL, fetchConfig } from './config'
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

  // ticketsData is always an array; single-ticket mode uses ticketsData[0]
  const [ticketsData, setTicketsData] = useState([])
  const [isDescriptionExpanded, setIsDescriptionExpanded] = useState(false)

  // Test plan generation state
  const [generatingPlan, setGeneratingPlan] = useState(false)
  const [testPlan, setTestPlan] = useState(null)
  const [planError, setPlanError] = useState(null)
  const [abortController, setAbortController] = useState(null)

  // Fetch config on mount to get Jira base URL
  useEffect(() => {
    fetchConfig()
  }, [])

  const isMultiTicket = ticketsData.length > 1
  // For single-ticket backward-compat: expose first ticket as ticketData
  const ticketData = ticketsData.length === 1 ? ticketsData[0] : null

  const handleFetchTicket = async (e) => {
    e.preventDefault()

    const keys = issueKey
      .split(',')
      .map((k) => k.trim().toUpperCase())
      .filter(Boolean)

    if (keys.length === 0) {
      setError('Please enter a Jira issue key')
      return
    }

    setLoading(true)
    setError(null)
    setTicketsData([])
    setIsDescriptionExpanded(false)
    setTestPlan(null)
    setPlanError(null)

    try {
      if (keys.length === 1) {
        // ── Single ticket — existing flow unchanged ──────────────────────────
        const response = await fetch(`${API_BASE_URL}/issue/${keys[0]}`)
        if (!response.ok) {
          const errorData = await response.json()
          throw new Error(errorData.detail || 'Failed to fetch ticket')
        }
        const data = await response.json()
        setTicketsData([data])
      } else {
        // ── Multiple tickets — fetch in parallel ─────────────────────────────
        const responses = await Promise.all(
          keys.map((k) => fetch(`${API_BASE_URL}/issue/${k}`))
        )

        const results = []
        for (let i = 0; i < responses.length; i++) {
          if (!responses[i].ok) {
            const errorData = await responses[i].json()
            throw new Error(
              `Failed to fetch ${keys[i]}: ${errorData.detail || 'Unknown error'}`
            )
          }
          results.push(await responses[i].json())
        }
        setTicketsData(results)
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  const handleClear = () => {
    setIssueKey('')
    setTicketsData([])
    setError(null)
    setIsDescriptionExpanded(false)
    setTestPlan(null)
    setPlanError(null)
  }

  const toggleDescription = () => {
    setIsDescriptionExpanded(!isDescriptionExpanded)
  }

  const handleGenerateTestPlan = async () => {
    if (ticketsData.length === 0) return

    const controller = new AbortController()
    setAbortController(controller)

    setGeneratingPlan(true)
    setPlanError(null)
    setTestPlan(null)

    try {
      if (!isMultiTicket) {
        // ── Single ticket — existing flow unchanged ────────────────────────
        const td = ticketsData[0]
        const imageUrls = td.attachments ? td.attachments.map((att) => att.url) : null

        const response = await fetch(`${API_BASE_URL}/generate-test-plan`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ticket_key: td.key,
            summary: td.summary,
            description: td.description,
            issue_type: td.issue_type,
            testing_context: {},
            development_info: td.development_info,
            image_urls: imageUrls,
            comments: td.comments || null,
            parent_info: td.parent || null,
            linked_info: td.linked_issues || null,
          }),
          signal: controller.signal,
        })

        if (!response.ok) {
          const errorData = await response.json()
          throw new Error(errorData.detail || 'Failed to generate test plan')
        }

        const data = await response.json()
        setTestPlan(data)
      } else {
        // ── Multi-ticket ───────────────────────────────────────────────────
        const response = await fetch(`${API_BASE_URL}/generate-test-plan/multi`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            tickets: ticketsData.map((td) => ({
              ticket_key: td.key,
              summary: td.summary,
              description: td.description,
              issue_type: td.issue_type,
              testing_context: {},
              development_info: td.development_info,
              image_urls: td.attachments ? td.attachments.map((a) => a.url) : null,
              comments: td.comments || null,
              parent_info: td.parent || null,
              linked_info: td.linked_issues || null,
            })),
          }),
          signal: controller.signal,
        })

        if (!response.ok) {
          const errorData = await response.json()
          if (errorData.detail === 'TICKETS_NO_SHARED_CONTEXT') {
            alert(
              "These tickets don't share any code changes or repository context.\n\n" +
                'Please select tickets with related development work (same repository or overlapping files changed).'
            )
            return
          }
          throw new Error(errorData.detail || 'Failed to generate test plan')
        }

        const data = await response.json()
        setTestPlan(data)
      }
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

  // For multi-ticket: block generation if any ticket has a non-testable type
  const nonTestableTicket = ticketsData.find((td) =>
    NON_TESTABLE_ISSUE_TYPES.has(td.issue_type)
  )

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
          hasTicketData={ticketsData.length > 0}
        />

        {error && (
          <div className="alert alert-error">
            <strong>Error:</strong> {error}
          </div>
        )}

        {ticketsData.length > 0 && (
          <>
            {isMultiTicket ? (
              // ── Multi-ticket: show a compact header per ticket ─────────────
              <div className="multi-ticket-list">
                {ticketsData.map((td, idx) => (
                  <TicketDetails
                    key={td.key}
                    ticketData={td}
                    isDescriptionExpanded={isDescriptionExpanded}
                    onToggleDescription={toggleDescription}
                    compact={true}
                  />
                ))}
              </div>
            ) : (
              // ── Single ticket: original full view ──────────────────────────
              <TicketDetails
                ticketData={ticketData}
                isDescriptionExpanded={isDescriptionExpanded}
                onToggleDescription={toggleDescription}
              />
            )}

            {nonTestableTicket ? (
              <div className="ticket-section">
                <div className="alert alert-info">
                  <strong>ℹ️ Note:</strong> Test plans are not generated for{' '}
                  {nonTestableTicket.issue_type} tickets ({nonTestableTicket.key}).
                  <br />
                  Test plan generation is available for Story, Task, and Bug issues only.
                </div>
              </div>
            ) : (
              <>
                <TestingContextForm
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
                    ticketsData={isMultiTicket ? ticketsData : null}
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
