/**
 * Collapsible left-rail browser: Projects -> Status columns -> Issues.
 * Clicking an issue calls onSelectIssue(key) — the parent decides what to do
 * (populate the form input + trigger fetch).
 *
 * Freshness:
 *   - Manual refresh button on each panel
 *   - Auto-refresh of the active panel when the tab becomes visible (i.e.
 *     when you tab back from Jira). Refreshes are "silent": existing data
 *     stays on screen while the fetch runs, swapping in once it returns.
 */

import { useEffect, useRef, useState } from 'react'
import { API_BASE_URL } from '../config'

// Group statuses by Jira's three statusCategory keys so the UI can render
// "Backlog / In Progress / Done" columns regardless of the project's custom names.
const CATEGORY_ORDER = ['new', 'indeterminate', 'done']
const CATEGORY_LABEL = {
  new: 'To Do',
  indeterminate: 'In Progress',
  done: 'Done',
}

function JiraBrowser({ onSelectIssue, selectedIssueKey }) {
  const [collapsed, setCollapsed] = useState(false)

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

  // ── Pure fetchers — never touch navigation state. `silent` keeps the
  //    current data on screen while reloading (used by refresh button + focus).

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

  // ── Navigation handlers — clear downstream state, then fetch (non-silent).

  const selectProject = (project) => {
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

  // Refresh whichever panel is currently visible. Silent so the user's data
  // doesn't flash to a spinner.
  // Held in a ref so the visibilitychange listener always sees the current
  // active project/status without re-subscribing on every navigation.
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

  // Initial projects load.
  useEffect(() => {
    fetchProjects()
  }, [])

  // Auto-refresh the active panel when the tab becomes visible — covers the
  // common "I just changed something in the Jira tab, now I'm back" case.
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        refreshActiveRef.current()
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [])

  if (collapsed) {
    return (
      <aside className="jira-browser jira-browser--collapsed">
        <button
          type="button"
          className="jira-browser__toggle"
          onClick={() => setCollapsed(false)}
          aria-label="Expand Jira browser"
          title="Browse Jira"
        >
          ›
        </button>
      </aside>
    )
  }

  const filteredProjects = (projects || []).filter((p) => {
    if (!projectFilter.trim()) return true
    const q = projectFilter.toLowerCase()
    return (
      p.name.toLowerCase().includes(q) || p.key.toLowerCase().includes(q)
    )
  })

  const groupedStatuses = CATEGORY_ORDER.map((cat) => ({
    key: cat,
    label: CATEGORY_LABEL[cat],
    items: (statuses || []).filter((s) => s.status_category === cat),
  })).filter((g) => g.items.length > 0)

  // Statuses that don't fall into the three known categories — surface them
  // rather than silently dropping, since custom workflows can introduce new ones.
  const ungroupedStatuses = (statuses || []).filter(
    (s) => !CATEGORY_ORDER.includes(s.status_category),
  )

  // The button is the same on every panel — it just calls refreshActive().
  const refreshButton = (busy) => (
    <button
      type="button"
      className={'jira-browser__refresh' + (busy ? ' jira-browser__refresh--busy' : '')}
      onClick={() => refreshActiveRef.current()}
      disabled={busy}
      aria-label="Refresh"
      title="Refresh from Jira"
    >
      ↻
    </button>
  )

  return (
    <aside className="jira-browser">
      <div className="jira-browser__header">
        <span className="jira-browser__title">Browse Jira</span>
        <button
          type="button"
          className="jira-browser__toggle"
          onClick={() => setCollapsed(true)}
          aria-label="Collapse Jira browser"
          title="Hide"
        >
          ‹
        </button>
      </div>

      {!activeProject && (
        <div className="jira-browser__panel">
          <div className="jira-browser__panel-bar">
            <input
              type="text"
              className="jira-browser__search"
              placeholder="Filter"
              value={projectFilter}
              onChange={(e) => setProjectFilter(e.target.value)}
              disabled={projectsLoading || !!projectsError}
            />
            {refreshButton(projectsLoading)}
          </div>
          {projectsLoading && <div className="jira-browser__hint">Loading projects…</div>}
          {projectsError && <div className="jira-browser__error">{projectsError}</div>}
          {projects && filteredProjects.length === 0 && !projectsLoading && (
            <div className="jira-browser__hint">No projects match.</div>
          )}
          <ul className="jira-browser__list">
            {filteredProjects.map((p) => (
              <li key={p.key}>
                <button
                  type="button"
                  className="jira-browser__row"
                  onClick={() => selectProject(p)}
                >
                  {p.avatar_url && (
                    <img
                      src={p.avatar_url}
                      alt=""
                      className="jira-browser__avatar"
                      loading="lazy"
                    />
                  )}
                  <span className="jira-browser__row-name">{p.name}</span>
                  <span className="jira-browser__row-meta">{p.key}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {activeProject && !activeStatus && (
        <div className="jira-browser__panel">
          <div className="jira-browser__panel-bar">
            <button
              type="button"
              className="jira-browser__back"
              onClick={goBackToProjects}
            >
              ← Projects
            </button>
            {refreshButton(statusesLoading)}
          </div>
          <div className="jira-browser__breadcrumb">
            <strong>{activeProject.name}</strong>
            <span className="jira-browser__row-meta">{activeProject.key}</span>
          </div>
          {statusesLoading && <div className="jira-browser__hint">Loading statuses…</div>}
          {statusesError && <div className="jira-browser__error">{statusesError}</div>}
          {groupedStatuses.map((group) => (
            <div key={group.key} className="jira-browser__group">
              <div className={`jira-browser__group-label jira-browser__group-label--${group.key}`}>
                {group.label}
              </div>
              <ul className="jira-browser__list">
                {group.items.map((s) => (
                  <li key={s.name}>
                    <button
                      type="button"
                      className="jira-browser__row"
                      onClick={() => selectStatus(s)}
                    >
                      <span className="jira-browser__row-name">{s.name}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          {ungroupedStatuses.length > 0 && (
            <div className="jira-browser__group">
              <div className="jira-browser__group-label">Other</div>
              <ul className="jira-browser__list">
                {ungroupedStatuses.map((s) => (
                  <li key={s.name}>
                    <button
                      type="button"
                      className="jira-browser__row"
                      onClick={() => selectStatus(s)}
                    >
                      <span className="jira-browser__row-name">{s.name}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {activeProject && activeStatus && (
        <div className="jira-browser__panel">
          <div className="jira-browser__panel-bar">
            <button
              type="button"
              className="jira-browser__back"
              onClick={goBackToStatuses}
            >
              ← {activeProject.key}
            </button>
            {refreshButton(issuesLoading)}
          </div>
          <div className="jira-browser__breadcrumb">
            <strong>{activeStatus.name}</strong>
            <span className="jira-browser__row-meta">{activeProject.key}</span>
          </div>
          {issuesLoading && <div className="jira-browser__hint">Loading issues…</div>}
          {issuesError && <div className="jira-browser__error">{issuesError}</div>}
          {issues && issues.length === 0 && !issuesLoading && (
            <div className="jira-browser__hint">No issues in this column.</div>
          )}
          <ul className="jira-browser__list">
            {(issues || []).map((iss) => (
              <li key={iss.key}>
                <button
                  type="button"
                  className={
                    'jira-browser__row jira-browser__row--issue' +
                    (selectedIssueKey === iss.key ? ' jira-browser__row--active' : '')
                  }
                  onClick={() => onSelectIssue(iss.key)}
                  title={`${iss.issue_type} · ${iss.summary}`}
                >
                  <div className="jira-browser__issue-meta">
                    {iss.issue_type && (
                      <span
                        className={`jira-browser__type jira-browser__type--${iss.issue_type.toLowerCase().replace(/\s+/g, '-')}`}
                      >
                        {iss.issue_type}
                      </span>
                    )}
                    <span className="jira-browser__issue-key">{iss.key}</span>
                  </div>
                  <span className="jira-browser__row-name">{iss.summary}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </aside>
  )
}

export default JiraBrowser
