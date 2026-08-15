import hashlib
import os
import re
from pathlib import Path

try:
    from mutagen import File as MutagenFile
except Exception:
    MutagenFile = None


YOUTUBE_ID_IN_NAME_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]")
YOUTUBE_ID_LABEL_RE = re.compile(
    r"youtube(?:\s+video)?\s+id\s*[:=]\s*([A-Za-z0-9_-]{11})",
    flags=re.IGNORECASE,
)
YOUTUBE_URL_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^\s#]*&)?v=|shorts/|embed/|live/))"
    r"([A-Za-z0-9_-]{11})",
    flags=re.IGNORECASE,
)

_HASH_CACHE = {}
_YOUTUBE_ID_CACHE = {}


def _file_cache_key(path):
    path = Path(path).resolve()
    stat = path.stat()
    return str(path), stat.st_size, stat.st_mtime_ns


def calculate_file_hash(path, algorithm="sha256"):
    path = Path(path).resolve()
    cache_key = (*_file_cache_key(path), algorithm)
    cached = _HASH_CACHE.get(cache_key)
    if cached:
        return cached

    digest = hashlib.new(algorithm)
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)

    value = digest.hexdigest()
    _HASH_CACHE[cache_key] = value
    return value


def _iter_tag_text(value):
    if value is None:
        return
    if isinstance(value, bytes):
        for encoding in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                yield value.decode(encoding)
                return
            except Exception:
                continue
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _iter_tag_text(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_tag_text(item)
        return

    for attribute in ("text", "url", "data"):
        attribute_value = getattr(value, attribute, None)
        if attribute_value is not None and attribute_value is not value:
            yield from _iter_tag_text(attribute_value)

    yield str(value)


def _youtube_id_from_text(text):
    text = str(text or "")
    for pattern in (YOUTUBE_ID_LABEL_RE, YOUTUBE_URL_ID_RE):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def youtube_id_from_audio(path):
    path = Path(path).resolve()
    cache_key = _file_cache_key(path)
    if cache_key in _YOUTUBE_ID_CACHE:
        return _YOUTUBE_ID_CACHE[cache_key]

    name_match = YOUTUBE_ID_IN_NAME_RE.search(path.name)
    if name_match:
        youtube_id = name_match.group(1)
        _YOUTUBE_ID_CACHE[cache_key] = youtube_id
        return youtube_id

    youtube_id = ""
    if MutagenFile is not None:
        try:
            audio = MutagenFile(path, easy=False)
            tags = getattr(audio, "tags", None)
            if tags:
                for text in _iter_tag_text(tags):
                    youtube_id = _youtube_id_from_text(text)
                    if youtube_id:
                        break
        except Exception:
            pass

    _YOUTUBE_ID_CACHE[cache_key] = youtube_id
    return youtube_id


def normalized_relative_name(path, base_dir=None):
    path = Path(path).resolve()
    if base_dir is not None:
        try:
            name = os.path.relpath(str(path), str(Path(base_dir).resolve()))
        except ValueError:
            name = path.name
    else:
        name = path.name

    return os.path.normcase(name.replace("\\", "/").strip())


def upload_history_key(path, base_dir=None):
    path = Path(path).resolve()
    stat = path.stat()
    youtube_id = youtube_id_from_audio(path) or "-"
    name = normalized_relative_name(path, base_dir=base_dir)
    sha256 = calculate_file_hash(path, "sha256")
    return (
        f"upload-v2|youtube={youtube_id}|name={name}|"
        f"size={stat.st_size}|sha256={sha256}"
    )
