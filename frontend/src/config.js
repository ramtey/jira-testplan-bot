/**
 * Frontend configuration
 */

// API base URL - can be overridden with VITE_API_URL environment variable
export const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000'

// Fetch config from backend (including Jira base URL).
// `workflowProjectPrefixes` decides which Jira projects expose the QA
// workflow buttons. Default matches the backend default so the gate works
// before the network call completes; backend value wins once it arrives.
let jiraBaseUrl = null
let workflowProjectPrefixes = ['SK']

export const fetchConfig = async () => {
  try {
    const response = await fetch(`${API_BASE_URL}/config`)
    if (response.ok) {
      const data = await response.json()
      jiraBaseUrl = data.jira_base_url
      if (Array.isArray(data.workflow_project_prefixes)) {
        workflowProjectPrefixes = data.workflow_project_prefixes
      }
    }
  } catch (error) {
    console.error('Failed to fetch config:', error)
  }
}

export const getJiraBaseUrl = () => jiraBaseUrl

export const getJiraTicketUrl = (ticketKey) => {
  if (!jiraBaseUrl) return null
  return `${jiraBaseUrl}/browse/${ticketKey}`
}

// Returns true when the QA workflow buttons should be shown for this ticket.
// Match is case-insensitive on the project prefix (the part before the `-`).
export const isWorkflowEnabledForTicket = (ticketKey) => {
  if (!ticketKey || workflowProjectPrefixes.length === 0) return false
  const prefix = ticketKey.split('-')[0]?.toUpperCase()
  if (!prefix) return false
  return workflowProjectPrefixes.some((p) => p.toUpperCase() === prefix)
}

// Feature flag: surface the "Pass to UAT" CTA inside the walkthrough card
// (alongside the existing button in the workflow header) so we can watch which
// entry point QA actually uses before deleting the other. Toggle at runtime via
//   localStorage.setItem('jtb.walkthroughCardCta', '1')
// or globally at build time via VITE_WALKTHROUGH_CARD_CTA=1.
export const isWalkthroughCardCtaEnabled = () => {
  if (import.meta.env.VITE_WALKTHROUGH_CARD_CTA === '1') return true
  try {
    return window.localStorage.getItem('jtb.walkthroughCardCta') === '1'
  } catch {
    return false
  }
}

// CustomEvent name the walkthrough-card CTA dispatches to ask the workflow
// header to open its "Pass to UAT" form. Kept as a constant so both ends of
// the bridge import the same string.
export const OPEN_PASS_TO_UAT_EVENT = 'jtb:open-pass-to-uat'
