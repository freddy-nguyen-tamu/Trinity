import os

from mutagen.mp3 import MP3
from mutagen.id3 import COMM, WOAS


YOUTUBE_ID_COMMENT_PREFIX = "YouTube ID:"


def youtube_watch_url(video_id):
    video_id = str(video_id or "").strip()
    if not video_id:
        return ""
    return f"https://www.youtube.com/watch?v={video_id}"


def _with_mp3_extension(path):
    root, _ = os.path.splitext(path)
    return root + ".mp3"


def _add_candidate(candidates, path):
    if not path:
        return

    path = os.path.abspath(os.fspath(path))
    candidates.append(path)
    if os.path.splitext(path)[1].casefold() != ".mp3":
        candidates.append(_with_mp3_extension(path))


def candidate_mp3_paths(info, ydl=None):
    candidates = []
    info = info or {}

    for item in info.get("requested_downloads") or []:
        if isinstance(item, dict):
            for key in ("filepath", "filename", "_filename"):
                _add_candidate(candidates, item.get(key))

    for key in ("filepath", "filename", "_filename"):
        _add_candidate(candidates, info.get(key))

    if ydl is not None:
        try:
            _add_candidate(candidates, ydl.prepare_filename(info))
        except Exception:
            pass

    seen = set()
    result = []
    for path in candidates:
        lowered = path.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        if lowered.endswith(".mp3"):
            result.append(path)

    return result


def write_youtube_metadata_tags(mp3_path, youtube_url, video_id=""):
    if not mp3_path or not os.path.isfile(mp3_path):
        return False

    audio = MP3(mp3_path)
    if audio.tags is None:
        audio.add_tags()

    changed = False
    tags = audio.tags

    if youtube_url:
        existing_urls = [
            str(frame.url or "").strip()
            for frame in tags.getall("WOAS")
            if str(frame.url or "").strip()
        ]
        if not existing_urls:
            tags.delall("WOAS")
            tags.add(WOAS(url=youtube_url))
            changed = True

    video_id = str(video_id or "").strip()
    if video_id:
        comment_text = f"{YOUTUBE_ID_COMMENT_PREFIX} {video_id}"
        existing_comments = tags.getall("COMM")
        has_youtube_id_comment = any(
            video_id in " ".join(str(item) for item in getattr(frame, "text", []) or [])
            for frame in existing_comments
        )
        if not has_youtube_id_comment:
            # Windows Explorer's Details tab reads normal COMM frames as Comments.
            tags.add(COMM(encoding=3, lang="eng", desc="", text=comment_text))
            changed = True

    if changed:
        audio.save(v2_version=3)

    return changed


def write_youtube_url_tag(mp3_path, youtube_url):
    return write_youtube_metadata_tags(mp3_path, youtube_url)


def tag_downloaded_mp3_with_youtube_url(
    info,
    ydl,
    youtube_url,
    download_dir,
    started_at=0,
    video_id="",
):
    candidates = candidate_mp3_paths(info, ydl)

    if video_id and os.path.isdir(download_dir):
        video_marker = f"[{video_id}]".casefold()
        for name in os.listdir(download_dir):
            if name.casefold().endswith(".mp3") and video_marker in name.casefold():
                _add_candidate(candidates, os.path.join(download_dir, name))

    if os.path.isdir(download_dir):
        recent_mp3s = []
        for name in os.listdir(download_dir):
            if not name.casefold().endswith(".mp3"):
                continue
            path = os.path.join(download_dir, name)
            try:
                modified_at = os.path.getmtime(path)
            except OSError:
                continue
            if modified_at >= max(0, started_at - 2):
                recent_mp3s.append((modified_at, path))

        for _, path in sorted(recent_mp3s, reverse=True):
            _add_candidate(candidates, path)

    seen = set()
    for path in candidates:
        lowered = os.path.abspath(path).casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        if not os.path.isfile(path):
            continue

        try:
            changed = write_youtube_metadata_tags(path, youtube_url, video_id=video_id)
        except Exception as error:
            print(f"Could not embed YouTube metadata in {path}: {error}")
            continue

        if changed:
            print(f"Embedded YouTube metadata in MP3: {os.path.basename(path)}")
        else:
            print(f"MP3 already has YouTube metadata tags: {os.path.basename(path)}")
        return path

    print(f"Could not find final MP3 to tag with YouTube URL: {youtube_url}")
    return None
