#!/usr/bin/env python3
"""
Thin client for the Hyros REST API, scoped to the School of Traditional Skills account.

The credential is read from $HYROS_API_KEY first (that is how GitHub Actions supplies
it) and otherwise from hyros_key.txt beside this file, which is mode 600 and gitignored.
It is never printed, never written into the built page, and never committed.

Run this file directly to probe which endpoints and parameters the account supports:

    python3 hyros_api.py
"""
import concurrent.futures as cf
import datetime as dt
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


def paged(path, params, key="result", cap=400):
    """Follow nextPageId until the endpoint runs out. Returns the concatenated rows.

    `cap` is a runaway guard, NOT a row limit, so exhausting it is a hard error.
    It used to be 40 pages and return quietly: at 250 rows a page that silently
    truncated every pull at 10,000 rows. Hyros returns leads NEWEST FIRST, so on
    2026-09-10, with 11,199 tagged leads, the 1,199 oldest were dropped without a
    word. The dashboard showed 0 leads on 2026-09-07 and 1,089 on 09-08 against
    the true 137 and 2,141, and cost per lead was overstated across the board
    because the spend for those days stayed while their leads vanished.
    """
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
            return out
    raise SystemExit(
        f"{path}: still more pages after {cap} ({len(out):,} rows). Refusing to return a "
        f"truncated pull, which would silently undercount the oldest days. Raise the cap.")


def windows(start, end, hours, offset_s=0, grid_from=None, grid_to=None):
    """Contiguous (fromDate, toDate) pairs covering start..end, NEWEST FIRST.

    start and end are naive account-local datetimes: Hyros reads fromDate/toDate in the
    account's timezone and treats BOTH bounds as inclusive to the second (checked
    2026-09-18). Each window's toDate is the next one's fromDate, so a row stamped on an
    edge comes back twice and paged_windows drops the copy by id, and nothing can fall
    between two windows whatever precision Hyros keeps underneath.

    Inner edges sit every `hours` from grid_from (default start) to grid_to (default
    end), shifted by offset_s. Hyros caches a query by its parameters: on 2026-09-18 a
    repeated /sales query kept returning a list without a sale that a query with any
    other parameters already showed. A run-unique offset makes every window a query
    Hyros has not seen, while start and end, which decide what is counted, never move.
    """
    step = dt.timedelta(hours=hours)
    edge = (grid_from or start) + dt.timedelta(seconds=offset_s % int(step.total_seconds()))
    stop = grid_to or end
    edges = [start]
    while edge < stop:
        if start < edge < end:
            edges.append(edge)
        edge += step
    edges.append(end)
    fmt = "%Y-%m-%dT%H:%M:%S"
    return [(a.strftime(fmt), b.strftime(fmt)) for a, b in zip(edges, edges[1:])][::-1]


def paged_windows(path, params, wins, key="result", workers=12):
    """paged() once per (fromDate, toDate) window, several at a time. Rows come back in
    window order (newest first, as windows() returns them), deduplicated by id.

    Hyros caps a page at 250 rows, takes ~1.3s a page, and one cursor cannot be split,
    so a single paged() over 60,000 tagged leads was five minutes of a six-minute
    refresh. Cut by creation time the same pull runs side by side: 23s at 12 workers on
    2026-09-18, every one of the 59,865 leads the sequential pull had plus the 169
    registered since, and no 429s. Each window still follows its own cursor to the end,
    so paged()'s refusal to return a truncated pull holds window by window.
    """
    def one(w):
        return paged(path, {**params, "fromDate": w[0], "toDate": w[1]}, key=key)

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        parts = list(ex.map(one, wins))
    seen, out = set(), []
    for rows in parts:
        for r in rows:
            rid = r.get("id")
            if rid is not None:
                if rid in seen:
                    continue
                seen.add(rid)
            out.append(r)
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
