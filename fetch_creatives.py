#!/usr/bin/env python3
"""
Keep the creative previews in step with whatever the dashboard is currently ranking.

The Refresh button runs this between pull.py and build_dashboard.py. Before it did,
previews were frozen at a hand-picked set: on 2026-09-08 six of the top ten image ads
and two of the three video ads rendered a "no preview" tile because newer creatives
had climbed the ranking and nobody had fetched them.

Only ads the page actually shows are fetched, so this stays cheap: the top image and
video ads, plus any creative credited with a purchase. Assets already held are left
alone, which is what keeps a normal refresh to zero Meta calls.

    python3 fetch_creatives.py            # fetch whatever the current ranking needs
    python3 fetch_creatives.py --all      # every ad with spend or leads
    python3 fetch_creatives.py --prune    # also drop assets no longer referenced
"""
import base64
import io
import json
import pathlib
import sys
import urllib.request

from PIL import Image

from meta_api import creative_for

HERE = pathlib.Path(__file__).resolve().parent
DATA = HERE / "data"
ASSETS = DATA / "creative_assets.json"
CACHE = DATA / "creatives"
MAX_EDGE, QUALITY = 720, 76
TOP_IMAGE, TOP_VIDEO, TOP_SELLER = 10, 3, 10
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def rank(a):
    """Same ordering build_dashboard.py uses, so we fetch exactly what it renders."""
    return (-a["leads"], a["spend"] if a["leads"] else 1e9, -a["impressions"])


def wanted(data, everything=False):
    ads = data["ads"]
    if everything:
        return [a for a in ads if a["spend"] or a["leads"]]
    imgs = sorted([a for a in ads if a["type"] == "image"], key=rank)[:TOP_IMAGE]
    vids = sorted([a for a in ads if a["type"] == "video"], key=rank)[:TOP_VIDEO]
    buys = sorted([a for a in ads if a["purchases"]], key=lambda a: -a["revenue"])[:TOP_SELLER]
    seen, out = set(), []
    for a in imgs + vids + buys:
        if a["id"] not in seen:
            seen.add(a["id"])
            out.append(a)
    return out


def download(url, tries=3):
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:
            if n == tries - 1:
                raise
            import time
            time.sleep(2 * (n + 1))


def encode(raw):
    im = Image.open(io.BytesIO(raw))
    w0, h0 = im.size
    im = im.convert("RGB")
    im.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=QUALITY, optimize=True, progressive=True)
    jpg = buf.getvalue()
    return jpg, w0, h0


def main():
    everything = "--all" in sys.argv
    data = json.loads((DATA / "dashboard_data.json").read_text())
    assets = json.loads(ASSETS.read_text()) if ASSETS.exists() else {}
    CACHE.mkdir(parents=True, exist_ok=True)

    need = [a for a in wanted(data, everything) if a["id"] not in assets]
    if not need:
        print("  previews already cover everything on the page")
    else:
        print(f"  fetching {len(need)} creative(s) the page needs")

    added, failed = 0, []
    for a in need:
        kind, url, extra = creative_for(a["id"])
        if not url:
            failed.append((a["id"], a["name"][:44], extra.get("error", "no image url")))
            continue
        try:
            jpg, w0, h0 = encode(download(url))
        except Exception as e:
            failed.append((a["id"], a["name"][:44], f"{type(e).__name__}"))
            continue
        (CACHE / f"{a['id']}.jpg").write_bytes(jpg)
        acct = "3014083142121289" if a["account"] == "TSA" else "1246959949464564"
        rec = {
            "creative_id": "", "kind": kind or a["type"],
            "data_uri": "data:image/jpeg;base64," + base64.b64encode(jpg).decode(),
            "aspect": round(w0 / h0, 4), "orig_w": w0, "orig_h": h0,
            "ads_manager_url": ("https://adsmanager.facebook.com/adsmanager/manage/ads"
                                f"?act={acct}&selected_ad_ids={a['id']}"),
        }
        if extra.get("video_id"):
            rec["watch_url"] = f"https://www.facebook.com/watch/?v={extra['video_id']}"
        assets[a["id"]] = rec
        added += 1

    if "--prune" in sys.argv:
        keep = {a["id"] for a in wanted(data, True)}
        for gone in [k for k in assets if k not in keep]:
            assets.pop(gone)

    ASSETS.write_text(json.dumps(assets, indent=2))
    size = sum(len(v["data_uri"]) for v in assets.values()) / 1024 / 1024
    print(f"  added {added}, stored {len(assets)}, {size:.2f} MB of base64 (16 MB cap)")
    for i, n, why in failed:
        print(f"  could not fetch {i} {n}: {why}")
    # A missing preview renders a neutral tile, so this is never fatal.


if __name__ == "__main__":
    main()
