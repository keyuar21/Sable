#!/usr/bin/env python3
"""Call /pin/set for each demo user so their PIN is hashed and stored.
Runs against local mock auth service on port 5005.
"""
import time
import sys
import json
import urllib.request

AUTH_URL = 'http://localhost:5005/pin/set'
DEMO_PINS = [
    ("you@mockbank", "1234"),
    ("priya@mockbank", "1234"),
    ("rahul@mockbank", "1234"),
]

def post_pin(vpa, pin):
    data = json.dumps({"vpa": vpa, "pin": pin}).encode()
    req = urllib.request.Request(AUTH_URL, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.read().decode()
    except Exception as e:
        raise

if __name__ == '__main__':
    # Wait for auth service to be up
    for attempt in range(20):
        try:
            urllib.request.urlopen('http://localhost:5005/health', timeout=2).read()
            break
        except Exception:
            time.sleep(0.5)
    else:
        print('Auth service not reachable at http://localhost:5005 — skipping PIN init', file=sys.stderr)
        sys.exit(1)

    for vpa, pin in DEMO_PINS:
        try:
            print(f"Setting PIN for {vpa}...", flush=True)
            res = post_pin(vpa, pin)
            print(f"OK: {vpa}")
        except Exception as e:
            print(f"Failed to set PIN for {vpa}: {e}", file=sys.stderr)

    print('Done.')
