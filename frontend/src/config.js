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
