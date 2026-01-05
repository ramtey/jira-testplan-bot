# Testing Complex Features Guide

## Overview
This guide shows how to get comprehensive test plans for complex features with multiple categories or scenarios.

## The Problem
For complex tickets (like keyword blocking with 50+ categories), the LLM might generate only generic test cases instead of covering all categories with specific examples.

## The Solution: Hybrid Approach

### 1. Enhanced System Prompt (Automatic)
The system now automatically:
- Detects when tickets have multiple categories/scenarios
- Prompts the LLM to create specific test cases for EACH category
- Requires concrete examples instead of generic placeholders
- Increases test case counts (2-5 happy path, 3-6 edge cases)

### 2. Special Instructions Field (User-Guided)
For very complex scenarios, use the **"Special Testing Instructions"** field to guide the LLM.

## Example: Keyword Blocking Feature

### Ticket Description Summary:
- Multiple keyword categories (racism, fraud, FNF mentions, competitors, etc.)
- Two different behaviors (block + new chat vs. respond + continue)
- 50+ specific keywords/phrases across 10+ categories

### How to Use Special Instructions:

```
Generate test cases covering specific examples from these key categories:
1. Hard block keywords (racism, fraud) - should block chat
2. Soft block keywords (FNF mentions, competitors) - should respond but allow continue
3. Source of income discrimination (section 8, vouchers, housing assistance)
4. Familial status (no kids, adults only, families not allowed)
5. FNF company names (Fidelity National Title, Chicago Title, etc.)
6. Competitor names (First American, Stewart Title, Dotloop, etc.)

For each category, use at least 2-3 actual examples from the ticket description.
Test both exact matches and case-insensitive variants.
Include tests for phrases that combine multiple keywords.
```

### Expected Output:
With this guidance, you should get:
- **Happy Path**: 4-5 test cases covering different "allowed" conversation flows
- **Edge Cases**: 5-8 test cases with specific keyword examples from each major category
- **Regression**: Checks that normal conversations still work
- **Non-Functional**: Performance, false positives, case sensitivity

## When to Use Special Instructions

✅ **Use when:**
- Ticket has 5+ categories/scenarios
- You need specific examples from documentation
- Feature has complex rule combinations
- Previous generation was too generic

❌ **Don't need to use when:**
- Simple CRUD operations
- Single clear user flow
- Well-defined acceptance criteria

## Other Testing Context Fields

| Field | Use Case | Example |
|-------|----------|---------|
| **Acceptance Criteria** | Missing/weak ACs in ticket | "Given user enters 'no section 8', when submitted, then chat is blocked" |
| **Test Data Notes** | Specific test accounts/data needed | "Need test account with admin role, test keywords for each category" |
| **Environments** | Feature flags, config differences | "Feature flag 'keyword_blocking_v2' must be enabled in staging" |
| **Roles/Permissions** | Different user types | "Test with: guest users, authenticated users, admin users" |
| **Out of Scope** | What NOT to test | "Don't test keyword management UI (separate ticket)" |
| **Risk Areas** | What could break | "Existing chat functionality, message history, user sessions" |

## Tips for Better Test Plans

1. **Be Specific**: Use actual examples from your ticket
2. **List Categories**: If there are multiple categories, list them explicitly
3. **Provide Context**: Explain WHY certain tests matter
4. **Include Edge Cases**: Mention specific edge cases you're worried about
5. **Reference Documentation**: If there's a long spec, summarize key testing points

## Example Special Instructions for Different Scenarios

### API with Multiple Endpoints
```
Generate tests for each endpoint:
- POST /users (create with validation)
- GET /users/:id (retrieve with auth)
- PUT /users/:id (update with partial data)
- DELETE /users/:id (soft delete)

Include authentication failures and rate limiting for each.
```

### Multi-Step Wizard
```
Test each step independently:
1. Step 1: User info (validation for email, phone)
2. Step 2: Address (autocomplete, manual entry)
3. Step 3: Payment (CC, ACH, saved methods)
4. Step 4: Review & confirm

Test navigation: forward, back, skip, abandon/resume.
```

### Permission Matrix
```
Test each role's access to each resource:
- Admin: full CRUD on all resources
- Manager: read all, write own team only
- User: read/write own resources only
- Guest: read public resources only

Test permission boundaries and escalation attempts.
```

## Summary

**Recommendation**:
1. ✅ The enhanced system prompt will handle most cases automatically
2. ✅ Use "Special Instructions" for complex scenarios with 5+ categories
3. ✅ Always review and refine the generated test plan
4. ✅ Iterate: regenerate with more specific instructions if needed
