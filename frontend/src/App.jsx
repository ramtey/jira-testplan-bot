import { useState, useEffect, useRef } from 'react'
import './App.css'
import { API_BASE_URL, fetchConfig } from './config'
import { loadStored, saveStored } from './utils/sessionStorage'
import { useTestPlan } from './hooks/useTestPlan'
import { useBugLens } from './hooks/useBugLens'
import TicketForm from './components/TicketForm'
import TicketDetails from './components/TicketDetails'
import ActionButtons from './components/ActionButtons'
import TestPlanDisplay from './components/TestPlanDisplay'
import BugAnalysisDisplay from './components/BugAnalysisDisplay'
import TokenStatus from './components/TokenStatus'
import RunHistoryBanner from './components/RunHistoryBanner'
import HistoricalPlanPreview from './components/HistoricalPlanPreview'
import EpicChildrenList from './components/EpicChildrenList'
import JiraBrowser from './components/JiraBrowser'

// Issue types that don't require test plans
const NON_TESTABLE_ISSUE_TYPES = new Set(['Epic', 'Spike'])

// Keys used to persist state across reloads (e.g. after laptop sleep → Vite HMR reload).
// The plan/analysis keys are owned by the respective hooks.
const STORAGE_KEYS = {
  issueKey: 'jtb.issueKey',
  ticketsData: 'jtb.ticketsData',
  runHistory: 'jtb.runHistory',
}

function App() {
  const [issueKey, setIssueKey] = useState(() => loadStored(STORAGE_KEYS.issueKey, ''))
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // ticketsData is always an array; single-ticket mode uses ticketsData[0]
  const [ticketsData, setTicketsData] = useState(() => loadStored(STORAGE_KEYS.ticketsData, []))
  const [isDescriptionExpanded, setIsDescriptionExpanded] = useState(false)

  const testPlan = useTestPlan()
  const bugLens = useBugLens()

  // Scroll refs for auto-scrolling to results
  const testPlanRef = useRef(null)
  const bugAnalysisRef = useRef(null)

  // Run history (single-ticket only — multi-ticket scope deferred)
  const [runHistory, setRunHistory] = useState(() => loadStored(STORAGE_KEYS.runHistory, []))
  // Historical plan preview opened from the banner — sits beside the live plan,
  // not in place of it. Not persisted: it's an ephemeral comparison view.
  const [historyPreview, setHistoryPreview] = useState(null)

  useEffect(() => saveStored(STORAGE_KEYS.issueKey, issueKey), [issueKey])
  useEffect(() => saveStored(STORAGE_KEYS.ticketsData, ticketsData), [ticketsData])
  useEffect(() => saveStored(STORAGE_KEYS.runHistory, runHistory), [runHistory])

  // Fetch config on mount to get Jira base URL
  useEffect(() => {
    fetchConfig()
  }, [])

  // Auto-scroll to results when they appear
  useEffect(() => {
    if (testPlan.plan && testPlanRef.current) {
      testPlanRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [testPlan.plan])

  useEffect(() => {
    if (bugLens.analysis && bugAnalysisRef.current) {
      bugAnalysisRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }, [bugLens.analysis])

  const isMultiTicket = ticketsData.length > 1
  // For single-ticket backward-compat: expose first ticket as ticketData
  const ticketData = ticketsData.length === 1 ? ticketsData[0] : null

  // Fetch prior test-plan runs for a ticket. Silently no-op on failure so the
  // banner just doesn't appear — DB outages shouldn't block the main flow.
  const loadRunHistory = async (key) => {
    try {
      const res = await fetch(`${API_BASE_URL}/runs/by-ticket/${key}`)
      if (!res.ok) {
        setRunHistory([])
        return
      }
      const data = await res.json()
      setRunHistory(Array.isArray(data.runs) ? data.runs : [])
    } catch {
      setRunHistory([])
    }
  }

  const fetchTicketsByKeys = async (keys) => {
    if (keys.length === 0) {
      setError('Please enter a Jira issue key')
      return
    }

    setLoading(true)
    setError(null)
    setTicketsData([])
    setIsDescriptionExpanded(false)
    testPlan.reset()
    bugLens.reset()
    setRunHistory([])
    setHistoryPreview(null)

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
        // Fire-and-forget: history fetch happens in parallel with the user
        // reading the ticket — don't block the main render.
        loadRunHistory(data.key)
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

  const handleFetchTicket = async (e) => {
    e.preventDefault()
    const keys = issueKey
      .split(',')
      .map((k) => k.trim().toUpperCase())
      .filter(Boolean)
    await fetchTicketsByKeys(keys)
  }

  const handleSelectFromBrowser = async (key) => {
    setIssueKey(key)
    await fetchTicketsByKeys([key.toUpperCase()])
  }

  // Refresh just the current ticket's metadata in place — used after workflow
  // actions so the user keeps their generated test plan, history, etc.
  const refreshCurrentTicket = async (actionId) => {
    if (ticketsData.length !== 1) return
    const key = ticketsData[0].key
    let refreshed
    try {
      const response = await fetch(`${API_BASE_URL}/issue/${key}`)
      if (!response.ok) return
      refreshed = await response.json()
      setTicketsData([refreshed])
    } catch {
      return
    }

    // After Pull-to-Testing, auto-generate a test plan if none has ever been
    // generated for this ticket — saves the QA tester an extra click.
    // Skipped when a prior run already exists in the DB or one is loaded in
    // this session, so re-pulls and bounce-backs don't re-spend on the LLM.
    if (actionId !== 'pull-to-testing') return
    if (NON_TESTABLE_ISSUE_TYPES.has(refreshed.issue_type)) return
    if (testPlan.plan) return
    try {
      const res = await fetch(`${API_BASE_URL}/runs/by-ticket/${key}`)
      const data = res.ok ? await res.json() : { runs: [] }
      const runs = Array.isArray(data.runs) ? data.runs : []
      setRunHistory(runs)
      if (runs.length === 0) {
        handleGenerateTestPlan([refreshed])
      }
    } catch {
      // network blip — leave the user to click Generate manually
    }
  }

  const handleClear = () => {
    setIssueKey('')
    setTicketsData([])
    setError(null)
    setIsDescriptionExpanded(false)
    testPlan.reset()
    bugLens.reset()
    setRunHistory([])
    setHistoryPreview(null)
  }

  const toggleDescription = () => {
    setIsDescriptionExpanded(!isDescriptionExpanded)
  }

  const handleGenerateTestPlan = async (overrideTickets) => {
    const tickets = overrideTickets || ticketsData
    if (tickets.length === 0) return
    bugLens.reset()
    const plan = await testPlan.generate(tickets)
    // Refresh history so the new run appears in the banner with a bumped version.
    if (plan && tickets.length === 1) loadRunHistory(tickets[0].key)
  }

  const handleAnalyzeBug = async () => {
    if (ticketsData.length === 0) return
    testPlan.reset()
    await bugLens.analyze(ticketsData)
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
        <h1><img src="/favicon.svg" alt="logo" style={{ width: '1.2em', height: '1.2em', verticalAlign: 'middle', marginRight: '0.4em' }} />Jira Test Plan Bot</h1>
        <p>Fetch Jira tickets and analyze description quality</p>
      </header>

      <div className="app-shell">
        <JiraBrowser
          onSelectIssue={handleSelectFromBrowser}
          selectedIssueKey={ticketData?.key || null}
        />

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
                onActionComplete={refreshCurrentTicket}
              />
            )}

            {nonTestableTicket ? (
              !isMultiTicket && nonTestableTicket.issue_type === 'Epic' ? (
                <EpicChildrenList epicKey={nonTestableTicket.key} />
              ) : (
                <div className="ticket-section">
                  <div className="alert alert-info">
                    <strong>ℹ️ Note:</strong> Test plans are not generated for{' '}
                    {nonTestableTicket.issue_type} tickets ({nonTestableTicket.key}).
                    <br />
                    Test plan generation is available for Story, Task, and Bug issues only.
                  </div>
                </div>
              )
            ) : (
              <>
                {!isMultiTicket &&
                  runHistory.length > 0 &&
                  !bugLens.analyzing &&
                  !bugLens.analysis && (
                    <RunHistoryBanner
                      runs={runHistory}
                      ticketData={ticketData}
                      onViewPlan={(plan, meta) =>
                        setHistoryPreview({ plan, ...meta })
                      }
                    />
                  )}

                <ActionButtons
                  onGenerateTestPlan={handleGenerateTestPlan}
                  onStopGeneration={testPlan.stop}
                  generatingPlan={testPlan.generating}
                  onAnalyzeBug={handleAnalyzeBug}
                  onStopBugAnalysis={bugLens.stop}
                  analyzingBug={bugLens.analyzing}
                  showBugLens={isBugTickets}
                />

                {testPlan.error && (
                  <div className="alert alert-error">
                    <strong>Error:</strong> {testPlan.error}
                  </div>
                )}

                {bugLens.error && (
                  <div className="alert alert-error">
                    <strong>Error:</strong> {bugLens.error}
                  </div>
                )}

                {testPlan.plan && (
                  <div ref={testPlanRef}>
                    <TestPlanDisplay
                      testPlan={testPlan.plan}
                      ticketData={ticketData}
                      ticketsData={isMultiTicket ? ticketsData : null}
                    />
                  </div>
                )}

                {!isMultiTicket && historyPreview && !bugLens.analysis && !bugLens.analyzing && (
                  <HistoricalPlanPreview
                    key={historyPreview.planId}
                    plan={historyPreview.plan}
                    version={historyPreview.version}
                    createdAt={historyPreview.createdAt}
                    ticketData={ticketData}
                    showActions={!testPlan.plan}
                    onClose={() => setHistoryPreview(null)}
                  />
                )}

                {bugLens.analysis && (
                  <div ref={bugAnalysisRef}>
                    <BugAnalysisDisplay analysis={bugLens.analysis} />
                  </div>
                )}
              </>
            )}
          </>
        )}
        </main>
      </div>
    </div>
  )
}

export default App
