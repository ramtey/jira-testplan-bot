import { useState, useEffect, useRef } from 'react'
// Order matters: load old App.css first, then the new design-system layers
// so the new tokens/base/components win cleanly without !important.
import './App.css'
import './styles/tokens.css'
import './styles/base.css'
import './styles/components.css'
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
import Icon from './components/Icon'
import { Alert } from './components/ui'

// Issue types that don't require test plans
const NON_TESTABLE_ISSUE_TYPES = new Set(['Epic', 'Spike'])

// Keys used to persist state across reloads (e.g. after laptop sleep → Vite HMR reload).
// sessionStorage is per-tab, so multiple windows already keep independent state.
// The URL ?key= param is the canonical source on first paint so deep links and
// bookmarks land on the right ticket.
const STORAGE_KEYS = {
  issueKey: 'jtb.issueKey',
  ticketsData: 'jtb.ticketsData',
  runHistory: 'jtb.runHistory',
  railCollapsed: 'jtb.railCollapsed',
}

const readKeyFromUrl = () => {
  if (typeof window === 'undefined') return ''
  try {
    return new URLSearchParams(window.location.search).get('key') || ''
  } catch {
    return ''
  }
}

const writeKeyToUrl = (key) => {
  if (typeof window === 'undefined') return
  try {
    const url = new URL(window.location.href)
    if (key) {
      url.searchParams.set('key', key)
    } else {
      url.searchParams.delete('key')
    }
    window.history.replaceState(null, '', url.toString())
  } catch {
    // History API unavailable — ignore, app still works without deep links.
  }
}

function App() {
  const [issueKey, setIssueKey] = useState(
    () => readKeyFromUrl() || loadStored(STORAGE_KEYS.issueKey, '')
  )
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [railCollapsed, setRailCollapsed] = useState(() => loadStored(STORAGE_KEYS.railCollapsed, false))

  // ticketsData is always an array; single-ticket mode uses ticketsData[0]
  const [ticketsData, setTicketsData] = useState(() => loadStored(STORAGE_KEYS.ticketsData, []))
  const [isDescriptionExpanded, setIsDescriptionExpanded] = useState(false)

  const testPlan = useTestPlan()
  const bugLens = useBugLens()

  // Scroll refs for auto-scrolling to results
  const testPlanRef = useRef(null)
  const bugAnalysisRef = useRef(null)

  // Run history (single-ticket only)
  const [runHistory, setRunHistory] = useState(() => loadStored(STORAGE_KEYS.runHistory, []))
  const [historyPreview, setHistoryPreview] = useState(null)

  useEffect(() => saveStored(STORAGE_KEYS.issueKey, issueKey), [issueKey])
  useEffect(() => saveStored(STORAGE_KEYS.ticketsData, ticketsData), [ticketsData])
  useEffect(() => saveStored(STORAGE_KEYS.runHistory, runHistory), [runHistory])
  useEffect(() => saveStored(STORAGE_KEYS.railCollapsed, railCollapsed), [railCollapsed])

  // Fetch config on mount to get Jira base URL
  useEffect(() => {
    fetchConfig()
  }, [])

  // Reflect the active ticket set in the URL. Comma-separated for multi-ticket.
  // Drives bookmarkable / shareable links and lets two tabs open different
  // tickets via URL without sharing state.
  useEffect(() => {
    const urlKey = ticketsData.length
      ? ticketsData.map((t) => t.key).join(',')
      : ''
    writeKeyToUrl(urlKey)
  }, [ticketsData])

  // Fetch on first paint if the URL carried a ?key= but we have no ticket
  // loaded yet (deep link or hard reload of a shared URL). Uses the same
  // comma-separated parsing as the input form so multi-ticket URLs work too.
  // Runs only once — the dep list is intentionally empty.
  useEffect(() => {
    const urlKey = readKeyFromUrl()
    if (!urlKey || ticketsData.length > 0) return
    const keys = urlKey
      .split(',')
      .map((k) => k.trim().toUpperCase())
      .filter(Boolean)
    if (keys.length === 0) return
    setIssueKey(urlKey)
    fetchTicketsByKeys(keys)
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
  const ticketData = ticketsData.length === 1 ? ticketsData[0] : null

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
        const response = await fetch(`${API_BASE_URL}/issue/${keys[0]}`)
        if (!response.ok) {
          const errorData = await response.json()
          throw new Error(errorData.detail || 'Failed to fetch ticket')
        }
        const data = await response.json()
        setTicketsData([data])
        loadRunHistory(data.key)
      } else {
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
    if (plan && tickets.length === 1) loadRunHistory(tickets[0].key)
  }

  const handleAnalyzeBug = async () => {
    if (ticketsData.length === 0) return
    testPlan.reset()
    await bugLens.analyze(ticketsData)
  }

  const nonTestableTicket = ticketsData.find((td) =>
    NON_TESTABLE_ISSUE_TYPES.has(td.issue_type)
  )

  const isBugTickets = ticketsData.length > 0 && ticketsData.every((td) => td.issue_type === 'Bug')

  return (
    <div className="app" data-rail={railCollapsed ? 'collapsed' : 'open'}>
      <header className="hdr appHeader">
        <div className="hdr-brand">
          <img src="/favicon.svg" alt="" style={{ width: 22, height: 22, borderRadius: 'var(--r-sm)' }} />
          <span>Jira Test Plan</span>
          <span style={{ color: 'var(--fg-faint)', fontWeight: 400, marginLeft: 2 }}>·</span>
          <span style={{ color: 'var(--fg-muted)', fontWeight: 500, marginLeft: 2 }}>Bot</span>
        </div>
        <span className="hdr-sep" />
        <button
          type="button"
          className="hbtn"
          onClick={() => setRailCollapsed(!railCollapsed)}
          title="Toggle rail"
          aria-label="Toggle rail"
        >
          <Icon name="panel-left" size={15} />
        </button>
        <span className="hdr-sep" />
        <div className="hdr-search" role="button" tabIndex={0}>
          <Icon name="search" size={13} />
          <span>{ticketData?.key || (ticketsData.length > 1 ? ticketsData.map(t => t.key).join(', ') : 'Jump to ticket…')}</span>
        </div>
        <div className="hdr-spacer" />
        <div className="hdr-actions">
          <TokenStatus />
        </div>
      </header>

      <aside className="appRail">
        <JiraBrowser
          onSelectIssue={handleSelectFromBrowser}
          selectedIssueKey={ticketData?.key || null}
          railCollapsed={railCollapsed}
        />
      </aside>

      {loading && (
        <div className="fetch-overlay" role="status" aria-live="polite">
          <div className="fetch-overlay__spinner" aria-hidden="true" />
          <div className="fetch-overlay__label">Fetching ticket…</div>
        </div>
      )}

      <main className="appMain main-shell">
        <div className="main-inner">
          <TicketForm
            issueKey={issueKey}
            setIssueKey={setIssueKey}
            loading={loading}
            onSubmit={handleFetchTicket}
            onClear={handleClear}
            hasTicketData={ticketsData.length > 0}
          />

          {error && (
            <div style={{ marginTop: 'var(--s-5)' }}>
              <Alert tone="danger" title="Error">{error}</Alert>
            </div>
          )}

          {ticketsData.length > 0 && (
            <>
              {isMultiTicket ? (
                <div className="multi-ticket-list" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--s-3)', marginTop: 'var(--s-6)' }}>
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
                  <div style={{ marginTop: 'var(--s-6)' }}>
                    <Alert tone="info" title={`Test plans not generated for ${nonTestableTicket.issue_type}`}>
                      Skipped for {nonTestableTicket.key}. Test plan generation runs for Story, Task, and Bug issues.
                    </Alert>
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
                    <div style={{ marginTop: 'var(--s-4)' }}>
                      <Alert tone="danger" title="Error">{testPlan.error}</Alert>
                    </div>
                  )}

                  {bugLens.error && (
                    <div style={{ marginTop: 'var(--s-4)' }}>
                      <Alert tone="danger" title="Error">{bugLens.error}</Alert>
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
        </div>
      </main>
    </div>
  )
}

export default App
