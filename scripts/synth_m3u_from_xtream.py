#!/usr/bin/env python3
"""Synthesize an M3U from the Xtream Codes player_api.

Used as a fallback when the provider's /get.php endpoint is blocked by the
CDN's WAF (some IPs receive a non-standard HTTP 884). The Xtream API endpoint
is usually unaffected.

Environment:
  XTREAM_HOST  e.g. http://mil79711.wd.business-cdn-8k.com
  XTREAM_USER  e.g. ac50ff82173e
  XTREAM_PASS  e.g. 0b9695fafe

Writes the synthesized M3U to stdout. The format mirrors what a typical
Xtream get.php?type=m3u_plus would produce:

  #EXTM3U
  #EXTINF:-1 tvg-id="<epg_channel_id>" tvg-name="<name>" tvg-logo="<icon>" group-title="<cat>",<name>
  <XTREAM_HOST>/live/<USER>/<PASS>/<stream_id>.ts
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request


def get_json(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": "iptv-epg-builder/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main():
    host = os.environ["XTREAM_HOST"].rstrip("/")
    user = os.environ["XTREAM_USER"]
    pwd = os.environ["XTREAM_PASS"]
    q = urllib.parse.urlencode({"username": user, "password": pwd})

    sys.stderr.write(f"fetching categories...\n")
    try:
        cats = get_json(f"{host}/player_api.php?{q}&action=get_live_categories")
    except Exception as e:
        sys.stderr.write(f"  categories failed: {e}; continuing without names\n")
        cats = []
    cat_name = {c["category_id"]: c["category_name"] for c in cats}
    sys.stderr.write(f"  {len(cat_name)} categories\n")

    sys.stderr.write(f"fetching streams...\n")
    streams = get_json(f"{host}/player_api.php?{q}&action=get_live_streams")
    sys.stderr.write(f"  {len(streams)} streams\n")

    out = sys.stdout
    out.write("#EXTM3U\n")
    for s in streams:
        name = (s.get("name") or "").replace('"', '\\"').replace("\n", " ")
        icon = (s.get("stream_icon") or "").replace('"', '\\"')
        cid = s.get("epg_channel_id") or ""
        cat_id = s.get("category_id") or ""
        cat_label = cat_name.get(cat_id, "")
        stream_id = s["stream_id"]
        attrs = f'tvg-id="{cid}" tvg-name="{name}" tvg-logo="{icon}" group-title="{cat_label}"'
        out.write(f"#EXTINF:-1 {attrs},{name}\n")
        out.write(f"{host}/live/{user}/{pwd}/{stream_id}.ts\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
