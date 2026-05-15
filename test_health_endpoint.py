#!/usr/bin/env python3
"""
Test script to verify the database health endpoint.

Run this after starting the API server.
"""
import json
import sys

try:
    import requests
except ImportError:
    print("Installing requests...")
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests


def test_database_health():
    """Test the database health endpoint."""

    # Test basic health endpoint
    print("Testing basic health endpoint...")
    try:
        response = requests.get("http://localhost:8000/health", timeout=5)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.json()}")
    except requests.exceptions.ConnectionError:
        print("ERROR: Could not connect to API. Is the server running?")
        print("Start the server with: uvicorn app.main:app --reload")
        return False
    except Exception as e:
        print(f"ERROR: {e}")
        return False

    print("\n" + "="*60)

    # Test database health endpoint
    print("\nTesting database health endpoint...")
    try:
        response = requests.get("http://localhost:8000/api/v1/health/db", timeout=10)
        print(f"Status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"Overall Status: {data.get('overall_status')}")
            print(f"Duration: {data.get('duration_seconds', 0):.3f}s")
            print(f"Timestamp: {data.get('timestamp')}")

            print("\nChecks:")
            checks = data.get('checks', {})
            for check_name, check_result in checks.items():
                if isinstance(check_result, dict):
                    status = check_result.get('status', 'unknown')
                    print(f"  - {check_name}: {status}")
                else:
                    print(f"  - {check_name}: {check_result}")

            # Validate response structure
            required_keys = ['overall_status', 'timestamp', 'duration_seconds', 'checks']
            missing_keys = [k for k in required_keys if k not in data]
            if missing_keys:
                print(f"\nWARNING: Missing required keys: {missing_keys}")

            print("\n" + "="*60)
            print("SUCCESS: Database health endpoint is working!")
            return True
        else:
            print(f"ERROR: Status code {response.status_code}")
            print(f"Response: {response.text}")
            return False

    except requests.exceptions.Timeout:
        print("ERROR: Request timed out. Database health check may be slow.")
        return False
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = test_database_health()
    sys.exit(0 if success else 1)