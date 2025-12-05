#!/usr/bin/env python3
"""
Example script to demonstrate using the Jira Test Plan Bot API.
"""

import requests
import json
import sys


def check_health(base_url: str = "http://localhost:8000"):
    """Check if the server is healthy."""
    try:
        response = requests.get(f"{base_url}/health")
        response.raise_for_status()
        print("✅ Server is healthy!")
        print(f"Response: {response.json()}")
        return True
    except Exception as e:
        print(f"❌ Health check failed: {e}")
        return False


def generate_test_plan(issue_key: str, base_url: str = "http://localhost:8000"):
    """Generate a test plan for a Jira issue."""
    try:
        response = requests.post(
            f"{base_url}/generate",
            json={"issue_key": issue_key},
            headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        result = response.json()
        
        print(f"✅ Test plan generated successfully!")
        print(f"Issue: {result.get('issue_key')}")
        print(f"Message: {result.get('message')}")
        return True
    except requests.exceptions.HTTPError as e:
        print(f"❌ Failed to generate test plan: {e}")
        if e.response is not None:
            try:
                error_detail = e.response.json()
                print(f"Error details: {json.dumps(error_detail, indent=2)}")
            except (json.JSONDecodeError, ValueError):
                print(f"Response: {e.response.text}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False


def main():
    """Main function."""
    # Check if server is running
    print("Checking server health...")
    if not check_health():
        print("\n⚠️  Please make sure the server is running:")
        print("   python -m app.main")
        print("   or")
        print("   uvicorn app.main:app --reload")
        sys.exit(1)
    
    print("\n" + "="*50)
    
    # Get issue key from command line or use example
    if len(sys.argv) > 1:
        issue_key = sys.argv[1]
    else:
        issue_key = "PROJ-123"
        print(f"No issue key provided, using example: {issue_key}")
    
    print(f"\nGenerating test plan for issue: {issue_key}")
    generate_test_plan(issue_key)


if __name__ == "__main__":
    main()
