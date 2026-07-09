import os
import json
from yt_dlp import YoutubeDL

# Config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "_working_downloads")
HISTORY_FILE = "download_history.json"
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
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r") as f:
        return set(json.load(f))


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(list(history), f, indent=2)


def download_playlist(playlist_url):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    history = load_history()
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
    if lazy:
        ydl_opts_extract["playlistend"] = 200

    with YoutubeDL(ydl_opts_extract) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    video_entries = info.get("entries", [])
    print(f"Playlist has {len(video_entries)} videos.")

    # Download each video
    already_downloaded_streak = 0

    for entry in video_entries:
        video_id = entry.get("id")
        video_title = entry.get("title")

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
        print(f"⬇ Downloading: {video_title}")

        ydl_opts_dl = {
            "format": "bestaudio[ext=m4a]/bestaudio/best[acodec!=none]/18/best",
            "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
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
            with YoutubeDL(ydl_opts_dl) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            history.add(video_id)
            save_history(history)
        except Exception as e:
            print(f"Error downloading {video_title}: {e}")

    print("All done!")


if __name__ == "__main__":
    playlist_url = "https://www.youtube.com/playlist?list=PLBkuXLqNhqX5FsS2CEaSDlGTKAHIBPtLe"
    download_playlist(playlist_url)
