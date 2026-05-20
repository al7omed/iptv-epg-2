#!/usr/bin/env python3
"""
Build a unified XMLTV EPG by merging epgshare01.online per-region files with the
provider EPG, filtering to channels referenced by the user's M3U playlist.

Inputs (env vars):
  M3U_URL          (required) URL to user's M3U playlist
  PROVIDER_EPG_URL (optional) URL to provider's existing XMLTV (gzipped or raw)

Output:
  docs/guide.xml      uncompressed XMLTV
  docs/guide.xml.gz   gzip-compressed

The matcher uses two strategies:
  1) Direct tvg-id match
  2) Normalized display-name match (handles US callsigns, prefixes, suffixes,
     unicode superscripts)
Plus: every channel from the provider EPG is kept verbatim, since the provider
already curated alias mappings for ~168 channels.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import gzip
import hashlib
import html
import io
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

# User-local timezone for snapping dummy programme blocks to a clean midnight.
USER_TZ_OFFSET = dt.timedelta(hours=3)  # GMT+3

# ---------------- epgshare01 sources ----------------

EPGSHARE_BASE = "https://epgshare01.online/epgshare01"
EPGSHARE_FILES = [
    "US2",
    "US_LOCALS1",
    "US_SPORTS1",
    "UK1",
    "BEIN1",
    "ALJAZEERA1",
    "AE1",
]

# Note: SA1 is stale (last update 2024) so omitted. Add country files as needed
# by editing this list.

# ---------------- name normalization ----------------

# Unicode superscript characters used in the user's M3U (RAW, HD, 60fps, hevc, etc.)
SUPERSCRIPT_CHARS = "ᴿᴬᵂᴴᴰᶠʰᵉᵛᶜᵘᵏ⁴⁶⁰⁸⁵ᵖˢ⁷⁸⁹⁰¹²³"

PREFIX_PATTERN = re.compile(
    r"^\s*(?:UK|US|AR|FR|DE|ES|IT|TR|EN|NL|PT|RU|SE|NO|FI|PL|CA|AU|NZ|IN|ZA|"
    r"MENA|VIP|NOW|NEW|BACK[ -]?UP|MAIN|EXYU|EX-YU|YU|"
    r"GOBX|MBC|OSN|BEIN|ALL|ALL[ -]?PPV|PPV)\s*[:|]+\s*",
    re.IGNORECASE,
)

# Suffixes/qualifiers to strip
SUFFIX_TOKENS_PATTERN = re.compile(
    r"\b("
    r"HD|FHD|UHD|4K|SD|HEVC|H265|H\.?264|RAW|"
    r"BACKUP|BACK[ -]?UP|MULTI[ -]?AUDIO|MULTI[ -]?AUDIO|HQ|LQ|"
    r"PLATINUM|VIP|EVENT|EVENTS|LIVE|PLUS1|\+1|TIMESHIFT|"
    r"60FPS|60[ -]?FPS|MAIN|MIRROR|FEED"
    r")\b",
    re.IGNORECASE,
)

CALLSIGN_PATTERN = re.compile(r"\(([KW][A-Z0-9]{2,5}(?:-(?:DT|LD|LP|CD|CA|TV)\d?)?)\)")
BARE_CALLSIGN_PATTERN = re.compile(r"^([KW][A-Z0-9]{2,5})(?:-(?:DT|LD|LP|CD|CA|TV)\d?)?$")
US_AFFILIATE_PREFIX = re.compile(r"^(?:NBC|FOX|CBS|ABC|CW|PBS|MNT|TELEMUNDO|UNIVISION|MYTV)\s*\d*\s*", re.IGNORECASE)


def _strip_us_callsign_suffix(cs: str) -> str:
    """Strip -DT, -LD, -LP, -CD, -CA, -TV (with optional digit) from a US callsign."""
    return re.sub(r"-(?:DT|LD|LP|CD|CA|TV)\d?$", "", cs)


def normalize_name(s: str) -> str:
    """Aggressive normalization for fuzzy matching. Returns '' for empty/junk."""
    if not s:
        return ""
    # Strip surrounding hash-borders (####### NAME #######)
    s = re.sub(r"^[#*=\-_\s]+|[#*=\-_\s]+$", "", s)
    # Strip unicode superscripts
    s = re.sub(f"[{SUPERSCRIPT_CHARS}]+", "", s)
    # Strip prefixes like "US:", "UK:"
    s = PREFIX_PATTERN.sub("", s)
    # Strip parenthesized qualifiers like (D), (H), (A), (S) at the end
    s = re.sub(r"\(([A-Z]{1,3}(?:\d?))\)\s*$", "", s)
    # Strip suffix tokens
    s = SUFFIX_TOKENS_PATTERN.sub("", s)
    # Collapse
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", "", s)
    return s


def extract_callsign(name: str) -> str | None:
    """Extract a US broadcast callsign from a channel name. Handles two forms:
       'NBC 4 (KNBC) LOS ANGELES' -> 'KNBC'
       'KNBC-DT' or 'KNBC'        -> 'KNBC'
    Returns the canonical form (no -DT/-LD/-LP/etc suffix).
    """
    m = CALLSIGN_PATTERN.search(name)
    if m:
        cs = _strip_us_callsign_suffix(m.group(1).upper())
        if 3 <= len(cs) <= 5 and cs[0] in ("K", "W"):
            return cs
    bare = BARE_CALLSIGN_PATTERN.match(name.strip().upper())
    if bare:
        return bare.group(1)
    return None


# ---------------- M3U parsing ----------------

EXTINF_LINE_RE = re.compile(r'#EXTINF[^,\n]*,([^\n]+)')
ATTR_RE = re.compile(r'(\b[\w-]+)="([^"]*)"')


def parse_m3u(text: str):
    """Return list of dicts: tvg_id, tvg_name, group, title, extinf_line, url_line, line_index.

    Order-independent attribute parsing. Captures the original lines so we can
    rewrite the M3U later while preserving everything except tvg-id.
    """
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("#EXTINF"):
            i += 1
            continue
        m = EXTINF_LINE_RE.match(line)
        title = m.group(1).strip() if m else ""
        comma_idx = line.find(",")
        attr_str = line[: comma_idx if comma_idx > 0 else len(line)]
        attrs = dict(ATTR_RE.findall(attr_str))
        # The following non-EXTINF lines (vlcopt, kodiprop, etc.) plus the URL
        # belong to this entry. Collect them all.
        following = []
        j = i + 1
        while j < len(lines) and lines[j].startswith("#"):
            following.append(lines[j])
            j += 1
        url_line = lines[j] if j < len(lines) else ""
        out.append({
            "tvg_id": attrs.get("tvg-id", "").strip(),
            "tvg_name": attrs.get("tvg-name", "").strip(),
            "group": attrs.get("group-title", "").strip(),
            "title": title,
            "extinf_line": line,
            "extra_lines": following,
            "url_line": url_line,
        })
        i = j + 1
    return out


# ---------------- auto tvg-id ----------------

AUTO_ID_INVALID = re.compile(r"[^a-z0-9]+")
AUTO_ID_BORDER = re.compile(r"^[#*=\-_\s]+|[#*=\-_\s]+$")


def auto_tvg_id(channel: dict) -> str:
    """Generate a stable, URL-safe tvg-id from a channel's name.

    Deterministic per name. Suffix '.auto' marks these as generated (so they
    never collide with the M3U's existing namespace).
    """
    name = channel["tvg_name"] or channel["title"]
    if not name:
        name = channel["title"] or "channel"
    s = AUTO_ID_BORDER.sub("", name).lower()
    s = AUTO_ID_INVALID.sub("-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = "ch" + hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    # Append a 4-char hash for stability across name collisions
    h = hashlib.md5(name.encode("utf-8")).hexdigest()[:4]
    return f"{s}-{h}.auto"


def assign_effective_ids(m3u_channels):
    """For each M3U channel, set 'effective_id' = original tvg-id if present,
    else a freshly generated auto id. Returns count of auto-generated."""
    auto_count = 0
    for ch in m3u_channels:
        if ch["tvg_id"]:
            ch["effective_id"] = ch["tvg_id"]
        else:
            ch["effective_id"] = auto_tvg_id(ch)
            auto_count += 1
    return auto_count


# ---------------- tvg-id map (replacement for M3U republish) ----------------
#
# We do NOT publish the M3U publicly because it contains the user's IPTV
# stream URLs with embedded auth tokens. Instead we publish a CSV mapping of
# (channel-name -> effective tvg-id) which is non-sensitive. The user runs
# scripts/patch_m3u.py locally to inject these tvg-ids into their local M3U.


def write_tvg_id_map(m3u_channels, dest: Path) -> int:
    """Write a CSV: tvg_name|title|tvg_id|effective_id, one row per M3U entry.
    Uses tab separator since channel names contain commas. Returns row count.
    """
    rows = ["tvg_name\ttitle\toriginal_tvg_id\teffective_tvg_id"]
    for ch in m3u_channels:
        rows.append("\t".join([
            ch["tvg_name"].replace("\t", " ").replace("\n", " "),
            ch["title"].replace("\t", " ").replace("\n", " "),
            ch["tvg_id"],
            ch["effective_id"],
        ]))
    dest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return len(m3u_channels)


def build_m3u_index(m3u_channels):
    """Build the matching index used to decide whether to keep an upstream channel."""
    tvg_ids = set()
    norm_names = set()
    callsigns = set()
    for ch in m3u_channels:
        if ch["tvg_id"]:
            tvg_ids.add(ch["tvg_id"])
        for name in (ch["tvg_name"], ch["title"]):
            n = normalize_name(name)
            if n and len(n) > 2:
                norm_names.add(n)
            cs = extract_callsign(name)
            if cs:
                callsigns.add(cs)
    return tvg_ids, norm_names, callsigns


# ---------------- upstream EPG handling ----------------

def fetch(url: str, dest: Path):
    """Download with retries."""
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "iptv-epg-builder/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
            return dest
        except Exception as e:
            last_err = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"failed to fetch {url}: {last_err}")


def read_xmltv(path: Path) -> bytes:
    """Read possibly gzipped XMLTV from disk, return raw bytes."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


# Streaming XMLTV parser: extract channel and programme elements without
# loading the whole tree into memory. Returns iter of (tag, attrs, inner_xml).
CHANNEL_RE = re.compile(rb"<channel\b[^>]*>.*?</channel>", re.DOTALL)
PROGRAMME_RE = re.compile(rb"<programme\b[^>]*?/>|<programme\b[^>]*?>.*?</programme>", re.DOTALL)
DISPLAY_NAME_RE = re.compile(rb"<display-name[^>]*>([^<]+)</display-name>")
CHANNEL_ID_RE = re.compile(rb'<channel\b[^>]*?\bid="([^"]+)"')
PROG_CHANNEL_RE = re.compile(rb'<programme\b[^>]*?\bchannel="([^"]+)"')


def iter_channels(xml_bytes: bytes):
    for m in CHANNEL_RE.finditer(xml_bytes):
        block = m.group(0)
        idm = CHANNEL_ID_RE.search(block)
        if not idm:
            continue
        cid = idm.group(1).decode("utf-8", errors="replace")
        names = [n.decode("utf-8", errors="replace") for n in DISPLAY_NAME_RE.findall(block)]
        yield cid, names, block


def iter_programmes(xml_bytes: bytes):
    for m in PROGRAMME_RE.finditer(xml_bytes):
        block = m.group(0)
        chm = PROG_CHANNEL_RE.search(block)
        if not chm:
            continue
        yield chm.group(1).decode("utf-8", errors="replace"), block


# ---------------- channel matching ----------------

def channel_matches(cid: str, display_names: list[str], tvg_ids: set, norm_names: set, callsigns: set) -> bool:
    if cid in tvg_ids:
        return True
    for n in display_names:
        nn = normalize_name(n)
        if nn and nn in norm_names:
            return True
        cs = extract_callsign(n)
        if cs and cs in callsigns:
            return True
    # channel id may itself be a callsign (e.g. 'KNBC-DT.us_locals1' or 'knbc.us')
    cid_head = cid.split(".")[0].upper()
    if _strip_us_callsign_suffix(cid_head) in callsigns:
        return True
    return False


# ---------------- main build ----------------

def main():
    m3u_url = os.environ.get("M3U_URL")
    if not m3u_url:
        print("ERROR: M3U_URL env var required", file=sys.stderr)
        return 2
    provider_url = os.environ.get("PROVIDER_EPG_URL", "")

    workdir = Path("epg-work")
    workdir.mkdir(exist_ok=True)
    out_dir = Path("docs")
    out_dir.mkdir(exist_ok=True)

    print(f"[1/6] fetching M3U...")
    m3u_path = workdir / "playlist.m3u"
    fetch(m3u_url, m3u_path)
    m3u_text = m3u_path.read_text(encoding="utf-8", errors="replace")
    m3u_channels = parse_m3u(m3u_text)
    print(f"      M3U entries: {len(m3u_channels)}")
    auto_n = assign_effective_ids(m3u_channels)
    print(f"      effective ids assigned: {len(m3u_channels) - auto_n} from M3U, {auto_n} auto-generated")
    tvg_ids, norm_names, callsigns = build_m3u_index(m3u_channels)
    # Include auto-generated effective ids in the matcher set so an upstream
    # source with that exact id (rare) still binds.
    tvg_ids |= {ch["effective_id"] for ch in m3u_channels}
    print(f"      index: {len(tvg_ids)} tvg-ids (incl. effective), {len(norm_names)} norm-names, {len(callsigns)} US callsigns")

    print(f"[2/6] fetching upstream EPGs from epgshare01...")
    upstream_paths = []
    with cf.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {}
        for name in EPGSHARE_FILES:
            url = f"{EPGSHARE_BASE}/epg_ripper_{name}.xml.gz"
            dest = workdir / f"{name}.xml.gz"
            futs[pool.submit(fetch, url, dest)] = name
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                p = fut.result()
                upstream_paths.append((name, p))
                print(f"      OK {name}: {p.stat().st_size//1024} KB")
            except Exception as e:
                print(f"      FAIL {name}: {e}")

    print(f"[3/6] fetching provider EPG (if configured)...")
    provider_path = None
    if provider_url:
        try:
            provider_path = workdir / "provider.xml"
            fetch(provider_url, provider_path)
            print(f"      OK provider: {provider_path.stat().st_size//1024} KB")
        except Exception as e:
            print(f"      FAIL provider: {e}")
            provider_path = None

    print(f"[4/6] filtering and merging channels...")
    # Output: build channel and programme dicts keyed by channel id.
    # Provider EPG takes priority (kept verbatim, all channels).
    kept_channels: dict[str, bytes] = {}
    kept_ids: set[str] = set()
    source_stats = {}

    if provider_path:
        print(f"      including provider EPG verbatim...")
        raw = read_xmltv(provider_path)
        for cid, names, block in iter_channels(raw):
            kept_channels[cid] = block
            kept_ids.add(cid)
        source_stats["provider"] = len(kept_ids)
        print(f"        +{source_stats['provider']} channels")

    for name, path in upstream_paths:
        raw = read_xmltv(path)
        before = len(kept_ids)
        count = 0
        for cid, names, block in iter_channels(raw):
            count += 1
            if cid in kept_ids:
                continue  # already have it from provider
            if channel_matches(cid, names, tvg_ids, norm_names, callsigns):
                kept_channels[cid] = block
                kept_ids.add(cid)
        added = len(kept_ids) - before
        source_stats[name] = added
        print(f"      {name}: scanned={count}, added={added}")

    print(f"      total kept channels: {len(kept_ids)}")

    print(f"[5/6] filtering and merging programmes...")
    kept_programmes: list[bytes] = []
    prog_count_by_source = {}

    if provider_path:
        raw = read_xmltv(provider_path)
        n = 0
        for chan_id, block in iter_programmes(raw):
            if chan_id in kept_ids:
                kept_programmes.append(block)
                n += 1
        prog_count_by_source["provider"] = n

    for name, path in upstream_paths:
        raw = read_xmltv(path)
        n = 0
        for chan_id, block in iter_programmes(raw):
            if chan_id in kept_ids:
                kept_programmes.append(block)
                n += 1
        prog_count_by_source[name] = n

    # Dedupe by (channel, start) — provider EPG was added first, so its
    # programmes win when the same slot appears in multiple sources.
    seen_keys = set()
    deduped = []
    for block in kept_programmes:
        m = re.search(rb'<programme\s+start="([^"]+)"[^>]*channel="([^"]+)"', block)
        if m:
            key = (m.group(1), m.group(2))
            if key in seen_keys:
                continue
            seen_keys.add(key)
        deduped.append(block)
    kept_programmes = deduped
    print(f"      total kept programmes (after dedupe): {len(kept_programmes)}")

    # ---------- dummy entries for uncovered tvg-ids ----------
    # Force-dummy list: tvg-ids the user marked as inaccurate. These get their
    # real EPG removed and replaced with dummies.
    override_path = Path("channels/dummy_override.txt")
    forced_ids: set[str] = set()
    if override_path.exists():
        for line in override_path.read_text().splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                forced_ids.add(line)
    print(f"[5b]  dummy override list: {len(forced_ids)} entries")

    if forced_ids:
        kept_channels = {cid: blk for cid, blk in kept_channels.items() if cid not in forced_ids}
        before = len(kept_programmes)
        kept_programmes = [
            blk for blk in kept_programmes
            if not (m := PROG_CHANNEL_RE.search(blk)) or m.group(1).decode("utf-8", "replace") not in forced_ids
        ]
        removed = before - len(kept_programmes)
        print(f"      removed {removed} programmes from overridden channels")
        kept_ids = set(kept_channels.keys())

    # Build map from effective_id -> M3U display name. Every M3U entry now
    # has an effective_id (original tvg-id or auto-generated).
    m3u_display = {}
    for ch in m3u_channels:
        tid = ch["effective_id"]
        name = ch["tvg_name"] or ch["title"]
        if name and tid not in m3u_display:
            m3u_display[tid] = name

    # ---------- backfill pass ----------
    # An upstream/provider channel often matches an M3U entry via callsign or
    # name, but the player binds by tvg-id strictly. Find those mismatches and
    # clone the upstream's channel+programmes under the M3U's effective_id so
    # real EPG data is actually displayed.
    print(f"[5c]  backfill pass (rewire upstream data to M3U effective ids)")
    backfill_cs: dict[str, str] = {}
    backfill_nn: dict[str, str] = {}
    for cid, block in kept_channels.items():
        names = [n.decode("utf-8", "replace") for n in DISPLAY_NAME_RE.findall(block)]
        for n in names:
            cs = extract_callsign(n)
            if cs:
                backfill_cs.setdefault(cs, cid)
            nn = normalize_name(n)
            if nn and len(nn) > 3:
                backfill_nn.setdefault(nn, cid)
        cid_cs = _strip_us_callsign_suffix(cid.split(".")[0].upper())
        if 3 <= len(cid_cs) <= 5 and cid_cs[0] in ("K", "W"):
            backfill_cs.setdefault(cid_cs, cid)

    progs_by_chan: dict[str, list[bytes]] = {}
    for p in kept_programmes:
        m = PROG_CHANNEL_RE.search(p)
        if m:
            sid = m.group(1).decode("utf-8", "replace")
            progs_by_chan.setdefault(sid, []).append(p)

    def rewrite_channel_id(block: bytes, old: str, new: str) -> bytes:
        return re.sub(
            rb'(<channel\b[^>]*?\bid=")' + re.escape(old.encode()) + rb'(")',
            lambda m: m.group(1) + new.encode() + m.group(2),
            block, count=1,
        )

    def rewrite_prog_channel(block: bytes, old: str, new: str) -> bytes:
        return re.sub(
            rb'(<programme\b[^>]*?\bchannel=")' + re.escape(old.encode()) + rb'(")',
            lambda m: m.group(1) + new.encode() + m.group(2),
            block, count=1,
        )

    backfilled = 0
    backfill_progs = 0
    backfill_added_programmes: list[bytes] = []
    for ch in m3u_channels:
        tid = ch["effective_id"]
        if tid in kept_ids or tid in forced_ids:
            continue
        candidate_cid = None
        for nm in (ch["tvg_name"], ch["title"]):
            if not nm:
                continue
            cs = extract_callsign(nm)
            if cs and cs in backfill_cs:
                candidate_cid = backfill_cs[cs]
                break
            nn = normalize_name(nm)
            if nn and nn in backfill_nn:
                candidate_cid = backfill_nn[nn]
                break
        if not candidate_cid:
            continue
        src_block = kept_channels[candidate_cid]
        new_block = rewrite_channel_id(src_block, candidate_cid, tid)
        kept_channels[tid] = new_block
        kept_ids.add(tid)
        backfilled += 1
        for src_prog in progs_by_chan.get(candidate_cid, []):
            backfill_added_programmes.append(rewrite_prog_channel(src_prog, candidate_cid, tid))
            backfill_progs += 1
    kept_programmes.extend(backfill_added_programmes)
    print(f"      backfilled {backfilled} M3U ids (+{backfill_progs} cloned programmes)")

    uncovered_ids = (set(m3u_display.keys()) - kept_ids) | forced_ids
    uncovered_ids = {tid for tid in uncovered_ids if tid in m3u_display}
    print(f"      dummy entries to add: {len(uncovered_ids)} (covers every remaining M3U channel)")

    # Snap dummy block to local-midnight GMT+3, span 8 days (yesterday + 7).
    now_utc = dt.datetime.now(dt.timezone.utc)
    local_now = now_utc + USER_TZ_OFFSET
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    block_start_utc = local_midnight - USER_TZ_OFFSET - dt.timedelta(days=1)
    block_stop_utc = block_start_utc + dt.timedelta(days=8)

    def fmt_xmltv_time(t: dt.datetime) -> str:
        return t.strftime("%Y%m%d%H%M%S +0000")

    start_str = fmt_xmltv_time(block_start_utc)
    stop_str = fmt_xmltv_time(block_stop_utc)

    dummy_channels: list[bytes] = []
    dummy_programmes: list[bytes] = []
    for tid in sorted(uncovered_ids):
        name_xml = html.escape(m3u_display[tid], quote=True)
        tid_xml = html.escape(tid, quote=True)
        ch_block = (
            f'<channel id="{tid_xml}"><display-name>{name_xml}</display-name></channel>'
        ).encode("utf-8")
        dummy_channels.append(ch_block)
        p = (
            f'<programme start="{start_str}" stop="{stop_str}" channel="{tid_xml}">'
            f'<title lang="en">No EPG</title></programme>'
        ).encode("utf-8")
        dummy_programmes.append(p)

    for blk in dummy_channels:
        m = CHANNEL_ID_RE.search(blk)
        if m:
            cid = m.group(1).decode("utf-8", "replace")
            kept_channels[cid] = blk
            kept_ids.add(cid)
    kept_programmes.extend(dummy_programmes)
    print(f"      added {len(dummy_channels)} dummy channels (single 8-day block each)")

    print(f"[6/6] writing output...")
    out_xml = out_dir / "guide.xml"
    out_gz = out_dir / "guide.xml.gz"

    header = (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<tv generator-info-name="iptv-epg-unified" '
        b'source-info-name="epgshare01.online + provider">\n'
    )
    footer = b"</tv>\n"

    # Full version (gzipped) — keeps descriptions and all metadata.
    with gzip.open(out_gz, "wb", compresslevel=6) as f:
        f.write(header)
        for cid in sorted(kept_channels):
            f.write(kept_channels[cid])
            f.write(b"\n")
        for p in kept_programmes:
            f.write(p)
            f.write(b"\n")
        f.write(footer)

    # Lite version (uncompressed) — title-only programmes so file stays under
    # GitHub Pages' 100 MB per-file limit. Strips <desc>, <credits>, <icon>,
    # <category>, <rating>, <star-rating>, <country>, <url>, <episode-num>,
    # <sub-title>, <date>, <language>, <orig-language>, <length>.
    strip_children = re.compile(
        rb"<(?:desc|credits|icon|category|rating|star-rating|country|url|"
        rb"episode-num|sub-title|date|language|orig-language|length|"
        rb"video|audio|previously-shown|premiere|last-chance|new|"
        rb"subtitles|review)\b[^>]*?(?:/>|>.*?</(?:desc|credits|icon|"
        rb"category|rating|star-rating|country|url|episode-num|sub-title|"
        rb"date|language|orig-language|length|video|audio|"
        rb"previously-shown|premiere|last-chance|new|subtitles|review)>)",
        re.DOTALL,
    )

    def strip_programme(block: bytes) -> bytes:
        return strip_children.sub(b"", block)

    with open(out_xml, "wb") as f:
        f.write(header)
        for cid in sorted(kept_channels):
            # Also strip icons from channel definitions in the lite version
            chan_stripped = strip_children.sub(b"", kept_channels[cid])
            f.write(chan_stripped)
            f.write(b"\n")
        for p in kept_programmes:
            f.write(strip_programme(p))
            f.write(b"\n")
        f.write(footer)

    print(f"      wrote {out_xml} ({out_xml.stat().st_size//1024} KB) — titles only")
    print(f"      wrote {out_gz} ({out_gz.stat().st_size//1024} KB) — full data, gzipped")

    # Publish the non-sensitive tvg-id map. The user uses patch_m3u.py locally
    # to inject these tvg-ids into their private M3U. We do NOT publish the
    # full M3U because it embeds the user's stream auth tokens.
    out_map = out_dir / "tvg-id-map.tsv"
    written = write_tvg_id_map(m3u_channels, out_map)
    print(f"      wrote {out_map} ({out_map.stat().st_size//1024} KB, {written} rows)")

    print()
    print("=== source breakdown (channels) ===")
    for src, n in source_stats.items():
        print(f"  {src:15s} {n:>6}")
    print()
    print("=== source breakdown (programmes) ===")
    for src, n in prog_count_by_source.items():
        print(f"  {src:15s} {n:>6}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
