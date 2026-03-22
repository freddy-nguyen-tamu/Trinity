import os
import json
import time
from yt_dlp import YoutubeDL

# ==================================================================
# CONFIGURATION
# ==================================================================
DOWNLOAD_DIR = "downloads"
HISTORY_FILE = "download_history.json"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLBkuXLqNhqX5FsS2CEaSDlGTKAHIBPtLe"

# Use Firefox instead of Chrome to avoid DPAPI issues on Windows
BROWSER = ("firefox",)

# Default behavior:
# try without cookies first because current YouTube/yt-dlp breakage
# often affects logged-in cookie sessions
USE_COOKIES_FOR_PLAYLIST = False


def make_common_ydl_opts(use_cookies=False):
    opts = {
        # Python API format (dict, not list)
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],

        # avoid mweb / try safer clients first
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "web", "web_safari", "tv"],
            }
        },

        # retry/network hardening
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 10,
        "socket_timeout": 30,
    }

    if use_cookies:
        opts["cookiesfrombrowser"] = BROWSER

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
    ydl_opts_extract = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": False,
        **make_common_ydl_opts(use_cookies=use_cookies),
    }

    with YoutubeDL(ydl_opts_extract) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    return info.get("entries", [])


def try_download_video(video_id, max_attempts=3, use_cookies=False):
    """Try downloading one video with retries."""
    ydl_opts_dl = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": False,
        **make_common_ydl_opts(use_cookies=use_cookies),
    }

    video_url = f"https://www.youtube.com/watch?v={video_id}"

    for attempt in range(1, max_attempts + 1):
        try:
            with YoutubeDL(ydl_opts_dl) as ydl:
                ydl.download([video_url])
            return True

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
    for index, entry in enumerate(video_entries, start=1):
        video_id = entry.get("id")
        video_title = entry.get("title") or video_id

        if not video_id:
            print(f"Skipping entry #{index}: missing video id")
            continue

        if video_id in history:
            print(f"Skipping already downloaded: {video_title}")
            continue

        print(f"[{index}/{len(video_entries)}] Downloading: {video_title}")

        # First try without cookies
        success = try_download_video(
            video_id,
            max_attempts=3,
            use_cookies=False,
        )

        # Fall back to cookies only if needed
        if not success:
            print("Retrying with browser cookies...")
            success = try_download_video(
                video_id,
                max_attempts=2,
                use_cookies=True,
            )

        if success:
            history.add(video_id)
            save_history(history)
            print(f"Saved to history: {video_title}")
        else:
            print(f"Giving up for now: {video_title}")

    print("All done!")


if __name__ == "__main__":
    download_playlist(PLAYLIST_URL)