#!/usr/bin/env python3
"""
Thin client for the Hyros REST API, scoped to the School of Traditional Skills account.

The credential is read from $HYROS_API_KEY first (that is how GitHub Actions supplies
it) and otherwise from hyros_key.txt beside this file, which is mode 600 and gitignored.
It is never printed, never written into the built page, and never committed.

Run this file directly to probe which endpoints and parameters the account supports:

    python3 hyros_api.py
"""
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.hyros.com/v1/api/v1.0"
HERE = pathlib.Path(__file__).resolve().parent


def api_key():
    env = os.environ.get("HYROS_API_KEY", "").strip()
    if env:
        return env
    f = HERE / "hyros_key.txt"
    if f.exists():
        k = f.read_text().strip()
        if k:
            return k
    raise SystemExit(
        "No Hyros credential. Set HYROS_API_KEY in the environment, or place the "
        f"account key at {f} (chmod 600). It is gitignored and must stay that way."
    )


def get(path, params=None, tries=4):
    """GET a Hyros endpoint. Retries on transient failures and 429s."""
    url = f"{BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        url, headers={"API-Key": api_key(), "Accept": "application/json"}
    )
    for n in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            if e.code in (429, 500, 502, 503, 504) and n < tries - 1:
                time.sleep(2 * (n + 1))
                continue
            return e.code, body
        except Exception as e:
            if n < tries - 1:
                time.sleep(2 * (n + 1))
                continue
            return None, f"{type(e).__name__}: {e}"
    return None, "exhausted retries"


def paged(path, params, key="result", cap=40):
    """Follow nextPageId until the endpoint runs out. Returns the concatenated rows."""
    out, cursor, params = [], None, dict(params or {})
    for _ in range(cap):
        if cursor:
            params["pageId"] = cursor
        code, d = get(path, params)
        if code != 200 or not isinstance(d, dict):
            raise SystemExit(f"{path} failed: HTTP {code} {str(d)[:200]}")
        rows = d.get(key) or []
        out.extend(rows)
        cursor = d.get("nextPageId")
        if not cursor or not rows:
            break
    return out


def _probe():
    def show(label, path, params=None):
        code, d = get(path, params)
        if isinstance(d, dict):
            res = d.get("result")
            n = len(res) if isinstance(res, list) else ("obj" if isinstance(res, dict) else "-")
            print(f"  {label:<46} HTTP {code}  result_len={n}")
            return d
        print(f"  {label:<46} HTTP {code}  {str(d)[:90]}")
        return None

    print("=== discovery endpoints ===")
    for pth in ("sources", "ads"):
        d = show(f"GET /{pth}", pth, {"pageSize": 2})
        if d and d.get("result"):
            print("   keys:", sorted(d["result"][0]))
            print("   sample:", json.dumps(d["result"][0])[:330])

    print("\n=== sales endpoint ===")
    d = show("GET /sales", "sales", {"pageSize": 1})
    if d and d.get("result"):
        print("   keys:", sorted(d["result"][0]))

    print("\n=== attribution ===")
    d = show(
        "GET /attribution facebook_adset",
        "attribution",
        {
            "startDate": "2026-09-07", "endDate": "2026-09-07",
            "attributionModel": "last_click", "level": "facebook_adset",
            "fields": "cost,clicks,impressions,leads,sales,revenue",
            "ids": "23860347240410791,23860347240420791",
        },
    )
    if d:
        print("   ", json.dumps(d.get("result"))[:420])

    print("\n=== tagged leads paging ===")
    rows = paged("leads", {"pageSize": 250, "tags": "!summit-2026"})
    print(f"  !summit-2026 leads pulled: {len(rows)}")
    if rows:
        ls = rows[0].get("lastSource") or {}
        print("  sample campaign:", (ls.get("category") or {}).get("name"))


if __name__ == "__main__":
    _probe()
