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
  const screenshot = walkthrough?.screenshot_url?.trim() || ''
  const notes = walkthrough?.notes?.trim() || ''
  if (!summary && !reason && !loom && !screenshot && !notes) return null
  return { complexity, summary, reason, loom, screenshot, notes }
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
  if (g.screenshot) md += `📷 **Screenshot:** ${g.screenshot}\n\n`
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
  if (g.screenshot) jira += `📷 Screenshot: ${g.screenshot}\n\n`
  if (g.notes) jira += `Setup / repro notes:\n${g.notes}\n\n`
  jira += '════════════════════════════════════════════\n\n'
  return jira
}

export const formatTestPlanAsMarkdown = (plan, ticketData, walkthrough = null) => {
  let markdown = `# Test Plan: ${ticketData.key}\n\n`
  markdown += `## ${ticketData.summary}\n\n`

  const hasAcs = planHasAnyAcs(plan)

  markdown += formatUatGuideMarkdown(plan, walkthrough)
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

export const formatTestPlanAsJira = (plan, walkthrough = null, options = {}) => {
  const { includeCovered = false } = options
  let jira = ''

  jira += formatUatGuideJira(plan, walkthrough)

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
