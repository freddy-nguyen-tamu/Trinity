#!/usr/bin/env python3
"""Mark the cutoff video AND OLDER entries of Trinity's fixed playlist as done.

This one-time seed runs on the CURRENT Windows laptop; it requires no old
laptop, audio downloads, Chrome, or Selenium. It reuses Trinity's exact
mediahuman.extract_playlist_entries() / yt-dlp configuration.

Trinity's playlist is ordered newest-first. Earlier releases mistakenly took
entries from playlist START to cutoff (only 10 entries for this cutoff). We
instead include the cutoff and every entry AFTER it, which are the older
entries. The 10 entries imported previously remain in the shared Sheet.

Use --preview to check enumeration before writing or --scope all to explicitly
mark every currently visible playlist entry. Only ID-bearing entries can be
reconstructed; unavailable entries without IDs are reported, not fabricated.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

DEFAULT_URL = "https://www.youtube.com/playlist?list=PLBkuXLqNhqX5FsS2CEaSDlGTKAHIBPtLe"
DEFAULT_CUTOFF = "h4xDaP2EiuU"
VALID_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
# Detect yt-dlp serving a truncated playlist instead of a ~3,000-item list.
DEFAULT_MIN_ENTRIES = 1000


def parse_url(url, cutoff=DEFAULT_CUTOFF):
    parsed = urlparse(url)
    if parsed.hostname not in (
        "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"
    ):
        raise ValueError("Expected a YouTube playlist URL")
    query = parse_qs(parsed.query)
    playlist = query.get("list", [""])[0]
    cutoff = cutoff or query.get("v", [""])[0]
    if not VALID_ID.fullmatch(cutoff):
        raise ValueError("Cutoff must be an 11-character YouTube video ID")
    if not playlist.startswith(("PL", "UU", "FL", "OL", "RD", "LL")):
        raise ValueError("Expected a YouTube playlist URL with a list= parameter")
    return cutoff, playlist


def select_ids(entries, cutoff, scope="older", min_entries=DEFAULT_MIN_ENTRIES):
    """Select validated IDs from cutoff to END, not start to cutoff.

    The playlist is fetched completely by mediahuman.py. A minimum size guard
    prevents an accidental partial enumeration from being treated as complete.
    Missing or private entries with no ID are reported as gaps. Never invent
    video IDs for such entries.

    Returns (ordered_distinct_ids, total_count, cutoff_position, gaps).
    """
    if not isinstance(entries, list) or not entries:
        raise ValueError("yt-dlp returned no playlist entries")
    if len(entries) < min_entries:
        raise ValueError(
            f"yt-dlp returned only {len(entries)} entries, but the playlist "
            f"is expected to contain about 3,000 (minimum {min_entries}). "
            "Refusing to seed an apparently truncated playlist."
        )
    cutoff_positions = [
        i for i, entry in enumerate(entries)
        if isinstance(entry, dict) and str(entry.get("id") or "") == cutoff
    ]
    if not cutoff_positions:
        raise ValueError(
            f"Cutoff {cutoff} was not found among {len(entries)} yt-dlp entries; "
            "no history was changed."
        )
    cutoff_position = cutoff_positions[0] + 1
    start = 0 if scope == "all" else cutoff_position - 1
    ordered = []
    gaps = []
    seen = set()
    for i in range(start, len(entries)):
        entry = entries[i]
        video_id = str(entry.get("id") or "") if isinstance(entry, dict) else ""
        if not VALID_ID.fullmatch(video_id):
            gaps.append(i + 1)
            continue
        if video_id not in seen:
            seen.add(video_id)
            ordered.append(video_id)
    if cutoff not in seen:
        raise ValueError("Selected entries do not include the cutoff ID")
    return ordered, len(entries), cutoff_position, gaps


def extract_yt_dlp(url, cutoff, scope, min_entries):
    """Reuse the existing headless yt-dlp extractor, with the full playlist."""
    import mediahuman

    previous_lazy = os.environ.get("TRINITY_LAZY_PLAYLIST")
    os.environ["TRINITY_LAZY_PLAYLIST"] = "0"
    failures = []
    try:
        for use_cookies in (True, False):
            mode = "saved YouTube/Firefox cookies" if use_cookies else "without cookies"
            print(f"Fetching full Trinity playlist with yt-dlp ({mode})...", flush=True)
            try:
                entries = mediahuman.extract_playlist_entries(
                    url, use_cookies=use_cookies
                )
                result = select_ids(entries, cutoff, scope, min_entries)
                print(f"Playlist enumerated successfully via yt-dlp ({mode}).", flush=True)
                return result
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                failures.append(f"{mode}: {exc}")
                print(f"Playlist attempt failed ({mode}): {exc}", file=sys.stderr)
        raise RuntimeError(
            "Trinity's yt-dlp extractor could not obtain a full playlist "
            f"containing {cutoff}; no history was changed. "
            + " | ".join(failures)
        )
    finally:
        if previous_lazy is None:
            os.environ.pop("TRINITY_LAZY_PLAYLIST", None)
        else:
            os.environ["TRINITY_LAZY_PLAYLIST"] = previous_lazy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default=DEFAULT_URL,
                        help="YouTube playlist URL (default: Trinity's fixed playlist)")
    parser.add_argument("--cutoff", default=DEFAULT_CUTOFF,
                        help="The video at the boundary of the already-downloaded older entries")
    parser.add_argument("--scope", choices=("older", "all"), default="older",
                        help="older = cutoff through playlist end (default); all = entire playlist")
    parser.add_argument("--min-entries", type=int, default=DEFAULT_MIN_ENTRIES,
                        help="Safety floor against truncated playlists (default: 1000)")
    parser.add_argument("--preview", action="store_true",
                        help="Enumerate and print the exact selection; do not write to Sheets")
    args = parser.parse_args(argv)
    if args.min_entries < 1:
        parser.error("--min-entries must be at least 1")
    cutoff, playlist = parse_url(args.url, args.cutoff)
    if playlist != parse_url(DEFAULT_URL)[1]:
        print("WARNING: Importing a different playlist from Trinity's default.")
    print(f"Playlist: {playlist}\nCutoff video: {cutoff}\nScope: {args.scope}", flush=True)
    ids, total, position, gaps = extract_yt_dlp(
        args.url, cutoff, args.scope, args.min_entries
    )
    print("Source: Trinity mediahuman.py / yt-dlp; no visible browser used")
    print(f"Total playlist entries enumerated: {total:,}")
    print(f"Cutoff position: {position:,} of {total:,}")
    print(f"Selected: {len(ids):,} distinct IDs "
          + ("from cutoff to playlist END (older entries)" if args.scope == "older"
             else "from the entire playlist"))
    print("First 5 selected:", ", ".join(ids[:5]))
    print("Last 5 selected: ", ", ".join(ids[-5:]))
    if gaps:
        print(f"WARNING: {len(gaps):,} unavailable/malformed playlist entries have "
              "no usable video ID (first positions: "
              f"{', '.join(map(str, gaps[:10]))}). Those IDs cannot be guessed.")
    print("Playlist edits since your old laptop was used cannot be verified.")
    if args.preview:
        print("PREVIEW ONLY: shared Google Sheet unchanged.")
        return 0

    # Import ALL FIVE namespaces in ONE shared-sheet read and (typically)
    # ONE append request. No per-ID or per-history writes. The remote sheet
    # decides which IDs were already written by a previous attempt.
    from shared_history import merge_many_histories, shared_sheet_url

    alias_ids = {"youtube:" + video_id for video_id in ids}
    history = {
        "download_history": {video_id: True for video_id in ids},
        "android_uploaded": {alias: True for alias in alias_ids},
        "drive_uploaded": {alias: True for alias in alias_ids},
        "test_processed": {alias: True for alias in alias_ids},
        "lyrics_not_found": {
            alias: {
                "video_id": alias.removeprefix("youtube:"),
                "history_seed": True,
                "source": "trinity_fixed_playlist_cutoff_and_older",
                "lyrics_failure_verified": False,
            }
            for alias in alias_ids
        },
    }
    _, writes = merge_many_histories(history)
    print("UPDATED:", shared_sheet_url())
    print(f"Verified {len(ids):,} selected video IDs across ALL FIVE shared-history "
          f"categories using {writes} bulk write request(s).")
    print("Existing records and previously deleted keys were preserved; "
          "rerunning safely skips previously imported IDs.")
    print("NOTE: This seeds claimed historical state; it does not prove "
          "actual uploads or lyrics failures.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
