#!/usr/bin/env python3
"""
Pull the Fall Summit 2026 numbers from Hyros and write data/dashboard_data.json.

No Claude, no MCP: this talks to the Hyros REST API directly, so the Refresh button
and the GitHub Actions job can both run it unattended.

    python3 pull.py            # refresh today's figures
    python3 pull.py --date 2026-09-08

Campaigns, ad sets and ads are DISCOVERED on every run rather than read from a stored
list. A hardcoded list is what went stale on 2026-09-07 and silently dropped an ad that
was holding 16 leads. The SLOTS table below now controls only the reading ORDER and the
subtotal blocks: a Fall Summit campaign that matches nothing in it is still counted, and
so is a new ad set Meta is billing before Hyros has registered it as a source.

Counting rules, set deliberately and not to be changed casually:
  - Leads, purchases and revenue count only leads carrying !summit-2026.
  - A sale counts only if the buyer has a Facebook ad touch: the sale's own source is
    a summit ad, or the lead reached the offer through one. A tagged sale with no ad
    touch anywhere is organic and is excluded from every figure, though it stays
    visible in the ledger marked Excluded. Where an ad touch exists, the sale counts
    whatever click closed it, credited to that ad.
  - Spend, clicks and impressions cannot be tag-filtered, so they are the platform
    figures for the summit campaigns, which carry no other traffic.
  - TWO windows are pulled every run: first_day..today is the running total, and
    today..today is today. The day figure is never derived from the cumulative one.
    data/daily.json holds day rows, never campaign-to-date rows.
"""
import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import json
import pathlib
import re
import sys
import time

from hyros_api import get, paged, paged_windows, windows

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"
TAG = "!summit-2026"
# Shifts the inner edges of every dated Hyros window so no two refreshes send the same
# query: Hyros answers a repeated query from its cache (see hyros_api.windows).
_RUN = int(time.time())

# The campaign slots, in the order they appear on the page. Each entry is the friendly
# slot name and the pattern that recognises its Hyros campaign. "Summit" has been
# optional since Pages 3 to 5 launched as "Fall 2026"; they read "Fall Summit 2026" now,
# but the tolerance stays because the rename could go either way again.
#
# The patterns must not overlap, and each one ends where the campaign name ends rather
# than running on: order-dependent matching is the kind of thing that survives review and
# then breaks silently a month later. "Scaling" therefore has to be the WHOLE segment. It
# used to accept anything starting with the word, which quietly folded
# "Fall Summit 2026 | Scaling Canda" into the Scaling row under the Scaling name; a
# campaign nobody declared now gets its own line instead of hiding inside somebody else's.
# Third field is the group the page subtotals on.
SLOTS = [
    ("Page 1",         r"Fall (Summit )?2026 \| Page 1\s*(?:\||$)", "named"),
    ("Page 2",         r"Fall (Summit )?2026 \| Page 2\s*(?:\||$)", "named"),
    ("Page 3",         r"Fall (Summit )?2026 \| Page 3\s*(?:\||$)", "named"),
    ("Page 4",         r"Fall (Summit )?2026 \| Page 4\s*(?:\||$)", "named"),
    ("Page 5",         r"Fall (Summit )?2026 \| Page 5\s*(?:\||$)", "named"),
    ("General Hooks",  r"Fall (Summit )?2026 \| General Hooks\s*(?:\||$)", "named"),
    ("Grid",           r"Fall (Summit )?2026 \| Grid( Photos)?\s*(?:\||$)", "named"),
    ("Video",          r"Fall (Summit )?2026 \| Videos?\s*(?:\||$)", "named"),
    # Launched 2026-09-08, where winners go to spend. One row each, by request: the
    # duplicate carries its own budget and can win or lose on its own.
    ("Scaling",        r"Fall (Summit )?2026 \| Scaling\s*(?:\||$)",  "scaling"),
    ("Scaling 2",      r"Fall (Summit )?2026 \| Scaling 2\b(?!.*-\s*Copy)", "scaling"),
]
DECLARED = {slot for slot, _pat, _group in SLOTS}
# Shown above each subtotal line. A group of one needs no subtotal; the page skips it.
GROUP_LABEL = {"named": "named campaigns", "scaling": "scaling campaigns"}
# The order the blocks read in. A campaign discovered into a group not listed here is
# appended after them rather than dropped.
GROUP_ORDER = ["named", "scaling"]
# A different, earlier summit. Its campaigns must never be swept in.
EXCLUDE = re.compile(r"Preservation", re.I)
# Deliberately off the sheet. "Scaling 2 | ABO - Copy" was a duplicate that never left
# review: Meta reports no delivery for it at all. Hidden rather than deleted, and
# checked against Meta every run, so if it ever starts spending the pull says so
# instead of quietly leaving the money off the page.
HIDDEN = re.compile(r"Fall (Summit )?2026 \| Scaling 2\b.*-\s*Copy", re.I)
IS_SUMMIT = re.compile(r"Fall (Summit )?2026", re.I)


def slot_for(campaign_name):
    """The declared slot for a campaign, or None if it matches no entry in SLOTS."""
    if not campaign_name or EXCLUDE.search(campaign_name) or HIDDEN.search(campaign_name):
        return None
    for slot, pat, _group in SLOTS:
        if re.search(pat, campaign_name, re.I):
            return slot
    return None


# A campaign matching no SLOT is still put on the board, under a label derived from its
# own name. Dropping it was the old behaviour and it was the wrong one: on 2026-09-14
# "TOF | Fall Summit 2026 | Warm Scaling 1 | ABO" existed in Hyros and appeared on no row,
# and every campaign launched after it would have joined it there. Declaring a slot is now
# how a campaign gets a chosen name and a chosen block, never how it gets counted.
_TRIM = (
    r"^\s*(?:TOF|MOF|BOF)\s*\|\s*",            # funnel stage
    r"Fall\s+(?:Summit\s+)?2026\s*\|\s*",      # the summit itself
    r"\s*\|\s*(?:ABO|CBO)\s*$",                 # buying type
)


def auto_slot(campaign_name):
    """A short row label for an undeclared campaign, cut from the campaign name."""
    name = campaign_name or ""
    for pat in _TRIM:
        name = re.sub(pat, "", name, flags=re.I)
    return name.strip(" |") or (campaign_name or "").strip() or "Unnamed campaign"


def auto_group(slot):
    """Which block an undeclared campaign subtotals into. Grouping only: the grand
    total takes every campaign whichever block it lands in."""
    return "scaling" if re.search(r"scal", slot, re.I) else "named"


def discovery_rows():
    """Every Facebook ad set (a Hyros source) and ad Hyros knows, fetched side by side.

    Its own function so main can run it while the tagged leads are still coming in: it
    needs nothing from them. Only discover(), which sorts these rows, does.
    """
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_src = ex.submit(paged, "sources", {"pageSize": 250, "integrationType": "FACEBOOK"})
        f_ads = ex.submit(paged, "ads", {"pageSize": 250, "integrationType": "FACEBOOK"})
        return f_src.result(), f_ads.result()


def discover(rows, lead_campaigns=(), first_day=None, today=None):
    """Every summit ad set and ad, from Hyros and from Meta.

    Returns (adsets, ads, slot_table, offboard). `slot_table` is the ordered
    (slot, group, auto) list the page renders. `offboard` names campaigns holding
    tagged leads that are deliberately not counted.

    A campaign reaches the board three ways: it matches a declared SLOT, it is named
    for this summit, or it is holding !summit-2026 leads. Only EXCLUDE (a different
    summit) and HIDDEN (a duplicate that never left review) keep one off.
    """
    lead_campaigns = {c for c in lead_campaigns if c}
    adsets, ads, auto, offboard = {}, {}, {}, set()

    def resolve(camp):
        """(slot, is_auto) for a campaign name; (None, False) leaves it off the board."""
        if not camp or EXCLUDE.search(camp) or HIDDEN.search(camp):
            return None, False
        slot = slot_for(camp)
        if slot:
            return slot, False
        if not (IS_SUMMIT.search(camp) or camp in lead_campaigns):
            return None, False
        slot = auto_slot(camp)
        if slot in DECLARED:          # never merge into a row it did not actually match
            slot = camp.strip()
        auto.setdefault(slot, auto_group(slot))
        return slot, True

    src_rows, ad_rows_raw = rows
    for s in src_rows:
        camp = ((s.get("category") or {}).get("name")) or ""
        slot, _auto = resolve(camp)
        if not slot:
            if camp in lead_campaigns:
                offboard.add(camp)
            continue
        src = s.get("adSource") or {}
        if not src.get("adSourceId"):
            continue
        adsets[src["adSourceId"]] = {
            "adSetId": src["adSourceId"], "adSetName": s.get("name") or "",
            "adAccountId": src.get("adAccountId", ""), "campaign": camp, "slot": slot,
        }

    # Hyros lists an ad set as a source only once it has carried traffic, so a campaign
    # launched this morning can be spending with nothing here to hang the spend on. Meta
    # bills it and knows the ad set at once, so the two lists are unioned by id and the
    # money is on the board from the first dollar. Ads still come from Hyros alone: an ad
    # set Meta has and Hyros does not cannot be holding a tagged lead yet.
    if first_day and today:
        try:
            from meta_cost import adsets_by_campaign
            for sid, info in adsets_by_campaign(first_day, today).items():
                if sid in adsets:
                    continue
                slot, _auto = resolve(info["campaign"])
                if not slot:
                    continue
                adsets[sid] = {
                    "adSetId": sid, "adSetName": info["adset_name"],
                    "adAccountId": info["account"], "campaign": info["campaign"],
                    "slot": slot,
                }
        except Exception as e:
            print(f"  could not sweep Meta for unregistered ad sets ({type(e).__name__}: {e})")

    for a in ad_rows_raw:
        parent = (a.get("source") or {})
        psrc = parent.get("adSource") or {}
        if psrc.get("adSourceId") not in adsets:
            continue
        src = a.get("adSource") or {}
        if not src.get("adSourceId"):
            continue
        meta = adsets[psrc["adSourceId"]]
        name = a.get("name") or ""
        is_video = bool(re.search(r"\bvideos?\b", meta["slot"], re.I)
                        or re.search(r"\bvideo\b", name, re.I))
        ads[src["adSourceId"]] = {
            "id": src["adSourceId"], "name": name,
            "campaign": meta["slot"], "adset": meta["adSetName"],
            "account": "TSA" if src.get("adAccountId") == "3014083142121289" else "STS",
            "type": "video" if is_video else "image",
        }

    # Declared slots keep their order, discovered ones follow inside their own block.
    # The sort is stable, so this is "group blocks in GROUP_ORDER, rows unchanged within".
    slot_table = [(slot, group) for slot, _pat, group in SLOTS] + sorted(auto.items())
    rank = {g: i for i, g in enumerate(GROUP_ORDER)}
    slot_table.sort(key=lambda t: rank.get(t[1], len(rank)))
    return adsets, ads, [(sl, g, sl in auto) for sl, g in slot_table], sorted(offboard)


def attribution(ids, level, start, end):
    """Spend / clicks / impressions per entity. Batched: the ids list gets long."""
    out = {}
    ids = [i for i in ids if i]
    chunks = [ids[i:i + 40] for i in range(0, len(ids), 40)]

    def one(chunk):
        return get("attribution", {
            "startDate": start, "endDate": end, "attributionModel": "last_click",
            "level": level, "fields": "cost,clicks,impressions,leads,sales,revenue",
            "ids": ",".join(chunk),
        })

    # Batches are independent, so they go out together. Sequentially the ad level alone
    # was 7.9s of the refresh; six at a time it is closer to 1.5s. Kept modest so Hyros
    # is not hammered, and the retry in hyros_api still covers a throttle.
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(one, chunks)) if chunks else []
    for code, d in results:
        if code != 200 or not isinstance(d, dict):
            raise SystemExit(f"attribution {level} failed: HTTP {code} {str(d)[:200]}")
        for r in d.get("result") or []:
            out[r["id"]] = {
                "spend": round(float(r.get("cost") or 0), 2),
                "clicks": int(r.get("clicks") or 0),
                "impressions": int(r.get("impressions") or 0),
                "leads": int(r.get("leads") or 0),
                "sales_lastclick": int(r.get("sales") or 0),
                "revenue_lastclick": float(r.get("revenue") or 0),
            }
    return out


def tagged_leads(first_day, today):
    """All tagged leads, counted per ad and per ad per calendar day.

    creationDate comes back in the account's own timezone, so its first ten
    characters are the local day the registration landed. That per-day split is
    what makes "Just Today" a real day figure instead of a copy of the running
    total.

    The campaign names come back too. They are how a campaign nobody named still
    earns its place on the board, and how an ad found holding leads can be told
    apart from one that belongs to a campaign kept off it deliberately.
    """
    # Pulled in three-hour slices of creation time, side by side, rather than down one
    # cursor (five minutes at 60,000 leads). The outer slices run from 2000 to 2099, so a
    # lead created before the summit and tagged since still comes back, as it did when
    # the pull had no dates at all.
    lo = dt.datetime.fromisoformat(first_day)
    hi = dt.datetime.fromisoformat(today) + dt.timedelta(days=1)
    wins = windows(dt.datetime(2000, 1, 1), dt.datetime(2099, 12, 31, 23, 59, 59), 3,
                   _RUN, grid_from=lo, grid_to=hi)
    rows = paged_windows("leads", {"pageSize": 250, "tags": TAG}, wins)
    per_ad, per_campaign_name, paid = collections.Counter(), collections.Counter(), 0
    per_day = collections.defaultdict(collections.Counter)
    ad_campaign = {}
    for L in rows:
        ls = L.get("lastSource") or {}
        sla = ls.get("sourceLinkAd")
        if sla and sla.get("adSourceId"):
            per_ad[sla["adSourceId"]] += 1
            paid += 1
            per_day[str(L.get("creationDate") or "")[:10]][sla["adSourceId"]] += 1
            camp = ((ls.get("category") or {}).get("name")) or ""
            per_campaign_name[camp] += 1
            ad_campaign.setdefault(sla["adSourceId"], camp)
    return rows, per_ad, per_day, paid, set(per_campaign_name), ad_campaign


def tagged_sales(start, end, summit_ads):
    """Sales in the window whose lead carries the tag, credited to the summit ad touch."""
    # One window per day, side by side: 4s against 29s down a single cursor. The same
    # sales as the date-only start..end query (checked 2026-09-18); the day a sale is
    # filed under still comes from its own timestamp in _norm_date, never its window.
    lo = dt.datetime.fromisoformat(start)
    hi = dt.datetime.fromisoformat(end).replace(hour=23, minute=59, second=59)
    rows = paged_windows("sales", {"pageSize": 250}, windows(lo, hi, 24, _RUN))
    ledger, per_ad = [], collections.Counter()
    rev_per_ad = collections.Counter()
    for s in rows:
        lead = s.get("lead") or {}
        if TAG not in (lead.get("tags") or []):
            continue
        amount = float((s.get("usdPrice") or s.get("price") or {}).get("price") or 0)
        credit_ad, credit_name = None, ""
        for which in ("firstSource", "lastSource"):
            sla = ((s.get(which) or {}).get("sourceLinkAd")) or {}
            aid = sla.get("adSourceId")
            if aid and aid in summit_ads:
                credit_ad, credit_name = aid, sla.get("name") or ""
                break
        if credit_ad:
            per_ad[credit_ad] += 1
            rev_per_ad[credit_ad] += amount
        name = f"{lead.get('firstName','')} {lead.get('lastName','')}".strip() or "unknown"
        last_name = ((s.get("lastSource") or {}).get("name")) or ""
        if credit_ad and last_name and "sourceLinkAd" not in str(s.get("lastSource") or {}):
            why = f"Reached the offer through {credit_name}, which gets the credit."
        elif credit_ad:
            why = f"Closed on {credit_name}, which gets the credit."
        else:
            why = ("No Facebook ad touch anywhere on this buyer, so it is organic and is "
                   "excluded from every figure on this page.")
        ledger.append({
            "sale_id": (s.get("id") or "")[:16], "date": _norm_date(s.get("creationDate")),
            "amount": round(amount, 2), "lead": name, "counted": bool(credit_ad),
            "classification": why, "campaign": summit_ads.get(credit_ad, {}).get("campaign", "n/a"),
            "ad": credit_name or "n/a",
            "refunded": bool(s.get("refundDate")), "recurring": bool(s.get("recurring")),
        })
    return ledger, per_ad, rev_per_ad


def _norm_date(v):
    """The calendar day a sale happened, in the ad account's timezone.

    Hyros hands back two different formats and only one of them carries an offset.
    Leads look like 2026-09-09T08:26:23-07:00, already account-local. Sales look like
    "Wed Sep 09 00:05:58 UTC 2026", which is UTC. Reading the date off that string
    filed every sale after 5pm Pacific on the following day: on 2026-09-09 that moved
    17 of yesterday evening's sales into today, so "Just Today" read 81 sales where
    Hyros itself showed 65. Spend and leads were always bucketed account-local, so the
    day rows disagreed with each other as well as with Hyros.
    """
    if not v:
        return ""
    from zoneinfo import ZoneInfo
    text = str(v)
    for f in ("%a %b %d %H:%M:%S UTC %Y", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            stamp = dt.datetime.strptime(text, f)
        except ValueError:
            continue
        if stamp.tzinfo is None:                      # the UTC form says so in words
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return stamp.astimezone(ZoneInfo(ACCOUNT_TZ)).strftime("%Y-%m-%d")
    try:                                              # a bare date is already a day
        return dt.datetime.strptime(text[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return text[:10]


# Hyros reports in the account's own timezone and rejects a date that is still in its
# future. GitHub's runners are UTC, so after 17:00 Pacific "today" on the runner is
# already tomorrow in Hyros and every attribution call 400s. Always ask the account.
ACCOUNT_TZ = "America/Los_Angeles"


def account_now():
    """Wall clock in the account's timezone, labelled.

    pulled_at used to be dt.datetime.now(), i.e. the builder's local clock: UTC when
    GitHub built the page, Eastern when the button did. The same field meant different
    things on the two copies, and neither matched the Pacific day the figures are
    scoped to. Stamping it in the account timezone makes it mean one thing everywhere.
    """
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(ACCOUNT_TZ)).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def account_stamp(utc_iso):
    """'2026-09-17T15:35:27' (UTC, as Windsor reports it) -> '2026-09-17 08:35 PDT'."""
    if not utc_iso:
        return ""
    try:
        from zoneinfo import ZoneInfo
        t = dt.datetime.fromisoformat(str(utc_iso).rstrip("Z")).replace(tzinfo=dt.timezone.utc)
        return t.astimezone(ZoneInfo(ACCOUNT_TZ)).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return f"{str(utc_iso)[:16].replace('T', ' ')} UTC"


def account_today():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo(ACCOUNT_TZ)).date().isoformat()
    except Exception:
        # No tz database (slim containers): fall back to UTC minus the Pacific offset.
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)).date().isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="defaults to today in the Hyros account timezone")
    args = ap.parse_args()
    today = args.date or account_today()
    cap = account_today()
    if today > cap:
        print(f"  {today} is in the future for the account; using {cap}")
        today = cap

    prev = {}
    p = DATA / "dashboard_data.json"
    if p.exists():
        prev = json.loads(p.read_text())
    cfg_p = HERE / "config.json"
    cfg = json.loads(cfg_p.read_text()) if cfg_p.exists() else {}
    # config.json is committed and carries no personal data, so a fresh CI checkout still
    # knows when the campaign window opened. dashboard_data.json is never committed.
    first_day = cfg.get("first_spend_day") or (prev.get("meta") or {}).get("first_spend_day") or today

    # Leads are pulled BEFORE discovery so a campaign can earn its place by holding
    # them, not only by being named the way this summit's campaigns have been named
    # so far. Naming has already changed once here.
    print("pulling tagged leads, and the Hyros campaign lists alongside...")
    with cf.ThreadPoolExecutor(max_workers=1) as bg:
        f_disc = bg.submit(discovery_rows)
        lead_rows, leads_per_ad, leads_per_day, paid_raw, lead_campaigns, ad_campaign = \
            tagged_leads(first_day, today)
        disc_rows = f_disc.result()
    print(f"  {len(lead_rows)} tagged, {paid_raw} ad-attributed")

    print("discovering summit campaigns, ad sets and ads...")
    adsets, ads, slot_table, offboard = discover(disc_rows, lead_campaigns, first_day, today)
    added = [sl for sl, _g, is_auto in slot_table if is_auto]
    if added:
        print("  on the board automatically, not named in SLOTS: " + ", ".join(added))
    if offboard:
        print("  campaigns holding tagged leads but kept off the board: " + "; ".join(offboard))
    slots_found = sorted({v["slot"] for v in adsets.values()})
    print(f"  {len(adsets)} ad sets, {len(ads)} ads across {len(slots_found)} slots: {', '.join(slots_found)}")
    if not adsets:
        raise SystemExit("No summit ad sets found. Campaign naming may have changed.")

    print("pulling sales...")
    ledger, sales_per_ad, rev_per_ad = tagged_sales(first_day, today, ads)
    print(f"  {len(ledger)} tagged sales, ${sum(l['amount'] for l in ledger):,.2f}")

    # An ad holding tagged leads that is not on the board is one of two things. If its
    # campaign is one EXCLUDE or HIDDEN keeps off deliberately, its leads come out of the
    # paid base and are reported on the page. Anything else is a campaign that should have
    # been discovered and was not, which is how an ad holding 16 leads vanished on
    # 2026-09-07, so that still stops the run rather than quietly shrinking the totals.
    missing = sorted(set(leads_per_ad) - set(ads))
    kept_off = [m for m in missing
                if EXCLUDE.search(ad_campaign.get(m, "")) or HIDDEN.search(ad_campaign.get(m, ""))]
    unexplained = [m for m in missing if m not in kept_off]
    if unexplained:
        raise SystemExit("ads hold tagged leads but were not discovered: " + ", ".join(
            f"{m} ({leads_per_ad[m]} leads, {ad_campaign.get(m) or 'unknown campaign'})"
            for m in unexplained))
    offboard_detail = collections.Counter()
    for m in kept_off:
        offboard_detail[ad_campaign.get(m) or "unknown campaign"] += leads_per_ad[m]
    offboard_leads = sum(offboard_detail.values())
    paid = paid_raw - offboard_leads
    if offboard_leads:
        print(f"  {offboard_leads} tagged leads last touched an off-board campaign and are "
              "not counted: " + "; ".join(f"{k} ({v})" for k, v in offboard_detail.most_common()))

    # Every day row is rebuilt from its own pull, never appended and frozen. A row
    # written mid-afternoon is only a snapshot: on 2026-09-08 the stored 2026-09-07
    # row still read $80.82 while the settled day was $105.82, a quarter short. The
    # window also starts at the earliest ad-attributed lead, not at first_spend_day,
    # because a few tagged leads landed before Hyros recorded any spend and would
    # otherwise belong to no day at all.
    def day_row(day, attr):
        led = [s for s in ledger if s["date"] == day and s["counted"]]
        return {
            "date": day,
            "spend": round(sum(x.get("spend", 0.0) for x in attr.values()), 2),
            "clicks": sum(x.get("clicks", 0) for x in attr.values()),
            "impressions": sum(x.get("impressions", 0) for x in attr.values()),
            "leads": sum(n for aid, n in leads_per_day.get(day, {}).items() if aid in ads),
            "purchases": len(led),
            "revenue": round(sum(s["amount"] for s in led), 2),
        }

    lead_days = {d for d, per in leads_per_day.items()
                 if d and any(aid in ads for aid in per)}
    span_start = min([first_day] + sorted(lead_days))
    days = []
    cur = dt.date.fromisoformat(span_start)
    end = dt.date.fromisoformat(today)
    while cur <= end:
        days.append(cur.isoformat())
        cur += dt.timedelta(days=1)

    # One sweep, one moment. The running total is the sum of these day pulls rather
    # than a separate first_day..today call: pulling them independently meant the two
    # were measured minutes apart, and on a day accruing a few hundred dollars the
    # running total could read LOWER than today, which is what the live page showed.
    print(f"pulling {len(days)} day windows ({days[0]} to {days[-1]})...")
    daily, per_day_attr = [], {}
    ids_all = list(adsets)
    # Cost comes from Meta, which bills it, not from Hyros, which relays it late. If
    # Meta cannot be reached the run still completes on Hyros figures, but the page
    # says so rather than quietly showing numbers that will not match Ads Manager.
    cost_source, cost_note = "meta", ""
    try:
        from meta_cost import cost_by_adset_day
        print("pulling spend from Meta...")
        per_day_attr = cost_by_adset_day(ids_all, days[0], today)
        for d in days:
            per_day_attr.setdefault(d, {})
    except Exception as e:
        cost_source = "hyros"
        cost_note = f"{type(e).__name__}: {e}"
        # Reaching here means Graph failed AND Windsor.ai did too (or has no key):
        # meta_cost switches road by itself before giving up.
        print(f"  Meta unavailable ({cost_note}); falling back to Hyros cost, which lags")
        per_day_attr = {}
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            fut = {ex.submit(attribution, ids_all, "facebook_adset", d, d): d for d in days}
            for f in cf.as_completed(fut):
                per_day_attr[fut[f]] = f.result()
    for day in days:
        row = day_row(day, per_day_attr[day])
        if row["spend"] or row["clicks"] or row["leads"] or row["purchases"]:
            daily.append(row)
    today_row = next((r for r in daily if r["date"] == today),
                     day_row(today, per_day_attr.get(today, {})))

    # Ad level stays one full-window pull: per-day would be 12 batched calls per day.
    # Taken here, straight after the sweep, so the gap against the campaign rows is
    # seconds rather than minutes.
    print("pulling ad-level attribution...")
    cost_via, cost_via_note, cost_via_as_of = "", "", ""
    if cost_source == "meta":
        from meta_cost import cost_by_ad, ROUTE
        ad_run = cost_by_ad(list(ads), days[0], today)
        # Taken now, before the lead count and hidden-campaign check below: a failure
        # there must not relabel spend that was already read through Graph.
        cost_via, cost_via_note = ROUTE["via"], ROUTE["note"]
        if cost_via == "windsor":
            # When Windsor really fetched from Meta, which a cache hit would push back.
            cost_via_as_of = account_stamp(ROUTE["as_of"])
            print("  spend read through Windsor.ai (same Meta figures, Graph was unavailable), "
                  f"fetched from Meta {cost_via_as_of or 'at an unknown time'}")
    else:
        ad_run = attribution(list(ads), "facebook_ad", first_day, today)

    as_day = per_day_attr.get(today, {})
    as_run = {}
    for attr in per_day_attr.values():
        for aid, m in attr.items():
            acc = as_run.setdefault(aid, {"spend": 0.0, "clicks": 0, "impressions": 0, "leads": 0})
            acc["spend"] += m.get("spend", 0.0)
            acc["clicks"] += m.get("clicks", 0)
            acc["impressions"] += m.get("impressions", 0)
            acc["leads"] += m.get("leads", 0)
    for m in as_run.values():
        m["spend"] = round(m["spend"], 2)


    ad_rows = []
    for aid, meta in ads.items():
        m = ad_run.get(aid, {})
        ad_rows.append({**meta,
                        "spend": m.get("spend", 0.0), "clicks": m.get("clicks", 0),
                        "impressions": m.get("impressions", 0),
                        "leads": leads_per_ad.get(aid, 0),
                        "purchases": sales_per_ad.get(aid, 0),
                        "revenue": round(rev_per_ad.get(aid, 0.0), 2),
                        "leads_hyros": m.get("leads", 0)})

    camp_rows = []
    for slot, group, is_auto in slot_table:
        sets_here = [k for k, v in adsets.items() if v["slot"] == slot]
        agg = [as_run.get(k, {}) for k in sets_here]
        ads_here = [a for a in ad_rows if a["campaign"] == slot]
        spend = round(sum(x.get("spend", 0.0) for x in agg), 2)
        camp_rows.append({
            "slot": slot,
            "group": group,
            # True when nobody declared this campaign and it was picked up on its own.
            # The page says so, so an unexpected row reads as discovery, not as a bug.
            "auto": is_auto,
            "hyros_name": next((adsets[k]["campaign"] for k in sets_here), f"(no {slot} campaign found)"),
            # Three campaigns (General Hooks, Grid, Videos) run under the SAME name in
            # both accounts and are shown as one row. Taking the first ad set's account
            # labelled all three "TSA" and hid the STS half of the spend from the reader.
            "accounts": sorted({("TSA" if adsets[k]["adAccountId"] == "3014083142121289" else "STS")
                                for k in sets_here}) or ["STS"],
            "account": next((("TSA" if adsets[k]["adAccountId"] == "3014083142121289" else "STS")
                             for k in sets_here), "STS"),
            "spend_by_account": {
                acct: round(sum(as_run.get(k, {}).get("spend", 0.0) for k in sets_here
                                if (("TSA" if adsets[k]["adAccountId"] == "3014083142121289" else "STS")
                                    == acct)), 2)
                for acct in sorted({("TSA" if adsets[k]["adAccountId"] == "3014083142121289" else "STS")
                                    for k in sets_here})
            },
            "spend": spend,
            "clicks": sum(x.get("clicks", 0) for x in agg),
            "impressions": sum(x.get("impressions", 0) for x in agg),
            "leads": sum(a["leads"] for a in ads_here),
            "purchases": sum(a["purchases"] for a in ads_here),
            "revenue": round(sum(a["revenue"] for a in ads_here), 2),
            "adsets": len(sets_here),
            "live_adsets": sum(1 for k in sets_here if as_run.get(k, {}).get("spend", 0) > 0),
        })

    total_spend = round(sum(c["spend"] for c in camp_rows), 2)

    # A campaign kept off the sheet must not be quietly spending. Cheap to check while
    # we already have Meta open, and it is the only thing standing between "hidden" and
    # "missing".
    hidden_spend, meta_leads = 0.0, 0
    if cost_source == "meta":
        try:
            from meta_cost import reported_leads
            meta_leads = reported_leads(list(adsets), days[0], today)
        except Exception:
            meta_leads = 0
        try:
            from meta_cost import campaign_spend
            for name, sp in campaign_spend("Fall Summit 2026", days[0], today).items():
                if HIDDEN.search(name) and sp > 0:
                    hidden_spend += sp
                    print(f"  WARNING: hidden campaign is spending: {name} ${sp:,.2f}")
        except Exception as e:
            print(f"  could not check hidden campaigns ({type(e).__name__})")
    # Organic: the buyer never touched a Fall Summit ad, so the sale is not the ads'
    # doing. STS runs organic traffic to the same offer, and those sales carry the tag
    # like any other. Excluded from every figure, kept in the ledger marked Excluded so
    # the money is visible rather than vanished.
    excluded = [s for s in ledger if not s["counted"]]

    # Persist the raw pull NEXT TO the derived file, not eighteen seconds earlier.
    # build_dashboard.py cross-checks one against the other, so a build starting inside
    # that gap saw the new raw leads against the old totals and hard-failed on a
    # mismatch that did not exist. Same pull, same moment.
    DATA.mkdir(exist_ok=True)
    (DATA / "raw_leads_summit2026.json").write_text(
        json.dumps({"result": lead_rows, "nextPageId": None}))

    daily_p = DATA / "daily.json"
    daily_p.write_text(json.dumps(daily, indent=2))

    # The day rows should add back up to the running total. Attribution restates a
    # little as Hyros settles, so this is recorded, not enforced.
    daily_spend_sum = round(sum(d["spend"] for d in daily), 2)

    meta = dict(prev.get("meta") or {})
    meta.update({
        "pulled_at": account_now(),
        "source": "Hyros REST API (School of Traditional Skills account)",
        "tag_filter": TAG, "attribution_model": "LAST_CLICK",
        "source_configuration": "ALL_SOURCES",
        "window_start": first_day, "window_end": today, "first_spend_day": first_day,
        "today": today,
        "note_running_equals_today": today == first_day,
        "cost_source": cost_source,
        "cost_source_note": cost_note,
        # "graph" or "windsor" when cost_source is meta. Both are Meta's billed figures;
        # Windsor.ai is the second road to them when Meta's own API refuses the token.
        "cost_via": cost_via,
        "cost_via_note": cost_via_note,
        "cost_via_as_of": cost_via_as_of,
        "cost_source_label": (("Meta Ads via Windsor.ai (spend, link clicks, impressions)"
                               if cost_via == "windsor" else
                               "Meta Ads (spend, link clicks, impressions)") if cost_source == "meta"
                              else "Hyros relayed cost - Meta was unreachable, so these lag"),
        "hidden_campaign_spend": hidden_spend,
        "meta_reported_leads": meta_leads,
        "daily_spend_sum": daily_spend_sum,
        "daily_vs_running_spend_gap": round(total_spend - daily_spend_sum, 2),
        "tagged_leads_total": len(lead_rows), "tagged_leads_paid": paid,
        "tagged_leads_organic_or_direct": len(lead_rows) - paid_raw,
        # Campaigns picked up without being declared, and tagged leads whose last ad
        # touch was a campaign this board deliberately does not count.
        "auto_discovered_campaigns": added,
        "offboard_tagged_leads": offboard_leads,
        "offboard_tagged_lead_campaigns": dict(offboard_detail),
        "hyros_report_leads_on_summit_adsets": sum(x.get("leads", 0) for x in as_run.values()),
        "ad_level_spend_sum": round(sum(a["spend"] for a in ad_rows), 2),
        # Renamed from uncredited_*: those were ADDED to the headline, these are removed
        # from it. A stale reader keying on the old name would silently restore them.
        "excluded_organic_purchases": len(excluded),
        "excluded_organic_revenue": round(sum(s["amount"] for s in excluded), 2),
        "ad_level_leads_are_tag_filtered": True,
        "sales_credit_rule": ("A sale counts only where the buyer has a Fall Summit ad touch, "
                              "credited to that ad whatever click closed it. Tagged sales with no "
                              "ad touch are organic and are excluded."),
        "attribution_model_note": ("Sales are credited on summit-tag membership. Spend, clicks and "
                                   "impressions are the last-click platform figures for the summit campaigns."),
        "ad_accounts": (prev.get("meta") or {}).get("ad_accounts") or [
            {"id": "1246959949464564", "name": "School of Traditional Skills Ad Account", "short": "STS"},
            {"id": "3014083142121289", "name": "Traditional Skills Academy", "short": "TSA"}],
    })

    out = {"meta": meta, "campaigns": camp_rows, "ads": ad_rows,
           "purchase_ledger": sorted(ledger, key=lambda s: (s["date"], s["amount"]), reverse=True),
           "today": today_row, "daily": daily}
    DATA.mkdir(exist_ok=True)
    p.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {p.relative_to(HERE)}")
    print(f"  today   spend ${today_row['spend']}  leads {today_row['leads']}  "
          f"clicks {today_row['clicks']}  purchases {today_row['purchases']}  "
          f"revenue ${today_row['revenue']}")
    print(f"  running spend ${total_spend}  leads {sum(c['leads'] for c in camp_rows)}  "
          f"clicks {sum(c['clicks'] for c in camp_rows)}  "
          f"purchases {sum(c['purchases'] for c in camp_rows)}  "
          f"revenue ${round(sum(c['revenue'] for c in camp_rows), 2)}")


if __name__ == "__main__":
    main()
