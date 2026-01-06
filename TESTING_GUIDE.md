# Testing Complex Features Guide

## Overview
This guide shows how to get comprehensive test plans for complex features with multiple categories or scenarios.

## The Problem
For complex tickets (like keyword blocking with 50+ categories), the LLM might generate only generic test cases instead of covering all categories with specific examples.

## The Solution: Smart Automatic Detection

### Primary Approach: Let the System Do the Work
The enhanced system prompt now automatically:
- **Analyzes the ticket description** to detect categories, groups, and patterns
- **Identifies behavior differences** (e.g., "block for X, allow continue for Y")
- **Extracts specific examples** from the ticket content
- **Generates comprehensive coverage** (3-5 happy path, 6-10 edge cases for multi-category features)
- **Creates test cases** for each category using concrete examples

**You should NOT need to write complex special instructions for most cases.**

### Fallback: Special Instructions (Only When Needed)
Use the **"Special Testing Instructions"** field ONLY when:
- The automatic detection misses critical categories
- You need to emphasize specific priorities
- The ticket has unusual structure the system can't parse

## Example: Keyword Blocking Feature

### Ticket Description Summary:
- Multiple keyword categories (racism, fraud, FNF mentions, competitors, etc.)
- Two different behaviors (block + new chat vs. respond + continue)
- 50+ specific keywords/phrases across 10+ categories

### ✅ Best Approach: Let It Work Automatically

**Simply provide a good ticket description** with clear categories and examples. The system will automatically:
- Detect the multiple categories (racism, source of income, familial status, FNF, competitors)
- Identify behavior differences (block vs allow continue)
- Extract specific examples ("no section 8", "no blacks", "Fidelity National Title")
- Generate 6-10 edge cases covering each category

**No special instructions needed!** Just click "Generate Test Plan."

### ⚠️ Only Use Special Instructions If Needed

If the automatic generation misses something important, add a **simple** note:

**Good (Simple):**
```
Focus on testing all keyword categories mentioned in the description.
Test both hard block (racism) and soft block (FNF mentions) behaviors.
```

**Avoid (Too Complex):**
```
Create test cases covering ALL these categories with specific examples:
HARD BLOCK (should refuse message + block chat):
- Race: "no blacks", "whites only"
[...20 more lines of detailed instructions...]
```

**Why?** The system already does this automatically. Keep it simple!

### Expected Output:
With automatic detection (no special instructions needed), you should get:
- **Happy Path**: 3-5 test cases covering normal conversation flows
- **Edge Cases**: 6-10 test cases with specific examples from EACH category detected in the ticket
- **Regression**: Specific checks related to the feature (e.g., "Normal chat messages without keywords still work")
- **Non-Functional**: Feature-specific tests (e.g., "Response time under 500ms for keyword matching")

## When to Use Special Instructions

✅ **Use when:**
- The automatic generation missed critical categories
- You need to emphasize specific test priorities
- The ticket structure is unusual or ambiguous
- You want to add context not in the ticket description

❌ **Don't use when:**
- The ticket has clear categories and examples (system will detect automatically)
- You're just restating what's already in the ticket
- You're trying to format the output (system handles this)
- Simple CRUD operations or single user flows

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

### 1. **Be Specific with Examples**
❌ Bad: "Test keyword blocking"
✅ Good: 'Test "no section 8", "no kids", "whites only"'

### 2. **Group by Behavior/Category**
Structure your instructions to match the feature's logic:
```
BEHAVIOR TYPE 1:
- Category A: examples
- Category B: examples

BEHAVIOR TYPE 2:
- Category C: examples
```

### 3. **Set Minimum Coverage Requirements**
Don't just list categories - specify how many tests you expect:
```
Generate at least:
- 3 happy path tests
- 6 edge cases (1+ per category)
- 3 regression tests
```

### 4. **Request Complete Test Cases**
Explicitly ask for:
- Steps AND expected results for every test case
- Specific error messages or behaviors
- Observable outcomes

### 5. **Include Edge Case Guidance**
Mention specific scenarios you're concerned about:
- Case sensitivity
- Mixed/combined keywords
- Boundary conditions
- Error states

### 6. **Clarify Behavior Expectations**
If different actions trigger different behaviors, spell it out:
```
Hard block: refuse message + block chat
Soft block: respond with warning + allow continue
```

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

## Common Pitfalls to Avoid

### 1. **Too Vague**
❌ "Test all keyword categories"
✅ List specific categories with examples

### 2. **Missing Coverage Requirements**
❌ Just listing categories
✅ "Generate at least 1 test per category"

### 3. **No Behavior Expectations**
❌ Listing keywords without context
✅ Explain what should happen for each type

### 4. **Incomplete Test Cases**
❌ Accepting edge cases without expected results
✅ Demand "steps AND expected result for every case"

### 5. **Generic Outputs**
❌ "Verify keyword matching works"
✅ 'Enter "no section 8" → expect "I can\'t answer that" message'

## Summary

**Best Practices**:
1. ✅ **Start simple**: Just click "Generate Test Plan" - let the system work automatically
2. ✅ **Good ticket descriptions win**: Clear categories and examples in the ticket = better test plans
3. ✅ **Special Instructions = last resort**: Only use when automatic detection fails
4. ✅ **Keep instructions simple**: A few sentences is enough, not a detailed template
5. ✅ **Review and regenerate**: If output is weak, try rephrasing the ticket description or adding brief context
6. ✅ **Trust the system**: The enhanced prompt detects categories, extracts examples, and generates comprehensive coverage automatically
