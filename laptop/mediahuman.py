import os
if os.environ.get("TRINITY_ENABLE_YTDLP_POT_PLUGIN", "").strip().lower() not in {"1", "true", "yes", "on"}:
    os.environ.setdefault("YTDLP_NO_PLUGINS", "1")

import json
import time
from itertools import islice
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError
from youtube_url_tag import tag_downloaded_mp3_with_youtube_url, youtube_watch_url

# ==================================================================
# CONFIGURATION
# ==================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "_working_downloads")
HISTORY_FILE = "download_history.json"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLBkuXLqNhqX5FsS2CEaSDlGTKAHIBPtLe"
ALREADY_DOWNLOADED_STREAK_LIMIT = 30
LAZY_PLAYLIST_LIMIT = 200
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
    """Load previously downloaded video IDs from file."""
    if not os.path.exists(HISTORY_FILE):
        return set()

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_history(history):
    """Save updated history to file."""
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(list(history), f, indent=2, ensure_ascii=False)


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
    if lazy:
        ydl_opts_extract["playlistend"] = LAZY_PLAYLIST_LIMIT

    with YoutubeDL(ydl_opts_extract) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    raw_entries = info.get("entries") or []
    return list(islice(raw_entries, LAZY_PLAYLIST_LIMIT)) if lazy else list(raw_entries)


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
                tag_downloaded_mp3_with_youtube_url(
                    info=info,
                    ydl=ydl,
                    youtube_url=video_url,
                    download_dir=DOWNLOAD_DIR,
                    started_at=started_at,
                    video_id=video_id,
                )
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


def download_playlist(playlist_url):
    """Download all mp3s from a playlist, skipping previously-downloaded ones."""

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Load previous history
    history = load_history()
    print(f"Loaded {len(history)} previously downloaded entries.")

    # ------------------------------------------------------------------
    # FETCH PLAYLIST INFORMATION
    # ------------------------------------------------------------------
    print("Fetching playlist information...")

    try:
        video_entries = extract_playlist_entries(
            playlist_url,
            use_cookies=USE_COOKIES_FOR_PLAYLIST,
        )
    except Exception as e:
        print(f"Playlist fetch failed in current mode: {e}")

        if USE_COOKIES_FOR_PLAYLIST:
            print("Retrying playlist fetch without cookies...")
            video_entries = extract_playlist_entries(playlist_url, use_cookies=False)
        else:
            print("Retrying playlist fetch with cookies...")
            video_entries = extract_playlist_entries(playlist_url, use_cookies=True)

    print(f"Playlist has {len(video_entries)} videos.")

    # ------------------------------------------------------------------
    # DOWNLOAD EACH VIDEO
    # ------------------------------------------------------------------
    already_downloaded_streak = 0

    for index, entry in enumerate(video_entries, start=1):
        video_id = entry.get("id")
        video_title = entry.get("title") or video_id

        if not video_id:
            print(f"Skipping entry #{index}: missing video id")
            already_downloaded_streak = 0
            continue

        if video_id in history:
            print(f"Skipping already downloaded: {video_title}")
            already_downloaded_streak += 1
            if already_downloaded_streak >= ALREADY_DOWNLOADED_STREAK_LIMIT:
                print(
                    f"Reached {ALREADY_DOWNLOADED_STREAK_LIMIT} already-downloaded "
                    "videos in a row; skipping the rest of this playlist."
                )
                break
            continue

        already_downloaded_streak = 0
        print(f"[{index}/{len(video_entries)}] Downloading: {video_title}")

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

    print("All done!")


if __name__ == "__main__":
    download_playlist(PLAYLIST_URL)
