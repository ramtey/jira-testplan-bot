import { useEffect, useMemo, useRef, useState } from 'react'
import {
  API_BASE_URL,
  isWalkthroughCardCtaEnabled,
  isWorkflowEnabledForTicket,
  OPEN_PASS_TO_UAT_EVENT,
} from '../config'
import Icon from './Icon'
import { Btn, Cbx, Alert, Modal } from './ui'

// Match the CSS exit duration so the feedback element stays in the DOM long enough.
const MESSAGE_EXIT_MS = 220

const TESTING_STATUS = 'in testing'

const ENVIRONMENT_OPTIONS = ['Integ', 'Staging', 'Prod']
const DEFAULT_ENVIRONMENTS = ['Integ']

const ENV_PATTERNS = {
  Integ: /\binteg\b/i,
  Staging: /\bstaging\b/i,
  Prod: /\bprod(uction)?\b/i,
}

function detectEnvironments(description, comments) {
  const sources = []
  if (Array.isArray(comments)) {
    const sorted = comments
      .filter((c) => c && typeof c.body === 'string' && c.body.length > 0)
      .slice()
      .sort((a, b) => (b.created || '').localeCompare(a.created || ''))
    sources.push(...sorted.map((c) => c.body))
  }
  if (description) sources.push(description)

  for (const text of sources) {
    const matched = ENVIRONMENT_OPTIONS.filter((env) =>
      ENV_PATTERNS[env].test(text)
    )
    if (matched.length > 0) return matched
  }
  return DEFAULT_ENVIRONMENTS
}

const ACTIONS = [
  {
    id: 'pull-to-testing',
    label: 'Pull to Testing',
    title: 'Move to In Testing and assign to me',
    variant: 'secondary',
    icon: 'arrow-down-right',
    showWhen: (status) => normalize(status) !== TESTING_STATUS,
  },
  {
    id: 'pass-to-uat',
    label: 'Pass to UAT',
    title: 'Move to UAT and reassign to the previous person',
    variant: 'success-soft',
    icon: 'check',
    showWhen: (status) => normalize(status) === TESTING_STATUS,
  },
  {
    id: 'fail-to-todo',
    label: 'Fail back to To Do',
    segmentLabel: 'To Do',
    title: 'Fail back to To Do and reassign to the previous person',
    variant: 'danger-soft',
    icon: 'arrow-left',
    showWhen: (status) => normalize(status) === TESTING_STATUS,
  },
  {
    id: 'fail-to-in-progress',
    label: 'Fail back to In Progress',
    segmentLabel: 'In Progress',
    title: 'Fail back to In Progress and reassign to the previous person',
    variant: 'danger-soft',
    icon: 'arrow-left',
    showWhen: (status) => normalize(status) === TESTING_STATUS,
  },
]

// Bounce-back actions: returned to development with a required reason + note.
const FAIL_ACTION_IDS = ['fail-to-todo', 'fail-to-in-progress']
const isFailAction = (id) => FAIL_ACTION_IDS.includes(id)

function normalize(status) {
  return (status || '').trim().toLowerCase()
}

function buildMentionCandidates({
  assignee,
  assigneeAccountId,
  assigneeHistory,
  assigneeHistoryAccountIds,
  comments,
  currentUserAccountId,
  prContributor,
}) {
  const seen = new Map()
  const skip = (id) => !id || (currentUserAccountId && id === currentUserAccountId)

  if (!skip(assigneeAccountId)) {
    seen.set(assigneeAccountId, {
      accountId: assigneeAccountId,
      name: assignee || 'Assignee',
      isAssignee: true,
    })
  }

  if (Array.isArray(assigneeHistory) && Array.isArray(assigneeHistoryAccountIds)) {
    for (let i = 0; i < assigneeHistory.length; i++) {
      const id = assigneeHistoryAccountIds[i]
      const name = assigneeHistory[i]
      if (skip(id) || seen.has(id)) continue
      seen.set(id, { accountId: id, name: name || id, isAssignee: false })
    }
  }

  // Slot the PR author in after prior assignees, mirroring the server's own
  // auto-pick chain (prior-assignee → PR contributor). Lets the tester see
  // — and override — the fallback on tickets where they're the only person
  // in the history (nothing else would surface anyone).
  if (prContributor && !skip(prContributor.accountId) && !seen.has(prContributor.accountId)) {
    seen.set(prContributor.accountId, {
      accountId: prContributor.accountId,
      name: prContributor.name || prContributor.accountId,
      isAssignee: false,
    })
  }

  if (Array.isArray(comments)) {
    for (const c of comments) {
      const id = c?.author_account_id
      if (skip(id) || seen.has(id)) continue
      seen.set(id, {
        accountId: id,
        name: c.author || id,
        isAssignee: false,
      })
    }
  }

  return Array.from(seen.values())
}

function EnvPill({ value, on, onToggle, disabled }) {
  return (
    <button
      type="button"
      className="env-pill"
      data-on={on ? 'true' : 'false'}
      disabled={disabled}
      onClick={onToggle}
      aria-pressed={on}
    >
      {on && <Icon name="check" size={11} />}
      {value}
    </button>
  )
}

function ImageDropzone({ files, onAdd, onRemove, disabled }) {
  const [isDragging, setIsDragging] = useState(false)
  const inputRef = useRef(null)

  // Paste support: when the form is focused, ctrl/cmd+V drops an image
  // from the clipboard straight into the upload list. Mirrors the
  // existing "paste a URL" muscle memory but lands a real attachment.
  useEffect(() => {
    if (disabled) return
    const handler = (e) => {
      const items = e.clipboardData?.items
      if (!items) return
      const imageItems = Array.from(items).filter(
        (it) => it.type.startsWith('image/') || it.type === 'application/pdf'
      )
      if (imageItems.length === 0) return
      e.preventDefault()
      const fileList = imageItems
        .map((it) => it.getAsFile())
        .filter(Boolean)
      onAdd(fileList)
    }
    window.addEventListener('paste', handler)
    return () => window.removeEventListener('paste', handler)
  }, [disabled, onAdd])

  const onDrop = (e) => {
    e.preventDefault()
    setIsDragging(false)
    if (disabled) return
    onAdd(e.dataTransfer.files)
  }

  return (
    <div>
      <div
        onClick={() => !disabled && inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault()
          if (!disabled) setIsDragging(true)
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={onDrop}
        style={{
          border: '1px dashed ' + (isDragging ? 'var(--accent)' : 'var(--border)'),
          background: isDragging ? 'rgba(59,130,246,.06)' : 'transparent',
          borderRadius: 'var(--r-md)',
          padding: 'var(--s-4) var(--s-5)',
          cursor: disabled ? 'not-allowed' : 'pointer',
          fontSize: 'var(--t-sm)',
          color: 'var(--fg-subtle)',
          transition: 'background 120ms, border-color 120ms',
        }}
      >
        <Icon name="image" size={13} style={{ marginRight: 6, verticalAlign: '-2px' }} />
        Click, drag, or paste files here. PNG / JPEG / GIF / WEBP / PDF, up to 10 MB each.
        <input
          ref={inputRef}
          type="file"
          accept="image/png,image/jpeg,image/gif,image/webp,application/pdf"
          multiple
          hidden
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => {
            onAdd(e.target.files)
            e.target.value = ''
          }}
        />
      </div>
      {files.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 8 }}>
          {files.map((f, i) => (
            <span
              key={i}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                background: 'var(--bg-subtle)',
                border: '1px solid var(--border)',
                borderRadius: 999,
                padding: '4px 10px',
                fontSize: 'var(--t-xs)',
              }}
              title={`${f.name} · ${(f.size / 1024).toFixed(0)} KB`}
            >
              <Icon name="image" size={11} />
              {f.name.length > 28 ? f.name.slice(0, 25) + '…' : f.name}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  onRemove(i)
                }}
                disabled={disabled}
                style={{
                  background: 'transparent',
                  border: 'none',
                  cursor: disabled ? 'not-allowed' : 'pointer',
                  color: 'var(--fg-subtle)',
                  padding: 0,
                  minWidth: 0,
                  width: 16,
                  height: 16,
                  boxShadow: 'none',
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  borderRadius: 999,
                  marginLeft: 2,
                }}
                aria-label={`Remove ${f.name}`}
              >
                <Icon name="x" size={12} />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

// Mirrors LOOM_URL_RE in src/app/models.py. Gates the pass-to-UAT /
// fail-to-* forms so the tester sees an inline error instead of a 422
// from the backend validator. Keep the two in sync.
const LOOM_URL_RE = /^https?:\/\/(?:www\.)?loom\.com\/share\/[A-Za-z0-9_-]+$/i

// Human-facing message per status returned by GET /issue/{key}/pr-looms.
// Kept as a lookup instead of an if-tree so cases stay parallel and easy to
// read. Note: "skipped" (non-SK project) is handled by not rendering the
// panel at all — the tester never sees a message about it.
const PR_LOOM_STATUS_COPY = {
  loading: 'Scanning merged PR for Looms…',
  found: 'Found in the merged PR description — tick to include in the hand-off comment.',
  no_prs: 'No PR is linked to this ticket yet.',
  no_merged_prs: 'A PR is linked, but nothing is merged yet.',
  no_looms: 'The merged PR description has no Loom link.',
  no_token: 'GitHub token not configured — PR-Loom scan is disabled server-side.',
  github_unreachable: "Couldn't read the merged PR from GitHub (rate limit / permissions).",
  error: 'PR scan failed — check the server log.',
}

// Copy for the sibling PR-image panel. Uses the same status keys as the Loom
// scan (both come off the same fetch), but only the "found" copy differs —
// the empty/error states share a scanner, so we key the "images specifically
// were absent" case as `no_images` (frontend-only) to avoid overriding the
// Loom panel's "no_looms" message when both are absent.
const PR_IMAGE_STATUS_COPY = {
  found: 'Screenshots from the merged PR — tick to attach them to the hand-off comment.',
  no_images: 'The merged PR description has no screenshots.',
}

// Route through the backend proxy so private-repo user-attachments URLs
// (which the browser can't authenticate to) still render as previews.
// See /pr-image-proxy in workflow_routes.py — it enforces the same host
// whitelist as the payload validator on the submit side.
function proxyImageUrl(rawUrl) {
  return `${API_BASE_URL}/issue/pr-image-proxy?url=${encodeURIComponent(rawUrl)}`
}

function PrImageDiscoveryPanel({ discovered, selected, onToggle, onPreviewFail }) {
  // Kept separate from the Loom panel because the layout differs — Loom
  // renders one URL per row, images render a tile grid so the tester can
  // see *what* they're about to attach without opening each link. Only
  // rendered when `discovered.length > 0`; empty/error states share the
  // Loom panel's status line. URLs whose preview fails to load are dropped
  // via `onPreviewFail` so the panel disappears entirely when nothing
  // actually renders — a URL that can't be previewed almost certainly
  // can't be downloaded server-side either, so keeping it around as an
  // opt-in checkbox would just invite a broken attachment.
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        padding: 'var(--s-3) var(--s-4)',
        background: 'rgba(59,130,246,.05)',
        border: '1px solid rgba(59,130,246,.20)',
        borderRadius: 'var(--r-md)',
      }}
    >
      <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', marginBottom: 2 }}>
        {PR_IMAGE_STATUS_COPY.found}
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(96px, 1fr))',
          gap: 8,
        }}
      >
        {discovered.map((url) => {
          const isSelected = selected.has(url)
          return (
            <button
              type="button"
              key={url}
              onClick={() => onToggle(url)}
              title={isSelected ? 'Click to skip this screenshot' : 'Click to include this screenshot'}
              aria-pressed={isSelected}
              style={{
                position: 'relative',
                padding: 0,
                border: '2px solid ' + (isSelected ? 'var(--accent)' : 'var(--border)'),
                borderRadius: 'var(--r-sm)',
                background: 'var(--bg-subtle)',
                cursor: 'pointer',
                overflow: 'hidden',
                aspectRatio: '4 / 3',
                boxShadow: isSelected ? '0 0 0 3px rgba(59,130,246,.15)' : 'none',
                transition: 'border-color 120ms, box-shadow 120ms',
              }}
            >
              <img
                src={proxyImageUrl(url)}
                alt=""
                loading="lazy"
                onError={() => onPreviewFail(url)}
                style={{
                  width: '100%',
                  height: '100%',
                  objectFit: 'cover',
                  display: 'block',
                }}
              />
              {isSelected && (
                <span
                  style={{
                    position: 'absolute',
                    top: 4,
                    right: 4,
                    background: 'var(--accent)',
                    color: 'white',
                    borderRadius: '50%',
                    width: 18,
                    height: 18,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    boxShadow: '0 1px 2px rgba(0,0,0,.25)',
                  }}
                >
                  <Icon name="check" size={11} />
                </span>
              )}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function PrLoomDiscoveryPanel({ status, discovered, selected, onToggle }) {
  const tone =
    status === 'found' ? 'info'
    : status === 'error' || status === 'no_token' || status === 'github_unreachable' ? 'warn'
    : 'muted'
  const palette = {
    info:  { bg: 'rgba(59,130,246,.05)', border: 'rgba(59,130,246,.20)' },
    warn:  { bg: 'rgba(234,179,8,.06)',  border: 'rgba(234,179,8,.28)' },
    muted: { bg: 'var(--bg-subtle)',     border: 'var(--border)' },
  }[tone]
  const copy = PR_LOOM_STATUS_COPY[status] || 'Scan finished.'
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
        padding: 'var(--s-3) var(--s-4)',
        background: palette.bg,
        border: '1px solid ' + palette.border,
        borderRadius: 'var(--r-md)',
      }}
    >
      <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', marginBottom: status === 'found' ? 2 : 0 }}>
        {copy}
      </div>
      {status === 'found' && discovered.map((url) => (
        <label
          key={url}
          className="cbx-label"
          style={{ alignItems: 'center', gap: 8 }}
        >
          <span
            className="cbx"
            data-checked={selected.has(url) ? 'true' : 'false'}
            role="checkbox"
            aria-checked={selected.has(url)}
            tabIndex={0}
            onClick={() => onToggle(url)}
            onKeyDown={(e) => {
              if (e.key === ' ' || e.key === 'Enter') {
                e.preventDefault()
                onToggle(url)
              }
            }}
          />
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
            style={{
              fontFamily: 'var(--font-mono, monospace)',
              fontSize: 'var(--t-xs)',
              color: 'var(--fg)',
              textDecoration: 'none',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {url}
          </a>
        </label>
      ))}
    </div>
  )
}

// Typeahead for people who never touched the ticket. Debounces onto the
// server-side Jira user-search endpoint so keystrokes don't hammer it, and
// filters out anyone already in the picker (picking them again would be a
// no-op and just clutters the dropdown). Silent on network failure — the
// dropdown stays empty rather than showing an error, matching how the
// pr-looms / pr-contributor fetches behave elsewhere in this form.
function UserSearchInput({ excludeAccountIds, currentUserAccountId, onPick, disabled }) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState([])
  const [isFocused, setIsFocused] = useState(false)
  const [isLoading, setIsLoading] = useState(false)
  const blurTimerRef = useRef(null)

  useEffect(() => {
    const trimmed = query.trim()
    if (trimmed.length < 2) {
      setResults([])
      setIsLoading(false)
      return
    }
    const controller = new AbortController()
    setIsLoading(true)
    const timer = setTimeout(async () => {
      try {
        const response = await fetch(
          `${API_BASE_URL}/issue/users/search?q=${encodeURIComponent(trimmed)}&limit=8`,
          { signal: controller.signal }
        )
        if (!response.ok) {
          setResults([])
          return
        }
        const data = await response.json().catch(() => ({}))
        setResults(Array.isArray(data?.results) ? data.results : [])
      } catch (err) {
        if (err?.name !== 'AbortError') setResults([])
      } finally {
        setIsLoading(false)
      }
    }, 200)
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [query])

  useEffect(() => () => {
    if (blurTimerRef.current) clearTimeout(blurTimerRef.current)
  }, [])

  // Exclude anyone already in the picker AND the current user — nobody
  // needs to @-mention or assign to themselves from this form.
  const excluded = new Set(excludeAccountIds || [])
  if (currentUserAccountId) excluded.add(currentUserAccountId)
  const visibleResults = results.filter((r) => !excluded.has(r.account_id))
  const showDropdown = isFocused && query.trim().length >= 2

  return (
    <div style={{ position: 'relative' }}>
      <input
        type="text"
        className="inp"
        placeholder="Search anyone in Jira by name or email…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onFocus={() => {
          if (blurTimerRef.current) clearTimeout(blurTimerRef.current)
          setIsFocused(true)
        }}
        onBlur={() => {
          // Delay so a click on a result registers before we tear the
          // dropdown down (mousedown → blur → click order).
          blurTimerRef.current = setTimeout(() => setIsFocused(false), 120)
        }}
        disabled={disabled}
      />
      {showDropdown && (
        <div
          role="listbox"
          style={{
            position: 'absolute',
            top: 'calc(100% + 6px)',
            left: 0,
            right: 0,
            background: 'var(--bg-overlay)',
            border: '1px solid var(--line-strong)',
            borderRadius: 'var(--r-md)',
            boxShadow: '0 12px 32px rgba(0,0,0,.45), 0 2px 6px rgba(0,0,0,.35)',
            zIndex: 20,
            maxHeight: 260,
            overflowY: 'auto',
            padding: 4,
          }}
        >
          {isLoading && visibleResults.length === 0 && (
            <div style={{ padding: '10px 12px', fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
              Searching…
            </div>
          )}
          {!isLoading && visibleResults.length === 0 && (
            <div style={{ padding: '10px 12px', fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
              {query.trim().length < 2 ? 'Type at least 2 characters.' : 'No matches.'}
            </div>
          )}
          {visibleResults.map((r) => (
            <button
              key={r.account_id}
              type="button"
              role="option"
              onMouseDown={(e) => {
                // Fire on mousedown so the input's onBlur (which hides the
                // dropdown) doesn't win the race and swallow the click.
                e.preventDefault()
                onPick({ accountId: r.account_id, name: r.display_name })
                setQuery('')
                setResults([])
              }}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                width: '100%',
                textAlign: 'left',
                padding: '8px 10px',
                background: 'transparent',
                border: 'none',
                borderRadius: 'var(--r-sm)',
                cursor: 'pointer',
                fontSize: 'var(--t-sm)',
                color: 'var(--fg)',
                transition: 'background 80ms',
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'rgba(255,255,255,.06)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
            >
              <Icon name="plus" size={12} style={{ color: 'var(--fg-subtle)' }} />
              {r.display_name}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function WorkflowActions({
  ticketKey,
  currentStatus,
  description,
  comments,
  assignee,
  assigneeAccountId,
  assigneeHistory,
  assigneeHistoryAccountIds,
  currentUserAccountId,
  childIssues,
  onActionComplete,
  videoChecklistSteps,
}) {
  const [pendingAction, setPendingAction] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const [isLeaving, setIsLeaving] = useState(false)
  const [noteForAction, setNoteForAction] = useState(null)
  const [loomUrlsText, setLoomUrlsText] = useState('')
  const [summary, setSummary] = useState('')
  const [environments, setEnvironments] = useState(DEFAULT_ENVIRONMENTS)
  const [reason, setReason] = useState('')
  const [imageFiles, setImageFiles] = useState([])
  const [mentionAccountIds, setMentionAccountIds] = useState([])
  // Tester-added people who never touched the ticket. Populated by the
  // "Add someone" typeahead. Merged into `mentionCandidates` below so they
  // render as pills alongside the ticket's own history, and dedup'd against
  // the base candidates so re-picking someone already listed is a no-op.
  const [extraCandidates, setExtraCandidates] = useState([])
  // Manual override for who gets the ticket after the transition.
  // Shapes:
  //   null                                    → server auto-picks (prior assignee → PR contributor)
  //   { accountId: string, name: string }     → assign to that person
  //   { accountId: null,   name: 'Unassigned' } → clear the assignee
  const [assigneeOverride, setAssigneeOverride] = useState(null)
  const [cascadeToSubtasks, setCascadeToSubtasks] = useState(false)
  // PR-Loom discovery: kicked off when the Pass-to-UAT form opens.
  // - prLoomStatus: null (haven't fetched yet) | "loading" | server-returned
  //   status string. Drives the inline status line so the tester can *see*
  //   the scan ran and what it found.
  // - discoveredPrLooms: URLs scraped from merged PR descriptions.
  // - selectedPrLooms: which of the discovered ones the tester wants to
  //   fold into the hand-off. Defaults to *all discovered* so the common
  //   case is a single click; individual URLs can be toggled off.
  const [prLoomStatus, setPrLoomStatus] = useState(null)
  const [discoveredPrLooms, setDiscoveredPrLooms] = useState([])
  const [selectedPrLooms, setSelectedPrLooms] = useState(() => new Set())
  // Same lifecycle as Loom discovery, but the panel below the Loom one shows
  // *thumbnails* of screenshots found in the merged PR description. Selected
  // URLs are downloaded server-side on submit and attached to the pass
  // comment as media nodes (same treatment as tester-uploaded screenshots).
  const [discoveredPrImages, setDiscoveredPrImages] = useState([])
  const [selectedPrImages, setSelectedPrImages] = useState(() => new Set())
  // Top PR contributor resolved to a Jira user, fetched lazily on
  // Pass-to-UAT open (see effect below). Shape: { accountId, name } | null.
  // Feeds into the mention picker so tickets where the current user is the
  // only person in the history still surface someone to hand the ticket to.
  const [prContributor, setPrContributor] = useState(null)
  // Tracks whether the tester has manually touched the assignee picker in
  // this form session. When true, we stop syncing the override to the
  // computed default so a late-arriving PR contributor can't clobber their
  // explicit pick. Reset on form open / close.
  const userChoseAssigneeRef = useRef(false)
  // Which column the single "Fail back" action returns the ticket to.
  const [failTargetId, setFailTargetId] = useState('fail-to-todo')
  const [failMenuOpen, setFailMenuOpen] = useState(false)
  const failBackRef = useRef(null)
  // Prompt shown when the server rejects Pass-to-UAT because the ticket is
  // high-complexity and no walkthrough is attached. Client-side pre-checks
  // moved to the server in step 5 — the frontend just handles the 409.
  //   null                            → no prompt
  //   { message: string, retry: fn }  → prompt open, retry() resends with the
  //                                     override flag on confirm
  const [walkthroughOverridePrompt, setWalkthroughOverridePrompt] = useState(null)
  // "Steps demonstrated in the video" checklist rendered inside the Notes
  // block of the Pass-to-UAT form. On submit, ticked/unticked state is
  // folded into the Jira comment (see composedSummary in onNoteSubmit).
  // State resets when the form closes.
  const [checklistTicked, setChecklistTicked] = useState(() => new Set())
  const checklistSteps = Array.isArray(videoChecklistSteps) ? videoChecklistSteps : []
  const loomInputRef = useRef(null)

  // Memoized so the OPEN_PASS_TO_UAT_EVENT effect can take
  // `defaultPassToUatAssignee` as a dep without re-registering every render.
  // Must live before the effects below (they close over these values) and
  // before the early returns further down (Rules of Hooks).
  const baseCandidates = useMemo(
    () =>
      buildMentionCandidates({
        assignee,
        assigneeAccountId,
        assigneeHistory,
        assigneeHistoryAccountIds,
        comments,
        currentUserAccountId,
        prContributor,
      }),
    [
      assignee,
      assigneeAccountId,
      assigneeHistory,
      assigneeHistoryAccountIds,
      comments,
      currentUserAccountId,
      prContributor,
    ]
  )

  // Tester-added extras get flagged `isExtra: true` so the pill renderer can
  // show a "+" prefix instead of the ★/plain treatment used for people the
  // ticket already touched. Dedup'd against baseCandidates in case the tester
  // searched for someone who's already in the list — we prefer the base entry
  // (it may carry `isAssignee: true`).
  const mentionCandidates = useMemo(() => {
    const seen = new Set(baseCandidates.map((p) => p.accountId))
    const additions = extraCandidates
      .filter((p) => p?.accountId && !seen.has(p.accountId))
      .map((p) => ({
        accountId: p.accountId,
        name: p.name || p.accountId,
        isAssignee: false,
        isExtra: true,
      }))
    return additions.length === 0 ? baseCandidates : [...baseCandidates, ...additions]
  }, [baseCandidates, extraCandidates])

  // For Pass-to-UAT, the ticket almost always goes back to the developer
  // who owned it before QA pulled it. baseCandidates[0] is exactly that
  // person (buildMentionCandidates orders current-assignee-first → prior
  // assignees → comment authors, skipping the current user), which matches
  // the server's own auto-pick. Deliberately reads from baseCandidates, not
  // mentionCandidates, so an "Add someone else" pick on a fresh ticket
  // doesn't get silently auto-assigned. Null when there's no candidate to
  // default to (fresh ticket, no history, no comments).
  const defaultPassToUatAssignee = useMemo(
    () =>
      baseCandidates[0]
        ? {
            accountId: baseCandidates[0].accountId,
            name: baseCandidates[0].name,
          }
        : null,
    [baseCandidates]
  )

  useEffect(() => {
    if (!feedback || feedback.kind !== 'success') return
    const dismiss = setTimeout(() => setIsLeaving(true), 15000)
    return () => clearTimeout(dismiss)
  }, [feedback])

  useEffect(() => {
    if (!isLeaving) return
    const t = setTimeout(() => {
      setFeedback(null)
      setIsLeaving(false)
    }, MESSAGE_EXIT_MS)
    return () => clearTimeout(t)
  }, [isLeaving])

  // Bridge from the walkthrough-card CTA (rendered in a sibling tree): when
  // it dispatches OPEN_PASS_TO_UAT_EVENT, open the same inline form the header
  // button opens. The CTA is only visible when this ticket is "In Testing",
  // but we re-check status here so a stale listener can't force the form open.
  // Also scrolls the form into view — the CTA lives below the plan, so opening
  // in the header without a scroll would look like the button did nothing.
  useEffect(() => {
    if (normalize(currentStatus) !== TESTING_STATUS) return
    const handler = () => {
      const passAction = ACTIONS.find((a) => a.id === 'pass-to-uat')
      if (!passAction) return
      setEnvironments(detectEnvironments(description, comments))
      userChoseAssigneeRef.current = false
      setAssigneeOverride(defaultPassToUatAssignee)
      setNoteForAction(passAction)
      setFeedback(null)
      setTimeout(() => {
        document
          .getElementById('workflow-actions-root')
          ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
      }, 50)
    }
    window.addEventListener(OPEN_PASS_TO_UAT_EVENT, handler)
    return () => window.removeEventListener(OPEN_PASS_TO_UAT_EVENT, handler)
  }, [currentStatus, description, comments, defaultPassToUatAssignee])

  // Prefetch PR-hosted Loom URLs the moment the Pass-to-UAT modal opens so
  // the discovery panel can render as soon as it's known — the tester sees
  // *what* would be added before deciding whether to include it. Silent on
  // network failure: the panel just stays hidden, matching "no PRs found".
  useEffect(() => {
    if (noteForAction?.id !== 'pass-to-uat' || !ticketKey) return
    const controller = new AbortController()
    setPrLoomStatus('loading')
    setDiscoveredPrLooms([])
    setSelectedPrLooms(new Set())
    setDiscoveredPrImages([])
    setSelectedPrImages(new Set())
    ;(async () => {
      try {
        const response = await fetch(
          `${API_BASE_URL}/issue/${ticketKey}/pr-looms`,
          { signal: controller.signal }
        )
        if (!response.ok) {
          setPrLoomStatus('error')
          return
        }
        const data = await response.json().catch(() => ({}))
        const looms = Array.isArray(data?.loom_urls) ? data.loom_urls : []
        const images = Array.isArray(data?.image_urls) ? data.image_urls : []
        setDiscoveredPrLooms(looms)
        setSelectedPrLooms(new Set(looms))
        setDiscoveredPrImages(images)
        // Default images to *unchecked*: they attach as real Jira attachments
        // (downloaded server-side), which is a heavier action than a URL
        // link — the tester should tick in deliberately, not out.
        setSelectedPrImages(new Set())
        setPrLoomStatus(
          data?.status || (looms.length + images.length > 0 ? 'found' : 'no_looms')
        )
      } catch (err) {
        if (err?.name !== 'AbortError') setPrLoomStatus('error')
      }
    })()
    return () => controller.abort()
  }, [noteForAction, ticketKey])

  // Resolve the top PR author to a Jira user when Pass-to-UAT opens, so the
  // picker can surface them alongside prior assignees. Runs the same lookup
  // as the server's fallback chain — expensive (GitHub API calls) but only
  // fired on demand, mirroring the pr-looms prefetch above. Silent on any
  // failure: the picker just doesn't gain a candidate.
  useEffect(() => {
    if (noteForAction?.id !== 'pass-to-uat' || !ticketKey) return
    const controller = new AbortController()
    setPrContributor(null)
    ;(async () => {
      try {
        const response = await fetch(
          `${API_BASE_URL}/issue/${ticketKey}/pr-contributor`,
          { signal: controller.signal }
        )
        if (!response.ok) return
        const data = await response.json().catch(() => ({}))
        if (data?.account_id && data?.display_name) {
          setPrContributor({
            accountId: data.account_id,
            name: data.display_name,
          })
        }
      } catch {
        // Silent — same treatment as the pr-looms fetch.
      }
    })()
    return () => controller.abort()
  }, [noteForAction, ticketKey])

  // If the PR contributor lands after the form opened and the tester hasn't
  // touched the picker yet, sync the override to the fresh default so the
  // "Ticket will be assigned to …" hint reflects who's actually about to
  // get it. Bypassed once the tester clicks a pill (see setAssigneeOverride
  // wrapper on the picker).
  useEffect(() => {
    if (noteForAction?.id !== 'pass-to-uat') return
    if (userChoseAssigneeRef.current) return
    setAssigneeOverride(defaultPassToUatAssignee)
  }, [noteForAction, defaultPassToUatAssignee])

  const togglePrLoom = (url) => {
    setSelectedPrLooms((prev) => {
      const next = new Set(prev)
      if (next.has(url)) next.delete(url)
      else next.add(url)
      return next
    })
  }

  const togglePrImage = (url) => {
    setSelectedPrImages((prev) => {
      const next = new Set(prev)
      if (next.has(url)) next.delete(url)
      else next.add(url)
      return next
    })
  }

  // Drop a PR image URL whose thumbnail failed to load. The proxy uses the
  // same GitHub token the server would use to download it on submit, so if
  // the preview 404s / 401s, the attachment step would too — better to hide
  // it than let the tester tick a broken URL and get a 502 at submit time.
  const forgetPrImage = (url) => {
    setDiscoveredPrImages((prev) => prev.filter((u) => u !== url))
    setSelectedPrImages((prev) => {
      if (!prev.has(url)) return prev
      const next = new Set(prev)
      next.delete(url)
      return next
    })
  }

  const hasSubtasks =
    Array.isArray(childIssues) &&
    childIssues.some((c) => /sub-?task/i.test(c?.issue_type || ''))

  // Pre-check "Also move all subtasks" whenever the ticket has subtasks — the
  // near-universal intent is to keep the parent and its children in the same
  // column. Users can still uncheck it before firing the action.
  useEffect(() => {
    if (hasSubtasks) setCascadeToSubtasks(true)
  }, [hasSubtasks])

  useEffect(() => {
    if (!failMenuOpen) return
    const onDocDown = (e) => {
      if (!failBackRef.current) return
      if (!failBackRef.current.contains(e.target)) setFailMenuOpen(false)
    }
    const onKey = (e) => {
      if (e.key === 'Escape') setFailMenuOpen(false)
    }
    document.addEventListener('mousedown', onDocDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDocDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [failMenuOpen])

  if (!isWorkflowEnabledForTicket(ticketKey)) return null

  const visibleActions = ACTIONS.filter((a) => a.showWhen(currentStatus))
  if (visibleActions.length === 0) return null

  // When the walkthrough-card CTA is the primary Pass-to-UAT entry point,
  // suppress the header button and the pre-submit nudge modal — the user has
  // literally just seen the walkthrough state on the card they clicked from,
  // so both are redundant friction. In the header we replace it with a small
  // "Ready to hand off" chip that scrolls to the card.
  const handOffFromCard =
    normalize(currentStatus) === TESTING_STATUS &&
    isWalkthroughCardCtaEnabled()

  const closeNoteForm = () => {
    setNoteForAction(null)
    setLoomUrlsText('')
    setSummary('')
    setEnvironments(DEFAULT_ENVIRONMENTS)
    setReason('')
    setImageFiles([])
    setMentionAccountIds([])
    setExtraCandidates([])
    setAssigneeOverride(null)
    setCascadeToSubtasks(hasSubtasks)
    setPrLoomStatus(null)
    setDiscoveredPrLooms([])
    setSelectedPrLooms(new Set())
    setDiscoveredPrImages([])
    setSelectedPrImages(new Set())
    setPrContributor(null)
    userChoseAssigneeRef.current = false
    setChecklistTicked(new Set())
  }

  const addImageFiles = (files) => {
    const incoming = Array.from(files || []).filter(
      (f) => f && (f.type.startsWith('image/') || f.type === 'application/pdf')
    )
    if (incoming.length === 0) return
    setImageFiles((prev) => [...prev, ...incoming])
  }

  const removeImageFile = (idx) => {
    setImageFiles((prev) => prev.filter((_, i) => i !== idx))
  }

  const toggleEnvironment = (env) => {
    setEnvironments((prev) =>
      prev.includes(env) ? prev.filter((e) => e !== env) : [...prev, env]
    )
  }

  const toggleMention = (accountId) => {
    setMentionAccountIds((prev) =>
      prev.includes(accountId)
        ? prev.filter((id) => id !== accountId)
        : [...prev, accountId]
    )
  }

  const runAction = async (action, body, files = [], overrideWalkthrough = false) => {
    setPendingAction(action.id)
    setFeedback(null)
    setIsLeaving(false)
    try {
      // The backend always reads multipart/form-data on this endpoint:
      // `payload` carries the JSON body and `images[]` carries any files.
      // We send an empty FormData (no payload, no files) for actions like
      // pull-to-testing that just transition.
      const form = new FormData()
      const withOverride = overrideWalkthrough
        ? { ...(body || {}), override_missing_walkthrough: true }
        : body
      const effectiveBody = cascadeToSubtasks
        ? { ...(withOverride || {}), cascade_to_subtasks: true }
        : withOverride
      if (effectiveBody && Object.keys(effectiveBody).length > 0) {
        form.append('payload', JSON.stringify(effectiveBody))
      }
      for (const file of files) {
        form.append('images', file, file.name)
      }
      const response = await fetch(
        `${API_BASE_URL}/issue/${ticketKey}/workflow/${action.id}`,
        { method: 'POST', body: form }
      )
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        // Server-side walkthrough gate: 409 with a structured detail body.
        // Open a single confirm prompt — retry runs the same request with the
        // override flag set so the server allows it through.
        if (
          response.status === 409 &&
          data.detail &&
          typeof data.detail === 'object' &&
          data.detail.error_code === 'walkthrough_required' &&
          !overrideWalkthrough
        ) {
          setPendingAction(null)
          setWalkthroughOverridePrompt({
            message:
              data.detail.message ||
              'This ticket has no walkthrough. Pass to UAT anyway?',
            retry: () => {
              setWalkthroughOverridePrompt(null)
              runAction(action, body, files, true)
            },
          })
          return
        }
        const detailText =
          typeof data.detail === 'string'
            ? data.detail
            : `Action failed (${response.status})`
        throw new Error(detailText)
      }
      const assigneeText =
        data.assigned_to === 'unassigned'
          ? 'unassigned'
          : `assigned to ${data.assigned_to}`
      const noteText = data.comment_posted ? ' · note posted' : ''
      const parentText =
        data.parent_transitioned && data.parent_key
          ? ` · parent ${data.parent_key} also moved`
          : ''
      const cascadedCount = Array.isArray(data.cascaded_subtasks)
        ? data.cascaded_subtasks.length
        : 0
      const cascadeText = cascadedCount > 0
        ? ` · ${cascadedCount} subtask${cascadedCount === 1 ? '' : 's'} moved`
        : ''
      const lead =
        isFailAction(action.id)
          ? `Bounced back to ${data.target_status}`
          : action.id === 'pass-to-uat'
            ? `Passed to ${data.target_status}`
            : `Moved to ${data.target_status}`
      setFeedback({
        kind: 'success',
        actionId: action.id,
        text: `${lead} · ${assigneeText}${noteText}${parentText}${cascadeText}`,
      })
      closeNoteForm()
      if (onActionComplete) onActionComplete(action.id)
    } catch (err) {
      setFeedback({ kind: 'error', text: err.message })
    } finally {
      setPendingAction(null)
    }
  }

  const onActionClick = (action) => {
    if (action.id === 'pass-to-uat') {
      setEnvironments(detectEnvironments(description, comments))
      // Default the assignee to the person the ticket came from (the dev),
      // so the tester can just hit "Pass to UAT" without extra clicks in
      // the common case. Fail-back forms deliberately don't pre-select —
      // there the tester is usually explicitly picking who to hand it
      // back to, and defaulting could paper over a wrong pick.
      userChoseAssigneeRef.current = false
      setAssigneeOverride(defaultPassToUatAssignee)
      setNoteForAction(action)
      setFeedback(null)
      return
    }
    if (isFailAction(action.id)) {
      setNoteForAction(action)
      setFeedback(null)
      return
    }
    runAction(action)
  }

  // Merge the assignee override into the outgoing payload.
  //
  // The picker is now always available for Pass-to-UAT (the "Add someone
  // else" search box lets the tester add candidates on a fresh ticket), so
  // the old "candidates non-empty" heuristic no longer identifies whether
  // the tester made a choice. Instead:
  //
  // - assigneeOverride set (default pre-selected, or the tester picked a
  //   pill) → send the override.
  // - Tester clicked a pill to clear the selection (userChoseAssigneeRef)
  //   → send an explicit unassign; they deliberately deselected.
  // - Neither → let the server's auto-pick chain run (prior assignee → PR
  //   contributor). Matches the pre-search-box behavior for fresh tickets.
  const applyAssigneeOverride = (body) => {
    if (!assigneeOverride && !userChoseAssigneeRef.current) return body
    return {
      ...(body || {}),
      assignee_override_set: true,
      assignee_override_account_id: assigneeOverride?.accountId ?? null,
      assignee_override_display_name:
        assigneeOverride?.name ?? 'Unassigned',
    }
  }

  const onNoteSubmit = (e) => {
    e.preventDefault()
    if (!noteForAction) return
    const looms = loomUrlsText
      .split(/\r?\n/)
      .map((s) => s.trim())
      .filter(Boolean)

    const badLoom = looms.find((u) => !LOOM_URL_RE.test(u))
    if (badLoom) {
      setFeedback({
        kind: 'error',
        text: `"${badLoom}" is not a valid Loom URL (expected https://www.loom.com/share/…).`,
      })
      return
    }

    if (noteForAction.id === 'pass-to-uat') {
      // No client-side gate — the server enforces the walkthrough rule and
      // returns a 409 that opens the override prompt in runAction.
      // PR-discovered Loom URLs the tester ticked on ride on their own
      // field so the pass comment can label them "(from merged PR)".
      // Deduped against typed URLs so a link that shows up in both places
      // renders once, as a tester-supplied Loom.
      const seenLooms = new Set(looms)
      const prLooms = discoveredPrLooms.filter(
        (url) => selectedPrLooms.has(url) && !seenLooms.has(url)
      )
      const prImages = discoveredPrImages.filter((url) => selectedPrImages.has(url))
      const trimmedSummary = summary.trim()
      // Fold the "steps demonstrated" checklist into the Jira comment when
      // at least one box is ticked. Silent when nothing is checked so the
      // comment stays clean for testers who don't engage with the widget.
      const composedSummary = (() => {
        if (checklistSteps.length === 0 || checklistTicked.size === 0) {
          return trimmedSummary || null
        }
        const bullets = checklistSteps
          .filter((_, i) => checklistTicked.has(i))
          .map((step) => `- ${step}`)
          .join('\n')
        return trimmedSummary ? `${trimmedSummary}\n\n${bullets}` : bullets
      })()
      const hasAnyField =
        looms.length > 0 ||
        prLooms.length > 0 ||
        prImages.length > 0 ||
        composedSummary ||
        environments.length > 0 ||
        imageFiles.length > 0
      const baseBody = hasAnyField
        ? {
            loom_urls: looms.length > 0 ? looms : null,
            pr_loom_urls: prLooms.length > 0 ? prLooms : null,
            pr_image_urls: prImages.length > 0 ? prImages : null,
            summary: composedSummary,
            environments: environments.length > 0 ? environments : null,
            mention_account_ids:
              mentionAccountIds.length > 0 ? mentionAccountIds : null,
          }
        : undefined
      runAction(noteForAction, applyAssigneeOverride(baseBody), imageFiles)
      return
    }

    if (isFailAction(noteForAction.id)) {
      const trimmedReason = reason.trim()
      if (!trimmedReason) {
        setFeedback({ kind: 'error', text: 'Reason is required.' })
        return
      }
      const body = {
        reason: trimmedReason,
        loom_urls: looms.length > 0 ? looms : null,
        mention_account_ids:
          mentionAccountIds.length > 0 ? mentionAccountIds : null,
      }
      runAction(noteForAction, applyAssigneeOverride(body), imageFiles)
    }
  }

  const isFail = isFailAction(noteForAction?.id)

  return (
    <div id="workflow-actions-root">
      {!noteForAction && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', flexWrap: 'wrap' }}>
          <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', textTransform: 'uppercase', letterSpacing: '.06em', fontWeight: 600, marginRight: 'var(--s-2)' }}>
            Workflow
          </span>
          {visibleActions
            .filter((action) => !isFailAction(action.id))
            .filter((action) => !(handOffFromCard && action.id === 'pass-to-uat'))
            .map((action) => (
              <Btn
                key={action.id}
                variant={action.variant}
                icon={action.icon}
                title={action.title}
                disabled={pendingAction !== null}
                loading={pendingAction === action.id}
                onClick={() => onActionClick(action)}
              >
                {action.label}
              </Btn>
            ))}
          {handOffFromCard && (
            <button
              type="button"
              className="handoff-chip"
              title="Pass-to-UAT lives on the walkthrough card below"
              onClick={() => {
                const target = document.getElementById('uat-guide-card')
                if (target) {
                  target.scrollIntoView({ behavior: 'smooth', block: 'center' })
                  target.classList.add('handoff-flash')
                  setTimeout(() => target.classList.remove('handoff-flash'), 1400)
                }
              }}
            >
              <Icon name="arrow-down-right" size={13} />
              Ready to hand off
            </button>
          )}
          {(() => {
            // The two bounce-backs share the same verb and differ only in the
            // column they return to. One split-button commits the bounce to the
            // selected destination; a chevron opens a small menu to change it.
            const failActions = visibleActions.filter((a) => isFailAction(a.id))
            if (failActions.length === 0) return null
            const selected =
              failActions.find((a) => a.id === failTargetId) || failActions[0]
            const hasChoice = failActions.length > 1
            return (
              <div
                ref={failBackRef}
                className="fail-back"
                role="group"
                aria-label="Fail back to development"
              >
                <button
                  type="button"
                  className="fb-trigger"
                  title={selected.title}
                  disabled={pendingAction !== null}
                  onClick={() => onActionClick(selected)}
                >
                  <Icon name="arrow-left" size={14} />
                  Fail back to {selected.segmentLabel}
                </button>
                {hasChoice && (
                  <>
                    <span className="fb-sep" />
                    <button
                      type="button"
                      className="fb-more"
                      title="Choose a different destination"
                      aria-haspopup="menu"
                      aria-expanded={failMenuOpen}
                      disabled={pendingAction !== null}
                      onClick={() => setFailMenuOpen((v) => !v)}
                    >
                      <Icon name="chevron-down" size={14} stroke={2} />
                    </button>
                    {failMenuOpen && (
                      <div className="fb-menu" role="menu">
                        {failActions.map((action) => (
                          <button
                            key={action.id}
                            type="button"
                            role="menuitem"
                            data-on={action.id === selected.id ? 'true' : 'false'}
                            disabled={pendingAction !== null}
                            onClick={() => {
                              setFailTargetId(action.id)
                              setFailMenuOpen(false)
                            }}
                          >
                            {action.segmentLabel}
                          </button>
                        ))}
                      </div>
                    )}
                  </>
                )}
              </div>
            )
          })()}
          <span style={{ flex: 1 }} />
          {hasSubtasks && (
            <Cbx
              checked={cascadeToSubtasks}
              onChange={setCascadeToSubtasks}
              label="Also move all subtasks"
            />
          )}
        </div>
      )}

      {noteForAction && (
        <form
          onSubmit={onNoteSubmit}
          style={{
            background: isFail ? 'rgba(239,68,68,.04)' : 'rgba(34,197,94,.03)',
            border: '1px solid ' + (isFail ? 'rgba(239,68,68,.25)' : 'rgba(34,197,94,.22)'),
            borderRadius: 'var(--r-md)',
            padding: 'var(--s-6)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-3)', marginBottom: 'var(--s-5)' }}>
            <Icon
              name={isFail ? 'arrow-left' : 'check-circle'}
              size={14}
              style={{ color: isFail ? 'var(--danger)' : 'var(--success)' }}
            />
            <span style={{ fontWeight: 600, color: 'var(--fg-strong)' }}>
              {isFail ? noteForAction.label : 'Pass to UAT'}
            </span>
            <span style={{ color: 'var(--fg-subtle)', fontSize: 'var(--t-sm)' }}>
              {isFail ? 'Will be returned to development.' : 'Will be moved to UAT.'}
            </span>
            <button type="button" className="hbtn" style={{ marginLeft: 'auto' }} onClick={closeNoteForm} disabled={pendingAction !== null}>
              <Icon name="x" size={13} />
            </button>
          </div>

          {isFail && (
            <div style={{ marginBottom: 'var(--s-5)' }}>
              <span className="lbl req">Reason</span>
              <textarea
                className="inp"
                style={{ minHeight: 70, borderColor: 'rgba(239,68,68,.45)', boxShadow: '0 0 0 3px rgba(239,68,68,.10)' }}
                placeholder="Required. What broke, where, and how to reproduce…"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                disabled={pendingAction !== null}
                required
                autoFocus
              />
              <div style={{ marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--danger)' }}>
                This becomes the bounce-back history. Be specific.
              </div>
            </div>
          )}

          <div style={{ display: 'grid', gridTemplateColumns: '160px 1fr', gap: 'var(--s-6) var(--s-7)', alignItems: 'start' }}>
            {noteForAction.id === 'pass-to-uat' && (
              <>
                <span className="lbl">Tested on</span>
                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                  {ENVIRONMENT_OPTIONS.map((env) => (
                    <EnvPill
                      key={env}
                      value={env}
                      on={environments.includes(env)}
                      onToggle={() => toggleEnvironment(env)}
                      disabled={pendingAction !== null}
                    />
                  ))}
                </div>
              </>
            )}

            <span className="lbl">Loom URL{loomUrlsText.includes('\n') ? 's' : ''}</span>
            <textarea
              ref={loomInputRef}
              className="inp mono"
              rows={2}
              placeholder="https://www.loom.com/share/…"
              value={loomUrlsText}
              onChange={(e) => setLoomUrlsText(e.target.value)}
              disabled={pendingAction !== null}
              style={{ minHeight: 40 }}
            />

            {noteForAction.id === 'pass-to-uat' && prLoomStatus && prLoomStatus !== 'skipped' && (
              <>
                <span className="lbl">From merged PR</span>
                <PrLoomDiscoveryPanel
                  status={prLoomStatus}
                  discovered={discoveredPrLooms}
                  selected={selectedPrLooms}
                  onToggle={togglePrLoom}
                />
              </>
            )}

            {noteForAction.id === 'pass-to-uat' && discoveredPrImages.length > 0 && (
              <>
                <span className="lbl">PR screenshots</span>
                <PrImageDiscoveryPanel
                  discovered={discoveredPrImages}
                  selected={selectedPrImages}
                  onToggle={togglePrImage}
                  onPreviewFail={forgetPrImage}
                />
              </>
            )}

            {noteForAction.id === 'pass-to-uat' && (
              <>
                <span className="lbl">Notes</span>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {checklistSteps.length > 0 && (
                    <div
                      style={{
                        border: '1px solid var(--border-muted)',
                        borderRadius: 6,
                        padding: '8px 10px',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 6,
                      }}
                    >
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 6,
                          fontSize: 'var(--t-sm)',
                          color: 'var(--fg-muted)',
                          fontWeight: 600,
                        }}
                      >
                        Steps demonstrated in the video
                        <span style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)', fontWeight: 500 }}>
                          {checklistTicked.size} / {checklistSteps.length}
                        </span>
                      </div>
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                        {checklistSteps.map((step, i) => (
                          <Cbx
                            key={i}
                            id={`video-checklist-${i}`}
                            checked={checklistTicked.has(i)}
                            onChange={(next) =>
                              setChecklistTicked((prev) => {
                                const copy = new Set(prev)
                                if (next) copy.add(i)
                                else copy.delete(i)
                                return copy
                              })
                            }
                            label={step}
                          />
                        ))}
                      </div>
                    </div>
                  )}
                  <textarea
                    className="inp"
                    rows={3}
                    placeholder={
                      checklistSteps.length > 0
                        ? 'Additional notes (optional). Markdown supported.'
                        : 'Optional. Markdown supported.'
                    }
                    value={summary}
                    onChange={(e) => setSummary(e.target.value)}
                    disabled={pendingAction !== null}
                  />
                </div>
              </>
            )}

            <span className="lbl">Screenshots</span>
            <ImageDropzone
              files={imageFiles}
              onAdd={addImageFiles}
              onRemove={removeImageFile}
              disabled={pendingAction !== null}
            />

            {(noteForAction.id === 'pass-to-uat' || mentionCandidates.length > 0) && (
              <>
                {noteForAction.id === 'pass-to-uat' && (
                  <>
                    <span className="lbl">Add someone</span>
                    <UserSearchInput
                      excludeAccountIds={mentionCandidates.map((p) => p.accountId)}
                      currentUserAccountId={currentUserAccountId}
                      onPick={(person) =>
                        setExtraCandidates((prev) =>
                          prev.some((p) => p.accountId === person.accountId)
                            ? prev
                            : [...prev, person]
                        )
                      }
                      disabled={pendingAction !== null}
                    />
                  </>
                )}

                {mentionCandidates.length > 0 && (
                  <>
                    <span className="lbl">Assign to</span>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                        {mentionCandidates.map((person) => {
                          const selected =
                            assigneeOverride?.accountId === person.accountId
                          const prefix = person.isAssignee
                            ? '★ '
                            : person.isExtra
                              ? '+ '
                              : ''
                          return (
                            <EnvPill
                              key={person.accountId}
                              value={`${prefix}${person.name}`}
                              on={selected}
                              onToggle={() => {
                                userChoseAssigneeRef.current = true
                                setAssigneeOverride((prev) =>
                                  prev?.accountId === person.accountId
                                    ? null
                                    : { accountId: person.accountId, name: person.name }
                                )
                              }}
                              disabled={pendingAction !== null}
                            />
                          )
                        })}
                      </div>
                      <div style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-subtle)' }}>
                        {assigneeOverride
                          ? `Ticket will be assigned to ${assigneeOverride.name}.`
                          : 'Ticket will be unassigned — pick someone above to change that.'}
                      </div>
                    </div>

                    <span className="lbl">Notify</span>
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      {mentionCandidates.map((person) => {
                        const selected = mentionAccountIds.includes(person.accountId)
                        const prefix = person.isAssignee
                          ? '★ '
                          : person.isExtra
                            ? '+ '
                            : ''
                        return (
                          <EnvPill
                            key={person.accountId}
                            value={`${prefix}${person.name}`}
                            on={selected}
                            onToggle={() => toggleMention(person.accountId)}
                            disabled={pendingAction !== null}
                          />
                        )
                      })}
                    </div>
                  </>
                )}
              </>
            )}
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--s-4)', marginTop: 'var(--s-6)', flexWrap: 'wrap' }}>
            {hasSubtasks && (
              <Cbx
                checked={cascadeToSubtasks}
                onChange={setCascadeToSubtasks}
                label="Also move all subtasks"
              />
            )}
            <span style={{ flex: 1 }} />
            <Btn variant="ghost" onClick={closeNoteForm} disabled={pendingAction !== null}>Cancel</Btn>
            <Btn
              type="submit"
              variant={isFail ? 'danger' : 'primary'}
              icon={isFail ? 'arrow-left' : 'check'}
              disabled={pendingAction !== null}
              loading={pendingAction === noteForAction.id}
            >
              {noteForAction.label}
            </Btn>
          </div>
        </form>
      )}

      {feedback && (() => {
        const isError = feedback.kind === 'error'
        const isBounce = !isError && isFailAction(feedback.actionId)
        const tone = isError ? 'danger' : isBounce ? 'warning' : 'success'
        const title = isError
          ? 'Action failed'
          : isBounce
            ? 'Returned to dev'
            : 'Success'
        const icon = isBounce ? 'arrow-left' : undefined
        return (
          <div style={{ marginTop: 'var(--s-4)', opacity: isLeaving ? 0 : 1, transition: `opacity ${MESSAGE_EXIT_MS}ms` }}>
            <Alert tone={tone} title={title} icon={icon}>
              {feedback.text}
            </Alert>
          </div>
        )
      })()}

      {/* Server-side walkthrough gate: the backend rejected Pass-to-UAT
          because this ticket is high-complexity with no walkthrough. Confirm
          resubmits with the override flag; cancel leaves the form open so
          the user can add a Loom / screenshot / notes and try again. */}
      {walkthroughOverridePrompt && (
        <Modal
          title="Pass to UAT without a walkthrough?"
          sub="This ticket is flagged high-complexity — testers usually need a video, screenshot, or notes to run it."
          onClose={() => setWalkthroughOverridePrompt(null)}
          width={460}
          foot={
            <>
              <Btn
                variant="ghost"
                onClick={() => setWalkthroughOverridePrompt(null)}
              >
                Add one first
              </Btn>
              <Btn
                variant="primary"
                icon="check"
                onClick={walkthroughOverridePrompt.retry}
              >
                Pass without it
              </Btn>
            </>
          }
        >
          <div style={{ fontSize: 'var(--t-sm)', color: 'var(--fg)', lineHeight: '20px' }}>
            {walkthroughOverridePrompt.message}
          </div>
        </Modal>
      )}
    </div>
  )
}

export default WorkflowActions
