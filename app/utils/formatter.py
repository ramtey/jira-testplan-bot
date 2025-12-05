from app.models import TestPlan


def format_test_plan_to_markdown(test_plan: TestPlan, issue_key: str) -> str:
    """
    Format a TestPlan object into Markdown format.
    
    Args:
        test_plan: The TestPlan object to format
        issue_key: The Jira issue key for reference
        
    Returns:
        Formatted Markdown string
    """
    markdown = f"# Test Plan for {issue_key}\n\n"
    markdown += "_Auto-generated test plan_\n\n"
    markdown += "---\n\n"
    
    # Happy Path
    markdown += "## 🎯 Happy Path Test Cases\n\n"
    for i, test_case in enumerate(test_plan.happy_path, 1):
        markdown += f"### Test Case {i}: {test_case.scenario}\n\n"
        markdown += "**Steps:**\n"
        for j, step in enumerate(test_case.steps, 1):
            markdown += f"{j}. {step}\n"
        markdown += f"\n**Expected Result:** {test_case.expected_result}\n\n"
    
    # Edge Cases
    markdown += "## ⚠️ Edge Cases\n\n"
    for i, test_case in enumerate(test_plan.edge_cases, 1):
        markdown += f"### Edge Case {i}: {test_case.scenario}\n\n"
        markdown += "**Steps:**\n"
        for j, step in enumerate(test_case.steps, 1):
            markdown += f"{j}. {step}\n"
        markdown += f"\n**Expected Result:** {test_case.expected_result}\n\n"
    
    # Regression Checklist
    markdown += "## ✅ Regression Checklist\n\n"
    markdown += "Please verify the following existing functionality:\n\n"
    for item in test_plan.regression_checklist:
        markdown += f"- [ ] {item}\n"
    markdown += "\n"
    
    # Questions
    if test_plan.questions:
        markdown += "## ❓ Questions for Clarification\n\n"
        for i, question in enumerate(test_plan.questions, 1):
            markdown += f"{i}. {question}\n"
        markdown += "\n"
    
    markdown += "---\n"
    markdown += "_Generated automatically by Jira Test Plan Bot_"
    
    return markdown
