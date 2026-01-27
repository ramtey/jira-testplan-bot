import { useState, useEffect } from 'react'
import { API_BASE_URL } from '../config'

const TokenStatus = () => {
  const [tokenHealth, setTokenHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState(false)
  const [lastChecked, setLastChecked] = useState(null)

  const fetchTokenHealth = async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/health/tokens`)
      if (response.ok) {
        const data = await response.json()
        setTokenHealth(data)
        setLastChecked(new Date())
      }
    } catch (error) {
      console.error('Failed to fetch token health:', error)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // Initial fetch
    fetchTokenHealth()

    // Poll every 5 minutes
    const interval = setInterval(fetchTokenHealth, 5 * 60 * 1000)

    return () => clearInterval(interval)
  }, [])

  const getStatusIcon = (service) => {
    if (service.is_valid) {
      return '✅'
    } else if (!service.is_required) {
      return 'ℹ️'
    } else {
      return '❌'
    }
  }

  const getStatusText = (service) => {
    if (service.is_valid) {
      return 'Connected'
    } else if (!service.is_required) {
      return 'Not configured (optional)'
    } else {
      switch (service.error_type) {
        case 'expired':
          return 'Expired'
        case 'invalid':
          return 'Invalid'
        case 'missing':
          return 'Not configured'
        case 'rate_limited':
          return 'Rate Limited'
        case 'insufficient_permissions':
          return 'Insufficient Permissions'
        case 'service_unavailable':
          return 'Service Unavailable'
        default:
          return 'Error'
      }
    }
  }

  const getStatusClass = (service) => {
    if (service.is_valid) {
      return 'token-status-valid'
    } else if (!service.is_required) {
      return 'token-status-optional'
    } else {
      return 'token-status-error'
    }
  }

  if (loading) {
    return (
      <div className="token-status-widget">
        <div className="token-status-header" onClick={() => setExpanded(!expanded)}>
          <span className="token-status-title">⏳ Checking API tokens...</span>
        </div>
      </div>
    )
  }

  if (!tokenHealth) {
    return null
  }

  const hasErrors = tokenHealth.services.some(
    (s) => !s.is_valid && s.is_required
  )

  return (
    <div className={`token-status-widget ${hasErrors ? 'has-errors' : ''}`}>
      <div
        className="token-status-header"
        onClick={() => setExpanded(!expanded)}
        style={{ cursor: 'pointer' }}
      >
        <span className="token-status-title">
          {tokenHealth.overall_health ? '✅' : '⚠️'} API Tokens
          {hasErrors && ' - Action Required'}
        </span>
        <span className="token-status-toggle">
          {expanded ? '▼' : '▶'}
        </span>
      </div>

      {expanded && (
        <div className="token-status-details">
          {tokenHealth.services.map((service) => (
            <div
              key={service.service_name}
              className={`token-service ${getStatusClass(service)}`}
            >
              <div className="token-service-header">
                <span className="token-service-icon">
                  {getStatusIcon(service)}
                </span>
                <span className="token-service-name">
                  {service.service_name}
                  {service.is_required && <span className="required-badge">Required</span>}
                </span>
                <span className="token-service-status">
                  {getStatusText(service)}
                </span>
              </div>

              {service.error_message && (
                <div className="token-service-error">
                  <p>{service.error_message}</p>
                  {service.help_url && (
                    <a
                      href={service.help_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="token-help-link"
                    >
                      Get/Manage Token →
                    </a>
                  )}
                </div>
              )}

              {service.is_valid && service.details && (
                <div className="token-service-details">
                  {service.details.user_email && (
                    <span className="token-detail">📧 {service.details.user_email}</span>
                  )}
                  {service.details.user_name && (
                    <span className="token-detail">👤 {service.details.user_name}</span>
                  )}
                  {service.details.user_login && (
                    <span className="token-detail">👤 @{service.details.user_login}</span>
                  )}
                </div>
              )}
            </div>
          ))}

          <div className="token-status-footer">
            <button
              onClick={fetchTokenHealth}
              className="token-refresh-btn"
              disabled={loading}
            >
              🔄 Refresh
            </button>
            {lastChecked && (
              <span className="token-last-checked">
                Last checked: {lastChecked.toLocaleTimeString()}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export default TokenStatus
