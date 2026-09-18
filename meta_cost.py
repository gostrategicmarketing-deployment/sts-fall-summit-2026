#!/usr/bin/env python3
"""
Spend, link clicks and impressions per ad set per day, straight from Meta.

Why not Hyros, when everything else here is: Hyros does not measure cost, it relays
what Meta bills, and it relays it late. On 2026-09-09 Hyros had $3,758.15 against
Meta's $4,052.86 for the same campaigns and window, the newest campaigns worst hit
(Scaling: $5.59 against $54.20). Every derived figure inherits that error, so cost
per lead, cost per click and ROAS were all reading better than they were.

Leads, purchases and revenue stay on Hyros, which is the point of Hyros: it counts
server-side and catches what the pixel drops.

Ad sets are matched BY ID, never by campaign name. The accounts carry non-summit
campaigns too ($184.53 of other spend in the same window), and the summit campaigns
have already been renamed once, so a name filter would both leak and miss.

TWO ROADS TO THE SAME NUMBERS (added 2026-09-17). The Graph token here is on a
development-tier app it shares with other dashboards, and at 11:16 ET on 2026-09-17 a
refresh spent ten minutes in retries and then died on "Application request limit
reached". Windsor.ai reads the same accounts through its own Meta app, so that limit
does not reach it. Checked the same day: 2026-09-14 identical to the cent on spend,
impressions and link clicks, and a first-time read of today matched Graph to the cent.
A REPEATED read is served from Windsor's cache, which is handled below (_RUN) and
measured on every pull (data_fetched_at). Graph is still tried first because it is Meta itself; the first
time it fails in a run, the rest of that run reads through Windsor, so every table on
the page comes the same way. Without a Windsor key nothing changes from before.

    python3 meta_cost.py 2026-09-07 2026-09-09            # totals for a window
    python3 meta_cost.py 2026-09-07 2026-09-09 --windsor  # same, through Windsor only
"""
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from meta_api import get

ACCOUNTS = [("STS", "1246959949464564"), ("TSA", "3014083142121289")]
PAGE_LIMIT = 500
HERE = pathlib.Path(__file__).resolve().parent
WINDSOR = "https://connectors.windsor.ai/facebook"

# Which road this run took. pull.py records it on the page, so a pull read through
# Windsor says so rather than passing silently as a Graph pull. `as_of` is the oldest
# moment Windsor actually fetched any ad-set row from Meta (UTC), not when we asked.
ROUTE = {"via": "graph", "note": "", "as_of": ""}

# Windsor on the Basic plan answers a query it has seen before FROM CACHE, for up to six
# hours. Measured 2026-09-17: the same summit query at 11:23 and 11:35 ET both returned
# $2,048.37 while Meta had moved to $2,103.91. Reordering the fields does not escape it;
# a different field list or filter does. So every run adds a filter that excludes
# nothing (spend is never below minus the current epoch) but is unique to that run, and
# the ad-set pull asks for data_fetched_at so the page can prove how fresh it is.
_RUN = int(time.time())


def windsor_key():
    """$WINDSOR_API_KEY, then windsor_key.txt beside this file. None when neither is set."""
    env = os.environ.get("WINDSOR_API_KEY", "").strip()
    if env:
        return env
    f = HERE / "windsor_key.txt"
    if f.exists():
        return f.read_text().strip() or None
    return None


def _on_route(graph, windsor):
    """Graph while it answers, Windsor from the first time it does not.

    SystemExit is caught too: meta_api raises it when there is no Graph token at all,
    which is a reason to take the other road, not to stop the refresh.
    """
    if ROUTE["via"] == "graph":
        try:
            return graph()
        except (Exception, SystemExit) as e:
            if not windsor_key():
                raise
            ROUTE.update(via="windsor", note=f"{type(e).__name__}: {str(e)[:160]}")
            print(f"  Meta Graph unavailable ({ROUTE['note'][:100]}); "
                  "reading the same Meta figures through Windsor.ai")
    return windsor()


def _graph_tries():
    # meta_api waits 5s, 20s and 45s on a rate limit before giving up. Worth it when
    # there is nowhere else to go; with Windsor standing by it is a minute wasted per call.
    return 2 if windsor_key() else 4


def _windsor(fields, since, until, tries=3):
    """Rows from Windsor's Meta connector for both summit accounts.

    The key travels in the query string (Windsor accepts no header auth), so it is
    scrubbed out of anything that could reach a log.
    """
    key = windsor_key()
    if not key:
        raise RuntimeError("no Windsor key: set WINDSOR_API_KEY or create windsor_key.txt")
    if "spend" not in fields:
        raise ValueError("every Windsor query here carries spend, which the cache-busting filter reads")
    url = WINDSOR + "?" + urllib.parse.urlencode({
        "api_key": key, "fields": ",".join(fields),
        "date_from": since, "date_to": until,
        "select_accounts": ",".join(a for _s, a in ACCOUNTS),
        "filter": json.dumps([["spend", "gte", -_RUN]]),
    })
    for n in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                body = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:200].replace(key, "<key>")
            if n < tries - 1 and e.code in (429, 500, 502, 503, 504):
                time.sleep(5 * (n + 1))
                continue
            raise RuntimeError(f"Windsor HTTP {e.code} {msg}") from None
        except Exception as e:
            if n < tries - 1:
                time.sleep(5 * (n + 1))
                continue
            raise RuntimeError(f"Windsor unreachable ({type(e).__name__})") from None
        if isinstance(body, dict) and isinstance(body.get("data"), list):
            return body["data"]
        raise RuntimeError("Windsor returned no data: " + str(body)[:200].replace(key, "<key>"))
    raise RuntimeError("Windsor: exhausted retries")


_ACCOUNT_SPEND = {}


def _windsor_check(rows, since, until, what):
    """Refuse a Windsor pull that does not add back up to the accounts' own totals.

    Windsor documents no paging and no row cap, and a reader that silently returns a
    short result is how 2026-09-07 once rendered zero leads. One tiny call per window
    says what each account spent in total; the detailed rows must reach it.
    """
    key = (since, until)
    if key not in _ACCOUNT_SPEND:
        per = {}
        for r in _windsor(["account_id", "spend"], since, until):
            a = str(r.get("account_id"))
            per[a] = per.get(a, 0.0) + float(r.get("spend") or 0)
        _ACCOUNT_SPEND[key] = per
    per = _ACCOUNT_SPEND[key]
    for _short, acct in ACCOUNTS:
        want = per.get(acct, 0.0)
        got = sum(float(r.get("spend") or 0) for r in rows if str(r.get("account_id")) == acct)
        # Today is still accruing between the two calls, so allow a little drift. A
        # dropped page of rows is far larger than this.
        if abs(want - got) > max(5.0, want * 0.005):
            raise RuntimeError(f"Windsor {what} rows for act_{acct} sum to ${got:,.2f} but the "
                               f"account spent ${want:,.2f}; refusing a short pull")


_ROWS = {}


def _rows(account_id, since, until):
    """Every ad-set-day row in the window, memoised for the life of the process.

    cost_by_adset_day, adsets_by_campaign and campaign_spend all want the same rows.
    Pulling them separately spent three times the rate limit on one answer, and Meta
    announces a rate limit by failing the refresh, so the cheapest call is the one
    not made twice.
    """
    key = (account_id, since, until)
    if key not in _ROWS:
        _ROWS.update(_on_route(
            lambda: {key: _fetch_rows(account_id, since, until)},
            lambda: _windsor_adset_rows(since, until)))
    return _ROWS[key]


def _fetch_rows(account_id, since, until):
    """Every ad-set-day row in the window, following Meta's paging."""
    params = {
        "level": "adset",
        "time_increment": 1,
        "time_range": json.dumps({"since": since, "until": until}),
        "fields": "adset_id,adset_name,campaign_name,spend,inline_link_clicks,impressions",
        "limit": PAGE_LIMIT,
    }
    out, path, seen = [], f"act_{account_id}/insights", 0
    while True:
        code, d = get(path, params, tries=_graph_tries())
        if code != 200:
            raise RuntimeError(f"Meta insights failed for act_{account_id}: HTTP {code} {str(d)[:200]}")
        out += d.get("data") or []
        after = ((d.get("paging") or {}).get("cursors") or {}).get("after")
        if not (d.get("paging") or {}).get("next") or not after:
            return out
        params["after"] = after
        seen += 1
        if seen > 40:            # a window this small cannot legitimately page forever
            return out


def _windsor_adset_rows(since, until):
    """Both accounts' ad-set-day rows through Windsor, in the shape Graph returns them.

    Windsor's link_clicks is Graph's inline_link_clicks: they matched to the click on
    2026-09-14 across every summit campaign in both accounts.
    """
    # data_fetched_at turns Windsor's aggregation off, so rows may come back finer than
    # one per ad set per day. Every reader below sums by ad set and day, so that is safe.
    rows = _windsor(["date", "account_id", "campaign", "adset_id", "adset_name",
                     "spend", "link_clicks", "impressions", "data_fetched_at"], since, until)
    _windsor_check(rows, since, until, "ad set")
    stamps = sorted(str(r["data_fetched_at"]) for r in rows if r.get("data_fetched_at"))
    if stamps and (not ROUTE["as_of"] or stamps[0] < ROUTE["as_of"]):
        ROUTE["as_of"] = stamps[0]
    out = {(acct, since, until): [] for _s, acct in ACCOUNTS}
    for r in rows:
        k = (str(r.get("account_id")), since, until)
        if k not in out:
            continue
        # Graph leaves out an ad-set-day that delivered nothing; Windsor sends it as a
        # row of zeros. Kept, those would add idle ad sets to the board's counts.
        if not (float(r.get("spend") or 0) or r.get("impressions") or r.get("link_clicks")):
            continue
        out[k].append({
            "date_start": r.get("date"),
            "adset_id": str(r.get("adset_id") or ""),
            "adset_name": r.get("adset_name") or "",
            "campaign_name": r.get("campaign") or "",
            "spend": r.get("spend") or 0,
            "inline_link_clicks": r.get("link_clicks") or 0,
            "impressions": r.get("impressions") or 0,
        })
    return out


def cost_by_adset_day(adset_ids, since, until):
    """{day: {adset_id: {spend, clicks, impressions}}} for the given ad sets only.

    `clicks` is link clicks, not all clicks: the house rule is to report link clicks
    and cost per link click, and the page's conversion rate divides leads by them.
    """
    want = set(adset_ids)
    per_day = {}
    for _short, acct in ACCOUNTS:
        for r in _rows(acct, since, until):
            aid = r.get("adset_id")
            if aid not in want:
                continue
            day = r.get("date_start")
            slot = per_day.setdefault(day, {}).setdefault(
                aid, {"spend": 0.0, "clicks": 0, "impressions": 0})
            slot["spend"] += float(r.get("spend") or 0)
            slot["clicks"] += int(r.get("inline_link_clicks") or 0)
            slot["impressions"] += int(r.get("impressions") or 0)
    for day in per_day.values():
        for m in day.values():
            m["spend"] = round(m["spend"], 2)
    return per_day


def _graph_ad_rows(since, until):
    rows = []
    for _short, acct in ACCOUNTS:
        params = {
            "level": "ad",
            "time_range": json.dumps({"since": since, "until": until}),
            "fields": "ad_id,spend,inline_link_clicks,impressions",
            "limit": PAGE_LIMIT,
        }
        path, guard = f"act_{acct}/insights", 0
        while True:
            code, d = get(path, params, tries=_graph_tries())
            if code != 200:
                raise RuntimeError(f"Meta ad insights failed for act_{acct}: HTTP {code} {str(d)[:200]}")
            rows += d.get("data") or []
            after = ((d.get("paging") or {}).get("cursors") or {}).get("after")
            guard += 1
            if not (d.get("paging") or {}).get("next") or not after or guard > 40:
                break
            params["after"] = after
    return rows


def _windsor_ad_rows(since, until):
    rows = _windsor(["account_id", "ad_id", "spend", "link_clicks", "impressions"], since, until)
    _windsor_check(rows, since, until, "ad")
    return [{"ad_id": str(r.get("ad_id") or ""), "spend": r.get("spend") or 0,
             "inline_link_clicks": r.get("link_clicks") or 0,
             "impressions": r.get("impressions") or 0} for r in rows]


def cost_by_ad(ad_ids, since, until):
    """{ad_id: {spend, clicks, impressions}} over the whole window, ad level.

    Kept on the same source as the ad-set figures so the ad table and the campaign
    table reconcile; mixing Meta cost above with Hyros cost below would put a gap
    between two tables on one page.
    """
    want = set(ad_ids)
    out = {}
    rows = _on_route(lambda: _graph_ad_rows(since, until),
                     lambda: _windsor_ad_rows(since, until))
    for r in rows:
        aid = r.get("ad_id")
        if aid not in want:
            continue
        m = out.setdefault(aid, {"spend": 0.0, "clicks": 0, "impressions": 0})
        m["spend"] += float(r.get("spend") or 0)
        m["clicks"] += int(r.get("inline_link_clicks") or 0)
        m["impressions"] += int(r.get("impressions") or 0)
    for m in out.values():
        m["spend"] = round(m["spend"], 2)
    return out


def adsets_by_campaign(since, until):
    """{adset_id: {campaign, adset_name, account}} for every ad set with delivery.

    Hyros lists an ad set as a source only after it has carried traffic. A campaign
    launched this morning is therefore invisible there while Meta is already billing
    it, and its spend would sit on no row. This is the list that keeps a brand new
    campaign on the board from its first dollar; pull.py decides which of these
    campaigns belong to the summit, by the same rules it applies to Hyros.

    Reads the same ad-set-day rows the spend figures come from, so knowing about a
    new campaign costs no extra call.
    """
    out = {}
    for _short, acct in ACCOUNTS:
        for r in _rows(acct, since, until):
            if r.get("adset_id"):
                out[r["adset_id"]] = {
                    "campaign": r.get("campaign_name") or "",
                    "adset_name": r.get("adset_name") or "",
                    "account": acct,
                }
    return out


def _graph_leads(since, until):
    rows = []
    for _short, acct in ACCOUNTS:
        params = {
            "level": "adset",
            "time_range": json.dumps({"since": since, "until": until}),
            "fields": "adset_id,actions", "limit": PAGE_LIMIT,
        }
        code, d = get(f"act_{acct}/insights", params, tries=_graph_tries())
        if code != 200:
            raise RuntimeError(f"Meta lead count failed for act_{acct}: HTTP {code}")
        for r in d.get("data") or []:
            n = sum(int(float(a.get("value") or 0)) for a in r.get("actions") or []
                    if a.get("action_type") == "lead")
            rows.append({"adset_id": r.get("adset_id"), "leads": n})
    return rows


def _windsor_leads(since, until):
    return [{"adset_id": str(r.get("adset_id") or ""), "leads": int(float(r.get("actions_lead") or 0))}
            for r in _windsor(["adset_id", "actions_lead", "spend"], since, until)]


def reported_leads(adset_ids, since, until):
    """Meta's own lead count for the same ad sets, for the page's honesty note.

    Not used in any calculation. It exists so the page can say what Meta will show
    if somebody opens Ads Manager next to it, instead of leaving them to discover a
    ten percent gap on their own and mistrust both numbers.
    """
    want = set(adset_ids)
    rows = _on_route(lambda: _graph_leads(since, until),
                     lambda: _windsor_leads(since, until))
    return sum(r["leads"] for r in rows if r.get("adset_id") in want)


def campaign_spend(name_contains, since, until):
    """Spend per campaign name, used to check that nothing hidden is quietly spending."""
    out = {}
    for _short, acct in ACCOUNTS:
        for r in _rows(acct, since, until):
            camp = r.get("campaign_name") or ""
            if name_contains.lower() in camp.lower():
                out[camp] = round(out.get(camp, 0.0) + float(r.get("spend") or 0), 2)
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    since, until = (args[0], args[1]) if len(args) > 1 else ("2026-09-07", "2026-09-09")
    if "--windsor" in sys.argv:
        if not windsor_key():
            raise SystemExit(
                "No Windsor key yet. Copy the API key from https://onboard.windsor.ai/app/ and save it,\n"
                f"on its own with nothing else, as:\n  {HERE / 'windsor_key.txt'}\n"
                "then run this again.")
        ROUTE.update(via="windsor", note="forced with --windsor")
    for camp, spend in sorted(campaign_spend("Fall Summit 2026", since, until).items(),
                              key=lambda kv: -kv[1]):
        print(f"  {spend:>10,.2f}  {camp}")
    print(f"  (read through {ROUTE['via']})")
