import os
import re

from mutagen.mp3 import MP3
from mutagen.id3 import COMM, WOAS


YOUTUBE_ID_COMMENT_PREFIX = "YouTube ID:"


def has_incomplete_companion(audio_path):
    """Never publish an MP3 while yt-dlp still has an associated partial.

    In particular, yt-dlp may be writing title.mp4.part while a retry has
    already generated title.mp3. The MP3 is not safe to publish until the
    partial is resolved, even when it opens in a metadata parser.
    """
    folder = os.path.dirname(os.path.abspath(audio_path))
    stem = os.path.splitext(os.path.basename(audio_path))[0].casefold()
    if not os.path.isdir(folder):
        return False
    try:
        return any(
            name.casefold().startswith(stem + ".") and
            name.casefold().endswith((".part", ".ytdl", ".tmp", ".temp"))
            for name in os.listdir(folder)
        )
    except OSError:
        return True


def valid_completed_mp3(path):
    """Check an actual readable audio stream, not just yt-dlp's exit code."""
    if not path or not os.path.isfile(path) or has_incomplete_companion(path):
        return False
    try:
        if os.path.getsize(path) < 4096:
            return False
        audio = MP3(path)
        return float(getattr(audio.info, "length", 0) or 0) >= 1.0
    except Exception:
        return False


def _stored_youtube_ids(audio):
    """Read all existing ID3 identities to avoid retagging another video."""
    ids = set()
    if audio.tags:
        for frame in audio.tags.getall("COMM"):
            for value in getattr(frame, "text", []) or []:
                text = str(value)
                match = re.search(r"YouTube ID:\s*([A-Za-z0-9_-]{11})", text)
                if match:
                    ids.add(match.group(1))
        for frame in audio.tags.getall("WOAS"):
            match = re.search(
                r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})",
                str(getattr(frame, "url", "")), re.I,
            )
            if match:
                ids.add(match.group(1))
    return ids


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

    if not valid_completed_mp3(mp3_path):
        return False
    audio = MP3(mp3_path)
    existing_ids = _stored_youtube_ids(audio)
    if video_id and existing_ids and existing_ids != {video_id}:
        raise ValueError(
            f"File belongs to another YouTube video ({sorted(existing_ids)}): {mp3_path}"
        )
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

    # Never grab a conveniently recent MP3 from an unrelated download.  A
    # concurrent run previously caused mismatched tags and duplicate files.
    # yt-dlp's own requested_downloads/prepare_filename identify this video.

    seen = set()
    for path in candidates:
        lowered = os.path.abspath(path).casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        if not valid_completed_mp3(path):
            continue

        try:
            changed = write_youtube_metadata_tags(path, youtube_url, video_id=video_id)
        except Exception as error:
            print(f"Could not embed YouTube metadata in {path}: {error}")
            continue

        # A clean, unambiguous embedded identity is required before history
        # may be updated. A different file/failed tag update is not success.
        try:
            tagged = MP3(path)
            if not valid_completed_mp3(path) or _stored_youtube_ids(tagged) != {video_id}:
                print(f"MP3 identity could not be verified: {path}")
                continue
        except Exception as error:
            print(f"MP3 verification failed for {path}: {error}")
            continue
        if changed:
            print(f"Embedded YouTube metadata in MP3: {os.path.basename(path)}")
        else:
            print(f"MP3 already has YouTube metadata tags: {os.path.basename(path)}")
        return path

    print(f"Could not find final MP3 to tag with YouTube URL: {youtube_url}")
    return None


_YOUTUBE_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{11}(?![A-Za-z0-9_-])")
_YOUTUBE_LINK_PATTERN = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)


def recover_completed_mp3_ids(*folders):
    """Recover completed downloads from ID3 tags after a forced shutdown.

    We only accept MP3s with a valid embedded YouTube URL or our own
    'YouTube ID:' comment. Partial .part/.ytdl downloads and untagged files
    are never treated as successfully downloaded. No files are modified.
    """
    from mutagen.id3 import ID3, ID3NoHeaderError

    found = {}
    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for current, _, filenames in os.walk(folder):
            for name in filenames:
                if not name.casefold().endswith(".mp3"):
                    continue
                path = os.path.join(current, name)
                try:
                    if not valid_completed_mp3(path):
                        continue
                    tags = ID3(path)
                    ids = []
                    for frame in tags.getall("COMM"):
                        for line in getattr(frame, "text", []) or []:
                            text = str(line)
                            if YOUTUBE_ID_COMMENT_PREFIX in text:
                                portion = text.split(YOUTUBE_ID_COMMENT_PREFIX, 1)[1].strip()
                                match = _YOUTUBE_ID_PATTERN.search(portion)
                                if match:
                                    ids.append(match.group())
                    for frame in tags.getall("WOAS"):
                        match = _YOUTUBE_LINK_PATTERN.search(str(getattr(frame, "url", "")))
                        if match:
                            ids.append(match.group(1))
                    # Reject conflicting metadata, which may indicate a reused
                    # audio file or an interrupted tag update.
                    if ids and len(set(ids)) == 1:
                        found.setdefault(ids[0], path)
                except Exception:
                    # Malformed or partially written MP3s must not block
                    # recovery of other successfully completed downloads.
                    continue
    return found
