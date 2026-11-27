import os
if os.environ.get("TRINITY_ENABLE_YTDLP_POT_PLUGIN", "").strip().lower() not in {"1", "true", "yes", "on"}:
    os.environ.setdefault("YTDLP_NO_PLUGINS", "1")

import json
import sys
import time
import argparse
from contextlib import contextmanager
from yt_dlp import YoutubeDL
from yt_dlp.version import __version__ as YTDLP_VERSION
import yt_dlp
from yt_dlp.utils import DownloadError
from youtube_url_tag import (
    tag_downloaded_mp3_with_youtube_url, youtube_watch_url, recover_completed_mp3_ids,
)
from shared_history import load_set, save_set, flush_pending

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "_working_downloads")
HISTORY_FILE = "Google Sheets / History / download_history"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLBkuXLqNhqX5FsS2CEaSDlGTKAHIBPtLe"
ALREADY_DOWNLOADED_STREAK_LIMIT = 30
if os.environ.get("TRINITY_SKIP_PLAYLIST_ON_30", "1") == "0":
    ALREADY_DOWNLOADED_STREAK_LIMIT = float("inf")

BROWSER = ("firefox",)
COOKIES_FILE = os.path.join(BASE_DIR, "youtube_cookies.txt")


def make_cookie_opts():
    if os.path.exists(COOKIES_FILE):
        return {"cookiefile": COOKIES_FILE}
    return {"cookiesfrombrowser": BROWSER}

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


def load_history():
    # All downloaders share one history in Google Sheets.
    return load_set("download_history")


def save_history(history):
    # Append only this process's additions/deletions, not a stale full snapshot.
    save_set("download_history", history)


def make_common_opts(use_cookies=False, player_clients=None, video_download=False):
    opts = {
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 10,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "ignoreerrors": True,
        "quiet": False,
        "verbose": False,
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

    # Playlist extraction uses "web" only, and must NOT skip the webpage player
    # (skipping webpage blocks pagination and fetches only ~100 items).
    # Video downloads use web/web_safari clients with cookies for auth.
    if video_download:
        opts["sleep_interval"] = 5
        opts["max_sleep_interval"] = 10

    return opts


def is_private_or_unavailable(entry):
    if not entry:
        return True

    title = (entry.get("title") or "").strip().lower()
    availability = (entry.get("availability") or "").strip().lower()

    if entry.get("id") is None:
        return True

    if title in {"[private video]", "private video", "[deleted video]", "deleted video"}:
        return True

    if availability in {"private", "subscriber_only", "premium_only"}:
        return True

    return False


class PlaylistFetchError(RuntimeError):
    """Playlist metadata could not be fetched, including a later page."""


@contextmanager
def get_playlist_entries(playlist_url, use_cookies=False):
    """Keep yt-dlp open while consuming playlist pages on demand.

    `extract_info(process=False)` returns the extractor's raw entries iterator.
    The old processed extraction and `list(entries)` both fetched *every*
    playlist item before the 30-consecutive-match check could run.
    """
    stream = (os.environ.get("TRINITY_LAZY_PLAYLIST", "0") == "1"
              or ALREADY_DOWNLOADED_STREAK_LIMIT != float("inf"))
    ydl_opts = {
        "extract_flat": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["web"]}},
        "retries": 10,
        "extractor_retries": 10,
        "socket_timeout": 30,
        "lazy_playlist": stream,
    }
    if use_cookies:
        ydl_opts.update(make_cookie_opts())

    with YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(playlist_url, download=False, process=False)
            if not isinstance(info, dict) or info.get("entries") is None:
                raise PlaylistFetchError("YouTube did not return playlist entries")
            entries = info["entries"]
            if not stream:
                entries = list(entries)
            total = len(entries) if not stream else info.get("playlist_count")
        except Exception as exc:
            raise PlaylistFetchError("Could not fetch playlist metadata") from exc

        if stream:
            print("Streaming playlist entries; fetching only as far as needed.")
        elif total is not None:
            print(f"Playlist has {total} entries (YouTube playlist order).")

        def checked_entries():
            try:
                yield from entries
            except Exception as exc:
                raise PlaylistFetchError("Could not fetch the next playlist page") from exc

        yield checked_entries(), total


def try_download(
    video_id,
    title_hint=None,
    use_cookies=False,
    player_clients=None,
    format_selector="140",
    max_attempts=1,
):
    url = youtube_watch_url(video_id)

    ydl_opts = {
        "format": format_selector,
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
        "continuedl": True,  # Resume retained .part fragments after interruption.
        "nopart": False,
        "noplaylist": True,
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            },
            {
                "key": "EmbedThumbnail",
            },
        ],
        "postprocessor_args": {
            "EmbedThumbnail": ["-id3v2_version", "3"],
        },
        "overwrites": False,
        **make_common_opts(use_cookies=use_cookies, player_clients=player_clients, video_download=True),
    }

    for attempt in range(1, max_attempts + 1):
        try:
            print(
                f"Downloading {title_hint or video_id} | "
                f"attempt {attempt}/{max_attempts} | "
                f"{'cookies' if use_cookies else 'no cookies'} | "
                f"clients={player_clients or 'default'} | format={format_selector}"
            )

            started_at = time.time()
            ydl_opts_no_ignore = {**ydl_opts, "ignoreerrors": False}
            with YoutubeDL(ydl_opts_no_ignore) as ydl:
                info = ydl.extract_info(url, download=True)
                completed_path = tag_downloaded_mp3_with_youtube_url(
                    info=info,
                    ydl=ydl,
                    youtube_url=url,
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
                print(f"Video unavailable (permanent), skipping: {title_hint or video_id}")
                return None
            if "Sign in to confirm" in msg or "not a bot" in msg:
                print(f"YouTube auth/bot gate hit for {video_id}; skipping (no usable cookies).")
                return None
            if "Please sign in" in msg or "Private video" in msg:
                print(f"YouTube auth/private video for {video_id}; skipping (no usable cookies).")
                return None
            print(f"Attempt failed: {e}")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"Attempt failed: {e}")

        if attempt < max_attempts:
            wait_seconds = min(2 ** attempt, 10)
            time.sleep(wait_seconds)

    return False


def write_failures(path, video_ids):
    if not path:
        return
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(video_ids, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def process_playlist_entries(entries, total, history, failed_video_ids, failures_json):
    """Stop on the thirtieth *consecutive* known ID, not after a full pre-scan."""
    last_history_sync = time.monotonic()
    already_downloaded_streak = 0

    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            print(f"Skipping playlist entry #{index}: no metadata")
            already_downloaded_streak = 0
            continue
        if is_private_or_unavailable(entry):
            title = entry.get("title") or f"video #{index}"
            print(f"Skipping private/unavailable video: {title}")
            already_downloaded_streak = 0
            continue

        video_id = entry.get("id")
        title = entry.get("title") or video_id or f"video #{index}"

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
            print(f"Skipping already downloaded: {title}")
            already_downloaded_streak += 1
            if already_downloaded_streak >= ALREADY_DOWNLOADED_STREAK_LIMIT:
                print(f"Reached {ALREADY_DOWNLOADED_STREAK_LIMIT} consecutive "
                      "already-downloaded videos at playlist position "
                      f"{index}; stopping without fetching older entries.")
                return
            continue

        already_downloaded_streak = 0
        progress = f"{index}/{total}" if total is not None else str(index)
        print(f"\n[{progress}] Processing: {title}")

        success = False

        for clients in COOKIE_CLIENT_GROUPS:
            for fmt in FORMAT_CANDIDATES:
                success = try_download(
                    video_id=video_id,
                    title_hint=title,
                    use_cookies=True,
                    player_clients=clients,
                    format_selector=fmt,
                    max_attempts=1,
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
            print(f"Video unavailable, giving up: {title}")
        elif not success:
            print("Retrying failed download without cookies...")
            for clients in NO_COOKIE_CLIENT_GROUPS:
                for fmt in FORMAT_CANDIDATES:
                    success = try_download(
                        video_id=video_id,
                        title_hint=title,
                        use_cookies=False,
                        player_clients=clients,
                        format_selector=fmt,
                        max_attempts=1,
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
                print(f"Video unavailable, giving up: {title}")

        if success:
            history.add(video_id)
            save_history(history)
            print(f"Saved to history immediately after successful download: {title}")
        else:
            print(f"Giving up for now: {title}")
            failed_video_ids.append(video_id)
            # Save progress before continuing so even a later crash keeps
            # the explicit failures already observed in this run.
            write_failures(failures_json, failed_video_ids)



def download_playlist(playlist_url, failures_json=None):
    # Diagnose multiple Windows Python installations without guessing: the
    # interactive CMD yt-dlp may be different from Trinity's bundled launcher.
    print(f"Trinity downloader: yt-dlp {YTDLP_VERSION} from {yt_dlp.__file__}; "
          f"Python: {sys.executable}", flush=True)
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    failed_video_ids = []
    write_failures(failures_json, failed_video_ids)

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
    print(f"Loaded {len(history)} previously downloaded entries from {HISTORY_FILE}.")

    print("Fetching playlist information...")
    try:
        with get_playlist_entries(playlist_url, use_cookies=True) as (entries, total):
            process_playlist_entries(entries, total, history, failed_video_ids, failures_json)
    except PlaylistFetchError as exc:
        # A later page may fail even after some successful downloads. Their
        # history is already durable, so restarting from the top is safe.
        print(f"Playlist fetch with cookies failed: {exc}")
        print("Retrying playlist scan without cookies...")
        with get_playlist_entries(playlist_url, use_cookies=False) as (entries, total):
            process_playlist_entries(entries, total, history, failed_video_ids, failures_json)

    flush_pending()  # One quota-friendly Sheets upload for the entire scan.
    print("\nAll done!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--failures-json", default=None)
    arguments = parser.parse_args()
    download_playlist(PLAYLIST_URL, failures_json=arguments.failures_json)

