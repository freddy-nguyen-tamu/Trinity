import subprocess
import sys
import os
import shutil

def check_ffmpeg():
    """Check if ffmpeg is available in PATH."""
    return shutil.which("ffmpeg") is not None

def download_first_four_seconds(youtube_url, output_filename="first_4_seconds.mp4"):
    if not output_filename.endswith((".mp4", ".mkv", ".webm")):
        output_filename += ".mp4"

    # Use Python's -m to run yt-dlp as a module
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--download-sections", "*00:00:00-00:00:04",
        "--force-keyframes-at-cuts",
        "-f", "best[ext=mp4]",
        "-o", output_filename,
        youtube_url
    ]

    try:
        print(f"Downloading first 4 seconds from {youtube_url} ...")
        subprocess.run(cmd, check=True)
        print(f"Success! Video saved as {output_filename}")
    except subprocess.CalledProcessError as e:
        print(f"Error during download: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("yt-dlp not found. Please install it with: pip install yt-dlp", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    # First, ensure ffmpeg is installed
    if not check_ffmpeg():
        print("ffmpeg is required. Please install ffmpeg and add it to your PATH.", file=sys.stderr)
        print("On Windows, you can install via Chocolatey: choco install ffmpeg", file=sys.stderr)
        print("Or download from https://ffmpeg.org/download.html and add the 'bin' folder to PATH.", file=sys.stderr)
        sys.exit(1)

    video_url = "https://www.youtube.com/watch?v=iBcOUnYGGbw"
    download_first_four_seconds(video_url, "dong_colau_4s.mp4")