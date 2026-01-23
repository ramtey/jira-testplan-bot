/**
 * Frontend configuration
 */

// API base URL - can be overridden with VITE_API_URL environment variable
export const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8001'

// Fetch config from backend (including Jira base URL)
let jiraBaseUrl = null

export const fetchConfig = async () => {
  try {
    const response = await fetch(`${API_BASE_URL}/config`)
    if (response.ok) {
      const data = await response.json()
      jiraBaseUrl = data.jira_base_url
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
