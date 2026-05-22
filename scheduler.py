import requests
import os

def run_daily_job():
    backend_url = os.environ.get("BACKEND_URL", "https://web-production-a085e.up.railway.app")
    try:
        print(f"Triggering daily job via web server...")
        res = requests.post(f"{backend_url}/run-daily", timeout=30)
        data = res.json()
        if data.get("success"):
            print(f"Daily job triggered successfully. Check web server logs for results.")
        else:
            print(f"Daily job error: {data.get('error')}")
    except Exception as e:
        print(f"Failed to trigger daily job: {e}")

if __name__ == "__main__":
    run_daily_job()
