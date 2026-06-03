"""
GitHub log push connectivity test.

Run locally:   GITHUB_LOG_TOKEN=... python test_github_log.py
Run on GitHub: Actions → "Alpaca connectivity test" → Run workflow

Checks:
  1. GITHUB_LOG_TOKEN set
  2. PUT logs/test-connectivity.json → 200/201
  3. GET same file → 200 (verifica che sia nel repo)
"""
import base64
import json
import os
import sys

import requests

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    icon = "✓" if ok else "✗"
    print(f"  {icon}  {label}" + (f"  →  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def main():
    print("\n=== GitHub log push test ===\n")

    print("[1] Token")
    token = os.getenv("GITHUB_LOG_TOKEN")
    check("GITHUB_LOG_TOKEN set", bool(token), "set" if token else "MISSING")
    if not token:
        print("\n  ⛔ Token mancante — impossibile proseguire.\n")
        sys.exit(1)

    repo    = os.getenv("GITHUB_REPO", "pietrozambo-tech/trading-system")
    date_str = "test-connectivity"
    api_url  = f"https://api.github.com/repos/{repo}/contents/logs/{date_str}.json"
    headers  = {
        "Authorization":        f"Bearer {token}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    print("\n[2] Push file di test")
    payload = {"test": True, "message": "GitHub log push connectivity test"}
    content = base64.b64encode(json.dumps(payload).encode()).decode()

    sha = None
    try:
        r = requests.get(api_url, headers=headers, timeout=10)
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception as e:
        check("GET pre-check", False, str(e))

    body: dict = {"message": f"test: verify GitHub log push ({date_str})", "content": content}
    if sha:
        body["sha"] = sha

    try:
        r = requests.put(api_url, json=body, headers=headers, timeout=30)
        check("PUT logs/test-connectivity.json", r.status_code in (200, 201),
              f"HTTP {r.status_code}" if r.status_code not in (200, 201) else "OK")
    except Exception as e:
        check("PUT logs/test-connectivity.json", False, str(e))

    print("\n[3] Verifica presenza nel repo")
    try:
        r = requests.get(api_url, headers=headers, timeout=10)
        check("GET file presente nel repo", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:
        check("GET file presente nel repo", False, str(e))

    print("\n" + "=" * 40)
    if failures:
        print(f"  ✗  {len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"       - {f}")
        print("=" * 40 + "\n")
        sys.exit(1)
    else:
        print("  ✓  GitHub log push funziona correttamente.")
        print("=" * 40 + "\n")


if __name__ == "__main__":
    main()
