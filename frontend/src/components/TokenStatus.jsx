import { useState, useEffect, useRef } from 'react'
import { API_BASE_URL } from '../config'
import Icon from './Icon'

const SERVICE_ICONS = {
  Jira: 'link',
  Claude: 'sparkles',
  GitHub: 'github',
  Figma: 'figma',
}

const TokenStatus = () => {
  const [tokenHealth, setTokenHealth] = useState(null)
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState(false)
  const [lastChecked, setLastChecked] = useState(null)
  const drawerRef = useRef(null)

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
    fetchTokenHealth()
    const interval = setInterval(fetchTokenHealth, 5 * 60 * 1000)
    return () => clearInterval(interval)
  }, [])

  // Click-outside dismisses the drawer
  useEffect(() => {
    if (!open) return
    const onDown = (e) => {
      if (drawerRef.current && !drawerRef.current.contains(e.target)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  if (loading) {
    return (
      <button type="button" className="tokpill" data-state="ok">
        <span className="spin" style={{ width: 10, height: 10 }} />
        Checking…
      </button>
    )
  }

  if (!tokenHealth) return null

  const services = tokenHealth.services || []
  const requiredErrors = services.filter((s) => !s.is_valid && s.is_required)
  const state = requiredErrors.length > 0 ? 'danger' : 'ok'
  const label = state === 'danger'
    ? `${requiredErrors.length} token${requiredErrors.length === 1 ? '' : 's'} need attention`
    : 'Tokens OK'

  const serviceState = (svc) => {
    if (svc.is_valid) return 'ok'
    if (!svc.is_required) return 'muted'
    return 'danger'
  }

  const serviceStatusText = (svc) => {
    if (svc.is_valid) return 'Valid'
    if (!svc.is_required) return 'Not configured'
    switch (svc.error_type) {
      case 'expired': return 'Expired'
      case 'invalid': return 'Invalid'
      case 'missing': return 'Not configured'
      case 'rate_limited': return 'Rate limited'
      case 'insufficient_permissions': return 'Insufficient permissions'
      case 'service_unavailable': return 'Service unavailable'
      default: return 'Error'
    }
  }

  return (
    <div ref={drawerRef} style={{ position: 'relative' }}>
      <button
        type="button"
        className="tokpill"
        data-state={state}
        onClick={() => setOpen(!open)}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`Tokens, ${label}`}
      >
        <span className="dot" data-state={state} />
        {label}
      </button>

      {open && (
        <div
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            right: 0,
            width: 360,
            background: 'var(--bg-overlay)',
            border: '1px solid var(--line-strong)',
            borderRadius: 'var(--r-lg)',
            boxShadow: 'var(--elev-3)',
            zIndex: 100,
          }}
          role="dialog"
        >
          <div style={{ padding: 'var(--s-5) var(--s-6) var(--s-4)', borderBottom: '1px solid var(--line)', display: 'flex', alignItems: 'center', gap: 'var(--s-3)' }}>
            <Icon name="key" size={14} style={{ color: 'var(--accent)' }} />
            <div style={{ fontSize: 'var(--t-md)', fontWeight: 600, color: 'var(--fg-strong)' }}>Token health</div>
            <span style={{ flex: 1 }} />
            <button type="button" className="hbtn" onClick={() => setOpen(false)} aria-label="Close">
              <Icon name="x" size={13} />
            </button>
          </div>
          <div>
            {services.map((svc, i) => {
              const st = serviceState(svc)
              return (
                <div
                  key={svc.service_name}
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 'var(--s-4)',
                    padding: 'var(--s-4) var(--s-6)',
                    borderBottom: i < services.length - 1 ? '1px solid var(--divider)' : 'none',
                    background: st === 'danger' ? 'rgba(239,68,68,.04)' : 'transparent',
                  }}
                >
                  <Icon
                    name={SERVICE_ICONS[svc.service_name] || 'key'}
                    size={16}
                    style={{
                      color: st === 'danger' ? 'var(--danger)' : st === 'muted' ? 'var(--fg-faint)' : 'var(--fg-muted)',
                      marginTop: 2,
                      flexShrink: 0,
                    }}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)' }}>
                      <span style={{ fontSize: 'var(--t-md)', fontWeight: 600, color: 'var(--fg-strong)' }}>{svc.service_name}</span>
                      {st === 'ok' && <span className="chip" data-size="sm"><span className="dot" style={{ background: 'var(--success)' }} />Valid</span>}
                      {st === 'danger' && <span className="chip" data-size="sm"><span className="dot" style={{ background: 'var(--danger)' }} />{serviceStatusText(svc)}</span>}
                      {st === 'muted' && <span className="chip" data-size="sm">Not set</span>}
                    </div>
                    {svc.error_message && (
                      <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-muted)', marginTop: 4 }}>
                        {svc.error_message}
                      </div>
                    )}
                    {svc.is_valid && svc.details && (
                      <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', marginTop: 4, display: 'flex', gap: 'var(--s-4)', flexWrap: 'wrap' }}>
                        {svc.details.user_email && <span>{svc.details.user_email}</span>}
                        {svc.details.user_name && <span>{svc.details.user_name}</span>}
                        {svc.details.user_login && <span>@{svc.details.user_login}</span>}
                      </div>
                    )}
                    {svc.help_url && !svc.is_valid && (
                      <a
                        href={svc.help_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        style={{ fontSize: 'var(--t-xs)', color: 'var(--accent)', marginTop: 4, display: 'inline-flex', alignItems: 'center', gap: 4 }}
                      >
                        Get / manage token <Icon name="external" size={10} />
                      </a>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
          <div style={{ padding: 'var(--s-3) var(--s-6)', borderTop: '1px solid var(--line)', background: 'var(--bg-surface)', display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
            <span>{lastChecked ? `Checked ${lastChecked.toLocaleTimeString()}` : 'Not yet checked'}</span>
            <button type="button" className="btn" data-variant="ghost" data-size="sm" onClick={fetchTokenHealth}>
              <Icon name="refresh" className="ic" />
              Refresh
            </button>
          </div>
        </div>
      )}
    </div>
  )
}

export default TokenStatus
