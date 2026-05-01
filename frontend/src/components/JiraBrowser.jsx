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
  new: 'Backlog',
  indeterminate: 'Active',
  done: 'Done',
}

// Pinned + recent projects are persisted in localStorage so the user's
// shortcuts survive across sessions. We store keys only and look up the full
// project metadata from the loaded list at render time.
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

  const isFiltering = projectFilter.trim().length > 0
  const filteredProjects = (projects || []).filter((p) => {
    if (!isFiltering) return true
    const q = projectFilter.toLowerCase()
    return (
      p.name.toLowerCase().includes(q) || p.key.toLowerCase().includes(q)
    )
  })

  const projectByKey = Object.fromEntries((projects || []).map((p) => [p.key, p]))
  const pinnedProjects = pinnedKeys.map((k) => projectByKey[k]).filter(Boolean)
  // Exclude pinned from recents to avoid showing the same project twice in
  // adjacent shortcut sections.
  const recentProjects = recentKeys
    .filter((k) => !pinnedKeys.includes(k))
    .map((k) => projectByKey[k])
    .filter(Boolean)

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

          {/* Filter active: show a single flat list of matches (no shortcut
              sections — they'd be confusing while searching). */}
          {isFiltering && projects && (
            filteredProjects.length === 0 ? (
              <div className="jira-browser__hint">No projects match.</div>
            ) : (
              <ul className="jira-browser__list">
                {filteredProjects.map((p) => (
                  <ProjectRow
                    key={p.key}
                    project={p}
                    isPinned={pinnedKeys.includes(p.key)}
                    onSelect={selectProject}
                    onTogglePin={togglePin}
                  />
                ))}
              </ul>
            )
          )}

          {/* No filter: show Pinned, Recent, then the full alphabetical list. */}
          {!isFiltering && projects && (
            <>
              {pinnedProjects.length > 0 && (
                <div className="jira-browser__group">
                  <div className="jira-browser__group-label">Pinned</div>
                  <ul className="jira-browser__list">
                    {pinnedProjects.map((p) => (
                      <ProjectRow
                        key={p.key}
                        project={p}
                        isPinned={true}
                        onSelect={selectProject}
                        onTogglePin={togglePin}
                      />
                    ))}
                  </ul>
                </div>
              )}
              {recentProjects.length > 0 && (
                <div className="jira-browser__group">
                  <div className="jira-browser__group-label">Recent</div>
                  <ul className="jira-browser__list">
                    {recentProjects.map((p) => (
                      <ProjectRow
                        key={p.key}
                        project={p}
                        isPinned={false}
                        onSelect={selectProject}
                        onTogglePin={togglePin}
                      />
                    ))}
                  </ul>
                </div>
              )}
              <div className="jira-browser__group">
                {(pinnedProjects.length > 0 || recentProjects.length > 0) && (
                  <div className="jira-browser__group-label">All projects</div>
                )}
                <ul className="jira-browser__list">
                  {filteredProjects.map((p) => (
                    <ProjectRow
                      key={p.key}
                      project={p}
                      isPinned={pinnedKeys.includes(p.key)}
                      onSelect={selectProject}
                      onTogglePin={togglePin}
                    />
                  ))}
                </ul>
              </div>
            </>
          )}
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

// A project row with a pin button. The pin button is a sibling of the main
// click target — nesting buttons inside buttons isn't valid HTML.
function ProjectRow({ project, isPinned, onSelect, onTogglePin }) {
  return (
    <li className="jira-browser__row-wrap">
      <button
        type="button"
        className="jira-browser__row"
        onClick={() => onSelect(project)}
      >
        {project.avatar_url && (
          <img
            src={project.avatar_url}
            alt=""
            className="jira-browser__avatar"
            loading="lazy"
          />
        )}
        <span className="jira-browser__row-name">{project.name}</span>
        <span className="jira-browser__row-meta">{project.key}</span>
      </button>
      <button
        type="button"
        className={'jira-browser__star' + (isPinned ? ' jira-browser__star--on' : '')}
        onClick={() => onTogglePin(project.key)}
        aria-label={isPinned ? `Unpin ${project.key}` : `Pin ${project.key}`}
        title={isPinned ? 'Unpin' : 'Pin to top'}
      >
        {isPinned ? '★' : '☆'}
      </button>
    </li>
  )
}

export default JiraBrowser
