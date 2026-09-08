#!/usr/bin/env python3
"""
Minimal Meta Graph client, used only to fetch ad creative images for the previews.

Every metric on the dashboard comes from Hyros. Meta is touched for one thing: the
creative image files, because Hyros does not serve them and a published artifact
cannot hot-link fbcdn (the URLs expire within days).

Credential order: $FB_TOKEN, then fb_token.txt beside this file, then the shared
read-only token under ~/Documents/Claude/Projects/PBI 2/. It is a user token, so it
reaches every ad account the user administers, STS and TSA included.

Run directly to check the token can read this account's creatives.
"""
import json
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com/v21.0"
HERE = pathlib.Path(__file__).resolve().parent
FALLBACK = pathlib.Path.home() / "Documents/Claude/Projects/PBI 2/fb_token.txt"


def token():
    env = os.environ.get("FB_TOKEN", "").strip()
    if env:
        return env
    for f in (HERE / "fb_token.txt", FALLBACK):
        if f.exists():
            t = f.read_text().strip()
            if t:
                return t
    raise SystemExit(
        "No Meta token. Set FB_TOKEN, or place a read-only token at "
        f"{HERE / 'fb_token.txt'} (chmod 600). Only used for creative images; every "
        "metric comes from Hyros."
    )


def get(path, params=None, tries=3):
    p = dict(params or {})
    p["access_token"] = token()
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode(p)
    for n in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503) and n < tries - 1:
                time.sleep(2 * (n + 1))
                continue
            return e.code, body
        except Exception as e:
            if n < tries - 1:
                time.sleep(2 * (n + 1))
                continue
            return None, f"{type(e).__name__}: {e}"
    return None, "exhausted retries"


def creative_for(ad_id):
    """Returns (kind, image_url, extra) for one ad, or (None, None, {}) if unreadable."""
    code, d = get(ad_id, {"fields": "creative{id,image_url,thumbnail_url,object_type,video_id}"})
    if code != 200 or not isinstance(d, dict):
        return None, None, {"error": str(d)[:120]}
    c = (d.get("creative") or {})
    if c.get("object_type") == "VIDEO" or c.get("video_id"):
        return "video", c.get("thumbnail_url"), {"video_id": c.get("video_id")}
    return "image", c.get("image_url") or c.get("thumbnail_url"), {}


if __name__ == "__main__":
    data = json.loads((HERE / "data" / "dashboard_data.json").read_text())
    assets = json.loads((HERE / "data" / "creative_assets.json").read_text())
    missing = [a for a in data["ads"] if a["id"] not in assets and (a["leads"] or a["spend"])][:4]
    print(f"  token source: {'FB_TOKEN env' if os.environ.get('FB_TOKEN') else 'token file'}")
    print(f"  probing {len(missing)} ads this account has never had previews for:")
    for a in missing:
        kind, url, extra = creative_for(a["id"])
        ok = "OK  " if url else "FAIL"
        print(f"    {ok} {a['account']}  {kind}  {a['name'][:44]}")
        if not url:
            print(f"         {extra.get('error','')[:110]}")
