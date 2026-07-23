/**
 * Reusable UI primitives — wrap the CSS classes in components.css so JSX
 * stays declarative.
 */

import { useState } from 'react'
import Icon from './Icon'

export function Btn({ variant = 'secondary', size, icon, iconRight, loading, disabled, children, onClick, style, title, type = 'button' }) {
  return (
    <button type={type} className="btn" data-variant={variant} data-size={size} disabled={disabled || loading} onClick={onClick} style={style} title={title}>
      {loading ? <span className="spin" /> : (icon && <Icon name={icon} className="ic" />)}
      {children}
      {iconRight && <Icon name={iconRight} className="ic" />}
    </button>
  )
}

export function Chip({ children, size, style, dot, dotColor, pulse }) {
  const chipStyle = pulse ? { ...style, '--dot-color': dotColor } : style
  return (
    <span
      className={pulse ? 'chip chip-live' : 'chip'}
      data-size={size}
      style={chipStyle}
    >
      {dot && <span className={pulse ? 'dot dot-pulse' : 'dot'} style={{ background: dotColor }} />}
      {children}
    </span>
  )
}

export function ItChip({ type, label }) {
  const t = (type || '').toLowerCase().replace(/[\s-]/g, '')
  const normalized = t === 'subtask' || t === 'subt' ? 'subtask' : t
  const letter = { story: 'S', bug: 'B', task: 'T', spike: 'P', epic: 'E', subtask: 's' }[normalized] || '?'
  return (
    <span className="chip-it" data-type={normalized}>
      <span className="bar">{letter}</span>
      <span className="lbl-it">{label || type}</span>
    </span>
  )
}

// Story-Points chip. Renders only when a numeric value is present so callers
// can drop it unconditionally beside <ItChip/> and it self-hides for
// unsized Stories / non-Story issue types.
export function SPChip({ points }) {
  if (points === null || points === undefined) return null
  const n = Number(points)
  if (!Number.isFinite(n)) return null
  const display = Number.isInteger(n) ? String(n) : n.toFixed(1)
  return (
    <span className="chip-sp" title={`Story points: ${display}`}>
      <span className="chip-sp-num">{display}</span>
      <span className="chip-sp-unit">pts</span>
    </span>
  )
}

export function TypeMark({ type, size = 14 }) {
  const t = (type || '').toLowerCase().replace(/[\s-]/g, '')
  const normalized = t === 'subtask' ? 'subtask' : t
  const colors = {
    story: 'var(--it-story)',
    bug: 'var(--it-bug)',
    task: 'var(--it-task)',
    spike: 'var(--it-spike)',
    epic: 'var(--it-epic)',
    subtask: 'var(--it-subtask)',
  }
  const letters = { story: 'S', bug: 'B', task: 'T', spike: 'P', epic: 'E', subtask: 's' }
  return (
    <span
      className="it"
      style={{
        background: colors[normalized] || 'var(--bg-raised)',
        width: size,
        height: size,
        fontSize: Math.max(9, size * 0.65),
        borderRadius: 3,
        display: 'grid',
        placeItems: 'center',
        color: 'white',
        fontWeight: 700,
        flexShrink: 0,
      }}
    >
      {letters[normalized] || '?'}
    </span>
  )
}

export function StatPill({ cat, children }) {
  return <span className="statpill" data-cat={cat}>{children}</span>
}

export function ACTag({ children, style }) {
  return <span className="actag" style={style}>{children}</span>
}

export function Pri({ level }) {
  return (
    <span className="pri" data-level={level}>
      <span className="dot" />
      {level}
    </span>
  )
}

export function Avatar({ name, hue, size = 18 }) {
  const initials = (name || '?').split(' ').map(s => s[0]).slice(0, 2).join('').toUpperCase()
  const h = hue || ['a', 'b', 'c', 'd', 'e', 'f', 'g'][((name || '').charCodeAt(0) || 0) % 7]
  return <span className="avatar" data-h={h} style={{ width: size, height: size, fontSize: size * 0.5 }}>{initials}</span>
}

export function Asn({ name, hue, muted, suffix }) {
  return (
    <span className="asn" data-muted={muted ? 'true' : undefined}>
      <Avatar name={name} hue={hue} />
      <span>{name}{suffix && <span style={{ color: 'var(--fg-subtle)' }}> {suffix}</span>}</span>
    </span>
  )
}

export function Tag({ children }) {
  return <span className="tag">{children}</span>
}

export function Cbx({ checked, onChange, label, id }) {
  return (
    <label className="cbx-label" htmlFor={id}>
      <span
        className="cbx"
        data-checked={checked ? 'true' : 'false'}
        role="checkbox"
        aria-checked={checked}
        tabIndex={0}
        onClick={() => onChange && onChange(!checked)}
        onKeyDown={(e) => {
          if (e.key === ' ' || e.key === 'Enter') {
            e.preventDefault()
            onChange && onChange(!checked)
          }
        }}
      />
      {label && <span>{label}</span>}
    </label>
  )
}

export function Alert({ tone = 'info', title, children, icon, action }) {
  const iconName = icon || ({
    info: 'info',
    warning: 'alert',
    danger: 'alert-circle',
    success: 'check-circle',
    muted: 'info',
  })[tone]
  return (
    <div className="alert" data-tone={tone} role="alert">
      {icon !== false && <Icon name={iconName} className="ic" />}
      <div className="body">
        {title && <div className="ttl">{title}</div>}
        <div className="desc">{children}</div>
      </div>
      {action}
    </div>
  )
}

export function Coll({ icon, title, preview, meta, defaultOpen = false, open: openProp, onToggle, children }) {
  const [localOpen, setLocalOpen] = useState(defaultOpen)
  const open = openProp !== undefined ? openProp : localOpen
  const toggle = () => {
    if (onToggle) onToggle(!open)
    else setLocalOpen(!open)
  }
  return (
    <div className="coll" data-open={open ? 'true' : 'false'}>
      <button type="button" className="coll-head" onClick={toggle}>
        <Icon name="chevron-right" className="chev" />
        {icon && <Icon name={icon} className="ic" />}
        <span className="ttl">{title}</span>
        {!open && preview && <span className="preview">{preview}</span>}
        {meta && <span className="meta">{meta}</span>}
      </button>
      {open && <div className="coll-body">{children}</div>}
    </div>
  )
}

export function SectLbl({ children, action, style }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 'var(--s-4)', ...style }}>
      <span style={{ fontSize: 'var(--t-xs)', fontWeight: 600, letterSpacing: 'var(--tracking-overline)', textTransform: 'uppercase', color: 'var(--fg-subtle)' }}>{children}</span>
      {action}
    </div>
  )
}

export function Prog({ value, max = 100, tone, indeterminate, style }) {
  return (
    <div className="prog" data-tone={tone} data-indeterminate={indeterminate ? 'true' : undefined} style={style}>
      <div className="fill" style={{ width: indeterminate ? '100%' : `${Math.round((value / max) * 100)}%` }} />
    </div>
  )
}

export function Sk({ w = '100%', h = 12, style }) {
  return <div className="sk" style={{ width: w, height: h, ...style }} />
}

export function Toast({ icon = 'check-circle', children, action }) {
  return (
    <div className="toast">
      <Icon name={icon} className="ic" />
      <div style={{ flex: 1 }}>{children}</div>
      {action}
    </div>
  )
}

export function Modal({ title, sub, onClose, children, foot, width }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" style={width ? { width } : undefined} onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <div className="modal-head">
          <div style={{ flex: 1 }}>
            <div className="ttl">{title}</div>
            {sub && <div className="sub">{sub}</div>}
          </div>
          <button type="button" className="hbtn" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div className="modal-body">{children}</div>
        {foot && <div className="modal-foot">{foot}</div>}
      </div>
    </div>
  )
}

export { Icon }
