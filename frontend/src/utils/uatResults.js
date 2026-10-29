// The UAT results comment: one producer for the review panel's preview and
// for the comment the backend posts on Approve & post (it runs this file
// under node through scripts/format-uat-results.mjs). Input is the run's
// review view from GET /uat-runs/{id}/review.

export const UAT_RESULTS_MARKER = '🤖 UAT run results'

export const NEXT_STEP_LABELS = {
  'pass-to-uat': 'Pass to UAT',
  'fail-to-in-progress': 'Fail back to In Progress',
  'fail-to-todo': 'Fail back to To Do',
}

export const nextStepLabel = (step) => NEXT_STEP_LABELS[step] || 'Stay in testing'

// Plain words for each Not Run code, as a reader of the ticket needs them.
export const NOT_RUN_LABELS = {
  missing_fixture: 'Missing test data',
  needs_client_token: 'Needs a signed-in token',
  not_automatable: 'Needs a person',
  not_independently_verified: 'Could not be verified independently',
  blocked_environment: 'Test environment blocked',
  waiting_for_build: 'Build not deployed yet',
  pr_not_merged: 'Pull request not merged',
  no_runner_profile: 'No runner for this repo',
  wrong_build: 'Wrong build deployed',
  deferred_to_ci: 'Left to CI',
  dropped_time_cap: 'Run hit its time limit',
  permission_denied: 'Runner was not allowed a tool it needed',
  run_cancelled: 'Run cancelled',
  run_aborted: 'Runner stopped before this case',
  plan_changed: 'Plan changed during the run',
}

const BUILD_LABELS = {
  included: 'deployed',
  not_included: 'not deployed yet',
  unverifiable: 'could not be checked',
  unverified: 'no deploy source (checked in the app only)',
  pr_not_merged: 'pull request not merged',
}

const MAX_TITLES = 12
const MAX_TITLE_CHARS = 90

const caseLabel = (c) => c.title || c.id

const shortLabel = (c) => {
  const t = caseLabel(c).replace(/\s+/g, ' ').trim()
  return t.length > MAX_TITLE_CHARS ? `${t.slice(0, MAX_TITLE_CHARS - 1).trimEnd()}…` : t
}

const titleList = (cases) => {
  const shown = cases.slice(0, MAX_TITLES).map(shortLabel)
  const more = cases.length - shown.length
  return more > 0 ? `${shown.join('; ')}; and ${more} more` : shown.join('; ')
}

const evidenceText = (result) => {
  const ev = result?.evidence || []
  const assertion = ev.find((e) => e.kind === 'assertion' && e.text)
  const refs = ev
    .filter((e) => e.ref && (e.kind === 'screenshot' || e.kind === 'video_ts'))
    .map((e) => (e.kind === 'video_ts' && e.at_s != null ? `video ${fmtTs(e.at_s)}` : e.ref.split('/').pop()))
  return { assertion: assertion?.text || null, refs }
}

const fmtTs = (s) => {
  const n = Math.max(0, Math.round(Number(s) || 0))
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`
}

const actorText = (result) => {
  const a = result?.actor || {}
  if (a.kind === 'human') return a.name || a.email || 'a person'
  if (a.kind === 'agent') return 'runner'
  return 'unknown'
}

// Cases grouped the way the panel and the comment both read them.
export const groupCases = (view) => {
  const needsPerson = new Set(view.needs_person_codes || [])
  const groups = { failed: [], passed: [], needsPerson: {}, blocked: {}, noResult: [] }
  for (const c of view.cases || []) {
    const r = c.result
    if (!r) groups.noResult.push(c)
    else if (r.status === 'failed') groups.failed.push(c)
    else if (r.status === 'passed') groups.passed.push(c)
    else if (r.status === 'not_run') {
      const code = r.not_run?.code || 'unknown'
      const bucket = needsPerson.has(code) ? groups.needsPerson : groups.blocked
      ;(bucket[code] ||= []).push(c)
    }
  }
  return groups
}

const countOf = (byCode) => Object.values(byCode).reduce((n, cs) => n + cs.length, 0)

// `opts` lets the panel preview a decision before it is made; once the run is
// approved the backend passes the view alone and the recorded review is used.
export const formatUatResultsAsJira = (view, opts = {}) => {
  const review = view.review || {}
  const nextStep = 'nextStep' in opts ? opts.nextStep : review.next_step ?? null
  const notes = ('notes' in opts ? opts.notes : review.notes) || ''
  const reviewer = opts.reviewer || review.actor?.name || review.actor?.email || null
  const groups = groupCases(view)
  const counts = view.counts || {}
  const lines = []

  lines.push(`${UAT_RESULTS_MARKER} — plan v${view.plan_version} · run ${view.run_id}`)
  lines.push('')
  lines.push(
    `**Result: ${counts.passed || 0} passed · ${counts.failed || 0} failed · ${counts.not_run || 0} not run**` +
      (reviewer ? ` — reviewed by ${reviewer}` : ''),
  )
  lines.push(`**Next step:** ${nextStepLabel(nextStep)}`)
  if (notes.trim()) lines.push(`**Reviewer notes:** ${notes.trim().replace(/\n+/g, ' ')}`)

  if ((view.builds || []).length) {
    lines.push('', '### Builds tested')
    for (const b of view.builds) {
      const where = [b.env, b.deployed_commit && String(b.deployed_commit).slice(0, 7)].filter(Boolean).join(', ')
      lines.push(`- ${b.repo}: ${BUILD_LABELS[b.status] || b.status}${where ? ` (${where})` : ''}`)
    }
  }

  if (groups.failed.length) {
    lines.push('', `### Failed (${groups.failed.length})`)
    for (const c of groups.failed) {
      const { assertion, refs } = evidenceText(c.result)
      const repos = (c.repos || []).length ? ` [${c.repos.join(', ')}]` : ''
      lines.push(
        `- **${caseLabel(c)}**${repos}${assertion ? ` — ${assertion}` : ''}${refs.length ? ` (${refs.join(', ')})` : ''}`,
      )
    }
  }

  if (groups.passed.length) {
    lines.push('', `### Passed (${groups.passed.length})`)
    for (const c of groups.passed) {
      const { assertion } = evidenceText(c.result)
      const flaky = c.result.flaky ? ' (flaky: passed on retry)' : ''
      lines.push(`- ${caseLabel(c)}${flaky} — ${actorText(c.result)}${assertion ? `: ${assertion}` : ''}`)
    }
  }

  const personCount = countOf(groups.needsPerson)
  if (personCount) {
    lines.push('', `### Still needs a person (${personCount})`)
    for (const [code, cs] of Object.entries(groups.needsPerson)) {
      lines.push(`- ${NOT_RUN_LABELS[code] || code} (${cs.length}): ${titleList(cs)}`)
    }
  }

  const blockedCount = countOf(groups.blocked)
  if (blockedCount) {
    lines.push('', `### Not run (${blockedCount})`)
    for (const [code, cs] of Object.entries(groups.blocked)) {
      lines.push(`- ${NOT_RUN_LABELS[code] || code} (${cs.length}): ${titleList(cs)}`)
    }
  }

  if (groups.noResult.length) {
    lines.push('', `### No result recorded (${groups.noResult.length})`, `- ${titleList(groups.noResult)}`)
  }

  const media = view.media || {}
  const attached = [media.video, ...(media.fail_frames || [])].filter((m) => m && !m.too_large)
  const tooLarge = [media.video, ...(media.fail_frames || [])].filter((m) => m && m.too_large)
  lines.push('', '### Video')
  if (media.video && !media.video.too_large) {
    lines.push(`📎 ${media.video.name} (attached to this ticket)`)
    if (view.how_to_see_it) lines.push(`What it shows: ${String(view.how_to_see_it).replace(/\n+/g, ' ')}`)
  } else {
    lines.push(media.video ? `Recorded but too large to attach: ${media.video.name}` : 'No walkthrough was recorded in this run.')
  }
  const frames = attached.filter((m) => m !== media.video)
  if (frames.length) lines.push(`Failure frames attached: ${frames.map((m) => m.name).join(', ')}`)
  if (tooLarge.length && tooLarge.some((m) => m !== media.video)) {
    lines.push(`Too large to attach: ${tooLarge.filter((m) => m !== media.video).map((m) => m.name).join(', ')}`)
  }

  lines.push('', `_Run by the UAT runner; results were checked by a person before posting._`)
  return lines.join('\n') + '\n'
}
