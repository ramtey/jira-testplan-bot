/**
 * Collapsible left-rail browser: Projects -> Status columns -> Issues.
 * Clicking an issue calls onSelectIssue(key) — the parent decides what to do.
 */

import { useEffect, useRef, useState } from 'react'
import { API_BASE_URL } from '../config'
import Icon from './Icon'
import { TypeMark } from './ui'

const CATEGORY_ORDER = ['new', 'indeterminate', 'done']
const CATEGORY_LABEL = {
  new: 'Backlog',
  indeterminate: 'Active',
  done: 'Done',
}
const CATEGORY_DOT_COLOR = {
  new: 'var(--fg-faint)',
  indeterminate: 'var(--accent)',
  done: 'var(--success)',
}

const PINNED_STORAGE_KEY = 'jtb.browser.pinnedProjects'
const RECENTS_STORAGE_KEY = 'jtb.browser.recentProjects'
const MAX_RECENTS = 5

const loadStored = (key, fallback) => {
  try {
    const raw = localStorage.getItem(key)
    if (raw === null) return fallback
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : fallback
  } catch {
    return fallback
  }
}

const saveStored = (key, value) => {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // storage quota / disabled — ignore
  }
}

function projectMark(project, size = 14) {
  const initials = (project.key || '?').slice(0, 2)
  // Hash project key into a color for a stable per-project tint.
  const colors = ['#3b82f6', '#a855f7', '#14b8a6', '#f59e0b', '#ec4899', '#22c55e', '#ef4444']
  const idx = Math.abs(
    (project.key || '').split('').reduce((a, c) => a + c.charCodeAt(0), 0)
  ) % colors.length
  return (
    <span
      style={{
        width: size,
        height: size,
        borderRadius: 3,
        background: colors[idx],
        color: 'white',
        display: 'grid',
        placeItems: 'center',
        fontSize: 9,
        fontWeight: 700,
        flexShrink: 0,
      }}
    >
      {initials}
    </span>
  )
}

function RefreshBtn({ busy, onClick }) {
  return (
    <button
      type="button"
      className="hbtn"
      onClick={onClick}
      disabled={busy}
      aria-label="Refresh"
      title="Refresh"
      style={{ width: 22, height: 22 }}
    >
      <Icon name="refresh" size={11} />
    </button>
  )
}

function JiraBrowser({ onSelectIssue, selectedIssueKey, railCollapsed }) {
  const [projects, setProjects] = useState(null)
  const [projectsError, setProjectsError] = useState(null)
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [projectFilter, setProjectFilter] = useState('')

  const [activeProject, setActiveProject] = useState(null)
  const [statuses, setStatuses] = useState(null)
  const [statusesError, setStatusesError] = useState(null)
  const [statusesLoading, setStatusesLoading] = useState(false)

  const [activeStatus, setActiveStatus] = useState(null)
  const [issues, setIssues] = useState(null)
  const [issuesError, setIssuesError] = useState(null)
  const [issuesLoading, setIssuesLoading] = useState(false)

  const [pinnedKeys, setPinnedKeys] = useState(() => loadStored(PINNED_STORAGE_KEY, []))
  const [recentKeys, setRecentKeys] = useState(() => loadStored(RECENTS_STORAGE_KEY, []))

  useEffect(() => saveStored(PINNED_STORAGE_KEY, pinnedKeys), [pinnedKeys])
  useEffect(() => saveStored(RECENTS_STORAGE_KEY, recentKeys), [recentKeys])

  const togglePin = (projectKey) => {
    setPinnedKeys((prev) =>
      prev.includes(projectKey)
        ? prev.filter((k) => k !== projectKey)
        : [...prev, projectKey],
    )
  }

  const trackRecent = (projectKey) => {
    setRecentKeys((prev) =>
      [projectKey, ...prev.filter((k) => k !== projectKey)].slice(0, MAX_RECENTS),
    )
  }

  const fetchProjects = async ({ silent = false } = {}) => {
    if (!silent) setProjects(null)
    setProjectsError(null)
    setProjectsLoading(true)
    try {
      const res = await fetch(`${API_BASE_URL}/jira/projects`)
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to load projects')
      }
      const data = await res.json()
      setProjects(data.projects || [])
    } catch (e) {
      setProjectsError(e.message)
    } finally {
      setProjectsLoading(false)
    }
  }

  const fetchStatuses = async (projectKey, { silent = false } = {}) => {
    if (!silent) setStatuses(null)
    setStatusesError(null)
    setStatusesLoading(true)
    try {
      const res = await fetch(`${API_BASE_URL}/jira/projects/${projectKey}/statuses`)
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to load statuses')
      }
      const data = await res.json()
      setStatuses(data.statuses || [])
    } catch (e) {
      setStatusesError(e.message)
    } finally {
      setStatusesLoading(false)
    }
  }

  const fetchIssues = async (projectKey, statusName, { silent = false } = {}) => {
    if (!silent) setIssues(null)
    setIssuesError(null)
    setIssuesLoading(true)
    try {
      const res = await fetch(
        `${API_BASE_URL}/jira/projects/${projectKey}/issues?status=${encodeURIComponent(statusName)}`,
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail || 'Failed to load issues')
      }
      const data = await res.json()
      setIssues(data.issues || [])
    } catch (e) {
      setIssuesError(e.message)
    } finally {
      setIssuesLoading(false)
    }
  }

  const selectProject = (project) => {
    trackRecent(project.key)
    setActiveProject(project)
    setActiveStatus(null)
    setIssues(null)
    setIssuesError(null)
    fetchStatuses(project.key)
  }

  const selectStatus = (status) => {
    if (!activeProject) return
    setActiveStatus(status)
    fetchIssues(activeProject.key, status.name)
  }

  const goBackToProjects = () => {
    setActiveProject(null)
    setStatuses(null)
    setStatusesError(null)
    setActiveStatus(null)
    setIssues(null)
    setIssuesError(null)
  }

  const goBackToStatuses = () => {
    setActiveStatus(null)
    setIssues(null)
    setIssuesError(null)
  }

  const refreshActiveRef = useRef(() => {})
  refreshActiveRef.current = () => {
    if (activeProject && activeStatus) {
      fetchIssues(activeProject.key, activeStatus.name, { silent: true })
    } else if (activeProject) {
      fetchStatuses(activeProject.key, { silent: true })
    } else {
      fetchProjects({ silent: true })
    }
  }

  useEffect(() => {
    fetchProjects()
  }, [])

  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        refreshActiveRef.current()
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [])

  // Collapsed mode — show only project marks as a vertical strip
  if (railCollapsed) {
    const projectByKey = Object.fromEntries((projects || []).map((p) => [p.key, p]))
    const pinned = pinnedKeys.map((k) => projectByKey[k]).filter(Boolean)
    return (
      <div className="rail" style={{ padding: '12px 0', alignItems: 'center', flexDirection: 'column', display: 'flex', gap: 6 }}>
        {pinned.map((p) => (
          <button
            key={p.key}
            type="button"
            title={p.name}
            onClick={() => selectProject(p)}
            style={{ background: 'transparent', border: 0, cursor: 'pointer', padding: 0 }}
          >
            {projectMark(p, 22)}
          </button>
        ))}
      </div>
    )
  }

  const isFiltering = projectFilter.trim().length > 0
  const filteredProjects = (projects || []).filter((p) => {
    if (!isFiltering) return true
    const q = projectFilter.toLowerCase()
    return p.name.toLowerCase().includes(q) || p.key.toLowerCase().includes(q)
  })

  const projectByKey = Object.fromEntries((projects || []).map((p) => [p.key, p]))
  const pinnedProjects = pinnedKeys.map((k) => projectByKey[k]).filter(Boolean)
  const recentProjects = recentKeys
    .filter((k) => !pinnedKeys.includes(k))
    .map((k) => projectByKey[k])
    .filter(Boolean)

  const groupedStatuses = CATEGORY_ORDER.map((cat) => ({
    key: cat,
    label: CATEGORY_LABEL[cat],
    items: (statuses || []).filter((s) => s.status_category === cat),
  })).filter((g) => g.items.length > 0)

  const ungroupedStatuses = (statuses || []).filter(
    (s) => !CATEGORY_ORDER.includes(s.status_category),
  )

  return (
    <div className="rail">
      {/* ─── Projects view ─────────────────────────────────────────────── */}
      {!activeProject && (
        <>
          <div style={{ padding: 'var(--s-4) var(--s-4) var(--s-2)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '.04em', fontWeight: 600 }}>
              Browse
            </span>
            <RefreshBtn busy={projectsLoading} onClick={() => refreshActiveRef.current()} />
          </div>
          <div className="rail-filter">
            <Icon name="search" size={12} style={{ color: 'var(--fg-faint)', flexShrink: 0 }} />
            <input
              type="text"
              placeholder="Filter projects"
              value={projectFilter}
              onChange={(e) => setProjectFilter(e.target.value)}
              disabled={projectsLoading || !!projectsError}
            />
          </div>

          <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
            {projectsLoading && projects === null && (
              <div style={{ padding: '8px 14px', color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>Loading projects…</div>
            )}
            {projectsError && (
              <div style={{ padding: '8px 14px', color: 'var(--danger)', fontSize: 'var(--t-xs)' }}>{projectsError}</div>
            )}

            {!isFiltering && pinnedProjects.length > 0 && (
              <>
                <div className="rail-group">Pinned <span className="count">{pinnedProjects.length}</span></div>
                {pinnedProjects.map((p) => (
                  <ProjectRow key={p.key} project={p} isPinned onSelect={selectProject} onTogglePin={togglePin} />
                ))}
              </>
            )}

            {!isFiltering && recentProjects.length > 0 && (
              <>
                <div className="rail-group">Recent</div>
                {recentProjects.map((p) => (
                  <ProjectRow key={p.key} project={p} isPinned={false} onSelect={selectProject} onTogglePin={togglePin} />
                ))}
              </>
            )}

            <div className="rail-group">
              {isFiltering ? 'Matches' : 'All projects'}{' '}
              <span className="count">{filteredProjects.length}</span>
            </div>
            {filteredProjects.length === 0 && !projectsLoading && (
              <div style={{ padding: '8px 14px', color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>No projects match.</div>
            )}
            {filteredProjects.map((p) => (
              <ProjectRow
                key={p.key}
                project={p}
                isPinned={pinnedKeys.includes(p.key)}
                onSelect={selectProject}
                onTogglePin={togglePin}
              />
            ))}
          </div>
        </>
      )}

      {/* ─── Statuses view ─────────────────────────────────────────────── */}
      {activeProject && !activeStatus && (
        <>
          <div style={{ padding: 'var(--s-3) var(--s-4)', display: 'flex', alignItems: 'center', gap: 'var(--s-3)', borderBottom: '1px solid var(--divider)' }}>
            <button
              type="button"
              className="hbtn"
              onClick={goBackToProjects}
              title="Back to projects"
              style={{ width: 22, height: 22 }}
            >
              <Icon name="chevron-left" size={12} />
            </button>
            {projectMark(activeProject)}
            <span style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {activeProject.name}
            </span>
            <RefreshBtn busy={statusesLoading} onClick={() => refreshActiveRef.current()} />
          </div>

          <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
            {statusesLoading && statuses === null && (
              <div style={{ padding: '8px 14px', color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>Loading…</div>
            )}
            {statusesError && (
              <div style={{ padding: '8px 14px', color: 'var(--danger)', fontSize: 'var(--t-xs)' }}>{statusesError}</div>
            )}

            {groupedStatuses.map((group) => (
              <div key={group.key}>
                <div className="rail-group">
                  <span style={{ width: 6, height: 6, borderRadius: 50, background: CATEGORY_DOT_COLOR[group.key] }} />
                  <span style={{ color: 'var(--fg-muted)' }}>{group.label}</span>
                  <span className="count">{group.items.length}</span>
                </div>
                {group.items.map((s) => (
                  <button
                    key={s.name}
                    type="button"
                    className="rail-row"
                    onClick={() => selectStatus(s)}
                  >
                    <span className="name">{s.name}</span>
                    <Icon name="chevron-right" size={11} style={{ color: 'var(--fg-faint)' }} />
                  </button>
                ))}
              </div>
            ))}

            {ungroupedStatuses.length > 0 && (
              <div>
                <div className="rail-group">
                  Other <span className="count">{ungroupedStatuses.length}</span>
                </div>
                {ungroupedStatuses.map((s) => (
                  <button
                    key={s.name}
                    type="button"
                    className="rail-row"
                    onClick={() => selectStatus(s)}
                  >
                    <span className="name">{s.name}</span>
                    <Icon name="chevron-right" size={11} style={{ color: 'var(--fg-faint)' }} />
                  </button>
                ))}
              </div>
            )}
          </div>
        </>
      )}

      {/* ─── Issues view ───────────────────────────────────────────────── */}
      {activeProject && activeStatus && (
        <>
          <div style={{ padding: 'var(--s-3) var(--s-4)', display: 'flex', alignItems: 'center', gap: 'var(--s-3)', borderBottom: '1px solid var(--divider)' }}>
            <button
              type="button"
              className="hbtn"
              onClick={goBackToStatuses}
              title={`Back to ${activeProject.key}`}
              style={{ width: 22, height: 22 }}
            >
              <Icon name="chevron-left" size={12} />
            </button>
            <span style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {activeStatus.name}
            </span>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>{activeProject.key}</span>
            <RefreshBtn busy={issuesLoading} onClick={() => refreshActiveRef.current()} />
          </div>

          <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
            {issuesLoading && issues === null && (
              <div style={{ padding: '8px 14px', color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>Loading…</div>
            )}
            {issuesError && (
              <div style={{ padding: '8px 14px', color: 'var(--danger)', fontSize: 'var(--t-xs)' }}>{issuesError}</div>
            )}
            {issues && issues.length === 0 && !issuesLoading && (
              <div style={{ padding: '8px 14px', color: 'var(--fg-subtle)', fontSize: 'var(--t-xs)' }}>No issues in this column.</div>
            )}

            {(issues || []).map((iss) => (
              <button
                key={iss.key}
                type="button"
                className="rail-row"
                data-active={selectedIssueKey === iss.key ? 'true' : 'false'}
                onClick={() => onSelectIssue(iss.key)}
                title={`${iss.issue_type || ''} · ${iss.summary}`}
                style={{ padding: '5px var(--s-5) 5px var(--s-4)' }}
              >
                {iss.issue_type && <TypeMark type={iss.issue_type} size={13} />}
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, color: selectedIssueKey === iss.key ? 'var(--accent)' : 'var(--fg-subtle)', flexShrink: 0 }}>
                  {iss.key}
                </span>
                <span className="name">{iss.summary}</span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function ProjectRow({ project, isPinned, onSelect, onTogglePin }) {
  return (
    <div style={{ position: 'relative', display: 'flex' }}>
      <button
        type="button"
        className="rail-row"
        onClick={() => onSelect(project)}
        style={{ paddingRight: 36 }}
      >
        {projectMark(project)}
        <span className="name">{project.name}</span>
        <span className="key">{project.key}</span>
      </button>
      <button
        type="button"
        onClick={() => onTogglePin(project.key)}
        aria-label={isPinned ? `Unpin ${project.key}` : `Pin ${project.key}`}
        title={isPinned ? 'Unpin' : 'Pin to top'}
        style={{
          position: 'absolute',
          right: 6,
          top: '50%',
          transform: 'translateY(-50%)',
          width: 22,
          height: 22,
          display: 'grid',
          placeItems: 'center',
          background: 'transparent',
          border: 0,
          cursor: 'pointer',
          color: isPinned ? 'var(--warning)' : 'var(--fg-faint)',
        }}
      >
        <Icon name="star" size={12} stroke={isPinned ? 2 : 1.5} />
      </button>
    </div>
  )
}

export default JiraBrowser
