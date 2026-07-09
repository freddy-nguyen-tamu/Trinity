import os
import json
import time
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "_working_downloads")
HISTORY_FILE = "download_history.json"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLBkuXLqNhqX5FsS2CEaSDlGTKAHIBPtLe"
ALREADY_DOWNLOADED_STREAK_LIMIT = 30

BROWSER = ("firefox",)

CLIENT_GROUPS = [
    ["android_vr"],
    ["android_vr", "web_safari"],
    ["android_vr", "web_safari", "web"],
]

FORMAT_CANDIDATES = [
    "140",
    "251",
    "250",
    "249",
    "bestaudio/best",
]


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return set()

    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set(str(x) for x in data)
    except Exception as e:
        print(f"Could not read {HISTORY_FILE}: {e}")

    return set()


def save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(history), f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Could not write {HISTORY_FILE}: {e}")


def make_common_opts(use_cookies=False, player_clients=None):
    if player_clients is None:
        player_clients = ["android_vr"]

    opts = {
        "extractor_args": {
            "youtube": {
                "player_client": player_clients,
            }
        },
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 5,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 1,
        "ignoreerrors": True,
        "quiet": False,
        "verbose": False,
    }

    if use_cookies:
        opts["cookiesfrombrowser"] = BROWSER

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


def get_playlist_entries(playlist_url, use_cookies=False):
    ydl_opts = {
        "extract_flat": True,
        "skip_download": True,
        "lazy_playlist": True,
        **make_common_opts(use_cookies=use_cookies, player_clients=["android_vr"]),
    }

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    entries = list(info.get("entries") or [])
    return [entry for entry in entries if not is_private_or_unavailable(entry)]


def try_download(
    video_id,
    title_hint=None,
    use_cookies=False,
    player_clients=None,
    format_selector="140",
    max_attempts=1,
):
    url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        "format": format_selector,
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s [%(id)s].%(ext)s"),
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
        **make_common_opts(use_cookies=use_cookies, player_clients=player_clients),
    }

    for attempt in range(1, max_attempts + 1):
        try:
            print(
                f"Downloading {title_hint or video_id} | "
                f"attempt {attempt}/{max_attempts} | "
                f"{'cookies' if use_cookies else 'no cookies'} | "
                f"clients={player_clients} | format={format_selector}"
            )

            ydl_opts_no_ignore = {**ydl_opts, "ignoreerrors": False}
            with YoutubeDL(ydl_opts_no_ignore) as ydl:
                ydl.download([url])
            return True

        except DownloadError as e:
            msg = str(e)
            if "Video unavailable" in msg or "video has been removed" in msg:
                print(f"Video unavailable (permanent), skipping: {title_hint or video_id}")
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


def download_playlist(playlist_url):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    history = load_history()
    print(f"Loaded {len(history)} previously downloaded entries from {HISTORY_FILE}.")

    print("Fetching playlist information...")
    try:
        entries = get_playlist_entries(playlist_url, use_cookies=True)
    except Exception as e:
        print(f"Playlist fetch failed with cookies: {e}")
        print("Retrying playlist fetch without cookies...")
        entries = get_playlist_entries(playlist_url, use_cookies=False)

    print(f"Playlist has {len(entries)} downloadable videos.")

    already_downloaded_streak = 0

    for index, entry in enumerate(entries, start=1):
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

        if video_id in history:
            print(f"Skipping already downloaded: {title}")
            already_downloaded_streak += 1
            if already_downloaded_streak >= ALREADY_DOWNLOADED_STREAK_LIMIT:
                print(
                    f"Reached {ALREADY_DOWNLOADED_STREAK_LIMIT} already-downloaded "
                    "videos in a row; skipping the rest of this playlist."
                )
                break
            continue

        already_downloaded_streak = 0
        print(f"\n[{index}/{len(entries)}] Processing: {title}")

        success = False

        for clients in CLIENT_GROUPS:
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
            for clients in CLIENT_GROUPS:
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

    print("\nAll done!")


if __name__ == "__main__":
    download_playlist(PLAYLIST_URL)
