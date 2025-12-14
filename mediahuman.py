import os
import json
from yt_dlp import YoutubeDL

# ==================================================================
# CONFIGURATION
# ==================================================================
DOWNLOAD_DIR = "downloads"
HISTORY_FILE = "download_history.json"

# Use Firefox instead of Chrome to avoid DPAPI issues on Windows
BROWSER = ("firefox",)

# Extra args to bypass SABR issues by using mweb client
COMMON_YDL_OPTS = {
    "extractor_args": {
        "youtube": {
            "player_client": ["mweb"],
        }
    },
    "cookiesfrombrowser": BROWSER,
}


def load_history():
    """Load previously downloaded video IDs from file."""
    if not os.path.exists(HISTORY_FILE):
        return set()
    with open(HISTORY_FILE, "r") as f:
        return set(json.load(f))


def save_history(history):
    """Save updated history to file."""
    with open(HISTORY_FILE, "w") as f:
        json.dump(list(history), f, indent=2)


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
    ydl_opts_extract = {
        "extract_flat": True,
        "skip_download": True,
        **COMMON_YDL_OPTS,
    }

    with YoutubeDL(ydl_opts_extract) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    video_entries = info.get("entries", [])
    print(f"Playlist has {len(video_entries)} videos.")

    # ------------------------------------------------------------------
    # DOWNLOAD EACH VIDEO
    # ------------------------------------------------------------------
    for entry in video_entries:
        video_id = entry.get("id")
        video_title = entry.get("title")

        if video_id in history:
            print(f"✔ Skipping already downloaded: {video_title}")
            continue

        print(f"⬇ Downloading: {video_title}")

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
            **COMMON_YDL_OPTS,
        }

        try:
            with YoutubeDL(ydl_opts_dl) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

            # Add to history
            history.add(video_id)
            save_history(history)

        except Exception as e:
            print(f"❌ Error downloading {video_title}: {e}")

    print("All done!")


if __name__ == "__main__":
    playlist_url = "https://www.youtube.com/playlist?list=PLBkuXLqNhqX5FsS2CEaSDlGTKAHIBPtLe"
    download_playlist(playlist_url)
