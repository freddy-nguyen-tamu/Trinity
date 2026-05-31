import os
import json
from yt_dlp import YoutubeDL

# Config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_BASE_DIR = r"C:\ytb"
DOWNLOAD_DIR = os.path.join(OUTPUT_BASE_DIR, "_working_downloads")
HISTORY_FILE = "download_history.json"

# Use Firefox instead of Chrome to avoid DPAPI issues on Windows
BROWSER = ("firefox",)


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
    ydl_opts_extract = {
        "extract_flat": True,
        "skip_download": True,
        "cookiesfrombrowser": BROWSER,   # use browser cookies
    }

    with YoutubeDL(ydl_opts_extract) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    video_entries = info.get("entries", [])
    print(f"Playlist has {len(video_entries)} videos.")

    # Download each video
    for entry in video_entries:
        video_id = entry.get("id")
        video_title = entry.get("title")

        if video_id in history:
            print(f"Skipping already downloaded: {video_title}")
            continue

        print(f"⬇ Downloading: {video_title}")

        ydl_opts_dl = {
            "format": "bestaudio/best",
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
            "cookiesfrombrowser": BROWSER,  # use browser cookies during download
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
