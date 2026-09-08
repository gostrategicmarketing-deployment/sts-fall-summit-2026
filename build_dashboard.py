#!/usr/bin/env python3
"""
Fall Summit 2026 dashboard renderer.

Reads data/dashboard_data.json (pulled from the Hyros MCP, tag-scoped to
!summit-2026) and writes fall-summit-2026-dashboard.html.

Every derived figure (CPL, CPC, page CVR, cost per purchase, ROAS) is computed
here, never hand-typed. Re-run after refreshing the data file.
"""
import json, html, datetime, pathlib

import sys
ROOT = pathlib.Path(__file__).parent
REDACT   = "--redact" in sys.argv          # anonymise the purchase ledger for public hosting
# Creatives ship as files next to the page by default. Inlining them as base64 made the
# document 6.4 MB, 99% of it images that never change, and the browser re-downloaded the
# lot on every refresh. As files they are fetched once and cached. --inline-images
# restores the self-contained build for anywhere that cannot load side files.
INLINE   = "--inline-images" in sys.argv
OUT_NAME = "fall-summit-2026-dashboard.html"
if "--out" in sys.argv:
    OUT_NAME = sys.argv[sys.argv.index("--out") + 1]
D = json.loads((ROOT / "data" / "dashboard_data.json").read_text())
_af = ROOT / "data" / "creative_assets.json"
ASSETS = json.loads(_af.read_text()) if _af.exists() else {}

IMG_DIR = ROOT / "creatives"


def _export_images():
    """Write each creative beside the page and return {ad_id: relative src}."""
    import base64 as _b64
    srcs = {}
    if INLINE:
        return {k: v["data_uri"] for k, v in ASSETS.items()}
    IMG_DIR.mkdir(exist_ok=True)
    for aid, a in ASSETS.items():
        uri = a.get("data_uri") or ""
        if "," not in uri:
            continue
        f = IMG_DIR / f"{aid}.jpg"
        raw = _b64.b64decode(uri.split(",", 1)[1])
        if not f.exists() or f.read_bytes() != raw:
            f.write_bytes(raw)
        srcs[aid] = f"creatives/{aid}.jpg"
    return srcs


IMG_SRC = _export_images()
M, CAMPS, ADS, LEDGER, DAILY = D["meta"], D["campaigns"], D["ads"], D["purchase_ledger"], D["daily"]

CAMPAIGN_ORDER = ["Page 1", "Page 2", "Page 3", "Page 4", "Page 5", "General Hooks", "Grid", "Video"]

if REDACT:
    # Buyer identities never leave the private artifact. Every metric, date and amount is untouched;
    # only the person's name is replaced, and staff test accounts collapse to one label.
    _n = 0; _seen = {}
    for _s in sorted(D["purchase_ledger"], key=lambda x: (x["date"], x["sale_id"])):
        lead = _s["lead"]
        if "@schooloftraditionalskills.com" in lead:
            _seen[lead] = "STS staff (test)"
        elif lead not in _seen:
            _n += 1
            _seen[lead] = f"Customer {_n}"
    for _s in D["purchase_ledger"]:
        _s["lead"] = _seen[_s["lead"]]

# ---------- helpers ----------
def esc(s): return html.escape(str(s))
def money(v, dp=2): return f"${v:,.{dp}f}"
def num(v): return f"{v:,}"
def div(a, b): return (a / b) if b else None
def fmt(v, kind):
    if v is None: return '<span class="nil">no data</span>'
    if kind == "money":  return money(v)
    if kind == "pct":    return f"{v*100:.1f}%"
    if kind == "x":      return f"{v:.2f}x"
    return num(v)

def block(rows):
    """Roll a set of rows into one metric bundle with all derived rates."""
    s   = round(sum(r["spend"] for r in rows), 2)
    clk = sum(r["clicks"] for r in rows)
    imp = sum(r["impressions"] for r in rows)
    ld  = sum(r["leads"] for r in rows)
    pur = sum(r["purchases"] for r in rows)
    rev = round(sum(r["revenue"] for r in rows), 2)
    return dict(spend=s, clicks=clk, impressions=imp, leads=ld, purchases=pur, revenue=rev,
                cpl=div(s, ld), cpc=div(s, clk), cvr=div(ld, clk),
                cpp=div(s, pur), roas=div(rev, s), ctr=div(clk, imp),
                cpm=div(s, imp) * 1000 if imp else None)

TOTAL = block(CAMPS)
# A tagged sale with no summit ad touch counts toward the headline but sits in no
# campaign, so fold it back in here and recompute what depends on it. Without this the
# headline silently under-reported the tag's own purchases.
_unc_n = int(M.get("uncredited_purchases", 0) or 0)
_unc_rev = float(M.get("uncredited_revenue", 0) or 0)
if _unc_n or _unc_rev:
    TOTAL["purchases"] += _unc_n
    TOTAL["revenue"] = round(TOTAL["revenue"] + _unc_rev, 2)
    TOTAL["cpp"] = div(TOTAL["spend"], TOTAL["purchases"])
    TOTAL["roas"] = div(TOTAL["revenue"], TOTAL["spend"])

# "Just Today" is its own window, pulled separately by pull.py. It is never derived
# from TOTAL: rendering one block into both decks is exactly what made the two read
# identical on 2026-09-08. A data file without it is stale, so say so and stop.
if "today" not in D:
    raise SystemExit("dashboard_data.json has no `today` block. Re-run pull.py, "
                     "which pulls the day window separately from the running total.")
TODAY = block([D["today"]])

# ---------- reconciliation ----------
# Ad-level tagged leads must add up to every campaign row and to the paid-attributed
# total. Fail loudly rather than render a page whose rows disagree with each other.
_paid = M["tagged_leads_paid"]
if sum(a["leads"] for a in ADS) != _paid:
    raise SystemExit(f"ad leads {sum(a['leads'] for a in ADS)} != tagged_leads_paid {_paid}")
if TOTAL["leads"] != _paid:
    raise SystemExit(f"campaign leads {TOTAL['leads']} != tagged_leads_paid {_paid}")
for _c in CAMPS:
    _s = sum(a["leads"] for a in ADS if a["campaign"] == _c["slot"])
    if _s != _c["leads"]:
        raise SystemExit(f'{_c["slot"]}: ad leads {_s} != campaign leads {_c["leads"]}')
# Every ad carrying a !summit-2026 lead must exist in the ads table. An ad dropped from
# the dataset while it still holds leads makes the ad rows disagree with the campaign rows,
# which happened on 2026-09-07. Checked against the raw lead pull, which is the source of truth.
_rawp = ROOT / "data" / "raw_leads_summit2026.json"
if _rawp.exists():
    import collections as _c
    _seen = _c.Counter()
    for _L in json.loads(_rawp.read_text())["result"]:
        _sla = (_L.get("lastSource") or {}).get("sourceLinkAd")
        if _sla: _seen[_sla["adSourceId"]] += 1
    _absent = sorted(set(_seen) - {a["id"] for a in ADS})
    if _absent:
        raise SystemExit("ads hold tagged leads but are absent from the dataset: "
                         + ", ".join(f"{i} ({_seen[i]} leads)" for i in _absent))
    if sum(_seen.values()) != _paid:
        raise SystemExit(f"raw lead pull has {sum(_seen.values())} ad-attributed leads "
                         f"but meta.tagged_leads_paid says {_paid}")

_counted = [s for s in LEDGER if s["counted"]]
if TOTAL["purchases"] != len(_counted):
    raise SystemExit(f'campaign purchases {TOTAL["purchases"]} != ledger {len(_counted)}')
if round(TOTAL["revenue"], 2) != round(sum(s["amount"] for s in _counted), 2):
    raise SystemExit("campaign revenue != ledger revenue")

# Ad rankings: leads first (this is a registration funnel), cheapest lead breaks ties.
def rank(ads): return sorted(ads, key=lambda a: (-a["leads"], a["spend"] if a["leads"] else 1e9, -a["impressions"]))
IMAGE_ADS = rank([a for a in ADS if a["type"] == "image"])[:10]
VIDEO_ADS = rank([a for a in ADS if a["type"] == "video"])[:3]
HERO_ADS  = sorted([a for a in ADS if a["purchases"] > 0], key=lambda a: -a["revenue"])

CAMPS_BY_SLOT = {c["slot"]: c for c in CAMPS}
MAX_CAMP_SPEND = max([c["spend"] for c in CAMPS] + [0.01])
MAX_CAMP_LEADS = max([c["leads"] for c in CAMPS] + [1])
MAX_AD_LEADS   = max([a["leads"] for a in ADS] + [1])

# ---------- fragments ----------
def kpi(label, value, sub=""):
    sub = f'<span class="kpi-sub">{sub}</span>' if sub else ""
    return f'<div class="kpi"><span class="kpi-lab">{esc(label)}</span><span class="kpi-val">{value}</span>{sub}</div>'

def deck(b):
    return "".join([
        kpi("Leads",            fmt(b["leads"], "n"),   "registrations tagged !summit-2026"),
        kpi("Cost per lead",    fmt(b["cpl"], "money")),
        kpi("Link clicks",      fmt(b["clicks"], "n")),
        kpi("Cost per click",   fmt(b["cpc"], "money")),
        kpi("Spend",            fmt(b["spend"], "money"), "both ad accounts"),
        kpi("Page conversion",  fmt(b["cvr"], "pct"),   "leads &divide; link clicks"),
        kpi("Purchases",        fmt(b["purchases"], "n")),
        kpi("Revenue",          fmt(b["revenue"], "money")),
        kpi("Cost per purchase",fmt(b["cpp"], "money")),
        kpi("ROAS",             fmt(b["roas"], "x"),    "break-even at 1.00x"),
    ])

def thumb(ad):
    """Creative preview: click to enlarge, with a durable link out to Ads Manager."""
    a = ASSETS.get(ad["id"])
    if not a:
        return ('<span class="thumb thumb-missing" aria-hidden="true">'
                '<span>no<br>preview</span></span>')
    label = esc(short_name(ad["name"]))
    play = '<span class="thumb-play" aria-hidden="true">&#9654;</span>' if a["kind"] == "video" else ""
    src = IMG_SRC.get(ad["id"], a.get("data_uri", ""))
    return (f'<button type="button" class="thumb" data-full="{src}" '
            f'data-name="{label}" data-manage="{esc(a["ads_manager_url"])}" '
            f'data-watch="{esc(a.get("watch_url",""))}" '
            f'data-dims="{a["orig_w"]} &times; {a["orig_h"]}" '
            f'aria-label="Enlarge creative: {label}">'
            f'<img src="{src}" alt="{label}" loading="lazy" '
            f'width="64" height="64" decoding="async">{play}</button>')

def bar(value, maximum, tone):
    pct = min(100, (value / maximum * 100) if maximum else 0)
    return f'<span class="bar bar-{tone}"><span style="width:{pct:.1f}%"></span></span>'

def campaign_rows():
    out = []
    for slot in CAMPAIGN_ORDER:
        c = CAMPS_BY_SLOT[slot]
        b = block([c])
        live = c["spend"] > 0 or c["leads"] > 0
        pill = ('<span class="pill pill-live">Delivering</span>' if live
                else '<span class="pill pill-idle">Not delivering</span>')
        adsets = f'{c["live_adsets"]} of {c["adsets"]} ad sets live'
        out.append(f"""<tr class="{'' if live else 'row-idle'}">
  <th scope="row">
    <span class="slot">{esc(slot)}</span>
    <span class="cname">{esc(c["hyros_name"])}</span>
    <span class="cmeta"><span class="acct acct-{c['account'].lower()}">{esc(c["account"])}</span>{esc(adsets)}</span>
  </th>
  <td class="st">{pill}</td>
  <td class="n">{money(c["spend"])}{bar(c["spend"], MAX_CAMP_SPEND, "spend")}</td>
  <td class="n">{num(c["leads"])}{bar(c["leads"], MAX_CAMP_LEADS, "leads")}</td>
  <td class="n">{fmt(b["cpl"], "money")}</td>
  <td class="n">{num(c["clicks"])}</td>
  <td class="n">{fmt(b["cpc"], "money")}</td>
  <td class="n">{fmt(b["cvr"], "pct")}</td>
  <td class="n">{num(c["purchases"])}</td>
  <td class="n">{money(c["revenue"])}</td>
  <td class="n">{fmt(b["cpp"], "money")}</td>
  <td class="n hi">{fmt(b["roas"], "x")}</td>
</tr>""")
    t = TOTAL
    out.append(f"""<tr class="row-total">
  <th scope="row"><span class="slot">All campaigns</span><span class="cmeta">{len(CAMPAIGN_ORDER)} campaigns, both accounts</span></th>
  <td class="st"></td>
  <td class="n">{money(t["spend"])}</td>
  <td class="n">{num(t["leads"])}</td>
  <td class="n">{fmt(t["cpl"], "money")}</td>
  <td class="n">{num(t["clicks"])}</td>
  <td class="n">{fmt(t["cpc"], "money")}</td>
  <td class="n">{fmt(t["cvr"], "pct")}</td>
  <td class="n">{num(t["purchases"])}</td>
  <td class="n">{money(t["revenue"])}</td>
  <td class="n">{fmt(t["cpp"], "money")}</td>
  <td class="n hi">{fmt(t["roas"], "x")}</td>
</tr>""")
    return "\n".join(out)

def seqid_chip(name):
    for part in name.split("|"):
        p = part.strip()
        if p.startswith("sts") and "_" in p:
            return f'<code class="seq">{esc(p.split("_")[0])}</code>'
    return ""

def short_name(name):
    parts = [p.strip() for p in name.split("|")]
    parts = [p for p in parts if not (p.startswith("sts") and "_" in p)]
    return " | ".join(parts)

def ad_rows(ads, empty_slots=0, kind="image"):
    out = []
    for i, a in enumerate(ads, 1):
        b = block([a])
        flags = ""
        if a["purchases"]:
            flags += '<span class="flag flag-buy">Produced a purchase</span>'
        if a["spend"] == 0 and a["leads"] > 0:
            flags += '<span class="flag flag-note">No recorded spend: leads landed on this ad while budget sat on the duplicated ad set</span>'
        if b["cvr"] is not None and b["cvr"] > 1:
            flags += '<span class="flag flag-note">More leads than platform-counted link clicks</span>'
        asset = ASSETS.get(a["id"])
        links = []
        if asset and asset.get("watch_url"):
            links.append(f'<a class="xlink" href="{esc(asset["watch_url"])}" target="_blank" rel="noopener">Watch video</a>')
        if asset:
            links.append(f'<a class="xlink" href="{esc(asset["ads_manager_url"])}" target="_blank" rel="noopener">Ads Manager</a>')
        out.append(f"""<tr>
  <td class="rk">{i}</td>
  <td class="tc">{thumb(a)}</td>
  <th scope="row">
    <span class="adname">{esc(short_name(a["name"]))}</span>
    <span class="cmeta">{seqid_chip(a["name"])}<span class="acct acct-{a['account'].lower()}">{esc(a["account"])}</span>{esc(a["campaign"])}<span class="sep">&middot;</span>{esc(a["adset"])}</span>
    {f'<span class="flags">{flags}</span>' if flags else ''}
    {f'<span class="xlinks">{"".join(links)}</span>' if links else ''}
  </th>
  <td class="n">{num(a["leads"])}{bar(a["leads"], MAX_AD_LEADS, "leads")}</td>
  <td class="n">{fmt(b["cpl"], "money")}</td>
  <td class="n">{money(a["spend"])}</td>
  <td class="n">{num(a["clicks"])}</td>
  <td class="n">{fmt(b["cvr"], "pct")}</td>
  <td class="n">{num(a["impressions"])}</td>
  <td class="n">{num(a["purchases"])}</td>
  <td class="n">{money(a["revenue"])}</td>
</tr>""")
    for j in range(empty_slots):
        n = len(ads)
        noun = f'{kind} ad' + ('' if n == 1 else 's')
        verb = 'exists' if n == 1 else 'exist'
        out.append(f'<tr class="row-empty"><td class="rk">{n+j+1}</td><td class="tc"></td>'
                   f'<th scope="row" colspan="9"><span class="adname nil">Slot open: only {n} '
                   f'{noun} {verb} in the summit campaigns so far</span></th></tr>')
    return "\n".join(out)

def funnel():
    t = TOTAL
    steps = [("Impressions", t["impressions"], None, None),
             ("Link clicks", t["clicks"], t["ctr"], "click-through"),
             ("Leads", t["leads"], t["cvr"], "page conversion"),
             ("Purchases", t["purchases"], div(t["purchases"], t["leads"]), "lead to sale")]
    out = []
    for i, (lab, val, rate, rname) in enumerate(steps):
        if i:
            out.append(f'<div class="fn-gap"><span class="fn-rate">{rate*100:.1f}%</span>'
                       f'<span class="fn-rname">{esc(rname)}</span></div>')
        out.append(f'<div class="fn-step"><span class="fn-val">{num(val)}</span>'
                   f'<span class="fn-lab">{esc(lab)}</span></div>')
    return "".join(out)

def ledger_rows():
    out = []
    for s in LEDGER:
        counted = s["counted"]
        state = ('<span class="pill pill-live">Counted</span>' if counted
                 else '<span class="pill pill-out">Excluded</span>')
        tags = ""
        if s["refunded"]: tags += '<span class="flag flag-out">Refunded</span>'
        if s["recurring"]: tags += '<span class="flag flag-note">Recurring rebill</span>'
        out.append(f"""<tr class="{'' if counted else 'row-idle'}">
  <td class="n mono">{esc(s["date"])}</td>
  <td class="n">{money(s["amount"])}</td>
  <td>{esc(s["lead"])}</td>
  <td class="st">{state}</td>
  <td class="why">{esc(s["classification"])}{f'<span class="flags">{tags}</span>' if tags else ''}</td>
</tr>""")
    return "\n".join(out)

def daily_rows():
    out = []
    for d in DAILY:
        b = block([d])
        out.append(f"""<tr>
  <td class="n mono">{esc(d["date"])}</td>
  <td class="n">{money(d["spend"])}</td>
  <td class="n">{num(d["clicks"])}</td>
  <td class="n">{num(d["leads"])}</td>
  <td class="n">{fmt(b["cpl"], "money")}</td>
  <td class="n">{fmt(b["cvr"], "pct")}</td>
  <td class="n">{num(d["purchases"])}</td>
  <td class="n">{money(d["revenue"])}</td>
  <td class="n hi">{fmt(b["roas"], "x")}</td>
</tr>""")
    return "\n".join(out)

hero_html = ""
if HERO_ADS:
    n = len(HERO_ADS)
    eyebrow = ("The only creative that has produced a sale" if n == 1
               else f"The {n} creatives that have produced a sale")
    cards = []
    for a in HERO_ADS:
        hb = block([a])
        leadword = "lead" if a["leads"] == 1 else "leads"
        cards.append(f"""<div class="hero-ad">
  <div class="hero-body">
  {thumb(a)}
  <div class="hero-copy">
  <p class="hero-name">{esc(short_name(a["name"]))}</p>
  <p class="hero-meta">{seqid_chip(a["name"])}<span class="acct acct-{a['account'].lower()}">{esc(a["account"])}</span>{esc(a["campaign"])} campaign<span class="sep">&middot;</span>{esc(a["adset"])}</p>
  <div class="hero-stats">
    <span><b>{money(a["revenue"])}</b>revenue</span>
    <span><b>{money(a["spend"])}</b>spend</span>
    <span><b>{fmt(hb['roas'],'x')}</b>ROAS</span>
    <span><b>{num(a["leads"])}</b>{leadword}</span>
  </div>
  </div></div>
</div>""")
    hero_html = (f'<div class="hero-wrap"><span class="hero-eyebrow">{esc(eyebrow)}</span>'
                 f'<div class="hero-grid">{"".join(cards)}</div></div>')

accounts = "".join(
    f'<li><span class="acct acct-{a["short"].lower()}">{esc(a["short"])}</span>{esc(a["name"])}'
    f'<code class="seq">{esc(a["id"])}</code></li>' for a in M["ad_accounts"])

def _prose_list(names):
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]

_idle = [c["slot"] for c in CAMPS if c["spend"] <= 0]
_idle_sentence = ""
if _idle:
    _verb = "has" if len(_idle) == 1 else "have"
    _noun = "campaign" if len(_idle) == 1 else "campaigns"
    _idle_sentence = (f" {len(_idle)} of the {len(CAMPS)} {_noun} {_verb} ad sets built but no "
                      f"delivery yet: {esc(_prose_list(_idle))}.")

if M.get("note_running_equals_today"):
    notice_html = (f'<b>Day one.</b> The first Fall Summit spend Hyros recorded is '
                   f'{esc(M["first_spend_day"])}, so <b>Just Today and Running Total are the same '
                   f'numbers today</b>. They separate from tomorrow onward.{_idle_sentence}')
else:
    # Days of DELIVERY, not rows: daily.json also carries lead-only days from before
    # any spend was recorded, and counting those overstated the campaign's age.
    _delivery_days = sum(1 for d in DAILY if d.get("spend", 0) > 0)
    notice_html = (f'<b>Day {_delivery_days} of delivery.</b> <b>Just Today</b> is '
                   f'{esc(M["window_end"])} on its own, midnight to now in the account time zone. '
                   f'<b>Running Total</b> covers {esc(M["first_spend_day"])} to '
                   f'{esc(M["window_end"])}, every day the Fall Summit campaigns have '
                   f'run.{_idle_sentence}')

NOINDEX = ('<meta name="robots" content="noindex, nofollow">\n' if REDACT else "")

# The published copy is served statically: there is nothing for a Refresh button to
# POST to, so it is not rendered there at all rather than shipped dead.
WORKFLOW_URL = ("https://github.com/gostrategicmarketing-deployment/"
                "sts-fall-summit-2026/actions/workflows/refresh.yml")

# Two different controls, because the two copies can do different things.
# Locally serve.py can re-pull Hyros, so the button does the work in place.
# The published copy is static: nothing there can hold a credential or run a pull, and
# GitHub will not dispatch a workflow unauthenticated. So rather than a dead button, it
# links to the run page, where one click on "Run workflow" rebuilds this page. Labelled
# for what it is, so nobody expects an instant update.
REFRESH_UI = (
    f'<a class="refresh-btn refresh-link" href="{WORKFLOW_URL}" target="_blank" '
    f'rel="noopener" title="Opens GitHub Actions. Press Run workflow to rebuild this '
    f'page from Hyros; it takes about a minute.">'
    f'<span class="rb-dot"></span><span>Rebuild on GitHub</span>'
    f'<span class="rb-ext" aria-hidden="true">&#8599;</span></a>'
) if REDACT else (
    '<button type="button" id="refreshBtn" class="refresh-btn" hidden>'
    '<span class="rb-dot"></span><span class="rb-ring" aria-hidden="true"></span>'
    '<span id="rbLabel">Refresh</span></button>'
    '<span id="rbErr" class="rb-err" hidden></span>')

PAGE = f"""<meta charset="utf-8">
{NOINDEX}<title>Fall Summit 2026 Acquisition Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:ital,wght@0,400;0,500;0,600;0,700;1,400&family=IM+Fell+Great+Primer:ital@0;1&display=swap">
<style>
:root {{
  --sun:#E9B242; --sun-deep:#C8912A; --ink:#35383F; --clay:#B5B5B5;
  --stone:#F4F4F4; --milk:#FFFFFF;
  --good:#4A6741; --bad:#8B3A3A;
  --ground:var(--stone); --surface:var(--milk); --surface-2:#FAFAFA;
  --text:var(--ink); --text-2:#5E6169; --text-3:#8A8D95;
  --line:rgba(53,56,63,.14); --line-soft:rgba(53,56,63,.07);
  --deck-ink-bg:var(--ink); --deck-ink-text:#F7F5F1; --deck-ink-line:rgba(233,178,66,.28);
  --bar-spend:rgba(53,56,63,.30); --bar-leads:var(--sun);
  --shadow:0 1px 2px rgba(53,56,63,.06), 0 8px 24px -16px rgba(53,56,63,.30);
  --f-display:"IM Fell Great Primer", Georgia, "Times New Roman", serif;
  --f-body:"Archivo", "Helvetica Neue", Arial, sans-serif;
  --f-mono:ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#24262B; --surface:#2E313A; --surface-2:#343841;
    --text:#F0EFEC; --text-2:#B9BCC4; --text-3:#8E929B;
    --line:rgba(240,239,236,.16); --line-soft:rgba(240,239,236,.08);
    --deck-ink-bg:#191B1F; --deck-ink-text:#F7F5F1; --deck-ink-line:rgba(233,178,66,.34);
    --good:#7FA372; --bad:#CC7B7B; --sun-deep:#E9B242;
    --bar-spend:rgba(240,239,236,.26); --bar-leads:var(--sun);
    --shadow:0 1px 2px rgba(0,0,0,.30), 0 10px 28px -18px rgba(0,0,0,.75);
  }}
}}
:root[data-theme="dark"] {{
  --ground:#24262B; --surface:#2E313A; --surface-2:#343841;
  --text:#F0EFEC; --text-2:#B9BCC4; --text-3:#8E929B;
  --line:rgba(240,239,236,.16); --line-soft:rgba(240,239,236,.08);
  --deck-ink-bg:#191B1F; --deck-ink-text:#F7F5F1; --deck-ink-line:rgba(233,178,66,.34);
  --good:#7FA372; --bad:#CC7B7B; --sun-deep:#E9B242;
  --bar-spend:rgba(240,239,236,.26); --bar-leads:var(--sun);
  --shadow:0 1px 2px rgba(0,0,0,.30), 0 10px 28px -18px rgba(0,0,0,.75);
}}

*, *::before, *::after {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--ground); color:var(--text);
  font-family:var(--f-body); font-size:15px; line-height:1.55;
  -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:1220px; margin:0 auto; padding:34px 22px 76px; }}
h1,h2,h3 {{ font-family:var(--f-display); font-weight:400; text-wrap:balance; margin:0; letter-spacing:.01em; }}
:focus-visible {{ outline:2px solid var(--sun); outline-offset:2px; border-radius:2px; }}
@media (prefers-reduced-motion: reduce) {{ * {{ animation:none !important; transition:none !important; }} }}

/* ---- masthead ---- */
.mast {{ display:flex; flex-wrap:wrap; align-items:flex-end; justify-content:space-between;
  gap:20px; padding-bottom:20px; border-bottom:2px solid var(--ink); }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) .mast {{ border-bottom-color:var(--sun); }} }}
:root[data-theme="dark"] .mast {{ border-bottom-color:var(--sun); }}
.mast-eyebrow {{ font-size:11px; letter-spacing:.20em; text-transform:uppercase;
  color:var(--text-3); font-weight:600; display:block; margin-bottom:9px; }}
.mast h1 {{ font-size:clamp(30px,4.4vw,46px); line-height:1.06; }}
.mast h1 em {{ font-style:italic; color:var(--sun-deep); }}
.stamp {{ text-align:right; font-size:12px; color:var(--text-2); line-height:1.75; }}
.stamp b {{ display:block; font-size:13px; color:var(--text); font-weight:600; }}
.refresh-btn {{ display:inline-flex; align-items:center; justify-content:center; gap:10px;
  font-family:var(--f-body); font-size:15px; font-weight:700; letter-spacing:.02em;
  color:var(--ink); background:var(--sun); border:none; border-radius:4px;
  padding:13px 26px; min-width:210px; cursor:pointer; margin-bottom:12px;
  box-shadow:0 1px 2px rgba(53,56,63,.14); transition:filter .12s ease, transform .08s ease; }}
.refresh-btn:hover {{ filter:brightness(1.07); }}
.refresh-btn:active {{ transform:translateY(1px); }}
/* Author display wins over the UA [hidden] rule, so say it explicitly or a button the
   script never wires up still renders, full size and completely dead. */
.refresh-btn[hidden], .rb-err[hidden] {{ display:none !important; }}
.refresh-link {{ text-decoration:none; }}
.rb-ext {{ font-size:13px; opacity:.75; }}
.refresh-btn[disabled] {{ cursor:progress; filter:saturate(.45); }}
.rb-dot {{ width:9px; height:9px; border-radius:50%; background:var(--ink); flex:0 0 auto; }}
.refresh-btn[disabled] .rb-dot {{ animation:rbpulse .9s ease-in-out infinite; }}
@keyframes rbpulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.25; }} }}
.rb-err {{ display:block; font-size:12px; color:var(--bad); max-width:34ch;
  margin-bottom:8px; text-align:right; }}

/* ---- refresh progress ---- */
/* A 21-second wait needs more than a pulsing dot. The bar advances stage by stage and
   creeps within a stage, so it never looks stalled; the decks dim so it is obvious the
   figures on screen are the old ones until the reload lands. */
#rbBar {{ position:fixed; top:0; left:0; height:3px; width:0; z-index:60;
  background:linear-gradient(90deg, var(--sun-deep), var(--sun));
  box-shadow:0 0 10px rgba(233,178,66,.7); opacity:0;
  transition:width .45s cubic-bezier(.22,.61,.36,1), opacity .25s ease; }}
body.is-refreshing #rbBar {{ opacity:1; }}
body.is-refreshing .deck .kpi-val,
body.is-refreshing .funnel .fn-val {{ opacity:.38; transition:opacity .3s ease; }}
body.is-refreshing .deck-today {{ position:relative; overflow:hidden; }}
body.is-refreshing .deck-today::after {{
  content:""; position:absolute; inset:0; pointer-events:none;
  background:linear-gradient(100deg, transparent 20%, rgba(233,178,66,.13) 50%, transparent 80%);
  background-size:220% 100%; animation:rbsweep 1.5s linear infinite; }}
@keyframes rbsweep {{ from {{ background-position:120% 0; }} to {{ background-position:-120% 0; }} }}
.rb-ring {{ width:15px; height:15px; flex:0 0 auto; border-radius:50%;
  border:2px solid rgba(53,56,63,.28); border-top-color:var(--ink);
  animation:rbspin .7s linear infinite; display:none; }}
.refresh-btn[disabled] .rb-ring {{ display:block; }}
.refresh-btn[disabled] .rb-dot {{ display:none; }}
@keyframes rbspin {{ to {{ transform:rotate(360deg); }} }}
@media (prefers-reduced-motion: reduce) {{
  .rb-ring {{ animation:none; border-top-color:var(--ink); }}
  body.is-refreshing .deck-today::after {{ animation:none; }}
  #rbBar {{ transition:opacity .2s ease; }}
}}

/* ---- launch notice ---- */
.notice {{ display:flex; gap:13px; align-items:flex-start; margin-top:24px; padding:15px 18px;
  background:var(--surface); border:1px solid var(--line); border-left:4px solid var(--sun);
  border-radius:3px; }}
.notice p {{ margin:0; font-size:14px; color:var(--text-2); }}
.notice b {{ color:var(--text); }}
.notice .n-ico {{ font-family:var(--f-display); font-size:22px; line-height:1; color:var(--sun-deep); }}

/* ---- sections ---- */
section {{ margin-top:52px; }}
.sec-head {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:14px; margin-bottom:16px; }}
.sec-head h2 {{ font-size:clamp(21px,2.5vw,27px); }}
.sec-head p {{ margin:0; color:var(--text-2); font-size:13.5px; max-width:62ch; }}

/* ---- KPI decks ---- */
.deck {{ border-radius:4px; padding:22px 24px 24px; }}
.deck-head {{ display:flex; flex-wrap:wrap; align-items:baseline; gap:12px; margin-bottom:18px; }}
.deck-head h2 {{ font-size:25px; }}
.deck-tag {{ font-size:11px; letter-spacing:.16em; text-transform:uppercase; font-weight:600; }}
.deck-note {{ margin:0; font-size:12.5px; }}
.grid {{ display:grid; gap:1px; grid-template-columns:repeat(5,1fr); }}
@media (max-width:900px) {{ .grid {{ grid-template-columns:repeat(2,1fr); }} }}
.kpi {{ display:flex; flex-direction:column; gap:3px; padding:14px 16px 15px; }}
.kpi-lab {{ font-size:11px; letter-spacing:.13em; text-transform:uppercase; font-weight:600; }}
.kpi-val {{ font-size:clamp(23px,3vw,31px); font-weight:600; letter-spacing:-.02em;
  font-variant-numeric:tabular-nums; line-height:1.12; }}
.kpi-sub {{ font-size:11.5px; line-height:1.35; }}

.deck-today {{ background:var(--deck-ink-bg); color:var(--deck-ink-text); box-shadow:var(--shadow); }}
.deck-today h2 {{ color:var(--deck-ink-text); }}
.deck-today .deck-tag {{ color:var(--sun); }}
.deck-today .deck-note {{ color:rgba(247,245,241,.62); }}
.deck-today .grid {{ background:var(--deck-ink-line); }}
.deck-today .kpi {{ background:var(--deck-ink-bg); }}
.deck-today .kpi-lab {{ color:rgba(247,245,241,.60); }}
.deck-today .kpi-val {{ color:var(--sun); }}
.deck-today .kpi-sub {{ color:rgba(247,245,241,.48); }}

.deck-run {{ background:var(--surface); border:1px solid var(--line); box-shadow:var(--shadow); }}
.deck-run .deck-tag {{ color:var(--sun-deep); }}
.deck-run .deck-note {{ color:var(--text-3); }}
.deck-run .grid {{ background:var(--line-soft); }}
.deck-run .kpi {{ background:var(--surface); }}
.deck-run .kpi-lab {{ color:var(--text-3); }}
.deck-run .kpi-val {{ color:var(--text); }}
.deck-run .kpi-sub {{ color:var(--text-3); }}
.deck-stack {{ display:flex; flex-direction:column; gap:16px; }}

/* ---- funnel ---- */
.funnel {{ display:flex; align-items:stretch; flex-wrap:wrap; gap:0;
  background:var(--surface); border:1px solid var(--line); border-radius:4px; padding:6px; }}
.fn-step {{ flex:1 1 130px; display:flex; flex-direction:column; gap:2px; padding:16px 18px; }}
.fn-val {{ font-size:26px; font-weight:600; font-variant-numeric:tabular-nums; letter-spacing:-.02em; }}
.fn-lab {{ font-size:11px; letter-spacing:.13em; text-transform:uppercase; color:var(--text-3); font-weight:600; }}
.fn-gap {{ display:flex; flex-direction:column; justify-content:center; align-items:center; gap:1px;
  padding:0 14px; border-left:1px solid var(--line-soft); border-right:1px solid var(--line-soft); min-width:96px; }}
.fn-rate {{ font-size:15px; font-weight:700; color:var(--sun-deep); font-variant-numeric:tabular-nums; }}
.fn-rname {{ font-size:10.5px; letter-spacing:.08em; text-transform:uppercase; color:var(--text-3); }}

/* ---- tables ---- */
.tw {{ overflow-x:auto; background:var(--surface); border:1px solid var(--line);
  border-radius:4px; box-shadow:var(--shadow); }}
table {{ border-collapse:collapse; width:100%; min-width:900px; }}
thead th {{ position:sticky; top:0; background:var(--surface-2); text-align:left;
  font-size:10.5px; letter-spacing:.11em; text-transform:uppercase; color:var(--text-3);
  font-weight:700; padding:11px 12px; border-bottom:1px solid var(--line); white-space:nowrap; z-index:1; }}
thead th.n {{ text-align:right; }}
tbody td, tbody th {{ padding:12px; border-bottom:1px solid var(--line-soft); vertical-align:top;
  font-weight:400; text-align:left; }}
tbody tr:last-child td, tbody tr:last-child th {{ border-bottom:none; }}
td.n, th.n {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
td.hi {{ font-weight:600; }}
td.mono, code.seq {{ font-family:var(--f-mono); }}
td.rk {{ font-family:var(--f-display); font-size:19px; color:var(--text-3); width:42px; text-align:center; }}
.row-idle {{ opacity:.62; }}
.row-total th, .row-total td {{ background:var(--surface-2); font-weight:600;
  border-top:2px solid var(--ink); }}
.row-empty th {{ color:var(--text-3); }}
.nil {{ color:var(--text-3); }}

.slot {{ display:block; font-family:var(--f-display); font-size:18px; line-height:1.2; }}
.cname {{ display:block; font-size:12px; color:var(--text-2); margin-top:1px; }}
.adname {{ display:block; font-size:14px; font-weight:500; line-height:1.35; max-width:46ch; }}
.cmeta {{ display:flex; flex-wrap:wrap; align-items:center; gap:7px; margin-top:5px;
  font-size:11.5px; color:var(--text-3); }}
.sep {{ color:var(--clay); }}
code.seq {{ font-size:11px; background:var(--surface-2); border:1px solid var(--line-soft);
  padding:1px 6px; border-radius:3px; color:var(--text-2); }}
.acct {{ font-size:10px; letter-spacing:.09em; font-weight:700; padding:2px 6px; border-radius:2px; }}
.acct-sts {{ background:rgba(233,178,66,.20); color:var(--sun-deep); }}
.acct-tsa {{ background:rgba(74,103,65,.16); color:var(--good); }}

.pill {{ display:inline-block; font-size:10.5px; letter-spacing:.08em; text-transform:uppercase;
  font-weight:700; padding:3px 9px; border-radius:99px; white-space:nowrap; }}
.pill-live {{ background:rgba(74,103,65,.15); color:var(--good); }}
.pill-idle {{ background:rgba(181,181,181,.22); color:var(--text-3); }}
.pill-out {{ background:rgba(139,58,58,.14); color:var(--bad); }}

.flags {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:6px; }}
.flag {{ font-size:11px; padding:2px 8px; border-radius:3px; line-height:1.4; }}
.flag-buy {{ background:var(--sun); color:#2A2206; font-weight:600; }}
.flag-note {{ background:var(--surface-2); color:var(--text-2); border:1px solid var(--line-soft); }}
.flag-out {{ background:rgba(139,58,58,.12); color:var(--bad); }}
.why {{ font-size:12.5px; color:var(--text-2); max-width:52ch; }}

.bar {{ display:block; height:3px; border-radius:2px; background:var(--line-soft); margin-top:6px; min-width:54px; }}
.bar > span {{ display:block; height:100%; border-radius:2px; }}
.bar-spend > span {{ background:var(--bar-spend); }}
.bar-leads > span {{ background:var(--bar-leads); }}

/* ---- creative thumbnails ---- */
td.tc, th.tc {{ width:76px; padding-right:0; }}
.thumb {{ display:block; position:relative; width:64px; height:64px; padding:0; border:1px solid var(--line);
  border-radius:3px; overflow:hidden; background:var(--surface-2); cursor:zoom-in; line-height:0;
  transition:transform .12s ease, border-color .12s ease; }}
.thumb:hover {{ transform:scale(1.06); border-color:var(--sun); }}
.thumb img {{ width:100%; height:100%; object-fit:cover; display:block; }}
.thumb-play {{ position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
  color:#fff; font-size:17px; text-shadow:0 1px 5px rgba(0,0,0,.85); background:rgba(0,0,0,.22); }}
.thumb-missing {{ display:flex; align-items:center; justify-content:center; cursor:default;
  font-size:9px; line-height:1.25; text-align:center; color:var(--text-3); text-transform:uppercase;
  letter-spacing:.06em; }}
.thumb-missing:hover {{ transform:none; border-color:var(--line); }}
.xlinks {{ display:flex; flex-wrap:wrap; gap:12px; margin-top:6px; }}
.xlink {{ font-size:11.5px; color:var(--sun-deep); text-decoration:none; border-bottom:1px solid currentColor;
  padding-bottom:1px; }}
.xlink:hover {{ color:var(--text); }}

/* ---- lightbox ---- */
.lb {{ position:fixed; inset:0; z-index:50; display:none; align-items:center; justify-content:center;
  background:rgba(20,21,25,.90); padding:28px; }}
.lb[open], .lb.on {{ display:flex; }}
.lb-inner {{ display:flex; flex-direction:column; gap:12px; max-width:min(760px,92vw); max-height:92vh; }}
.lb-img {{ max-width:100%; max-height:74vh; object-fit:contain; border-radius:4px;
  background:#111; box-shadow:0 20px 60px -20px rgba(0,0,0,.9); }}
.lb-bar {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; color:#F7F5F1; }}
.lb-name {{ font-family:var(--f-display); font-size:17px; flex:1 1 240px; line-height:1.3; }}
.lb-dims {{ font-family:var(--f-mono); font-size:11px; color:rgba(247,245,241,.55); }}
.lb-bar a {{ font-size:12px; color:var(--sun); text-decoration:none; border-bottom:1px solid currentColor; }}
.lb-close {{ background:none; border:1px solid rgba(247,245,241,.35); color:#F7F5F1; cursor:pointer;
  font-size:12px; padding:5px 12px; border-radius:3px; font-family:var(--f-body); }}
.lb-close:hover {{ border-color:var(--sun); color:var(--sun); }}

/* ---- hero ad ---- */
.hero-wrap {{ margin-bottom:16px; }}
.hero-grid {{ display:grid; gap:12px; margin-top:9px;
  grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); }}
.hero-ad {{ background:var(--deck-ink-bg); color:var(--deck-ink-text); border-radius:4px;
  padding:20px 22px; box-shadow:var(--shadow); border-left:4px solid var(--sun); }}
.hero-eyebrow {{ font-size:11px; letter-spacing:.18em; text-transform:uppercase;
  color:var(--sun-deep); font-weight:700; }}
.hero-body {{ display:flex; gap:16px; align-items:flex-start; }}
.hero-copy {{ flex:1 1 auto; min-width:0; }}
.hero-body .thumb {{ width:84px; height:84px; flex:0 0 auto; border-color:rgba(247,245,241,.22); }}
.hero-name {{ font-family:var(--f-display); font-size:clamp(18px,2.2vw,23px); margin:0; line-height:1.24; }}
.hero-meta {{ display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:9px 0 0;
  font-size:11.5px; color:rgba(247,245,241,.58); }}
.hero-meta code.seq {{ background:rgba(247,245,241,.09); border-color:rgba(247,245,241,.14);
  color:rgba(247,245,241,.80); }}
.hero-stats {{ display:flex; flex-wrap:wrap; gap:24px; margin-top:16px;
  padding-top:15px; border-top:1px solid var(--deck-ink-line); }}
.hero-stats span {{ display:flex; flex-direction:column; font-size:11px; letter-spacing:.11em;
  text-transform:uppercase; color:rgba(247,245,241,.55); font-weight:600; }}
.hero-stats b {{ font-size:25px; letter-spacing:-.02em; color:var(--sun); font-weight:600;
  font-variant-numeric:tabular-nums; text-transform:none; letter-spacing:normal; }}

/* ---- method ---- */
.method {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:1px;
  background:var(--line-soft); border:1px solid var(--line); border-radius:4px; overflow:hidden; }}
.mcard {{ background:var(--surface); padding:18px 20px; }}
.mcard h3 {{ font-size:16px; margin-bottom:8px; }}
.mcard p, .mcard li {{ font-size:13px; color:var(--text-2); margin:0 0 7px; }}
.mcard ul {{ margin:0; padding-left:17px; }}
.mcard li {{ margin-bottom:6px; }}
.mcard li:last-child, .mcard p:last-child {{ margin-bottom:0; }}
.mcard b {{ color:var(--text); font-weight:600; }}
.mcard code.seq {{ margin-left:6px; }}
.mcard ul.plain {{ list-style:none; padding:0; }}
.mcard ul.plain li {{ display:flex; flex-wrap:wrap; align-items:center; gap:7px; }}
.mcard-refresh {{ border-left:3px solid var(--sun); }}
.askline {{ display:inline-block; font-family:var(--f-mono); font-size:12px; background:var(--surface-2);
  border:1px solid var(--line); border-left:3px solid var(--sun); border-radius:3px;
  padding:9px 12px; color:var(--text) !important; }}
footer {{ margin-top:46px; padding-top:18px; border-top:1px solid var(--line);
  font-size:12px; color:var(--text-3); display:flex; flex-wrap:wrap; gap:8px 20px; }}
</style>

<div id="rbBar" role="progressbar" aria-label="Refresh progress" aria-hidden="true"></div>
<div class="wrap">
<header class="mast">
  <div>
    <span class="mast-eyebrow">School of Traditional Skills &middot; paid acquisition</span>
    <h1>Fall Summit 2026<br><em>Acquisition Board</em></h1>
  </div>
  <div class="stamp">
    {REFRESH_UI}
    <b>Pulled {esc(M["pulled_at"])}</b>
    Times are the ad account's<br>
    Source: Hyros<br>
    Sales credit: summit tag<br>
    Spend and clicks: last click
  </div>
</header>

<div class="notice">
  <span class="n-ico">&#9662;</span>
  <p>{notice_html}</p>
</div>

<section>
  <div class="deck-stack">
    <div class="deck deck-today">
      <div class="deck-head">
        <h2>Just Today</h2>
        <span class="deck-tag">{esc(M["window_end"])}</span>
        <p class="deck-note">Midnight to now, account time zone</p>
      </div>
      <div class="grid">{deck(TODAY)}</div>
    </div>
    <div class="deck deck-run">
      <div class="deck-head">
        <h2>Running Total</h2>
        <span class="deck-tag">{esc(M["first_spend_day"])} to {esc(M["window_end"])}</span>
        <p class="deck-note">Every day the Fall Summit campaigns have run</p>
      </div>
      <div class="grid">{deck(TOTAL)}</div>
    </div>
  </div>
</section>

<section>
  <div class="sec-head">
    <h2>Registration funnel</h2>
    <p>Running total, both accounts. Rates are the step-to-step conversion.</p>
  </div>
  <div class="funnel">{funnel()}</div>
</section>

<section>
  <div class="sec-head">
    <h2>Campaigns</h2>
    <p>Running total only, one row per campaign. Yellow bars are share of leads, grey bars share of spend.</p>
  </div>
  <div class="tw">
    <table>
      <thead><tr>
        <th scope="col">Campaign</th><th scope="col">Status</th>
        <th scope="col" class="n">Spend</th><th scope="col" class="n">Leads</th>
        <th scope="col" class="n">Cost / lead</th><th scope="col" class="n">Link clicks</th>
        <th scope="col" class="n">Cost / click</th><th scope="col" class="n">Page conv.</th>
        <th scope="col" class="n">Purchases</th><th scope="col" class="n">Revenue</th>
        <th scope="col" class="n">Cost / purchase</th><th scope="col" class="n">ROAS</th>
      </tr></thead>
      <tbody>{campaign_rows()}</tbody>
    </table>
  </div>
</section>

<section>
  <div class="sec-head">
    <h2>Top 10 image ads</h2>
    <p>Ranked by tagged registrations, cheapest lead breaking ties. Click any preview to see the creative full size.</p>
  </div>
  {hero_html}
  <div class="tw">
    <table>
      <thead><tr>
        <th scope="col" class="rk">#</th><th scope="col" class="tc">Preview</th><th scope="col">Creative</th>
        <th scope="col" class="n">Leads</th><th scope="col" class="n">Cost / lead</th>
        <th scope="col" class="n">Spend</th><th scope="col" class="n">Clicks</th>
        <th scope="col" class="n">Page conv.</th><th scope="col" class="n">Impressions</th>
        <th scope="col" class="n">Purch.</th><th scope="col" class="n">Revenue</th>
      </tr></thead>
      <tbody>{ad_rows(IMAGE_ADS)}</tbody>
    </table>
  </div>
</section>

<section>
  <div class="sec-head">
    <h2>Top 3 video ads</h2>
    <p>Three video ads are built and none has delivered an impression yet.</p>
  </div>
  <div class="tw">
    <table>
      <thead><tr>
        <th scope="col" class="rk">#</th><th scope="col" class="tc">Preview</th><th scope="col">Creative</th>
        <th scope="col" class="n">Leads</th><th scope="col" class="n">Cost / lead</th>
        <th scope="col" class="n">Spend</th><th scope="col" class="n">Clicks</th>
        <th scope="col" class="n">Page conv.</th><th scope="col" class="n">Impressions</th>
        <th scope="col" class="n">Purch.</th><th scope="col" class="n">Revenue</th>
      </tr></thead>
      <tbody>{ad_rows(VIDEO_ADS, empty_slots=max(0, 3-len(VIDEO_ADS)), kind="video")}</tbody>
    </table>
  </div>
</section>

<section>
  <div class="sec-head">
    <h2>Purchase ledger</h2>
    <p>Every Stripe sale Hyros holds against a {esc(M["tag_filter"])} lead. Tagged sales count whatever click
      closed them, so the only thing that excludes one here is falling outside the campaign window.
      {"Buyer names are replaced with sequential labels on this shared copy; every date, amount and classification is unchanged." if REDACT else ""}</p>
  </div>
  <div class="tw">
    <table>
      <thead><tr>
        <th scope="col" class="n">Date</th><th scope="col" class="n">Amount</th>
        <th scope="col">Lead</th><th scope="col">In the numbers</th><th scope="col">Reason</th>
      </tr></thead>
      <tbody>{ledger_rows()}</tbody>
    </table>
  </div>
</section>

<section>
  <div class="sec-head">
    <h2>Daily log</h2>
    <p>One row per day of delivery. This table grows as the summit runs.</p>
  </div>
  <div class="tw">
    <table>
      <thead><tr>
        <th scope="col" class="n">Date</th><th scope="col" class="n">Spend</th>
        <th scope="col" class="n">Link clicks</th><th scope="col" class="n">Leads</th>
        <th scope="col" class="n">Cost / lead</th><th scope="col" class="n">Page conv.</th>
        <th scope="col" class="n">Purchases</th><th scope="col" class="n">Revenue</th>
        <th scope="col" class="n">ROAS</th>
      </tr></thead>
      <tbody>{daily_rows()}</tbody>
    </table>
  </div>
</section>

<section>
  <div class="sec-head">
    <h2>How these numbers are built</h2>
    <p>Everything on this page comes from the Hyros MCP. No Meta figures, no pixel data.</p>
  </div>
  <div class="method">
    <div class="mcard">
      <h3>Ad accounts and creatives</h3>
      <ul class="plain">{accounts}</ul>
      <p>Both sit under one Hyros account, so the two are combined without double counting.</p>
      <p>Every metric on this page is Hyros. The creative previews are the only exception: those image
      files come from the Meta Ads API and are embedded in the page, so they keep working after Meta's
      own links expire.</p>
    </div>
    <div class="mcard">
      <h3>The tag filter</h3>
      <p>Leads, purchases and revenue count only people carrying <b>{esc(M["tag_filter"])}</b>:
      {M["tagged_leads_total"]} leads in total, of which <b>{M["tagged_leads_paid"]}</b> are
      attributed to a Fall Summit ad and {M["tagged_leads_organic_or_direct"]} arrived organic or direct.</p>
      <p>Hyros cannot tag-filter spend or clicks, so those are the platform figures for the
      Fall Summit campaigns, which carry no other traffic.</p>
      <p><b>A sale from a tagged lead counts whatever click closed it</b>, including an organic or
      direct last click. Credit goes to that lead's summit ad touch.</p>
    </div>
    <div class="mcard">
      <h3>Definitions</h3>
      <ul>
        <li><b>Page conversion</b> is leads divided by link clicks, as requested. It can exceed 100% on an
          individual ad when Hyros tracks a session Meta did not count as a link click.</li>
        <li><b>Cost per lead</b> is spend divided by tagged leads.</li>
        <li><b>Purchases and revenue</b> are every sale from a !summit-2026 lead inside the campaign window,
          regardless of last click.</li>
        <li><b>ROAS</b> is that revenue divided by spend. Break-even is 1.00x.</li>
      </ul>
    </div>
    <div class="mcard mcard-refresh">
      <h3>Refreshing this page</h3>
      {"".join([
        "<p>This copy is rebuilt on request, not on a timer. Someone with the dashboard open "
        "presses Refresh, which re-pulls Hyros and rebuilds this page a couple of minutes later.</p>"
        "<p>The timestamp above is the pull it was built from, in the ad account's time zone.</p>"
      ]) if REDACT else "".join([
        "<p>Two ways, neither of which needs Claude:</p>",
        "<p><b>Locally, right now.</b> Double-click <span class='askline'>Fall Summit Dashboard.command</span> "
        "and press Refresh on the page. It re-pulls Hyros and rebuilds in one click.</p>",
        "<p>The same press also rebuilds <b>the shared copy</b> at "
        "<span class='askline'>gostrategicmarketing-deployment.github.io/sts-fall-summit-2026/</span>, "
        "with buyer names redacted. Nothing runs on a timer.</p>",
      ])}
    </div>
    <div class="mcard">
      <h3>Known variances</h3>
      <ul>
        <li>Hyros reports <b>{M["hyros_report_leads_on_summit_adsets"]} attributed leads</b> on these ad sets
          against <b>{M["tagged_leads_paid"]}</b> carrying the tag. The tag-filtered figure is used everywhere here.</li>
        {f'<li><b>{_unc_n} purchase{"" if _unc_n == 1 else "s"} worth {money(_unc_rev)} '
          f'{"counts" if _unc_n == 1 else "count"} but {"sits" if _unc_n == 1 else "sit"} in no campaign.</b> '
          f'The tag says {"it is a summit sale" if _unc_n == 1 else "they are summit sales"}, but '
          f'{"its sale record carries" if _unc_n == 1 else "their sale records carry"} no summit ad touch, '
          f'so no creative can be credited. {"It is" if _unc_n == 1 else "They are"} in the headline and in '
          f'the ledger; the campaign rows below add up to {TOTAL["purchases"] - _unc_n} and '
          f'{money(TOTAL["revenue"] - _unc_rev)}.</li>' if _unc_n else ''}
        <li><b>Ad rows sum to {money(M["ad_level_spend_sum"])} of the {money(TOTAL["spend"])} total.</b> Hyros has not yet
          broken every ad set's spend down to individual ads, and the ad level is one full-window pull
          taken seconds after the per-day sweep rather than part of it. The campaign table and the
          headline figures use the ad-set numbers, which are authoritative; the creative ranking below
          uses the ad rows, so its spend column runs slightly light.</li>
        <li>Five older sales sit on tagged leads but predate {esc(M["first_spend_day"])}, the first day any
          Fall Summit ad ran: four staff test transactions and one Preservation Summit rebill. All five are
          listed in the purchase ledger.</li>
      </ul>
    </div>
  </div>
</section>

<footer>
  <span>School of Traditional Skills and Traditional Skills Academy</span>
  <span>Data pulled {esc(M["pulled_at"])} from the Hyros MCP</span>
  <span>Press Refresh to re-pull Hyros; nothing runs on a timer</span>
  <span>Rebuild locally: <code class="seq">python3 build_dashboard.py</code></span>
</footer>
</div>

<div class="lb" id="lb" role="dialog" aria-modal="true" aria-label="Creative preview">
  <div class="lb-inner">
    <img class="lb-img" id="lbImg" alt="">
    <div class="lb-bar">
      <span class="lb-name" id="lbName"></span>
      <span class="lb-dims" id="lbDims"></span>
      <a id="lbWatch" href="#" target="_blank" rel="noopener" hidden>Watch video</a>
      <a id="lbManage" href="#" target="_blank" rel="noopener">Open in Ads Manager</a>
      <button type="button" class="lb-close" id="lbClose">Close</button>
    </div>
  </div>
</div>
<script>
(function () {{
  var lb = document.getElementById('lb'), img = document.getElementById('lbImg'),
      name = document.getElementById('lbName'), dims = document.getElementById('lbDims'),
      manage = document.getElementById('lbManage'), watch = document.getElementById('lbWatch'),
      last = null;
  function open(btn) {{
    last = btn;
    img.src = btn.dataset.full;
    img.alt = btn.dataset.name;
    name.textContent = btn.dataset.name;
    dims.innerHTML = btn.dataset.dims;
    manage.href = btn.dataset.manage;
    if (btn.dataset.watch) {{ watch.href = btn.dataset.watch; watch.hidden = false; }}
    else {{ watch.hidden = true; }}
    lb.classList.add('on');
    document.getElementById('lbClose').focus();
  }}
  function close() {{
    lb.classList.remove('on');
    img.removeAttribute('src');
    if (last) {{ last.focus(); last = null; }}
  }}
  document.addEventListener('click', function (e) {{
    var btn = e.target.closest('.thumb[data-full]');
    if (btn) {{ open(btn); return; }}
    if (e.target === lb || e.target.id === 'lbClose') close();
  }});
  document.addEventListener('keydown', function (e) {{
    if (e.key === 'Escape' && lb.classList.contains('on')) close();
  }});
}})();

// Refresh button. Only useful when serve.py is hosting the page, since a static host
// has nothing to POST to; the published copy is rebuilt by the same press via Actions.
//
// The refresh takes roughly twenty seconds: Hyros is paged for leads, sales and a
// window per day, then the page is rebuilt. Earlier this button just sat there for
// that whole time and a second press got a 409, which read as "it did not work". It
// now polls for the live stage and a second press simply joins the run in flight.
(function () {{
  var local = ['localhost', '127.0.0.1', '::1'].indexOf(location.hostname) !== -1;
  var btn = document.getElementById('refreshBtn'),
      label = document.getElementById('rbLabel'),
      err = document.getElementById('rbErr'),
      bar = document.getElementById('rbBar');
  if (!local || !btn) return;
  btn.hidden = false;

  // Where the bar sits when each stage begins, weighted by how long each actually
  // takes: the Hyros pull is about two thirds of the wall clock, the rebuild most of
  // the rest. Within a stage the bar creeps toward the next mark so it never freezes.
  var MARKS = {{
    'Starting': 4,
    'Pulling Hyros': 8,
    'Refreshing previews': 66,
    'Rebuilding the page': 74,
    'Publishing the shared copy': 94
  }};
  var NEXT = {{
    'Starting': 8,
    'Pulling Hyros': 66,
    'Refreshing previews': 74,
    'Rebuilding the page': 94,
    'Publishing the shared copy': 99
  }};
  var pct = 0, creep = null;

  function draw(p) {{ pct = Math.max(pct, Math.min(p, 99.5)); bar.style.width = pct + '%'; }}

  function stageTo(stage) {{
    var from = MARKS[stage], to = NEXT[stage];
    if (from === undefined) return;
    draw(from);
    clearInterval(creep);
    // Asymptotic creep: fast at first, never quite reaching the next mark.
    creep = setInterval(function () {{ draw(pct + (to - pct) * 0.06); }}, 400);
  }}

  function stop() {{
    clearInterval(creep);
    creep = null;
    document.body.classList.remove('is-refreshing');
    btn.disabled = false;
  }}

  function fail(m) {{
    stop();
    bar.style.width = '0';
    label.textContent = 'Refresh';
    err.textContent = m;
    err.hidden = false;
  }}

  function poll() {{
    fetch('/status', {{ cache: 'no-store' }})
      .then(function (r) {{
        if (r.status === 404) {{
          throw new Error('This server is an older instance and has no /status. '
                        + 'Quit it and reopen Fall Summit Dashboard.command.');
        }}
        return r.json();
      }})
      .then(function (s) {{
        if (s.running) {{
          stageTo(s.stage);
          label.textContent = (s.stage || 'Working') + '\u2026 ' + Math.round(s.elapsed) + 's';
          setTimeout(poll, 600);
          return;
        }}
        if (s.error) {{ fail(s.error); return; }}
        clearInterval(creep);
        bar.style.width = '100%';
        label.textContent = 'Reloading\u2026';
        // Cache-bust: a plain reload can be served from cache, and then the page looks
        // unchanged even though it was just rebuilt.
        setTimeout(function () {{
          location.replace(location.pathname + '?t=' + Date.now());
        }}, 300);
      }})
      .catch(function (e) {{ fail(e.message || String(e)); }});
  }}

  btn.addEventListener('click', function () {{
    btn.disabled = true;
    err.hidden = true;
    pct = 0;
    document.body.classList.add('is-refreshing');
    stageTo('Starting');
    label.textContent = 'Starting\u2026';
    fetch('/refresh', {{ method: 'POST', cache: 'no-store' }})
      .then(function (r) {{ if (!r.ok && r.status !== 409) throw new Error('HTTP ' + r.status); }})
      .then(function () {{ setTimeout(poll, 350); }})
      .catch(function (e) {{ fail(e.message || String(e)); }});
  }});
}})();
</script>
"""

out = ROOT / OUT_NAME
out.write_text(PAGE)
print(f"wrote {out}  ({len(PAGE):,} bytes)")
print(f"total: spend {money(TOTAL['spend'])}  leads {TOTAL['leads']}  clicks {TOTAL['clicks']}  "
      f"CPL {money(TOTAL['cpl'])}  CVR {TOTAL['cvr']*100:.1f}%  purchases {TOTAL['purchases']}  ROAS {TOTAL['roas']:.2f}x")
print(f"image ads listed: {len(IMAGE_ADS)}   video ads listed: {len(VIDEO_ADS)}")
