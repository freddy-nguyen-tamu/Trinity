import os
if os.environ.get("TRINITY_ENABLE_YTDLP_POT_PLUGIN", "").strip().lower() not in {"1", "true", "yes", "on"}:
    os.environ.setdefault("YTDLP_NO_PLUGINS", "1")

import json
import time
from yt_dlp import YoutubeDL
from youtube_url_tag import (
    tag_downloaded_mp3_with_youtube_url, youtube_watch_url, recover_completed_mp3_ids,
)
from shared_history import load_set, save_set, flush_pending
from playlist_scan import last_unprocessed_position, safe_to_stop

# Config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "_working_downloads")
HISTORY_FILE = "Google Sheets / History / download_history"
ALREADY_DOWNLOADED_STREAK_LIMIT = 30
if os.environ.get("TRINITY_SKIP_PLAYLIST_ON_30", "1") == "0":
    ALREADY_DOWNLOADED_STREAK_LIMIT = float("inf")

# Use Firefox instead of Chrome to avoid DPAPI issues on Windows
BROWSER = ("firefox",)
COOKIES_FILE = os.path.join(BASE_DIR, "youtube_cookies.txt")


def make_cookie_opts():
    if os.path.exists(COOKIES_FILE):
        return {"cookiefile": COOKIES_FILE}
    return {"cookiesfrombrowser": BROWSER}


def load_history():
    # All downloaders share one history in Google Sheets.
    return load_set("download_history")


def save_history(history):
    # Append only this process's additions/deletions, not a stale full snapshot.
    save_set("download_history", history)


def download_playlist(playlist_url):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    history = load_history()
    recovered = recover_completed_mp3_ids(
        DOWNLOAD_DIR, os.path.join(BASE_DIR, "finished")
    )
    recovered_ids = set(recovered) - history
    if recovered_ids:
        # Durable local journal is fsynced before playlist processing starts.
        # If syncing Google fails, these completed files remain locally
        # recoverable and will not be downloaded again after restarting.
        save_history(history | recovered_ids)
        history.update(recovered_ids)
        print(f"Recovered {len(recovered_ids)} completed tagged MP3(s) into local "
              "pending download history.", flush=True)
    last_history_sync = time.monotonic()
    print(f"Loaded {len(history)} previously downloaded entries.")

    # Fetch playlist info
    print("Fetching playlist information...")
    lazy = os.environ.get("TRINITY_LAZY_PLAYLIST", "0") == "1"
    ydl_opts_extract = {
        "extract_flat": True,
        "skip_download": True,
        **make_cookie_opts(),
        "retries": 10,
        "extractor_retries": 10,
        "socket_timeout": 30,
        "lazy_playlist": lazy,
        "extractor_args": {
            "youtube": {
                "player_client": ["web"],
            },
        },
    }

    # Keep the extractor alive while consuming streamed playlist pages.
    with YoutubeDL(ydl_opts_extract) as ydl:
        info = ydl.extract_info(playlist_url, download=False)
        video_entries = list(info.get("entries") or [])
    print(f"Playlist has {len(video_entries)} videos (YouTube playlist order).")
    last_unprocessed = last_unprocessed_position(video_entries, history)
    print(f"Last unprocessed playlist position: {last_unprocessed or 'none'}; "
          "30-match early stop cannot skip IDs farther down.")

    # Download each video
    already_downloaded_streak = 0

    for index, entry in enumerate(video_entries, start=1):
        if not entry or not entry.get("id"):
            already_downloaded_streak = 0
            print(f"Skipping playlist entry #{index}: missing video ID")
            continue
        video_id = entry.get("id")
        video_title = entry.get("title")

        # Read new entries from other laptops during long-running playlists.
        # Throttling prevents excessive Sheets API requests.
        if time.monotonic() - last_history_sync >= 30:
            history = load_history()
            last_history_sync = time.monotonic()

        if video_id in history:
            print(f"Skipping already downloaded: {video_title}")
            already_downloaded_streak += 1
            if safe_to_stop(already_downloaded_streak, ALREADY_DOWNLOADED_STREAK_LIMIT,
                            index, last_unprocessed):
                print(
                    f"Reached {ALREADY_DOWNLOADED_STREAK_LIMIT} already-downloaded "
                    "videos in a row; no later unprocessed IDs; stopping."
                )
                break
            if already_downloaded_streak == ALREADY_DOWNLOADED_STREAK_LIMIT:
                print(f"Reached {ALREADY_DOWNLOADED_STREAK_LIMIT} consecutive matches, "
                      f"but an unprocessed ID exists at position {last_unprocessed}; "
                      "continuing in playlist order.")
            continue

        already_downloaded_streak = 0
        print(f"⬇ Downloading: {video_title}")

        ydl_opts_dl = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
        "continuedl": True,  # Resume retained .part fragments after interruption.
        "nopart": False,
            "writethumbnail": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                },
                {
                    "key": "EmbedThumbnail",
                }
            ],
            "quiet": False,
            **make_cookie_opts(),
        }

        try:
            video_url = youtube_watch_url(video_id)
            started_at = time.time()
            with YoutubeDL(ydl_opts_dl) as ydl:
                info = ydl.extract_info(video_url, download=True)
                tag_downloaded_mp3_with_youtube_url(
                    info=info,
                    ydl=ydl,
                    youtube_url=video_url,
                    download_dir=DOWNLOAD_DIR,
                    started_at=started_at,
                    video_id=video_id,
                )
        except Exception as e:
            print(f"Error downloading {video_title}: {e}")
            continue

        # A failed Sheet write must be reported as such, not silently skipped.
        history.add(video_id)
        save_history(history)

    flush_pending()  # One quota-friendly Sheets upload for the entire playlist.
    print("All done!")


if __name__ == "__main__":
    playlist_url = "https://www.youtube.com/playlist?list=PLBkuXLqNhqX5FsS2CEaSDlGTKAHIBPtLe"
    download_playlist(playlist_url)

