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

export const formatTestPlanAsJira = (plan, ticketData) => {
  let jira = `━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n`
  jira += `TEST PLAN: ${ticketData.key}\n`
  jira += `${ticketData.summary}\n`
  jira += `━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n`

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
