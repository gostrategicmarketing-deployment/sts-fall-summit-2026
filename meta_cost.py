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

    python3 meta_cost.py 2026-09-07 2026-09-09     # totals for a window
"""
import json
import sys

from meta_api import get

ACCOUNTS = [("STS", "1246959949464564"), ("TSA", "3014083142121289")]
PAGE_LIMIT = 500


def _rows(account_id, since, until):
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
        code, d = get(path, params)
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


def cost_by_ad(ad_ids, since, until):
    """{ad_id: {spend, clicks, impressions}} over the whole window, ad level.

    Kept on the same source as the ad-set figures so the ad table and the campaign
    table reconcile; mixing Meta cost above with Hyros cost below would put a gap
    between two tables on one page.
    """
    want = set(ad_ids)
    out = {}
    for _short, acct in ACCOUNTS:
        params = {
            "level": "ad",
            "time_range": json.dumps({"since": since, "until": until}),
            "fields": "ad_id,spend,inline_link_clicks,impressions",
            "limit": PAGE_LIMIT,
        }
        path, guard = f"act_{acct}/insights", 0
        while True:
            code, d = get(path, params)
            if code != 200:
                raise RuntimeError(f"Meta ad insights failed for act_{acct}: HTTP {code} {str(d)[:200]}")
            for r in d.get("data") or []:
                aid = r.get("ad_id")
                if aid not in want:
                    continue
                m = out.setdefault(aid, {"spend": 0.0, "clicks": 0, "impressions": 0})
                m["spend"] += float(r.get("spend") or 0)
                m["clicks"] += int(r.get("inline_link_clicks") or 0)
                m["impressions"] += int(r.get("impressions") or 0)
            after = ((d.get("paging") or {}).get("cursors") or {}).get("after")
            guard += 1
            if not (d.get("paging") or {}).get("next") or not after or guard > 40:
                break
            params["after"] = after
    for m in out.values():
        m["spend"] = round(m["spend"], 2)
    return out


def reported_leads(adset_ids, since, until):
    """Meta's own lead count for the same ad sets, for the page's honesty note.

    Not used in any calculation. It exists so the page can say what Meta will show
    if somebody opens Ads Manager next to it, instead of leaving them to discover a
    ten percent gap on their own and mistrust both numbers.
    """
    want, total = set(adset_ids), 0
    for _short, acct in ACCOUNTS:
        params = {
            "level": "adset",
            "time_range": json.dumps({"since": since, "until": until}),
            "fields": "adset_id,actions", "limit": PAGE_LIMIT,
        }
        code, d = get(f"act_{acct}/insights", params)
        if code != 200:
            return 0
        for r in d.get("data") or []:
            if r.get("adset_id") not in want:
                continue
            for a in r.get("actions") or []:
                if a.get("action_type") == "lead":
                    total += int(float(a.get("value") or 0))
    return total


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
    since, until = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("2026-09-07", "2026-09-09")
    for camp, spend in sorted(campaign_spend("Fall Summit 2026", since, until).items(),
                              key=lambda kv: -kv[1]):
        print(f"  {spend:>10,.2f}  {camp}")
