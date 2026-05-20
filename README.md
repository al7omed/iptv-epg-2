# iptv-epg-2

Self-updating unified XMLTV EPG for the second IPTV subscription (World 8K / mil79711). Same architecture as [iptv-epg](https://github.com/al7omed/iptv-epg): merges provider EPG + epgshare01 per-region guides, backfills M3U channel ids, adds "No EPG" dummies for uncovered entries. Every M3U channel gets EPG data in the player grid.

## What to paste into your player

**EPG URL** (12 MB gzipped, full programme descriptions, 12-hour cron refresh):

```
https://al7omed.github.io/iptv-epg-2/guide.xml.gz
```

**EPG fallback** (titles only, uncompressed):

```
https://al7omed.github.io/iptv-epg-2/guide.xml
```

## Patching your M3U for full coverage

Your subscription's M3U has ~54k streams. Many lack a `tvg-id` in the original — the player can't bind them to EPG without one. The build publishes a non-sensitive map at:

```
https://al7omed.github.io/iptv-epg-2/tvg-id-map.tsv
```

Run the patch script locally to inject `tvg-id`s into a copy of your M3U:

```sh
# fetch your M3U locally (keep this file private — never commit)
curl -fsSL "<your private M3U URL>" -o ~/playlist2.m3u

# patch with auto tvg-ids from the published map
curl -fsSL https://raw.githubusercontent.com/al7omed/iptv-epg-2/main/scripts/patch_m3u.py -o ~/patch_m3u.py
python3 ~/patch_m3u.py ~/playlist2.m3u ~/playlist2_patched.m3u
```

Use `~/playlist2_patched.m3u` as your player's M3U source. Re-run when the channel list changes.

## How it differs from iptv-epg

- Different provider package (World 8K / Xtream Codes API at `mil79711.wd.business-cdn-8k.com`).
- The workflow has a **Xtream API fallback**: if the direct `/get.php` URL is blocked by the CDN's WAF (some IPs receive HTTP 884), it synthesizes an M3U from `player_api.php?action=get_live_streams`.
- Provider EPG is much larger here (~77 MB raw, 8,228 channels — includes Hungarian/EU lineups).

## Configuration

GitHub Secrets:

- `M3U_URL` — your M3U playlist URL (`get.php?...&type=m3u_plus&output=ts`).
- `PROVIDER_EPG_URL` — provider's XMLTV URL (`xmltv.php?...`).
- `XTREAM_HOST` — e.g. `http://mil79711.wd.business-cdn-8k.com` (for fallback).
- `XTREAM_USER`, `XTREAM_PASS` — for fallback.

None of these appear in the repo or commit logs.

## Manual refresh

```sh
gh workflow run update-epg.yml -R al7omed/iptv-epg-2
```

## Marking inaccurate channels as dummies

Add the `effective_tvg_id` (from `tvg-id-map.tsv`) to `channels/dummy_override.txt`, one per line. The next build drops that channel's real EPG and uses a dummy block instead.

## Sister repos

- [iptv-epg](https://github.com/al7omed/iptv-epg) — first subscription.
- [bein-epg](https://github.com/al7omed/bein-epg) — focused, high-fidelity beIN MENA scraper.
