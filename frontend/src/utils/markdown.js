/**
 * Markdown formatting utilities
 */

export const formatTestPlanAsMarkdown = (plan, ticketData) => {
  let markdown = `# Test Plan: ${ticketData.key}\n\n`
  markdown += `## ${ticketData.summary}\n\n`

  if (plan.happy_path && plan.happy_path.length > 0) {
    markdown += '## ✅ Happy Path Test Cases\n\n'
    plan.happy_path.forEach((test, index) => {
      markdown += `### ${index + 1}. ${test.title}\n\n`
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
    })
  }

  if (plan.edge_cases && plan.edge_cases.length > 0) {
    markdown += '## 🔍 Edge Cases\n\n'
    plan.edge_cases.forEach((test, index) => {
      markdown += `### ${index + 1}. ${test.title}\n\n`
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
    })
  }

  if (plan.regression_checklist && plan.regression_checklist.length > 0) {
    markdown += '## 🔄 Regression Checklist\n\n'
    plan.regression_checklist.forEach(item => {
      markdown += `- ${item}\n`
    })
    markdown += '\n'
  }

  if (plan.non_functional && plan.non_functional.length > 0) {
    markdown += '## ⚡ Non-Functional Tests\n\n'
    plan.non_functional.forEach(item => {
      markdown += `- ${item}\n`
    })
    markdown += '\n'
  }

  if (plan.assumptions && plan.assumptions.length > 0) {
    markdown += '## 💡 Assumptions\n\n'
    plan.assumptions.forEach(item => {
      markdown += `- ${item}\n`
    })
    markdown += '\n'
  }

  if (plan.questions && plan.questions.length > 0) {
    markdown += '## ❓ Questions for PM/Dev\n\n'
    plan.questions.forEach(item => {
      markdown += `- ${item}\n`
    })
    markdown += '\n'
  }

  return markdown
}

export const formatTestPlanAsJira = (plan, ticketData) => {
  let jira = `h1. Test Plan: ${ticketData.key}\n\n`
  jira += `h2. ${ticketData.summary}\n\n`

  if (plan.happy_path && plan.happy_path.length > 0) {
    jira += 'h2. ✅ Happy Path Test Cases\n\n'
    plan.happy_path.forEach((test, index) => {
      jira += `h3. ${index + 1}. ${test.title}\n\n`
      if (test.steps && test.steps.length > 0) {
        jira += '*Steps:*\n'
        test.steps.forEach((step) => {
          jira += `# ${step}\n`
        })
        jira += '\n'
      }
      if (test.expected) {
        jira += `*Expected Result:* ${test.expected}\n\n`
      }
    })
  }

  if (plan.edge_cases && plan.edge_cases.length > 0) {
    jira += 'h2. 🔍 Edge Cases\n\n'
    plan.edge_cases.forEach((test, index) => {
      jira += `h3. ${index + 1}. ${test.title}\n\n`
      if (test.steps && test.steps.length > 0) {
        jira += '*Steps:*\n'
        test.steps.forEach((step) => {
          jira += `# ${step}\n`
        })
        jira += '\n'
      }
      if (test.expected) {
        jira += `*Expected Result:* ${test.expected}\n\n`
      }
    })
  }

  if (plan.regression_checklist && plan.regression_checklist.length > 0) {
    jira += 'h2. 🔄 Regression Checklist\n\n'
    plan.regression_checklist.forEach(item => {
      jira += `* ${item}\n`
    })
    jira += '\n'
  }

  if (plan.non_functional && plan.non_functional.length > 0) {
    jira += 'h2. ⚡ Non-Functional Tests\n\n'
    plan.non_functional.forEach(item => {
      jira += `* ${item}\n`
    })
    jira += '\n'
  }

  if (plan.assumptions && plan.assumptions.length > 0) {
    jira += 'h2. 💡 Assumptions\n\n'
    plan.assumptions.forEach(item => {
      jira += `* ${item}\n`
    })
    jira += '\n'
  }

  if (plan.questions && plan.questions.length > 0) {
    jira += 'h2. ❓ Questions for PM/Dev\n\n'
    plan.questions.forEach(item => {
      jira += `* ${item}\n`
    })
    jira += '\n'
  }

  return jira
}
