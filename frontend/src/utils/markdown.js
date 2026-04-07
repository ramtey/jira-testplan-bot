/**
 * Markdown formatting utilities
 */

export const formatTestPlanAsMarkdown = (plan, ticketData) => {
  let markdown = `# Test Plan: ${ticketData.key}\n\n`
  markdown += `## ${ticketData.summary}\n\n`

  if (plan.happy_path && plan.happy_path.length > 0) {
    markdown += '## ✅ Happy Path Test Cases\n\n'
    plan.happy_path.forEach((test, index) => {
      markdown += `### ${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        markdown += ` ${emoji} *${test.priority}*`
      }
      markdown += '\n\n'
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
    })
  }

  if (plan.edge_cases && plan.edge_cases.length > 0) {
    markdown += '## 🔍 Edge Cases & Error Scenarios\n\n'
    plan.edge_cases.forEach((test, index) => {
      markdown += `### ${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        markdown += ` ${emoji} *${test.priority}*`
      }
      if (test.category) {
        markdown += ` [${test.category}]`
      }
      markdown += '\n\n'
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
    })
  }

  if (plan.integration_tests && plan.integration_tests.length > 0) {
    markdown += '## 🔗 Integration & Backend Tests\n\n'
    plan.integration_tests.forEach((test, index) => {
      markdown += `### ${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        markdown += ` ${emoji} *${test.priority}*`
      }
      markdown += '\n\n'
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
    })
  }

  if (plan.regression_checklist && plan.regression_checklist.length > 0) {
    markdown += '## 🔄 Regression Checklist\n\n'
    plan.regression_checklist.forEach(item => {
      markdown += `- ${item}\n`
    })
    markdown += '\n'
  }

  return markdown
}

export const formatBugAnalysisAsMarkdown = (analysis) => {
  const isMulti = Array.isArray(analysis.ticket_keys)
  const ticketLabel = isMulti ? analysis.ticket_keys.join(', ') : analysis.ticket_key

  let md = `# Bug Lens Analysis: ${ticketLabel}\n\n`
  md += `**Status:** ${analysis.is_fixed ? '✅ Fixed' : '⚠️ Not yet fixed'}\n\n`

  md += `## Bug Summary\n\n${analysis.bug_summary}\n\n`

  md += `## Root Cause\n\n`
  md += analysis.root_cause
    ? `${analysis.root_cause}\n\n`
    : `*No code diff available — root cause derived from ticket description only.*\n\n`

  if (analysis.is_fixed) {
    md += `## Fix Explanation\n\n`
    md += analysis.fix_explanation ? `${analysis.fix_explanation}\n\n` : `*No fix details available.*\n\n`
  }

  if (!analysis.is_fixed && analysis.fix_complexity) {
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

export const formatTestPlanAsJira = (plan) => {
  let jira = ''

  if (plan.happy_path && plan.happy_path.length > 0) {
    jira += '✅ HAPPY PATH TEST CASES\n\n'
    plan.happy_path.forEach((test, index) => {
      jira += `${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        jira += ` ${emoji} ${test.priority.toUpperCase()}`
      }
      jira += '\n\n'
      if (test.steps && test.steps.length > 0) {
        jira += 'Steps:\n'
        test.steps.forEach((step, stepIndex) => {
          jira += `   ${stepIndex + 1}. ${step}\n`
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

  if (plan.edge_cases && plan.edge_cases.length > 0) {
    jira += '🔍 EDGE CASES & ERROR SCENARIOS\n\n'
    plan.edge_cases.forEach((test, index) => {
      jira += `${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        jira += ` ${emoji} ${test.priority.toUpperCase()}`
      }
      if (test.category) {
        jira += ` [${test.category}]`
      }
      jira += '\n\n'
      if (test.steps && test.steps.length > 0) {
        jira += 'Steps:\n'
        test.steps.forEach((step, stepIndex) => {
          jira += `   ${stepIndex + 1}. ${step}\n`
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

  if (plan.integration_tests && plan.integration_tests.length > 0) {
    jira += '🔗 INTEGRATION & BACKEND TESTS\n\n'
    plan.integration_tests.forEach((test, index) => {
      jira += `${index + 1}. ${test.title}`
      if (test.priority) {
        const emoji = test.priority === 'critical' ? '🔴' : test.priority === 'high' ? '🟡' : '🟢'
        jira += ` ${emoji} ${test.priority.toUpperCase()}`
      }
      jira += '\n\n'
      if (test.steps && test.steps.length > 0) {
        jira += 'Steps:\n'
        test.steps.forEach((step, stepIndex) => {
          jira += `   ${stepIndex + 1}. ${step}\n`
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

  return jira
}
