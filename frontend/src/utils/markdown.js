/**
 * Markdown formatting utilities
 */

// The three card sections whose cases can be flagged as already covered by an
// existing unit test. `regression_checklist` is plain strings and never flagged.
const CARD_SECTION_KEYS = ['happy_path', 'edge_cases', 'integration_tests']

const isCoveredByUnitTest = (test) => !!(test && test.covered_by_unit_test)

// Cases the planner flagged as already exercised by automated tests, pulled out
// of their original sections so QA isn't asked to re-run them manually.
const collectCoveredCases = (plan) => {
  const out = []
  CARD_SECTION_KEYS.forEach((key) => {
    const items = Array.isArray(plan?.[key]) ? plan[key] : []
    items.forEach((test) => {
      if (isCoveredByUnitTest(test)) out.push(test)
    })
  })
  return out
}

// A section's cases minus the ones already covered by unit tests.
const uncovered = (items) =>
  Array.isArray(items) ? items.filter((t) => !isCoveredByUnitTest(t)) : []

const formatCoversAcs = (test) => {
  const acIds = Array.isArray(test.covers_acs)
    ? test.covers_acs.filter((id) => typeof id === 'string' && id.trim())
    : []
  return acIds.length > 0 ? `**Covers:** ${acIds.join(', ')}\n\n` : ''
}

const planHasAnyAcs = (plan) =>
  ['happy_path', 'edge_cases', 'integration_tests'].some((key) => {
    const items = plan?.[key]
    if (!Array.isArray(items)) return false
    return items.some(
      (t) =>
        Array.isArray(t?.covers_acs) &&
        t.covers_acs.some((id) => typeof id === 'string' && id.trim())
    )
  })

const formatGroundedIn = (test, planHasAcs) => {
  const sources = Array.isArray(test.grounded_in)
    ? test.grounded_in.filter((s) => typeof s === 'string' && s.trim())
    : []
  const acIds = Array.isArray(test.covers_acs)
    ? test.covers_acs.filter((id) => typeof id === 'string' && id.trim())
    : []
  if (sources.length > 0) {
    const formatted = sources.map((s) => `\`${s}\``).join(', ')
    return `**Grounded in:** ${formatted}\n\n`
  }
  if (planHasAcs && acIds.length === 0) {
    return '> ⚠️ **Untraced** — no AC coverage and no `grounded_in` source. Verify any specific numbers, strings, or symbols in this test before running it.\n\n'
  }
  return ''
}

const formatNeedsVerification = (test) => {
  if (!test.needs_manual_verification) return ''
  return '> ⚠️ **Needs manual verification** — the AC element referenced here could not be verified in the PR diff or testID reference. See UI Grounding Warnings above for details.\n\n'
}

const SURFACE_LABELS = {
  backend_http: 'Backend / HTTP',
  web_ui: 'Web UI',
  mobile: 'Mobile',
  cli: 'CLI',
  manual_only: 'Manual only',
}

// What a runner needs to execute the case: surface, as whom, and where.
const formatSurface = (test) => {
  const bits = []
  if (typeof test.surface === 'string' && test.surface.trim()) {
    bits.push(SURFACE_LABELS[test.surface] || test.surface)
  }
  if (typeof test.credentials === 'string' && test.credentials.trim()) {
    bits.push(`credentials: ${test.credentials.trim()}`)
  }
  if (typeof test.environment === 'string' && test.environment.trim()) {
    bits.push(`environment: ${test.environment.trim()}`)
  }
  return bits.length > 0 ? `**Runs on:** ${bits.join(' · ')}\n\n` : ''
}

// Whether the expected result was read out of the implementation or assumed.
// A reviewer needs to know which assertions carry weight.
const formatExpectedVerification = (test) => {
  if (test.expected_verified === true) {
    const src = typeof test.expected_source === 'string' && test.expected_source.trim()
      ? `\`${test.expected_source.trim()}\``
      : '_source not cited_'
    return `**Expected verified against:** ${src}\n\n`
  }
  if (test.expected_verified === false) {
    return '> ⚠️ **Unverified — assumption.** This expected result was not read from the implementation. Confirm it against the code before treating a failure as a defect.\n\n'
  }
  return ''
}

// Rule-8 output: gaps that would break the ticket's goal but that no case
// covers. Deliberately not a checklist — these are not pass/fail items.
const formatRisksAndGaps = (plan) => {
  const gaps = Array.isArray(plan?.risks_and_gaps) ? plan.risks_and_gaps.filter(Boolean) : []
  if (gaps.length === 0) return ''
  let md = '## ⚠️ Risks / Gaps Observed\n\n'
  md += '_Not test cases — these are gaps in the ticket itself that no case can mark pass or fail._\n\n'
  gaps.forEach((g) => {
    if (typeof g === 'string') {
      md += `- ${g}\n`
      return
    }
    md += `- **${g.gap || 'Unnamed gap'}**\n`
    if (g.impact) md += `  - Impact: ${g.impact}\n`
    if (g.evidence) md += `  - Evidence: \`${g.evidence}\`\n`
  })
  return md + '\n'
}

const formatAcCoverageSummary = (coverage) => {
  if (!coverage || !coverage.tickets) return ''
  const entries = Object.entries(coverage.tickets).filter(
    ([, info]) => info && (info.total > 0 || (info.superseded?.length ?? 0) > 0)
  )
  if (entries.length === 0) return ''

  let md = '## Acceptance Criteria Coverage\n\n'
  entries.forEach(([key, info]) => {
    const covered = info.covered?.length ?? 0
    const total = info.total ?? 0
    const icon = covered === total ? '✅' : '⚠️'
    md += `- **${key}**: ${covered}/${total} ${icon}\n`
    if (info.uncovered && info.uncovered.length > 0) {
      info.uncovered.forEach((u) => {
        md += `    - ❌ \`${u.id}\` — ${u.text}\n`
      })
    }
  })
  md += '\n'
  return md
}

const formatSupersededAcs = (plan) => {
  const list = Array.isArray(plan.superseded_acs) ? plan.superseded_acs : []
  if (list.length === 0) return ''
  let md = '## 🔁 Superseded Acceptance Criteria\n\n'
  md += '_The newer ticket\'s AC overrides the older one; the older AC is intentionally not tested._\n\n'
  list.forEach((s) => {
    md += `- \`${s.loser_id}\` → \`${s.winner_id}\``
    if (s.reason) md += ` — ${s.reason}`
    md += '\n'
    if (s.loser_text) md += `    - Older: ${s.loser_text}\n`
    if (s.winner_text) md += `    - Newer: ${s.winner_text}\n`
  })
  md += '\n'
  return md
}

const formatGroundingWarnings = (plan) => {
  const list = Array.isArray(plan.grounding_warnings) ? plan.grounding_warnings : []
  if (list.length === 0) return ''
  let md = `## 🔍 UI Grounding Warnings (${list.length})\n\n`
  md += '_The model referenced these UI elements in test steps but could not verify them in the PR diff or testID reference. Confirm before running the tests._\n\n'
  list.forEach((w) => {
    md += `- \`${w.ac_id}\` — **${w.missing_element}**\n`
    md += `    - ${w.explanation}\n`
  })
  md += '\n'
  return md
}

// Pull together the LLM's "how to see it" orientation and the planner's
// walkthrough (Loom / screenshot / notes) into one normalized shape. Returns
// null when there's nothing worth showing, so callers can skip the section.
const collectUatGuide = (plan, walkthrough) => {
  const complexity = plan?.uat_complexity || null
  const summary = typeof plan?.how_to_see_it?.summary === 'string' ? plan.how_to_see_it.summary.trim() : ''
  const reason = typeof plan?.how_to_see_it?.reason === 'string' ? plan.how_to_see_it.reason.trim() : ''
  const loom = walkthrough?.loom_url?.trim() || ''
  const screenshots = (Array.isArray(walkthrough?.screenshots) ? walkthrough.screenshots : [])
    .map((s) => ({
      url: (s?.url || '').trim(),
      filename: (s?.filename || '').trim(),
    }))
    .filter((s) => s.url)
  const notes = walkthrough?.notes?.trim() || ''
  if (!summary && !reason && !loom && screenshots.length === 0 && !notes) return null
  return { complexity, summary, reason, loom, screenshots, notes }
}

// Markdown variant — prepended to the exported/downloaded plan.
const formatUatGuideMarkdown = (plan, walkthrough) => {
  const g = collectUatGuide(plan, walkthrough)
  if (!g) return ''
  const heading = g.complexity === 'high' ? '## 🧭 How to test this — start here' : '## 🧭 How to test this'
  let md = `${heading}\n\n`
  if (g.complexity) md += `**UAT complexity:** ${g.complexity}\n\n`
  if (g.summary) md += `${g.summary}\n\n`
  if (g.reason) md += `*Why it's tricky: ${g.reason}*\n\n`
  if (g.loom) md += `🎥 **Walkthrough:** ${g.loom}\n\n`
  if (g.screenshots.length > 0) {
    const label = g.screenshots.length === 1 ? 'Screenshot' : 'Screenshots'
    md += `📷 **${label}:**\n`
    for (const shot of g.screenshots) {
      md += shot.filename
        ? `- [${shot.filename}](${shot.url})\n`
        : `- ${shot.url}\n`
    }
    md += '\n'
  }
  if (g.notes) md += `**Setup / repro notes:**\n${g.notes}\n\n`
  md += '---\n\n'
  return md
}

// Plain-text variant — prepended to the Jira comment so the UAT assignee sees
// it first, above the test cases.
const formatUatGuideJira = (plan, walkthrough) => {
  const g = collectUatGuide(plan, walkthrough)
  if (!g) return ''
  const heading = g.complexity === 'high'
    ? '🧭 HOW TO TEST THIS — START HERE'
    : '🧭 HOW TO TEST THIS'
  let jira = `${heading}\n\n`
  if (g.complexity) jira += `UAT complexity: ${g.complexity.toUpperCase()}\n\n`
  if (g.summary) jira += `${g.summary}\n\n`
  if (g.reason) jira += `Why it's tricky: ${g.reason}\n\n`
  if (g.loom) jira += `🎥 Walkthrough: ${g.loom}\n\n`
  if (g.screenshots.length > 0) {
    const label = g.screenshots.length === 1 ? 'Screenshot' : 'Screenshots'
    jira += `📷 ${label}:\n`
    for (const shot of g.screenshots) {
      jira += shot.filename
        ? `- ${shot.filename} — ${shot.url}\n`
        : `- ${shot.url}\n`
    }
    jira += '\n'
  }
  if (g.notes) jira += `Setup / repro notes:\n${g.notes}\n\n`
  jira += '════════════════════════════════════════════\n\n'
  return jira
}

// ─── source provenance ──────────────────────────────────────────────────────
// What the plan was derived from. A reader needs this to weigh every case in
// it: cases written from a merged PR describe shipped behaviour, cases written
// from an open one describe a proposal, and a PR that was closed unmerged
// describes nothing at all. Plans generated before the pipeline recorded
// provenance carry none, and render nothing here.

const PR_STATE_LABELS = {
  merged: 'merged',
  open: 'open — not yet merged',
  closed_unmerged: 'closed without merging — NOT used',
  unknown: 'state unconfirmed',
}

const prLabel = (entry) => {
  const repo = entry.repository || ''
  const num = entry.number
  if (repo && num) return `${repo}#${num}`
  if (num) return `PR #${num}`
  return entry.url || entry.title || 'unidentified PR'
}

const shortSha = (sha) =>
  typeof sha === 'string' && sha.length >= 7 ? sha.slice(0, 7) : null

const provenanceEntries = (plan) => {
  const entries = plan?.source_provenance?.pull_requests
  return Array.isArray(entries) ? entries : []
}

const formatProvenanceMarkdown = (plan) => {
  const entries = provenanceEntries(plan)
  if (entries.length === 0) return ''
  let md = '## 🔗 Grounded in\n\n'
  if (plan.source_provenance.grounded_on_unmerged) {
    md += '> ⚠️ Part of this plan rests on code that has not merged. Those cases describe proposed behaviour and can drift as the PR changes.\n\n'
  }
  entries.forEach((e) => {
    const state = PR_STATE_LABELS[e.state] || e.state || 'unknown'
    const sha = shortSha(e.head_sha)
    md += `- ${e.url ? `[${prLabel(e)}](${e.url})` : prLabel(e)} — ${state}`
    if (sha) md += ` @ \`${sha}\``
    if (!e.used_as_grounding) md += ' — **excluded**'
    md += '\n'
  })
  if (entries.some((e) => !e.used_as_grounding)) {
    md += '\n_Closed-unmerged pull requests were deliberately not used as source. Code that was abandoned cannot be tested._\n'
  }
  md += '\n'
  return md
}

const formatProvenanceJira = (plan) => {
  const entries = provenanceEntries(plan)
  if (entries.length === 0) return ''
  let jira = '🔗 GROUNDED IN\n\n'
  if (plan.source_provenance.grounded_on_unmerged) {
    jira += '⚠️ Part of this plan rests on code that has not merged. Those cases describe proposed behaviour and can drift as the PR changes.\n\n'
  }
  entries.forEach((e) => {
    const state = PR_STATE_LABELS[e.state] || e.state || 'unknown'
    const sha = shortSha(e.head_sha)
    jira += `  • ${prLabel(e)} — ${state}`
    if (sha) jira += ` @ ${sha}`
    if (!e.used_as_grounding) jira += ' (excluded)'
    if (e.url) jira += `\n    ${e.url}`
    jira += '\n'
  })
  if (entries.some((e) => !e.used_as_grounding)) {
    jira += '\nClosed-unmerged pull requests were deliberately not used as source. Code that was abandoned cannot be tested.\n'
  }
  jira += '\n════════════════════════════════════════════\n\n'
  return jira
}

// ─── no implementation found ────────────────────────────────────────────────
// Replaces the plan entirely when nothing grounds it. Names where we looked,
// because "no plan" on its own reads as a bot failure rather than as a
// missing PR.

const formatNoSourceMarkdown = (plan, ticketData) => {
  let md = `# Test Plan: ${ticketData?.key || plan.ticket_key || ''}\n\n`
  if (ticketData?.summary) md += `## ${ticketData.summary}\n\n`
  md += '## 🚫 No implementation found — no plan generated\n\n'
  md += `${plan.no_source_message || ''}\n\n`
  const searched = Array.isArray(plan.searched) ? plan.searched : []
  if (searched.length > 0) {
    md += '**Searched:**\n\n'
    searched.forEach((item) => { md += `- ${item}\n` })
    md += '\n'
  }
  md += formatProvenanceMarkdown(plan)
  return md
}

const formatNoSourceJira = (plan) => {
  let jira = '🚫 NO IMPLEMENTATION FOUND — NO PLAN GENERATED\n\n'
  jira += `${plan.no_source_message || ''}\n\n`
  const searched = Array.isArray(plan.searched) ? plan.searched : []
  if (searched.length > 0) {
    jira += 'Searched:\n'
    searched.forEach((item) => { jira += `  • ${item}\n` })
    jira += '\n'
  }
  jira += formatProvenanceJira(plan)
  return jira
}

// ─── needs-spec quarantine ──────────────────────────────────────────────────
// Cases the pipeline could not trace to any source. Kept visible so nothing is
// silently dropped, but held outside the numbered sections and marked
// explicitly non-gradeable: before this section existed they were numbered
// alongside verified cases and testers graded them like everything else.

const needsSpecCases = (plan) =>
  Array.isArray(plan?.needs_spec_cases) ? plan.needs_spec_cases : []

const NEEDS_SPEC_BLURB =
  'Not test cases. Neither the UI these describe nor their expected results could be traced to the linked code, so they cannot be marked pass or fail. Confirm the intended behaviour, then regenerate.'

const formatNeedsSpecMarkdown = (plan) => {
  const cases = needsSpecCases(plan)
  if (cases.length === 0) return ''
  let md = `## 🚧 Needs spec — not verifiable from source (${cases.length})\n\n`
  md += `_${NEEDS_SPEC_BLURB}_\n\n`
  cases.forEach((test, index) => {
    md += `${index + 1}. **${typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}**\n`
    if (test.needs_spec_reason) md += `   - ${test.needs_spec_reason}\n`
  })
  md += '\n'
  return md
}

const formatNeedsSpecJira = (plan) => {
  const cases = needsSpecCases(plan)
  if (cases.length === 0) return ''
  let jira = `🚧 NEEDS SPEC — NOT VERIFIABLE FROM SOURCE (${cases.length})\n\n`
  jira += `${NEEDS_SPEC_BLURB}\n\n`
  cases.forEach((test, index) => {
    jira += `${index + 1}. ${typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}\n`
    if (test.needs_spec_reason) jira += `   ${test.needs_spec_reason}\n`
  })
  jira += '\n'
  return jira
}

// Cases written against a PR that has not merged, flagged inline so a tester
// who hits a mismatch knows the code may simply have moved on.
const formatUnmergedGrounding = (test) => {
  if (!test.grounded_in_unmerged) return ''
  return '> ⚠️ **Grounded in unmerged code** — the PR behind this case has not landed. Confirm it is still current before treating a failure as a defect.\n\n'
}

const formatUnmergedGroundingJira = (test) => {
  if (!test.grounded_in_unmerged) return ''
  return '⚠️ Grounded in unmerged code — the PR behind this case has not landed; confirm it is still current before treating a failure as a defect.\n\n'
}

export const formatTestPlanAsMarkdown = (plan, ticketData, walkthrough = null) => {
  // Nothing grounded this ticket, so there is no plan to render — only the
  // report of what was searched for.
  if (plan?.no_source) return formatNoSourceMarkdown(plan, ticketData)

  let markdown = `# Test Plan: ${ticketData.key}\n\n`
  markdown += `## ${ticketData.summary}\n\n`

  const hasAcs = planHasAnyAcs(plan)

  markdown += formatUatGuideMarkdown(plan, walkthrough)
  markdown += formatProvenanceMarkdown(plan)
  markdown += formatAcCoverageSummary(plan.ac_coverage)
  markdown += formatSupersededAcs(plan)
  markdown += formatGroundingWarnings(plan)

  const happyPathCases = uncovered(plan.happy_path)
  if (happyPathCases.length > 0) {
    markdown += '## ✅ Happy Path Test Cases\n\n'
    happyPathCases.forEach((test, index) => {
      markdown += `### ${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        markdown += ` ${emoji} *${test.priority}*`
      }
      markdown += '\n\n'
      markdown += formatNeedsVerification(test)
      markdown += formatUnmergedGrounding(test)
      if (test.preconditions) {
        markdown += `**Preconditions:** ${test.preconditions}\n\n`
      }
      if (test.steps && test.steps.length > 0) {
        markdown += '**Steps:**\n'
        test.steps.forEach((step, stepIndex) => {
          markdown += `${stepIndex + 1}. ${step}\n`
        })
        markdown += '\n'
      }
      if (test.expected) {
        markdown += `**Expected Result:** ${test.expected}\n\n`
      }
      if (test.test_data) {
        markdown += `**Test Data:** ${test.test_data}\n\n`
      }
      markdown += formatSurface(test)
      markdown += formatExpectedVerification(test)
      markdown += formatCoversAcs(test)
      markdown += formatGroundedIn(test, hasAcs)
    })
  }

  const edgeCases = uncovered(plan.edge_cases)
  if (edgeCases.length > 0) {
    markdown += '## 🔍 Edge Cases & Error Scenarios\n\n'
    edgeCases.forEach((test, index) => {
      markdown += `### ${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        markdown += ` ${emoji} *${test.priority}*`
      }
      if (test.category) {
        markdown += ` [${test.category}]`
      }
      markdown += '\n\n'
      markdown += formatNeedsVerification(test)
      markdown += formatUnmergedGrounding(test)
      if (test.preconditions) {
        markdown += `**Preconditions:** ${test.preconditions}\n\n`
      }
      if (test.steps && test.steps.length > 0) {
        markdown += '**Steps:**\n'
        test.steps.forEach((step, stepIndex) => {
          markdown += `${stepIndex + 1}. ${step}\n`
        })
        markdown += '\n'
      }
      if (test.expected) {
        markdown += `**Expected Result:** ${test.expected}\n\n`
      }
      if (test.test_data) {
        markdown += `**Test Data:** ${test.test_data}\n\n`
      }
      markdown += formatSurface(test)
      markdown += formatExpectedVerification(test)
      markdown += formatCoversAcs(test)
      markdown += formatGroundedIn(test, hasAcs)
    })
  }

  const integrationTests = uncovered(plan.integration_tests)
  if (integrationTests.length > 0) {
    markdown += '## 🔗 Integration & Backend Tests\n\n'
    integrationTests.forEach((test, index) => {
      const titlePrefix = test.cross_project ? '[Cross-project] ' : ''
      markdown += `### ${index + 1}. ${titlePrefix}${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        markdown += ` ${emoji} *${test.priority}*`
      }
      markdown += '\n\n'
      markdown += formatNeedsVerification(test)
      markdown += formatUnmergedGrounding(test)
      if (test.cross_project && test.seam) {
        const producer = test.seam.producer_repo || '?'
        const consumer = test.seam.consumer_repo || '?'
        const ident = test.seam.identifier ? ` — \`${test.seam.identifier}\`` : ''
        const verified = test.seam.verified === false ? ' (suspected)' : ''
        markdown += `**Seam:** ${producer} → ${consumer}${ident}${verified}\n\n`
      }
      if (test.preconditions) {
        markdown += `**Preconditions:** ${test.preconditions}\n\n`
      }
      if (test.steps && test.steps.length > 0) {
        markdown += '**Steps:**\n'
        test.steps.forEach((step, stepIndex) => {
          markdown += `${stepIndex + 1}. ${step}\n`
        })
        markdown += '\n'
      }
      if (test.expected) {
        markdown += `**Expected Result:** ${test.expected}\n\n`
      }
      if (test.test_data) {
        markdown += `**Test Data:** ${test.test_data}\n\n`
      }
      markdown += formatSurface(test)
      markdown += formatExpectedVerification(test)
      markdown += formatCoversAcs(test)
      markdown += formatGroundedIn(test, hasAcs)
    })
  }

  if (plan.regression_checklist && plan.regression_checklist.length > 0) {
    markdown += '## 🔄 Regression Checklist\n\n'
    plan.regression_checklist.forEach(item => {
      markdown += `- ${item}\n`
    })
    markdown += '\n'
  }

  markdown += formatRisksAndGaps(plan)

  markdown += formatNeedsSpecMarkdown(plan)

  const coveredCases = collectCoveredCases(plan)
  if (coveredCases.length > 0) {
    markdown += `## 🧪 Already Covered by Unit Tests (${coveredCases.length})\n\n`
    markdown += '_Exercised by existing automated tests — listed for completeness; QA can skip the manual run._\n\n'
    coveredCases.forEach((test, index) => {
      markdown += `${index + 1}. **${typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}**`
      if (test.unit_test_ref) markdown += ` — \`${test.unit_test_ref}\``
      markdown += '\n'
    })
    markdown += '\n'
  }

  return markdown
}

export const formatBugAnalysisAsMarkdown = (analysis) => {
  const isMulti = Array.isArray(analysis.ticket_keys)
  const ticketLabel = isMulti ? analysis.ticket_keys.join(', ') : analysis.ticket_key

  let md = `# Bug Lens Analysis: ${ticketLabel}\n\n`
  const fixStatus = analysis.fix_status || (analysis.is_fixed ? 'fixed' : 'not_fixed')
  const statusLabel = {
    fixed: '✅ Fixed',
    in_testing: '🧪 In testing — fix awaiting QA',
    not_fixed: '⚠️ Not yet fixed',
  }[fixStatus] || '⚠️ Not yet fixed'
  md += `**Status:** ${statusLabel}\n\n`

  if (analysis.is_regression != null) {
    md += `**Regression:** ${analysis.is_regression ? '🔁 Yes — this was previously working' : '🆕 No — feature was never functional'}\n`
    if (analysis.is_regression && analysis.regression_introduced_by) {
      md += `**Introduced by:** ${analysis.regression_introduced_by}\n`
    }
    md += '\n'
  }

  md += `## Bug Summary\n\n${analysis.bug_summary}\n\n`

  md += `## Root Cause\n\n`
  md += analysis.root_cause
    ? `${analysis.root_cause}\n\n`
    : `*No code diff available — root cause derived from ticket description only.*\n\n`

  if (analysis.affected_flow && analysis.affected_flow.length > 0) {
    md += `## Affected Flow\n\n`
    analysis.affected_flow.forEach((step, i) => {
      const clean = step.replace(/^\s*\d+[.)]\s+/, '')
      md += `${i + 1}. ${clean}\n`
    })
    md += '\n'
  }

  if (analysis.scope_of_impact && analysis.scope_of_impact.length > 0) {
    md += `## Scope of Impact\n\n`
    analysis.scope_of_impact.forEach(item => { md += `- ${item}\n` })
    md += '\n'
  }

  if (analysis.code_evidence && analysis.code_evidence.length > 0) {
    const withHits = analysis.code_evidence.filter(e => e.usages && e.usages.length > 0)
    md += `## Code Evidence\n\n`
    if (withHits.length === 0) {
      md += `_Searched for the suspected symbols but none were found in the candidate repos — the bug may live in a different repo, or the suspects were off. Verify the repo mapping and the symbol names before acting._\n\n`
    } else {
      md += `Places the suspected symbols actually appear in the repo — verify before acting.\n\n`
      withHits.forEach(entry => {
        md += `### \`${entry.suspect}\` in \`${entry.repo}\`\n\n`
        entry.usages.forEach(u => {
          const url = encodeURI(`https://github.com/${entry.repo}/blob/${u.ref}/${u.path}`) + `#L${u.line}`
          md += `- [\`${u.path}:${u.line}\`](${url})`
          if (u.snippet) {
            const safeSnippet = u.snippet.replace(/`/g, "'")
            md += ` — \`${safeSnippet}\``
          }
          md += '\n'
        })
        md += '\n'
      })
    }
  }

  if (analysis.blame_evidence && analysis.blame_evidence.length > 0) {
    md += `## Blame Trail\n\n`
    md += `\`git blame\` on the suspected defect site — the commit that introduced the current line and the PR it landed in. Verify before acting.\n\n`
    analysis.blame_evidence.forEach(b => {
      const fileUrl = b.repo
        ? encodeURI(`https://github.com/${b.repo}/blob/${b.commit_sha || 'HEAD'}/${b.path}`) + `#L${b.line}`
        : null
      const anchor = b.symbol ? `\`${b.symbol}\` — ` : ''
      const loc = fileUrl ? `[\`${b.path}:${b.line}\`](${fileUrl})` : `\`${b.path}:${b.line}\``
      md += `- ${anchor}${loc} in \`${b.repo || 'unknown-repo'}\`\n`
      if (b.commit_sha) {
        const short = b.commit_short || b.commit_sha.slice(0, 8)
        const commitUrl = `https://github.com/${b.repo}/commit/${b.commit_sha}`
        md += `  - Introduced by [\`${short}\`](${commitUrl})`
        if (b.commit_message) md += ` — ${b.commit_message}`
        md += '\n'
        if (b.pr_url) {
          md += `  - [PR #${b.pr_number}${b.pr_title ? ` — ${b.pr_title}` : ''}](${b.pr_url})\n`
        }
        const meta = [b.author_name, b.commit_date ? String(b.commit_date).slice(0, 10) : null].filter(Boolean).join(' · ')
        if (meta) md += `  - ${meta}\n`
      } else if (b.notes) {
        md += `  - _${b.notes}_\n`
      }
    })
    md += '\n'
  }

  if (analysis.why_tests_miss) {
    md += `## Why Tests Don't Catch This\n\n${analysis.why_tests_miss}\n\n`
  }

  if (fixStatus === 'fixed' || fixStatus === 'in_testing') {
    md += `## Fix Explanation\n\n`
    md += analysis.fix_explanation ? `${analysis.fix_explanation}\n\n` : `*No fix details available.*\n\n`
    if (fixStatus === 'in_testing') {
      md += `_The code change is in but QA hasn't validated it yet — confirm the fix behaves correctly before closing the ticket._\n\n`
    }
  }

  if (analysis.open_questions && analysis.open_questions.length > 0) {
    md += `## Open Questions\n\n`
    analysis.open_questions.forEach(q => { md += `- ${q}\n` })
    md += '\n'
  }

  if (analysis.assumptions && analysis.assumptions.length > 0) {
    md += `## Assumptions\n\n`
    analysis.assumptions.forEach(a => { md += `- ${a}\n` })
    md += '\n'
  }

  if (fixStatus === 'not_fixed' && analysis.fix_complexity) {
    md += `## Fix Complexity\n\n`
    md += `**Complexity:** ${analysis.fix_complexity.charAt(0).toUpperCase() + analysis.fix_complexity.slice(1)}`
    if (analysis.fix_effort_estimate) {
      md += ` — **Estimated effort:** ${analysis.fix_effort_estimate}`
    }
    md += '\n\n'
    if (analysis.fix_complexity_reasoning) {
      md += `${analysis.fix_complexity_reasoning}\n\n`
    }
  }

  if (analysis.regression_tests && analysis.regression_tests.length > 0) {
    md += `## Regression Tests\n\n`
    analysis.regression_tests.forEach(test => { md += `- ${test}\n` })
    md += '\n'
  }

  if (analysis.similar_patterns && analysis.similar_patterns.length > 0) {
    md += `## Similar Bug Patterns to Watch\n\n`
    analysis.similar_patterns.forEach(pattern => { md += `- ${pattern}\n` })
    md += '\n'
  }

  return md
}

const formatSurfaceJira = (test) => {
  const bits = []
  if (typeof test.surface === 'string' && test.surface.trim()) {
    bits.push(SURFACE_LABELS[test.surface] || test.surface)
  }
  if (typeof test.credentials === 'string' && test.credentials.trim()) {
    bits.push(`credentials: ${test.credentials.trim()}`)
  }
  if (typeof test.environment === 'string' && test.environment.trim()) {
    bits.push(`environment: ${test.environment.trim()}`)
  }
  return bits.length > 0 ? `Runs on: ${bits.join(' · ')}\n\n` : ''
}

const formatExpectedVerificationJira = (test) => {
  if (test.expected_verified === true) {
    const src = typeof test.expected_source === 'string' && test.expected_source.trim()
      ? test.expected_source.trim()
      : 'source not cited'
    return `Expected verified against: ${src}\n\n`
  }
  if (test.expected_verified === false) {
    return '⚠️ Unverified — assumption. This expected result was not read from the implementation; confirm against the code before treating a failure as a defect.\n\n'
  }
  return ''
}

export const formatTestPlanAsJira = (plan, walkthrough = null, options = {}) => {
  const { includeCovered = false } = options
  if (plan?.no_source) return formatNoSourceJira(plan)

  let jira = ''

  jira += formatUatGuideJira(plan, walkthrough)
  jira += formatProvenanceJira(plan)

  const happyPathCases = uncovered(plan.happy_path)
  if (happyPathCases.length > 0) {
    jira += '✅ HAPPY PATH TEST CASES\n\n'
    happyPathCases.forEach((test, index) => {
      let title = `**${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        title += ` ${emoji} ${test.priority.toUpperCase()}`
      }
      title += '**'
      jira += `${title}\n\n`
      if (test.needs_manual_verification) {
        jira += '⚠️ Needs manual verification — AC element not found in PR diff/testID reference. See UI Grounding Warnings.\n\n'
      }
      jira += formatUnmergedGroundingJira(test)
      if (test.preconditions) {
        jira += `Preconditions: ${test.preconditions}\n\n`
      }
      if (test.steps && test.steps.length > 0) {
        jira += 'Steps:\n'
        test.steps.forEach((step, stepIndex) => {
          jira += `${stepIndex + 1}. ${step}\n`
        })
        jira += '\n'
      }
      if (test.expected) {
        jira += `Expected Result: ${test.expected}\n\n`
      }
      if (test.test_data) {
        jira += `Test Data: ${test.test_data}\n\n`
      }
      jira += formatSurfaceJira(test)
      jira += formatExpectedVerificationJira(test)
      jira += '────────────────────────────────────────────\n\n'
    })
  }

  const edgeCases = uncovered(plan.edge_cases)
  if (edgeCases.length > 0) {
    jira += '🔍 EDGE CASES & ERROR SCENARIOS\n\n'
    edgeCases.forEach((test, index) => {
      let title = `**${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        title += ` ${emoji} ${test.priority.toUpperCase()}`
      }
      if (test.category) {
        title += ` [${test.category}]`
      }
      title += '**'
      jira += `${title}\n\n`
      if (test.needs_manual_verification) {
        jira += '⚠️ Needs manual verification — AC element not found in PR diff/testID reference. See UI Grounding Warnings.\n\n'
      }
      jira += formatUnmergedGroundingJira(test)
      if (test.preconditions) {
        jira += `Preconditions: ${test.preconditions}\n\n`
      }
      if (test.steps && test.steps.length > 0) {
        jira += 'Steps:\n'
        test.steps.forEach((step, stepIndex) => {
          jira += `${stepIndex + 1}. ${step}\n`
        })
        jira += '\n'
      }
      if (test.expected) {
        jira += `Expected Result: ${test.expected}\n\n`
      }
      if (test.test_data) {
        jira += `Test Data: ${test.test_data}\n\n`
      }
      jira += formatSurfaceJira(test)
      jira += formatExpectedVerificationJira(test)
      jira += '────────────────────────────────────────────\n\n'
    })
  }

  const integrationTests = uncovered(plan.integration_tests)
  if (integrationTests.length > 0) {
    jira += '🔗 INTEGRATION & BACKEND TESTS\n\n'
    integrationTests.forEach((test, index) => {
      let title = `**${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        title += ` ${emoji} ${test.priority.toUpperCase()}`
      }
      title += '**'
      jira += `${title}\n\n`
      if (test.needs_manual_verification) {
        jira += '⚠️ Needs manual verification — AC element not found in PR diff/testID reference. See UI Grounding Warnings.\n\n'
      }
      jira += formatUnmergedGroundingJira(test)
      if (test.preconditions) {
        jira += `Preconditions: ${test.preconditions}\n\n`
      }
      if (test.steps && test.steps.length > 0) {
        jira += 'Steps:\n'
        test.steps.forEach((step, stepIndex) => {
          jira += `${stepIndex + 1}. ${step}\n`
        })
        jira += '\n'
      }
      if (test.expected) {
        jira += `Expected Result: ${test.expected}\n\n`
      }
      if (test.test_data) {
        jira += `Test Data: ${test.test_data}\n\n`
      }
      jira += formatSurfaceJira(test)
      jira += formatExpectedVerificationJira(test)
      jira += '────────────────────────────────────────────\n\n'
    })
  }

  if (plan.regression_checklist && plan.regression_checklist.length > 0) {
    jira += '🔄 REGRESSION CHECKLIST\n\n'
    plan.regression_checklist.forEach(item => {
      jira += `  • ${item}\n`
    })
    jira += '\n'
  }

  const jiraGaps = Array.isArray(plan.risks_and_gaps) ? plan.risks_and_gaps.filter(Boolean) : []
  if (jiraGaps.length > 0) {
    jira += '⚠️ RISKS / GAPS OBSERVED\n\n'
    jira += 'Not test cases — gaps in the ticket itself that no case can mark pass or fail.\n\n'
    jiraGaps.forEach((g) => {
      if (typeof g === 'string') {
        jira += `  • ${g}\n`
        return
      }
      jira += `  • ${g.gap || 'Unnamed gap'}\n`
      if (g.impact) jira += `    Impact: ${g.impact}\n`
      if (g.evidence) jira += `    Evidence: ${g.evidence}\n`
    })
    jira += '\n'
  }

  jira += formatNeedsSpecJira(plan)

  if (includeCovered) {
    const coveredCases = collectCoveredCases(plan)
    if (coveredCases.length > 0) {
      jira += `🧪 ALREADY COVERED BY UNIT TESTS (${coveredCases.length})\n\n`
      jira += 'Exercised by existing automated tests — QA can skip the manual run.\n\n'
      coveredCases.forEach((test, index) => {
        jira += `${index + 1}. ${typeof test.title === 'string' ? test.title : JSON.stringify(test.title)}\n`
        if (test.unit_test_ref) jira += `   Covered by: ${test.unit_test_ref}\n`
      })
      jira += '\n'
    }
  }

  return jira
}
