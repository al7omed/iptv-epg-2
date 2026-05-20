#!/usr/bin/env python3
"""Patch a local M3U with auto-generated tvg-ids from the published map.

The build pipeline publishes a (non-sensitive) tvg-id-map.tsv on GitHub Pages.
This script:
  1. Downloads the map.
  2. Reads your local M3U file (which has your private stream URLs).
  3. For every #EXTINF entry that lacks a tvg-id, looks up the channel's name
     in the map and inserts the matching effective_tvg_id.
  4. Writes a patched M3U you can paste into your IPTV player.

Stream URLs and provider auth tokens never leave your machine.

Usage:
    python3 patch_m3u.py <input.m3u> <output.m3u> [map_url]

Default map URL: https://al7omed.github.io/iptv-epg-2/tvg-id-map.tsv

Pair the patched M3U with the EPG:
    https://al7omed.github.io/iptv-epg-2/guide.xml.gz
"""
from __future__ import annotations

import csv
import io
import re
import sys
import urllib.request

DEFAULT_MAP_URL = "https://al7omed.github.io/iptv-epg-2/tvg-id-map.tsv"

EXTINF_LINE_RE = re.compile(r"#EXTINF[^,\n]*,([^\n]+)")
ATTR_RE = re.compile(r'(\b[\w-]+)="([^"]*)"')
TVG_ID_ATTR_RE = re.compile(r'\s*tvg-id="[^"]*"')
EPG_URL = "https://al7omed.github.io/iptv-epg-2/guide.xml.gz"


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "patch_m3u/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_map(text: str) -> dict[tuple[str, str], str]:
    """Map (tvg_name, title) -> effective_id. Empty strings match any."""
    out = {}
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    next(reader, None)
    for row in reader:
        if len(row) < 4:
            continue
        tvg_name, title, original, effective = row[:4]
        out[(tvg_name, title)] = effective
        # Allow lookup by either name alone
        out.setdefault((tvg_name, ""), effective)
        out.setdefault(("", title), effective)
    return out


def patch_m3u(m3u_text: str, name_to_id: dict[tuple[str, str], str]) -> tuple[str, int, int]:
    out = []
    matched = 0
    skipped = 0
    for line in m3u_text.splitlines():
        if not line.startswith("#EXTINF"):
            out.append(line)
            continue
        m = EXTINF_LINE_RE.match(line)
        title = m.group(1).strip() if m else ""
        comma_idx = line.find(",")
        attr_str = line[:comma_idx] if comma_idx > 0 else line
        attrs = dict(ATTR_RE.findall(attr_str))
        existing_id = attrs.get("tvg-id", "").strip()
        tvg_name = attrs.get("tvg-name", "").strip()

        if existing_id:
            out.append(line)
            continue

        eff = (
            name_to_id.get((tvg_name, title))
            or name_to_id.get((tvg_name, ""))
            or name_to_id.get(("", title))
        )
        if not eff:
            skipped += 1
            out.append(line)
            continue

        clean = TVG_ID_ATTR_RE.sub("", line)
        head_match = re.match(r'(#EXTINF[^\s,]*)\s*(.*?,.*)$', clean, re.DOTALL)
        if head_match:
            head, tail = head_match.group(1), head_match.group(2)
            clean = f'{head} tvg-id="{eff}" {tail}'
        out.append(clean)
        matched += 1

    if out and out[0].startswith("#EXTM3U"):
        if "x-tvg-url" not in out[0]:
            out[0] = f'#EXTM3U x-tvg-url="{EPG_URL}"'
    else:
        out.insert(0, f'#EXTM3U x-tvg-url="{EPG_URL}"')
    return "\n".join(out) + "\n", matched, skipped


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    in_path, out_path = sys.argv[1], sys.argv[2]
    map_url = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_MAP_URL

    print(f"fetching map from {map_url}...")
    tsv = fetch(map_url)
    name_to_id = load_map(tsv)
    print(f"  loaded {len(name_to_id)} name mappings")

    with open(in_path, "r", encoding="utf-8", errors="replace") as f:
        m3u_text = f.read()

    patched, matched, skipped = patch_m3u(m3u_text, name_to_id)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(patched)

    print(f"patched {matched} entries, {skipped} had no map match (kept as-is)")
    print(f"wrote {out_path}")
    print(f"\nNext: paste this M3U URL into your player as file path, plus EPG URL:")
    print(f"  M3U: {out_path}")
    print(f"  EPG: {EPG_URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
