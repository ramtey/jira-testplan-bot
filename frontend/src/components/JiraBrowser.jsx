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

const IS_MAC =
  typeof navigator !== 'undefined' &&
  /mac|iphone|ipad|ipod/i.test(navigator.platform || navigator.userAgent || '')
const MULTI_SELECT_KEY = IS_MAC ? '⌘' : 'Ctrl'

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
  const fs = Math.max(9, Math.round(size * 0.55))
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      style={{ flexShrink: 0, display: 'block' }}
      aria-hidden="true"
    >
      <rect width={size} height={size} rx="3" fill={colors[idx]} />
      <text
        x="50%"
        y="50%"
        textAnchor="middle"
        dominantBaseline="central"
        fill="white"
        fontSize={fs}
        fontWeight="700"
        fontFamily="var(--font-sans)"
      >
        {initials}
      </text>
    </svg>
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
      style={{ width: 22, height: 22, padding: 0, flex: '0 0 auto' }}
    >
      <Icon name="refresh" size={11} />
    </button>
  )
}

function JiraBrowser({ onSelectIssue, onSelectMultiple, selectedIssueKey, railCollapsed }) {
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

  // Staged keys for multi-select fetch. ⌘/Ctrl-click adds a row here instead
  // of firing onSelectIssue, then the footer's Fetch button hands the whole
  // batch off to the parent. Reset whenever the issue list changes underneath.
  const [stagedKeys, setStagedKeys] = useState([])

  const toggleStaged = (key) => {
    setStagedKeys((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    )
  }

  const clearStaged = () => setStagedKeys([])

  const commitStaged = () => {
    if (stagedKeys.length === 0) return
    if (typeof onSelectMultiple === 'function') {
      onSelectMultiple(stagedKeys)
    } else {
      // Fallback: fire single-select for the first key so we never silently no-op.
      onSelectIssue(stagedKeys[0])
    }
    setStagedKeys([])
  }

  const handleIssueClick = (event, key) => {
    if (event.metaKey || event.ctrlKey) {
      event.preventDefault()
      toggleStaged(key)
      return
    }
    onSelectIssue(key)
  }

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
      const res = await fetch(`${API_BASE_URL}/jira/projects`, { cache: 'no-store' })
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
      const res = await fetch(
        `${API_BASE_URL}/jira/projects/${projectKey}/statuses`,
        { cache: 'no-store' },
      )
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
        { cache: 'no-store' },
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
    setStagedKeys([])
    fetchStatuses(project.key)
  }

  const selectStatus = (status) => {
    if (!activeProject) return
    setActiveStatus(status)
    setStagedKeys([])
    fetchIssues(activeProject.key, status.name)
  }

  const goBackToProjects = () => {
    setActiveProject(null)
    setStatuses(null)
    setStatusesError(null)
    setActiveStatus(null)
    setIssues(null)
    setIssuesError(null)
    setStagedKeys([])
  }

  const goBackToStatuses = () => {
    setActiveStatus(null)
    setIssues(null)
    setIssuesError(null)
    setStagedKeys([])
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

  // Silent background poll so board changes made in Jira (e.g. a status removed
  // from the workflow) propagate even when the user leaves the tab foregrounded
  // and doesn't click refresh. Pauses when the tab is hidden — visibilitychange
  // above already covers the resume case.
  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') {
        refreshActiveRef.current()
      }
    }, 60000)
    return () => clearInterval(id)
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
          <div style={{ padding: 'var(--s-3) var(--s-4)', display: 'flex', alignItems: 'center', gap: 'var(--s-3)', borderBottom: '1px solid var(--divider)', height: 36, boxSizing: 'border-box' }}>
            <button
              type="button"
              className="hbtn"
              onClick={goBackToProjects}
              title="Back to projects"
              style={{ width: 22, height: 22, padding: 0, flex: '0 0 auto' }}
            >
              <Icon name="chevron-left" size={12} />
            </button>
            <span style={{ display: 'inline-flex', alignItems: 'center', flex: '0 0 auto' }}>
              {projectMark(activeProject, 22)}
            </span>
            <span style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)', flex: '1 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', lineHeight: 1 }}>
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
          <div style={{ padding: 'var(--s-3) var(--s-4)', display: 'flex', alignItems: 'center', gap: 'var(--s-3)', borderBottom: '1px solid var(--divider)', height: 36, boxSizing: 'border-box' }}>
            <button
              type="button"
              className="hbtn"
              onClick={goBackToStatuses}
              title={`Back to ${activeProject.key}`}
              style={{ width: 22, height: 22, padding: 0, flex: '0 0 auto' }}
            >
              <Icon name="chevron-left" size={12} />
            </button>
            <span style={{ fontSize: 'var(--t-sm)', fontWeight: 600, color: 'var(--fg-strong)', flex: '1 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', lineHeight: 1 }}>
              {activeStatus.name}
            </span>
            <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', lineHeight: 1, flex: '0 0 auto' }}>{activeProject.key}</span>
            <RefreshBtn busy={issuesLoading} onClick={() => refreshActiveRef.current()} />
          </div>

          {issues && issues.length > 0 && stagedKeys.length === 0 && (
            <div
              style={{
                padding: '4px var(--s-4)',
                fontSize: 10.5,
                color: 'var(--fg-faint)',
                borderBottom: '1px solid var(--divider)',
                lineHeight: 1.3,
              }}
              title={`Hold ${MULTI_SELECT_KEY} and click rows to stage multiple tickets, then Fetch them together.`}
            >
              Tip: {MULTI_SELECT_KEY}-click to select multiple
            </div>
          )}

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

            {(() => {
              const rows = issues || []
              const isSubtask = (r) =>
                (r.issue_type || '').toLowerCase().replace(/[\s-]/g, '') === 'subtask'
              const keysInColumn = new Set(rows.map((r) => r.key))
              // Count hidden subtasks per visible parent. Only true Sub-tasks
              // are eligible — Stories/Tasks under an Epic keep their own row.
              const hiddenCountByParent = {}
              for (const r of rows) {
                if (isSubtask(r) && r.parent_key && keysInColumn.has(r.parent_key)) {
                  hiddenCountByParent[r.parent_key] =
                    (hiddenCountByParent[r.parent_key] || 0) + 1
                }
              }
              const visible = rows.filter(
                (r) => !(isSubtask(r) && r.parent_key && keysInColumn.has(r.parent_key)),
              )
              return visible.map((iss) => {
                const subCount = hiddenCountByParent[iss.key] || 0
                const isOrphanSub = isSubtask(iss) && iss.parent_key && !keysInColumn.has(iss.parent_key)
                // Only treat as out-of-sprint when the backend explicitly says
                // false — null/undefined means the project doesn't use sprints,
                // in which case we render normally.
                const outOfSprint = iss.in_active_sprint === false
                const isStaged = stagedKeys.includes(iss.key)
                const titleColor = selectedIssueKey === iss.key
                  ? undefined
                  : outOfSprint ? 'var(--fg-subtle)' : undefined
                return (
                  <button
                    key={iss.key}
                    type="button"
                    className="rail-row"
                    data-active={selectedIssueKey === iss.key ? 'true' : 'false'}
                    data-staged={isStaged ? 'true' : 'false'}
                    onClick={(e) => handleIssueClick(e, iss.key)}
                    title={
                      subCount > 0
                        ? `${iss.issue_type || ''} · ${iss.summary} (+${subCount} subtask${subCount === 1 ? '' : 's'} in this column)${outOfSprint ? ' · not in active sprint' : ''}`
                        : isOrphanSub
                          ? `Subtask of ${iss.parent_key} · ${iss.summary}${outOfSprint ? ' · not in active sprint' : ''}`
                          : `${iss.issue_type || ''} · ${iss.summary}${outOfSprint ? ' · not in active sprint' : ''}`
                    }
                    style={{
                      padding: isOrphanSub
                        ? '5px var(--s-5) 5px calc(var(--s-4) + 18px)'
                        : '5px var(--s-5) 5px var(--s-4)',
                      alignItems: isOrphanSub ? 'flex-start' : 'center',
                      position: 'relative',
                    }}
                  >
                    {isOrphanSub && (
                      <span
                        aria-hidden="true"
                        style={{
                          position: 'absolute',
                          left: 'calc(var(--s-4) + 4px)',
                          top: 0,
                          bottom: 0,
                          width: 1,
                          background: 'var(--divider)',
                        }}
                      />
                    )}
                    {iss.issue_type && <TypeMark type={iss.issue_type} size={13} />}
                    <span style={{ fontFamily: 'var(--font-mono)', fontSize: 10.5, color: selectedIssueKey === iss.key ? 'var(--accent)' : 'var(--fg-subtle)', flexShrink: 0 }}>
                      {iss.key}
                    </span>
                    <span className="name" style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, color: titleColor }}>
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {iss.summary}
                      </span>
                      {isOrphanSub && (
                        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 10, color: 'var(--fg-faint)', lineHeight: 1.1 }}>
                          <span style={{ textTransform: 'uppercase', letterSpacing: '.04em', fontWeight: 600 }}>
                            Subtask of
                          </span>
                          <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--fg-subtle)' }}>
                            {iss.parent_key}
                          </span>
                        </span>
                      )}
                    </span>
                    {outOfSprint && (
                      <span
                        style={{
                          fontSize: 9.5,
                          fontWeight: 600,
                          textTransform: 'uppercase',
                          letterSpacing: '.04em',
                          color: 'var(--fg-faint)',
                          background: 'transparent',
                          border: '1px solid var(--divider)',
                          borderRadius: 'var(--r-sm)',
                          padding: '1px 5px',
                          lineHeight: 1.2,
                          flexShrink: 0,
                        }}
                        aria-label="Not in active sprint"
                      >
                        Backlog
                      </span>
                    )}
                    {subCount > 0 && (
                      <span
                        style={{
                          fontSize: 10,
                          fontWeight: 600,
                          color: 'var(--fg-muted)',
                          background: 'var(--surface-2, rgba(255,255,255,0.05))',
                          border: '1px solid var(--divider)',
                          borderRadius: 'var(--r-sm)',
                          padding: '1px 5px',
                          lineHeight: 1.2,
                          flexShrink: 0,
                        }}
                        aria-label={`${subCount} subtask${subCount === 1 ? '' : 's'} grouped under this ticket`}
                      >
                        +{subCount} sub
                      </span>
                    )}
                  </button>
                )
              })
            })()}
          </div>

          {stagedKeys.length > 0 && (
            <div
              style={{
                padding: 'var(--s-3) var(--s-4)',
                borderTop: '1px solid var(--divider)',
                background: 'var(--bg-surface)',
                display: 'flex',
                alignItems: 'center',
                gap: 'var(--s-3)',
                flex: '0 0 auto',
              }}
              role="region"
              aria-label="Staged tickets"
            >
              <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-muted)', flex: '1 1 auto', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                <span style={{ fontWeight: 600, color: 'var(--fg-strong)' }}>{stagedKeys.length}</span>{' '}
                selected{' · '}
                <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--fg-subtle)' }}>
                  {stagedKeys.join(', ')}
                </span>
              </span>
              <button
                type="button"
                className="hbtn"
                onClick={clearStaged}
                title="Clear staged selection"
                style={{ height: 22, padding: '0 8px', fontSize: 'var(--t-xs)', flex: '0 0 auto' }}
              >
                Clear
              </button>
              <button
                type="button"
                onClick={commitStaged}
                title={`Fetch ${stagedKeys.length} ticket${stagedKeys.length === 1 ? '' : 's'}`}
                style={{
                  height: 22,
                  padding: '0 10px',
                  fontSize: 'var(--t-xs)',
                  fontWeight: 600,
                  background: 'var(--accent)',
                  color: 'var(--accent-ink, white)',
                  border: '1px solid var(--accent)',
                  borderRadius: 'var(--r-sm)',
                  cursor: 'pointer',
                  flex: '0 0 auto',
                }}
              >
                Fetch {stagedKeys.length}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}

function ProjectRow({ project, isPinned, onSelect, onTogglePin }) {
  return (
    <div style={{ position: 'relative' }}>
      <button
        type="button"
        className="rail-row"
        onClick={() => onSelect(project)}
        style={{ paddingRight: 28 }}
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
          right: 4,
          top: '50%',
          transform: 'translateY(-50%)',
          width: 18,
          height: 18,
          display: 'grid',
          placeItems: 'center',
          padding: 0,
          background: 'transparent',
          border: 0,
          cursor: 'pointer',
          color: isPinned ? 'var(--warning)' : 'var(--fg-faint)',
        }}
      >
        <Icon name="star" size={11} stroke={isPinned ? 2 : 1.5} />
      </button>
    </div>
  )
}

export default JiraBrowser
