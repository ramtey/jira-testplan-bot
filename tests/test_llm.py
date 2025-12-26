"""
Test LLM client integration.

Run this to verify your LLM setup is working correctly.
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.app.llm_client import get_llm_client, LLMError


async def test_llm_generation():
    """Test generating a test plan with mock data."""
    print("=" * 80)
    print("TESTING LLM TEST PLAN GENERATION")
    print("=" * 80)
    print()

    # Mock ticket data
    ticket_key = "TEST-123"
    summary = "Add password reset functionality"
    description = """Users should be able to reset their password via email.

Acceptance Criteria:
- Given a user clicks 'Forgot Password', when they enter their email, then they receive a reset link
- Given a user clicks the reset link, when they enter a new password, then their password is updated
- Given the reset link is older than 24 hours, when they click it, then they see an expired message
"""
    testing_context = {
        "testDataNotes": "Test with valid and invalid email addresses",
        "rolesPermissions": "Any authenticated user",
        "riskAreas": "Email delivery, security token generation",
    }

    try:
        llm = get_llm_client()
        print(f"✓ Using LLM client: {llm.__class__.__name__}")
        print(f"✓ Generating test plan for ticket: {ticket_key}")
        print(f"✓ Summary: {summary}")
        print()
        print("⏳ Calling LLM... (this may take 30-120 seconds)")
        print()

        test_plan = await llm.generate_test_plan(
            ticket_key=ticket_key,
            summary=summary,
            description=description,
            testing_context=testing_context,
        )

        print("=" * 80)
        print("✅ TEST PLAN GENERATED SUCCESSFULLY!")
        print("=" * 80)
        print()

        print(f"📝 Happy Path Test Cases: {len(test_plan.happy_path)}")
        for i, test in enumerate(test_plan.happy_path, 1):
            print(f"\n  {i}. {test.get('title', 'Untitled')}")
            print(f"     Steps: {len(test.get('steps', []))}")
            print(f"     Expected: {test.get('expected', 'N/A')[:60]}...")

        print(f"\n🔍 Edge Cases: {len(test_plan.edge_cases)}")
        for i, test in enumerate(test_plan.edge_cases, 1):
            print(f"  {i}. {test.get('title', 'Untitled')}")

        print(f"\n🔄 Regression Checklist: {len(test_plan.regression_checklist)} items")
        for item in test_plan.regression_checklist[:3]:
            print(f"  - {item}")

        print(f"\n⚡ Non-Functional: {len(test_plan.non_functional)} items")
        for item in test_plan.non_functional[:3]:
            print(f"  - {item}")

        print(f"\n💡 Assumptions: {len(test_plan.assumptions)}")
        print(f"❓ Questions: {len(test_plan.questions)}")

        print()
        print("=" * 80)
        print("✅ LLM integration test passed!")
        print("=" * 80)
        return True

    except LLMError as e:
        print()
        print("=" * 80)
        print("❌ LLM ERROR")
        print("=" * 80)
        print(f"\nError: {e}\n")

        # Provide helpful troubleshooting
        if "Failed to connect to Ollama" in str(e):
            print("💡 Troubleshooting:")
            print("   1. Install Ollama: https://ollama.com/download")
            print("   2. Start Ollama: ollama serve")
            print("   3. Pull a model: ollama pull llama3.1")
            print("   4. Or switch to Claude API in .env: LLM_PROVIDER=claude")
        elif "ANTHROPIC_API_KEY not set" in str(e):
            print("💡 Troubleshooting:")
            print("   1. Get Claude API key from your company")
            print("   2. Add to .env: ANTHROPIC_API_KEY=sk-ant-api03-...")
            print("   3. Or use Ollama: LLM_PROVIDER=ollama")

        print()
        return False
    except Exception as e:
        print()
        print("=" * 80)
        print("❌ UNEXPECTED ERROR")
        print("=" * 80)
        print(f"\nError: {e}\n")
        import traceback

        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = asyncio.run(test_llm_generation())
    sys.exit(0 if success else 1)
