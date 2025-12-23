"""
Simple test runner - no pytest required.

Run this to test all functionality with dummy data.
"""

import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

print("=" * 80)
print("TESTING JIRA TEST PLAN BOT - ADF PARSER & DESCRIPTION ANALYZER")
print("=" * 80)
print()

try:
    # Run the manual tests
    test_file = Path(__file__).parent / "test_manual.py"
    exec(open(test_file).read())
    print("\n✅ All ADF parsing and description analysis tests passed!")

except Exception as e:
    print(f"\n❌ Test failed with error: {e}")
    import traceback

    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 80)
print("TESTING COMPLETE")
print("=" * 80)
print()
print("Next steps:")
print("1. Run your API: uv run uvicorn src.app.main:app --reload")
print("2. Test with real Jira: GET http://127.0.0.1:8000/issue/YOUR-TICKET-KEY")
print("3. Check the Swagger docs: http://127.0.0.1:8000/docs")
