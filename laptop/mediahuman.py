
import os
if os.environ.get("TRINITY_ENABLE_YTDLP_POT_PLUGIN", "").strip().lower() not in {"1", "true", "yes", "on"}:
    os.environ.setdefault("YTDLP_NO_PLUGINS", "1")

import json
import time
import argparse
from contextlib import contextmanager
import re
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from youtube_url_tag import (
    tag_downloaded_mp3_with_youtube_url, youtube_watch_url, recover_completed_mp3_ids,
)
from shared_history import load_set, save_set, flush_pending

# ==================================================================
# CONFIGURATION
# ==================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "_working_downloads")
HISTORY_FILE = "Google Sheets / History / download_history"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLBkuXLqNhqX5FsS2CEaSDlGTKAHIBPtLe"
ALREADY_DOWNLOADED_STREAK_LIMIT = 30
if os.environ.get("TRINITY_SKIP_PLAYLIST_ON_30", "1") == "0":
    ALREADY_DOWNLOADED_STREAK_LIMIT = float("inf")

# Use Firefox instead of Chrome to avoid DPAPI issues on Windows
BROWSER = ("firefox",)
COOKIES_FILE = os.path.join(BASE_DIR, "youtube_cookies.txt")

# Default behavior:
# try with cookies first, fall back to no cookies
USE_COOKIES_FOR_PLAYLIST = True
COOKIE_CLIENT_GROUPS = [
    None,
    ["tv"],
    ["web_safari"],
    ["web"],
    ["web", "web_safari"],
]

NO_COOKIE_CLIENT_GROUPS = [
    None,
    ["android_vr"],
    ["tv"],
    ["android_vr", "tv"],
    ["web_safari"],
    ["web"],
    ["web", "web_safari"],
]

FORMAT_CANDIDATES = [
    "bestaudio/best",
    "bestaudio[ext=m4a]/bestaudio/best[acodec!=none]/18/best",
    "140",
    "251",
    "250",
    "249",
]


def make_cookie_opts():
    if os.path.exists(COOKIES_FILE):
        return {"cookiefile": COOKIES_FILE}
    return {"cookiesfrombrowser": BROWSER}


def make_common_ydl_opts(use_cookies=False, video_download=False, player_clients=None):
    opts = {
        # Python API format (dict, not list)
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],

        # retry/network hardening
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 10,
        "socket_timeout": 30,
    }

    if player_clients:
        safe_clients = [client for client in player_clients if not (use_cookies and client == "android_vr")]
        if safe_clients:
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": safe_clients,
                }
            }

    if use_cookies:
        opts.update(make_cookie_opts())

    if video_download:
        opts["sleep_interval"] = 5
        opts["max_sleep_interval"] = 10

    return opts


def load_history():
    # All downloaders share one history in Google Sheets.
    return load_set("download_history")


def save_history(history):
    # Append only this process's additions/deletions, not a stale full snapshot.
    save_set("download_history", history)


def extract_playlist_entries(playlist_url, use_cookies=False):
    """Fetch playlist metadata only."""
    lazy = os.environ.get("TRINITY_LAZY_PLAYLIST", "0") == "1"
    ydl_opts_extract = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": False,
        "lazy_playlist": lazy,
        **make_common_ydl_opts(use_cookies=use_cookies),
        "extractor_args": {
            "youtube": {
                "player_client": ["web"],
            },
        },
    }

    # Materialize the generator while YoutubeDL is still open: in streaming
    # mode it may need further playlist-page HTTP requests during iteration.
    with YoutubeDL(ydl_opts_extract) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
        # Complete ID enumeration is required to validate the 30-match stop.
        return list(info.get("entries") or [])



class PlaylistFetchError(RuntimeError):
    """A playlist metadata page could not be fetched."""


@contextmanager
def open_playlist_scan(playlist_url, use_cookies=False):
    """Iterate only the required playlist pages; hold yt-dlp open until done."""
    stream = (os.environ.get("TRINITY_LAZY_PLAYLIST", "0") == "1"
              or ALREADY_DOWNLOADED_STREAK_LIMIT != float("inf"))
    opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": False,
        "lazy_playlist": stream,
        **make_common_ydl_opts(use_cookies=use_cookies),
        "extractor_args": {"youtube": {"player_client": ["web"]}},
    }
    with YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(playlist_url, download=False, process=False)
            if not isinstance(info, dict) or info.get("entries") is None:
                raise PlaylistFetchError("YouTube returned no playlist entries")
            entries = info["entries"]
            if not stream:
                entries = list(entries)
            count = info.get("playlist_count") if stream else len(entries)
        except Exception as exc:
            raise PlaylistFetchError("Could not fetch playlist metadata") from exc

        if stream:
            print("Streaming playlist entries; fetching only as far as needed.")
        else:
            print(f"Playlist has {count} entries (YouTube playlist order).")

        def checked_entries():
            try:
                yield from entries
            except Exception as exc:
                raise PlaylistFetchError("Could not fetch the next playlist page") from exc

        yield checked_entries(), count

def try_download_video(
    video_id,
    max_attempts=1,
    use_cookies=False,
    player_clients=None,
    format_selector="bestaudio/best",
):
    """Try downloading one video with retries."""
    ydl_opts_dl = {
        "format": format_selector,
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
        "continuedl": True,  # Resume retained .part fragments after interruption.
        "nopart": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": False,
        **make_common_ydl_opts(
            use_cookies=use_cookies,
            video_download=True,
            player_clients=player_clients,
        ),
    }

    video_url = youtube_watch_url(video_id)

    for attempt in range(1, max_attempts + 1):
        try:
            mode = "with cookies" if use_cookies else "without cookies"
            print(
                f"Downloading {video_id} | attempt {attempt}/{max_attempts} | "
                f"{mode} | clients={player_clients or 'default'} | format={format_selector}"
            )
            started_at = time.time()
            ydl_opts_no_ignore = {**ydl_opts_dl, "ignoreerrors": False}
            with YoutubeDL(ydl_opts_no_ignore) as ydl:
                info = ydl.extract_info(video_url, download=True)
                completed_path = tag_downloaded_mp3_with_youtube_url(
                    info=info,
                    ydl=ydl,
                    youtube_url=video_url,
                    download_dir=DOWNLOAD_DIR,
                    started_at=started_at,
                    video_id=video_id,
                )
            if not completed_path:
                # yt-dlp can exit successfully without a verified final MP3.
                # Do not advance the playlist's durable download history.
                print(f"Download produced no verified MP3: {video_id}")
                return False
            return True

        except DownloadError as e:
            msg = str(e)
            if "Video unavailable" in msg or "video has been removed" in msg:
                print(f"Video unavailable (permanent), skipping: {video_id}")
                return None
            if "Sign in to confirm" in msg or "not a bot" in msg:
                print(f"YouTube auth/bot gate hit for {video_id}; skipping (no usable cookies).")
                return None
            if "Please sign in" in msg or "Private video" in msg:
                print(f"YouTube auth/private video for {video_id}; skipping (no usable cookies).")
                return None
            mode = "with cookies" if use_cookies else "without cookies"
            print(f"Attempt {attempt}/{max_attempts} failed ({mode}): {e}")

        except KeyboardInterrupt:
            raise

        except Exception as e:
            mode = "with cookies" if use_cookies else "without cookies"
            print(f"Attempt {attempt}/{max_attempts} failed ({mode}): {e}")

        if attempt < max_attempts:
            wait_seconds = min(2 ** attempt, 10)
            print(f"Waiting {wait_seconds}s before retry...")
            time.sleep(wait_seconds)

    return False


def process_video_entries(video_entries, count, history, streak_limit):
    """Process in playlist order and stop immediately on the selected streak."""
    last_history_sync = time.monotonic()
    # ------------------------------------------------------------------
    # DOWNLOAD EACH VIDEO
    # ------------------------------------------------------------------
    already_downloaded_streak = 0

    for index, entry in enumerate(video_entries, start=1):
        if not isinstance(entry, dict):
            print(f"Skipping playlist entry #{index}: no metadata")
            already_downloaded_streak = 0
            continue
        video_id = entry.get("id")
        video_title = entry.get("title") or video_id

        if not video_id:
            print(f"Skipping entry #{index}: missing video id")
            already_downloaded_streak = 0
            continue

        # Read new entries from other laptops during long-running playlists.
        # Throttling prevents excessive Sheets API requests.
        if time.monotonic() - last_history_sync >= 30:
            history.update(load_history())
            last_history_sync = time.monotonic()

        if video_id in history:
            print(f"Skipping already downloaded: {video_title}")
            already_downloaded_streak += 1
            if already_downloaded_streak >= streak_limit:
                print(f"Reached {streak_limit} consecutive already-downloaded "
                      f"videos at playlist position {index}; stopping without "
                      "fetching older entries.")
                return
            continue

        already_downloaded_streak = 0
        progress = f"{index}/{count}" if count is not None else str(index)
        print(f"[{progress}] Downloading: {video_title}")

        success = False

        for clients in COOKIE_CLIENT_GROUPS:
            for fmt in FORMAT_CANDIDATES:
                success = try_download_video(
                    video_id,
                    max_attempts=1,
                    use_cookies=True,
                    player_clients=clients,
                    format_selector=fmt,
                )
                if success:
                    break
                if success is None:
                    break
            if success:
                break
            if success is None:
                break

        # Fall back to no cookies only if needed (skip if video was removed permanently)
        if success is None:
            print(f"Video unavailable, giving up: {video_id}")
            continue
        elif not success:
            print("Retrying without cookies...")
            for clients in NO_COOKIE_CLIENT_GROUPS:
                for fmt in FORMAT_CANDIDATES:
                    success = try_download_video(
                        video_id,
                        max_attempts=1,
                        use_cookies=False,
                        player_clients=clients,
                        format_selector=fmt,
                    )
                    if success:
                        break
                    if success is None:
                        break
                if success:
                    break
                if success is None:
                    break

            if success is None:
                print(f"Video unavailable, giving up: {video_id}")
                continue

        if success:
            history.add(video_id)
            save_history(history)
            print(f"Saved to history: {video_title}")
        else:
            print(f"Giving up for now: {video_title}")



def download_playlist(playlist_url, failed_ids=None):
    """Download only the needed entries and retain history idempotency."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    history = load_history()
    recovered = recover_completed_mp3_ids(
        DOWNLOAD_DIR, os.path.join(BASE_DIR, "finished")
    )
    recovered_ids = set(recovered) - history
    if recovered_ids:
        save_history(history | recovered_ids)
        history.update(recovered_ids)
        print(f"Recovered {len(recovered_ids)} completed tagged MP3(s) into local "
              "pending download history.", flush=True)
    print(f"Loaded {len(history)} previously downloaded entries.")

    if failed_ids is not None:
        # Targeted thumbnail retry still never queries the playlist.
        video_entries = [{"id": video_id, "title": video_id} for video_id in failed_ids]
        print(f"Retrying {len(video_entries)} failed thumbnail.py video(s) only.")
        process_video_entries(video_entries, len(video_entries), history, float("inf"))
    else:
        print("Fetching playlist information...")
        try:
            with open_playlist_scan(playlist_url, USE_COOKIES_FOR_PLAYLIST) as (entries, count):
                process_video_entries(entries, count, history, ALREADY_DOWNLOADED_STREAK_LIMIT)
        except PlaylistFetchError as exc:
            print(f"Playlist scan failed in current cookie mode: {exc}")
            print("Retrying playlist scan with the alternate cookie mode...")
            with open_playlist_scan(playlist_url, not USE_COOKIES_FOR_PLAYLIST) as (entries, count):
                process_video_entries(entries, count, history, ALREADY_DOWNLOADED_STREAK_LIMIT)

    flush_pending()  # One quota-friendly Sheets upload for the entire scan.
    print("All done!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--failed-ids-json", default=None)
    args = parser.parse_args()
    ids = None
    if args.failed_ids_json is not None:
        with open(args.failed_ids_json, "r", encoding="utf-8") as handle:
            ids = json.load(handle)
        if not isinstance(ids, list) or any(
            not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", item)
            for item in ids
        ):
            parser.error("Invalid thumbnail failure manifest")
        ids = list(dict.fromkeys(ids))
    download_playlist(PLAYLIST_URL, failed_ids=ids)
