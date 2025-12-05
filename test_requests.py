import requests, sys, traceback

print("Python:", sys.version)
print("requests:", requests.__version__)

for url in [
    "https://api.github.com",
    "https://quote-api.jup.ag/health",
]:
    print("\nTesting", url)
    try:
        r = requests.get(url, timeout=10, verify=False)
        print("  OK status:", r.status_code)
    except Exception:
        traceback.print_exc()