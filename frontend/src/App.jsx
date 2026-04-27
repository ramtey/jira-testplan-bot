import { useState, useEffect, useRef } from 'react'
import './App.css'
import { API_BASE_URL, fetchConfig } from './config'
import TicketForm from './components/TicketForm'
import TicketDetails from './components/TicketDetails'
import TestingContextForm from './components/TestingContextForm'
import TestPlanDisplay from './components/TestPlanDisplay'
import BugAnalysisDisplay from './components/BugAnalysisDisplay'
import TokenStatus from './components/TokenStatus'

// Issue types that don't require test plans
const NON_TESTABLE_ISSUE_TYPES = new Set(['Epic', 'Spike', 'Sub-task'])

// Keys used to persist state across reloads (e.g. after laptop sleep → Vite HMR reload)
const STORAGE_KEYS = {
  issueKey: 'jtb.issueKey',
  ticketsData: 'jtb.ticketsData',
  testPlan: 'jtb.testPlan',
  bugAnalysis: 'jtb.bugAnalysis',
}

const loadStored = (key, fallback) => {
  try {
    const raw = sessionStorage.getItem(key)
    return raw === null ? fallback : JSON.parse(raw)
  } catch {
    return fallback
  }
}

const saveStored = (key, value) => {
  try {
    if (value === null || value === undefined || (Array.isArray(value) && value.length === 0)) {
      sessionStorage.removeItem(key)
    } else {
      sessionStorage.setItem(key, JSON.stringify(value))
    }
  } catch {
    // storage quota / disabled — ignore
  }
}

function App() {
  const [issueKey, setIssueKey] = useState(() => loadStored(STORAGE_KEYS.issueKey, ''))
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // ticketsData is always an array; single-ticket mode uses ticketsData[0]
  const [ticketsData, setTicketsData] = useState(() => loadStored(STORAGE_KEYS.ticketsData, []))
  const [isDescriptionExpanded, setIsDescriptionExpanded] = useState(false)

  // Test plan generation state
  const [generatingPlan, setGeneratingPlan] = useState(false)
  const [testPlan, setTestPlan] = useState(() => loadStored(STORAGE_KEYS.testPlan, null))
  const [planError, setPlanError] = useState(null)
  const [abortController, setAbortController] = useState(null)

  // Scroll refs for auto-scrolling to results
  const testPlanRef = useRef(null)
  const bugAnalysisRef = useRef(null)

  // Bug Lens state
  const [analyzingBug, setAnalyzingBug] = useState(false)
  const [bugAnalysis, setBugAnalysis] = useState(() => loadStored(STORAGE_KEYS.bugAnalysis, null))
  const [bugAnalysisError, setBugAnalysisError] = useState(null)
  const [bugAbortController, setBugAbortController] = useState(null)

  useEffect(() => saveStored(STORAGE_KEYS.issueKey, issueKey), [issueKey])
  useEffect(() => saveStored(STORAGE_KEYS.ticketsData, ticketsData), [ticketsData])
  useEffect(() => saveStored(STORAGE_KEYS.testPlan, testPlan), [testPlan])
  useEffect(() => saveStored(STORAGE_KEYS.bugAnalysis, bugAnalysis), [bugAnalysis])

  // Fetch config on mount to get Jira base URL
  useEffect(() => {
    fetchConfig()
  }, [])

  // Auto-scroll to results when they appear
  useEffect(() => {
    if (testPlan && testPlanRef.current) {
      testPlanRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [testPlan])

  useEffect(() => {
    if (bugAnalysis && bugAnalysisRef.current) {
      bugAnalysisRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [bugAnalysis])

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
    setBugAnalysis(null)
    setBugAnalysisError(null)

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
    setBugAnalysis(null)
    setBugAnalysisError(null)
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
    setBugAnalysis(null)
    setBugAnalysisError(null)

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

  const handleAnalyzeBug = async () => {
    if (ticketsData.length === 0) return

    const controller = new AbortController()
    setBugAbortController(controller)
    setAnalyzingBug(true)
    setBugAnalysisError(null)
    setBugAnalysis(null)
    setTestPlan(null)
    setPlanError(null)

    try {
      if (!isMultiTicket) {
        const td = ticketsData[0]
        const response = await fetch(`${API_BASE_URL}/bug-lens/analyze`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ticket_key: td.key,
            summary: td.summary,
            description: td.description,
            issue_type: td.issue_type,
            development_info: td.development_info,
            comments: td.comments || null,
            linked_info: td.linked_issues || null,
            status: td.status || null,
            status_category: td.status_category || null,
          }),
          signal: controller.signal,
        })
        if (!response.ok) {
          const errorData = await response.json()
          throw new Error(errorData.detail || 'Failed to analyze bug')
        }
        setBugAnalysis(await response.json())
      } else {
        const response = await fetch(`${API_BASE_URL}/bug-lens/analyze/multi`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            tickets: ticketsData.map((td) => ({
              ticket_key: td.key,
              summary: td.summary,
              description: td.description,
              issue_type: td.issue_type,
              development_info: td.development_info,
              comments: td.comments || null,
              linked_info: td.linked_issues || null,
              status: td.status || null,
              status_category: td.status_category || null,
            })),
          }),
          signal: controller.signal,
        })
        if (!response.ok) {
          const errorData = await response.json()
          throw new Error(errorData.detail || 'Failed to analyze bugs')
        }
        setBugAnalysis(await response.json())
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        setBugAnalysisError('Bug analysis was cancelled')
      } else {
        setBugAnalysisError(err.message)
      }
    } finally {
      setAnalyzingBug(false)
      setBugAbortController(null)
    }
  }

  const handleStopBugAnalysis = () => {
    if (bugAbortController) {
      bugAbortController.abort()
    }
  }

  // For multi-ticket: block generation if any ticket has a non-testable type
  const nonTestableTicket = ticketsData.find((td) =>
    NON_TESTABLE_ISSUE_TYPES.has(td.issue_type)
  )

  // Only show Bug Lens if every fetched ticket is a Bug
  const isBugTickets = ticketsData.length > 0 && ticketsData.every((td) => td.issue_type === 'Bug')

  return (
    <div className="app">
      <header className="app-header">
        <h1><img src="/favicon.svg" alt="logo" style={{ width: '1.4em', height: '1.4em', verticalAlign: 'middle', marginRight: '0.4em' }} />Jira Test Plan Bot</h1>
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
                {ticketsData.map((td) => (
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
                  onAnalyzeBug={handleAnalyzeBug}
                  onStopBugAnalysis={handleStopBugAnalysis}
                  analyzingBug={analyzingBug}
                  showBugLens={isBugTickets}
                />

                {planError && (
                  <div className="alert alert-error">
                    <strong>Error:</strong> {planError}
                  </div>
                )}

                {bugAnalysisError && (
                  <div className="alert alert-error">
                    <strong>Error:</strong> {bugAnalysisError}
                  </div>
                )}

                {testPlan && (
                  <div ref={testPlanRef}>
                    <TestPlanDisplay
                      testPlan={testPlan}
                      ticketData={ticketData}
                      ticketsData={isMultiTicket ? ticketsData : null}
                    />
                  </div>
                )}

                {bugAnalysis && (
                  <div ref={bugAnalysisRef}>
                    <BugAnalysisDisplay analysis={bugAnalysis} />
                  </div>
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
