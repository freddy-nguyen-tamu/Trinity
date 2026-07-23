import argparse
import difflib
import html
import os
if os.environ.get("TRINITY_ENABLE_YTDLP_POT_PLUGIN", "").strip().lower() not in {"1", "true", "yes", "on"}:
    os.environ.setdefault("YTDLP_NO_PLUGINS", "1")

import json
import re
import time
import unicodedata
import requests
from xml.etree import ElementTree
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.networking import Request as YtDlpRequest

from mutagen import File
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.id3 import ID3, TIT2, TPE1, TALB, USLT, ID3NoHeaderError
from mutagen.wave import WAVE
from mutagen.aiff import AIFF
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus

from lyrics_language import (
    detect_text_language,
    infer_expected_language,
    normalize_language_code,
    validate_subtitle_language,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AUDIO_FOLDER = os.path.join(BASE_DIR, "_working_downloads")
LYRICS_NOT_FOUND_HISTORY_PATH = os.path.join(BASE_DIR, "_lyrics_not_found_history.json")
LYRICS_NOT_FOUND_HISTORY_VERSION = 2
LYRICS_NOT_FOUND_RESOLVER_VERSION = 3


def parse_args():
    parser = argparse.ArgumentParser(description="Tag audio files and add lyrics.")
    parser.add_argument(
        "--audio-folder",
        default=DEFAULT_AUDIO_FOLDER,
        help="Folder to scan when --manifest is not provided.",
    )
    parser.add_argument(
        "--manifest",
        help="JSON manifest containing a files array of exact audio paths to process.",
    )
    parser.add_argument(
        "--no-move",
        action="store_true",
        help="Process files in place and do not move _working_downloads to finished.",
    )
    parser.add_argument(
        "--retry-missing-lyrics",
        action="store_true",
        help="Ignore the not-found lyrics cache and try LRCLIB/subtitles again.",
    )
    parser.add_argument(
        "--preferred-language",
        default="",
        help=(
            "Optional ISO 639-1 lyrics language override, for example vi, en, "
            "ko, ja, zh, or th."
        ),
    )
    parser.add_argument(
        "--disable-youtube-subtitles",
        action="store_true",
        help="Use LRCLIB/Genius only and never request YouTube subtitles.",
    )
    parser.add_argument(
        "--recheck-existing-lyrics",
        action="store_true",
        help=(
            "Resolve lyrics again even when a lyrics tag already exists. "
            "Existing lyrics are left unchanged when no safer replacement is found."
        ),
    )
    return parser.parse_args()


ARGS = parse_args()
AUDIO_FOLDER = os.path.abspath(ARGS.audio_folder)

GROQ_API_KEYS = []

base_key = os.getenv("GROQ_API_KEY")
if base_key:
    GROQ_API_KEYS.append(("GROQ_API_KEY", base_key))

for i in range(1, 10):
    key_name = f"GROQ_API_KEY{i}"
    key_value = os.getenv(key_name)
    if key_value:
        GROQ_API_KEYS.append((key_name, key_value))

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"

LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
LRCLIB_GET_URL = "https://lrclib.net/api/get"

GENIUS_API_URL = "https://api.genius.com"
GENIUS_API_TOKEN = os.getenv("GENIUS_API_TOKEN")

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
SUBTITLE_INFO_CACHE = {}
YOUTUBE_SUBTITLES_DISABLED_FOR_RUN = False

SUPPORTED_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".mp4",
    ".flac",
    ".ogg",
    ".opus",
    ".wav",
    ".wave",
    ".aiff",
    ".aif",
    ".aac",
}

if GROQ_API_KEYS:
    print(f"Loaded {len(GROQ_API_KEYS)} Groq API key(s): " + ", ".join(name for name, _ in GROQ_API_KEYS))
else:
    print("No Groq API keys found. Existing-tag skips still work; Groq-only steps will use fallbacks.")


def is_supported_audio_path(path):
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS


def load_manifest_file_entries(manifest_path):
    manifest_path = os.path.abspath(manifest_path)

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        raise SystemExit(f"Could not read manifest {manifest_path}: {e}")

    raw_files = payload.get("files", []) if isinstance(payload, dict) else payload
    if not isinstance(raw_files, list):
        raise SystemExit(f"Manifest {manifest_path} must contain a files array.")

    entries = []
    seen = set()

    for item in raw_files:
        if isinstance(item, dict):
            raw_path = item.get("path") or item.get("file") or ""
            display_name = item.get("name") or ""
        else:
            raw_path = str(item)
            display_name = ""

        if not raw_path:
            print(f"Skipping manifest item without path: {item}")
            continue

        if not os.path.isabs(raw_path):
            raw_path = os.path.join(BASE_DIR, raw_path)

        file_path = os.path.abspath(raw_path)
        if file_path in seen:
            continue
        seen.add(file_path)

        if not os.path.isfile(file_path):
            print(f"Skipping missing manifest file: {file_path}")
            continue

        if not is_supported_audio_path(file_path):
            print(f"Skipping unsupported manifest file: {file_path}")
            continue

        entries.append((display_name or os.path.basename(file_path), file_path))

    return sorted(entries, key=lambda item: item[0].casefold())


def load_folder_file_entries(folder):
    if not os.path.isdir(folder):
        raise SystemExit(f"Folder not found: {folder}")

    return sorted(
        (
            f,
            os.path.join(folder, f),
        )
        for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f))
        and is_supported_audio_path(f)
    )


if ARGS.manifest:
    file_entries = load_manifest_file_entries(ARGS.manifest)
else:
    file_entries = load_folder_file_entries(AUDIO_FOLDER)

if not file_entries:
    raise SystemExit("No supported audio files found.")

def make_groq_headers(api_key):
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

lyrics_headers = {
    "User-Agent": "audio-lyrics-tagger/1.0"
}


def norm_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def remove_square_bracket_content(text: str) -> str:
    if not text:
        return ""

    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\[[^\[\]\r\n]*\]", " ", text)

    return text


def clean_lyrics_for_tag(lyrics: str) -> str:
    cleaned_lines = []

    for line in (lyrics or "").splitlines():
        line = remove_square_bracket_content(line)
        line = re.sub(r"\s+", " ", line).strip()

        if line:
            cleaned_lines.append(line)
        elif cleaned_lines and cleaned_lines[-1] != "":
            cleaned_lines.append("")

    while cleaned_lines and cleaned_lines[-1] == "":
        cleaned_lines.pop()

    return "\n".join(cleaned_lines).strip()


def clean_input_filename(name: str) -> str:
    base, _ = os.path.splitext(name)
    base = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]\s*$", " ", base)
    base = base.replace("_", " ")
    base = norm_text(base)
    return base


def conservative_filename_title(name: str) -> str:
    s = clean_input_filename(name)

    junk_patterns = [
        r"\bOfficial Video\b",
        r"\bOfficial MV\b",
        r"\bOfficial Audio\b",
        r"\bLyric Video\b",
        r"\bLyrics\b",
        r"\bVisualizer\b",
        r"\bAudio\b",
        r"\bHD\b",
        r"\b4K\b",
    ]
    for pat in junk_patterns:
        s = re.sub(pat, "", s, flags=re.IGNORECASE)

    s = re.sub(r"\[[^\]]*\]", " ", s)
    s = re.sub(r"\([^\)]*\)", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -_")
    return norm_text(s)


def extract_json_object(text: str):
    t = text.strip()

    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()

    decoder = json.JSONDecoder()

    start_obj = t.find("{")
    if start_obj != -1:
        candidate = t[start_obj:]
        try:
            obj, _ = decoder.raw_decode(candidate)
            return obj
        except json.JSONDecodeError:
            pass

    repaired = t
    repaired = (
        repaired.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2019", "'")
    )
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)

    start_obj = repaired.find("{")
    if start_obj != -1:
        candidate = repaired[start_obj:]
        obj, _ = decoder.raw_decode(candidate)
        return obj

    raise ValueError("Could not extract valid JSON object from response.")


def ensure_dict(obj):
    if isinstance(obj, dict):
        return obj

    if isinstance(obj, str):
        s = obj.strip()

        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
            s = re.sub(r"\s*```$", "", s).strip()

        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        try:
            parsed = extract_json_object(s)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    raise ValueError(f"Expected dict but got {type(obj).__name__}")


def parse_retry_after_seconds(resp: requests.Response) -> float:
    ra = resp.headers.get("retry-after")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass

    try:
        j = resp.json()
        msg = j.get("error", {}).get("message", "")
        m = re.search(r"try again in\s+([0-9]*\.?[0-9]+)s", msg, re.IGNORECASE)
        if m:
            return float(m.group(1))
    except Exception:
        pass

    return 2.0


def groq_chat(messages, max_tokens=220, temperature=0, timeout=60, max_retries=8, json_mode=True):
    if not GROQ_API_KEYS:
        raise RuntimeError(
            "No Groq API keys are available. Set GROQ_API_KEY or GROQ_API_KEY1 through GROQ_API_KEY9."
        )

    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    backoff = 1.0

    for attempt in range(1, max_retries + 1):
        rate_limit_waits = []
        network_errors = []

        for key_name, api_key in GROQ_API_KEYS:
            try:
                resp = requests.post(
                    GROQ_API_URL,
                    headers=make_groq_headers(api_key),
                    json=payload,
                    timeout=timeout,
                )
            except requests.RequestException as e:
                network_errors.append((key_name, str(e)))
                print(
                    f"[NET] {key_name} network error. "
                    f"Trying next key before sleeping. Attempt {attempt}/{max_retries}"
                )
                continue

            if resp.status_code == 200:
                if key_name != "GROQ_API_KEY":
                    print(f"[OK] Groq request succeeded using {key_name}.")
                return resp.json()

            if resp.status_code == 429:
                wait_s = max(parse_retry_after_seconds(resp), backoff) + 0.25
                rate_limit_waits.append(wait_s)
                print(
                    f"[429] {key_name} rate limited. "
                    f"Trying next key before sleeping. Attempt {attempt}/{max_retries}"
                )
                continue

            if resp.status_code == 413:
                raise RuntimeError(
                    "Groq request too large. Reduce prompt size before retrying this step."
                )

            if resp.status_code == 400:
                try:
                    err = resp.json()
                except Exception:
                    err = {}

                code = err.get("error", {}).get("code", "")
                if code == "json_validate_failed" and json_mode:
                    print("[400] JSON validation failed in JSON mode. Retrying without JSON mode...")
                    return groq_chat(
                        messages=messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        timeout=timeout,
                        max_retries=max_retries,
                        json_mode=False,
                    )

            raise SystemExit(f"Groq API error using {key_name}: {resp.status_code} {resp.text}")

        if attempt == max_retries:
            if network_errors and not rate_limit_waits:
                last_key, last_error = network_errors[-1]
                raise SystemExit(f"Network error calling Groq using {last_key}: {last_error}")

            raise SystemExit("Groq API error: too many retries across all API keys.")

        if rate_limit_waits:
            wait_s = max(rate_limit_waits)
        else:
            wait_s = backoff + 0.25

        print(
            f"All {len(GROQ_API_KEYS)} loaded Groq API key(s) failed or were rate limited. "
            f"Sleeping {wait_s:.2f}s, then retrying from GROQ_API_KEY. "
            f"Attempt {attempt}/{max_retries}"
        )

        time.sleep(wait_s)
        backoff = min(backoff * 1.6, 20.0)

    raise SystemExit("Groq API error: too many retries across all API keys.")


def build_single_prompt(item_id, file_name):
    current_input = {"id": item_id, "file_name": file_name}

    return (
        "Understand the song name and author of the song. Extract title and artist from filename.\n\n"
        "Now process this input.\n\n"
        f"Input:\n{json.dumps(current_input, ensure_ascii=False)}\n\n"
        "For each filename, understand the song name and author of the song. Extract title and artist from filename. Return EXACTLY this JSON object:\n"
        "{\n"
        f'  "id": {item_id},\n'
        '  "title": "song name",\n'
        '  "artist": "artist name"\n'
        "}\n\n"
        "Rules:\n"
        "- After defining the author of the song, it should be deleted from the title.\n"
        "- Remove author name from the title.\n"
        "- Keep the title and author names exactly as they are in the filenames.\n"
        "- Remove obvious suffix junk such as: file extension, Official Video, Official MV, Lyrics, Lyric Video, Visualizer, Audio, HD, 4K.\n"
        "- Also ignore obvious non-title noise such as: fancam, concert/live tags, tas release, track numbers, album tags, mp3cut, and repeated artist names.\n"
        "- If artist is unclear, put Unknown.\n"
        "- Output JSON only."
    )


def call_and_parse(messages, max_tokens=220, json_mode=True, temperature=0):
    data_json = groq_chat(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=json_mode,
    )
    content = data_json["choices"][0]["message"]["content"]

    try:
        return ensure_dict(extract_json_object(content))
    except Exception:
        fix_messages = [
            {"role": "system", "content": "You repair malformed JSON. Return only valid JSON."},
            {"role": "user", "content": "Fix this into valid JSON only:\n\n" + content},
        ]
        fixed = groq_chat(fix_messages, max_tokens=max_tokens, temperature=0, json_mode=False)
        fixed_content = fixed["choices"][0]["message"]["content"]
        return ensure_dict(extract_json_object(fixed_content))


def looks_bad(text: str) -> bool:
    if not text:
        return True
    bad_markers = ["\ufffd", "\\u", "???"]
    return any(marker in text for marker in bad_markers)


YOUTUBE_ID_ONLY_RE = re.compile(r"^\[?[A-Za-z0-9_-]{11}\]?$")


def artist_is_invalid(artist):
    artist = norm_text(artist)
    if not artist or artist.casefold() == "unknown":
        return True
    if YOUTUBE_ID_ONLY_RE.fullmatch(artist):
        return True
    return looks_bad(artist)


def is_file_busy_error(error):
    text = str(error).lower()
    return (
        isinstance(error, (PermissionError, OSError))
        or "being used by another process" in text
        or "permission denied" in text
        or "access is denied" in text
        or "winerror 32" in text
    )


def retry_file_operation(description, operation, attempts=8, initial_delay=0.75):
    delay = initial_delay
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as e:
            if not is_file_busy_error(e):
                raise

            last_error = e
            if attempt >= attempts:
                break

            print(
                f"  File busy while {description}; retrying in {delay:.1f}s "
                f"({attempt}/{attempts}): {e}"
            )
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)

    raise last_error


def get_audio_object(file_path: str):
    try:
        return File(file_path)
    except Exception:
        return None


def read_duration_seconds(file_path: str):
    try:
        audio = get_audio_object(file_path)
        if audio and getattr(audio, "info", None) and getattr(audio.info, "length", None):
            return int(round(audio.info.length))
    except Exception:
        pass
    return None


def get_file_ext(file_path: str) -> str:
    return os.path.splitext(file_path)[1].lower()


MP4_TITLE_KEYS = ("\xa9nam", "\xc2\xa9nam", "\ufffdnam")
MP4_ARTIST_KEYS = ("\xa9ART", "\xc2\xa9ART", "\ufffdART")
MP4_ALBUM_KEYS = ("\xa9alb", "\xc2\xa9alb", "\ufffdalb")
MP4_LYRICS_KEYS = ("\xa9lyr", "\xc2\xa9lyr", "\ufffdlyr")


def tag_value_has_text(value) -> bool:
    if isinstance(value, list):
        return any(str(item).strip() for item in value)
    return bool(value and str(value).strip())


def first_tag_text(value):
    if isinstance(value, list):
        for item in value:
            text = tag_item_text(item)
            if text:
                return text
        return None
    text = tag_item_text(value)
    if text:
        return text
    return None


def tag_item_text(item):
    if item is None:
        return ""

    if isinstance(item, bytes):
        for encoding in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                text = item.decode(encoding).strip("\ufeff\x00").strip()
            except Exception:
                continue
            if text:
                return text
        return ""

    data = getattr(item, "data", None)
    if isinstance(data, bytes):
        return tag_item_text(data)

    text = str(item).strip()
    return text


def tag_key_looks_like_lyrics(key):
    lowered = str(key).casefold()
    return "lyric" in lowered and "lyricist" not in lowered


def lyrics_text_looks_usable(text):
    text = str(text or "").strip()
    if not text:
        return False

    letters = sum(1 for char in text if char.isalpha())
    nonempty_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if letters < 120:
        return False
    if len(nonempty_lines) < 5:
        return False

    folded = text.casefold()
    obvious_non_lyrics = (
        "subscribe to",
        "http://",
        "https://",
        "www.",
        "all rights reserved",
    )
    if any(marker in folded for marker in obvious_non_lyrics):
        return False

    return True


def first_mp4_lyrics(tags):
    if not tags:
        return None

    for key in MP4_LYRICS_KEYS:
        text = first_tag_text(tags.get(key))
        if text:
            return text

    for key, value in tags.items():
        if tag_key_looks_like_lyrics(key):
            text = first_tag_text(value)
            if text:
                return text

    return None


def first_tag_text_from_keys(tags, keys):
    if not tags:
        return ""

    for key in keys:
        text = first_tag_text(tags.get(key))
        if text:
            return norm_text(text)

    return ""


def first_id3_lyrics(tags):
    if not tags:
        return None

    for frame in tags.getall("USLT"):
        text = first_id3_frame_text(frame)
        if text:
            return text

    for frame in tags.getall("SYLT"):
        text = first_id3_frame_text(frame)
        if text:
            return text

    for frame in tags.getall("TXXX"):
        if tag_key_looks_like_lyrics(getattr(frame, "desc", "")):
            text = first_id3_frame_text(frame)
            if text:
                return text

    for frame in tags.getall("COMM"):
        if tag_key_looks_like_lyrics(getattr(frame, "desc", "")):
            text = first_id3_frame_text(frame)
            if text:
                return text

    return None


def first_id3_frame_text(frame):
    value = getattr(frame, "text", None)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, tuple):
                item = item[0] if item else ""
            text = tag_item_text(item)
            if text:
                parts.append(text)
        return " ".join(parts).strip()

    return tag_item_text(value)


def get_existing_audio_metadata(file_path: str):
    title = ""
    artist = ""
    album = ""
    lyrics = None

    try:
        audio = get_audio_object(file_path)
        if not audio:
            return {
                "title": title,
                "artist": artist,
                "album": album,
                "lyrics": lyrics,
                "has_lyrics": False,
            }

        if isinstance(audio, MP4):
            tags = audio.tags or {}
            title = first_tag_text_from_keys(tags, MP4_TITLE_KEYS)
            artist = first_tag_text_from_keys(tags, MP4_ARTIST_KEYS)
            album = first_tag_text_from_keys(tags, MP4_ALBUM_KEYS)
            lyrics = first_mp4_lyrics(tags)

        elif isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            tags = audio.tags or {}
            title = _first_tag_value(tags.get("title"))
            artist = _first_tag_value(tags.get("artist"))
            album = _first_tag_value(tags.get("album"))
            for key in ("lyrics", "unsyncedlyrics", "syncedlyrics", "lyric"):
                lyrics = first_tag_text(tags.get(key))
                if lyrics:
                    break
            if not lyrics:
                for key, value in tags.items():
                    if tag_key_looks_like_lyrics(key):
                        lyrics = first_tag_text(value)
                        if lyrics:
                            break

        elif isinstance(audio, (MP3, WAVE, AIFF)):
            try:
                tags = ID3(file_path)
            except Exception:
                tags = None

            if tags:
                if "TIT2" in tags:
                    title = norm_text(str(tags["TIT2"]))
                if "TPE1" in tags:
                    artist = norm_text(str(tags["TPE1"]))
                if "TALB" in tags:
                    album = norm_text(str(tags["TALB"]))
                lyrics = first_id3_lyrics(tags)

        else:
            tags = audio.tags or {}
            title = _first_tag_value(tags.get("title"))
            artist = _first_tag_value(tags.get("artist"))
            album = _first_tag_value(tags.get("album"))
            for key in ("lyrics", "unsyncedlyrics", "syncedlyrics", "lyric"):
                lyrics = first_tag_text(tags.get(key))
                if lyrics:
                    break
            if not lyrics:
                for key, value in tags.items():
                    if tag_key_looks_like_lyrics(key):
                        lyrics = first_tag_text(value)
                        if lyrics:
                            break

    except Exception:
        pass

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "lyrics": lyrics,
        "has_lyrics": lyrics_text_looks_usable(lyrics),
    }


def has_lyrics(file_path: str) -> bool:
    return get_existing_audio_metadata(file_path)["has_lyrics"]


def read_existing_lyrics(file_path: str):
    return get_existing_audio_metadata(file_path)["lyrics"]


def _first_tag_value(val):
    if isinstance(val, list):
        return norm_text(str(val[0])) if val else ""
    if val is None:
        return ""
    return norm_text(str(val))


def get_existing_basic_tags(file_path: str):
    title = ""
    artist = ""
    album = ""

    try:
        audio = get_audio_object(file_path)
        if not audio:
            return title, artist, album

        if isinstance(audio, MP4):
            title = _first_tag_value(audio.tags.get("\xa9nam") if audio.tags else "")
            artist = _first_tag_value(audio.tags.get("\xa9ART") if audio.tags else "")
            album = _first_tag_value(audio.tags.get("\xa9alb") if audio.tags else "")

        elif isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            title = _first_tag_value(audio.tags.get("title") if audio.tags else "")
            artist = _first_tag_value(audio.tags.get("artist") if audio.tags else "")
            album = _first_tag_value(audio.tags.get("album") if audio.tags else "")

        elif isinstance(audio, (MP3, WAVE, AIFF)):
            try:
                tags = ID3(file_path)
                if "TIT2" in tags:
                    title = norm_text(str(tags["TIT2"]))
                if "TPE1" in tags:
                    artist = norm_text(str(tags["TPE1"]))
                if "TALB" in tags:
                    album = norm_text(str(tags["TALB"]))
            except Exception:
                pass

        else:
            # Generic fallback for other mutagen-supported formats
            if audio.tags:
                title = _first_tag_value(audio.tags.get("title"))
                artist = _first_tag_value(audio.tags.get("artist"))
                album = _first_tag_value(audio.tags.get("album"))

    except Exception:
        pass

    return title, artist, album


def _extract_lyrics_from_item(item):
    if not isinstance(item, dict):
        return None
    lyrics = item.get("plainLyrics") or item.get("syncedLyrics")
    if lyrics and str(lyrics).strip():
        return str(lyrics).strip()
    return None


VERSION_NOISE_RE = re.compile(
    r"\b("
    r"official|video|audio|lyrics?|visualizer|mv|hd|4k|"
    r"remix|cover|live|acoustic|sped\s*up|slowed|nightcore|"
    r"prod(?:uced)?\.?\s*by"
    r")\b",
    flags=re.IGNORECASE,
)


TITLE_FEATURE_CLAUSE_RE = re.compile(
    r"[\(\[\{]\s*(?:feat(?:uring)?|ft\.?|with)\b.*?[\)\]\}]",
    flags=re.IGNORECASE,
)
TRAILING_FEATURE_RE = re.compile(
    r"\b(?:feat(?:uring)?|ft\.?|with)\b.+$",
    flags=re.IGNORECASE,
)


def strip_title_feature_clauses(value):
    value = TITLE_FEATURE_CLAUSE_RE.sub(" ", norm_text(value))
    value = TRAILING_FEATURE_RE.sub(" ", value)
    return norm_text(value)


def title_identity_variants(value):
    variants = []
    for candidate in (norm_text(value), strip_title_feature_clauses(value)):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


def fold_match_text(value):
    value = unicodedata.normalize("NFKD", norm_text(value).casefold())
    value = "".join(
        char
        for char in value
        if unicodedata.category(char) != "Mn"
    )
    value = VERSION_NOISE_RE.sub(" ", value)
    value = re.sub(r"\b(feat|ft|featuring)\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def text_match_ratio(left, right):
    left = fold_match_text(left)
    right = fold_match_text(right)
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def expected_title_token_overlap(expected, actual):
    expected_tokens = set(fold_match_text(expected).split())
    actual_tokens = set(fold_match_text(actual).split())
    if not expected_tokens or not actual_tokens:
        return 0.0
    return len(expected_tokens & actual_tokens) / len(expected_tokens)


def artist_token_overlap(expected, actual):
    expected_tokens = set(fold_match_text(expected).split())
    actual_tokens = set(fold_match_text(actual).split())
    if not expected_tokens or not actual_tokens:
        return 0.0
    return len(expected_tokens & actual_tokens) / len(expected_tokens)


def genius_highlight_properties(hit):
    properties = []
    for highlight in hit.get("highlights") or []:
        if not isinstance(highlight, dict):
            continue
        for key in ("property", "field", "path"):
            value = str(highlight.get(key) or "").strip().casefold()
            if value:
                properties.append(value)
    return properties


def genius_hit_is_lyrics_only_match(hit):
    properties = genius_highlight_properties(hit)
    if not properties:
        return False

    has_title_match = any(
        "title" in value
        or value == "name"
        or value.endswith(".name")
        for value in properties
    )
    has_lyric_match = any(
        "lyric" in value
        or "body" in value
        for value in properties
    )
    return has_lyric_match and not has_title_match


def genius_title_match_score(expected_title, result):
    expected_variants = title_identity_variants(expected_title)
    title_values = [
        result.get("title"),
        result.get("title_with_featured"),
        result.get("full_title"),
        result.get("name"),
    ]
    best_score = 0.0
    best_overlap = 0.0
    best_value = ""

    for expected_variant in expected_variants:
        for value in title_values:
            value = norm_text(value or "")
            if not value:
                continue

            score = text_match_ratio(expected_variant, value)
            overlap = expected_title_token_overlap(expected_variant, value)
            if (score, overlap) > (best_score, best_overlap):
                best_score = score
                best_overlap = overlap
                best_value = value

    return best_score, best_overlap, best_value


def score_lrclib_item(
    item,
    expected_title,
    expected_artist,
    expected_duration=None,
    expected_album="",
):
    item_title = item.get("trackName") or ""
    item_artist = item.get("artistName") or ""
    item_album = item.get("albumName") or ""

    title_score = text_match_ratio(expected_title, item_title)
    artist_score = artist_token_overlap(expected_artist, item_artist)
    album_score = (
        text_match_ratio(expected_album, item_album)
        if expected_album and item_album
        else 0.5
    )

    if title_score < 0.82:
        return None

    if (
        expected_artist
        and expected_artist.casefold() != "unknown"
        and artist_score < 0.50
    ):
        return None

    duration_score = 0.5
    item_duration = item.get("duration")

    if expected_duration and item_duration:
        try:
            duration_difference = abs(
                float(expected_duration) - float(item_duration)
            )
        except (TypeError, ValueError):
            duration_difference = None

        if duration_difference is not None:
            if duration_difference > 20:
                return None
            duration_score = max(
                0.0,
                1.0 - duration_difference / 20.0,
            )

    total = (
        0.55 * title_score
        + 0.25 * artist_score
        + 0.15 * duration_score
        + 0.05 * album_score
    )

    return {
        "total": total,
        "title": title_score,
        "artist": artist_score,
        "duration": duration_score,
        "album": album_score,
    }


def _lrclib_get(title, artist="", duration=None, album=""):
    params = {"track_name": title}

    if artist and artist != "Unknown":
        params["artist_name"] = artist
    if duration:
        params["duration"] = duration
    if album:
        params["album_name"] = album

    try:
        response = requests.get(
            LRCLIB_GET_URL,
            params=params,
            headers=lyrics_headers,
            timeout=20,
        )
        if response.status_code != 200:
            return None

        item = response.json()
        score = score_lrclib_item(
            item=item,
            expected_title=title,
            expected_artist=artist,
            expected_duration=duration,
            expected_album=album,
        )
        if not score or score["total"] < 0.78:
            print("  LRCLIB exact response failed identity validation.")
            return None

        lyrics = _extract_lyrics_from_item(item)
        if lyrics:
            print(
                "  LRCLIB exact match accepted: "
                f"score={score['total']:.3f}, "
                f"title={score['title']:.3f}, "
                f"artist={score['artist']:.3f}"
            )
            return lyrics

    except Exception as error:
        print(f"  LRCLIB exact lookup error: {error}")

    return None


def _lrclib_search(
    query,
    expected_title="",
    expected_artist="",
    expected_duration=None,
    expected_album="",
):
    try:
        response = requests.get(
            LRCLIB_SEARCH_URL,
            params={"q": query},
            headers=lyrics_headers,
            timeout=20,
        )
        if response.status_code != 200:
            return None

        data = response.json()
        if not isinstance(data, list) or not data:
            return None

        ranked = []
        for item in data:
            lyrics = _extract_lyrics_from_item(item)
            if not lyrics:
                continue

            score = score_lrclib_item(
                item=item,
                expected_title=expected_title,
                expected_artist=expected_artist,
                expected_duration=expected_duration,
                expected_album=expected_album,
            )
            if not score:
                continue

            ranked.append((score["total"], score, item, lyrics))

        ranked.sort(key=lambda row: row[0], reverse=True)

        if not ranked:
            return None

        best_total, best_score, best_item, best_lyrics = ranked[0]
        second_total = ranked[1][0] if len(ranked) > 1 else 0.0

        if best_total < 0.78:
            print(f"  LRCLIB search best score too low: {best_total:.3f}")
            return None

        if len(ranked) > 1 and best_total - second_total < 0.06:
            print("  LRCLIB search is ambiguous; top candidates are too close.")
            return None

        print(
            "  LRCLIB search match accepted: "
            f"title='{best_item.get('trackName')}', "
            f"artist='{best_item.get('artistName')}', "
            f"score={best_total:.3f}"
        )
        return best_lyrics

    except Exception as error:
        print(f"  LRCLIB search error: {error}")
        return None

    return None


def get_lyrics_from_lrclib(title, artist, duration=None, album=""):
    if not title:
        return None

    title = norm_text(title)
    artist = norm_text(artist)
    album = norm_text(album)

    if artist and artist != "Unknown":
        lyrics = _lrclib_get(title=title, artist=artist, duration=duration, album=album)
        if lyrics:
            return lyrics

    query = (
        f"{artist} {title}".strip()
        if artist and artist != "Unknown"
        else title
    )
    if artist and artist != "Unknown":
        expected_artist = artist
    else:
        expected_artist = ""

    return _lrclib_search(
        query=query,
        expected_title=title,
        expected_artist=expected_artist,
        expected_duration=duration,
        expected_album=album,
    )


def get_lyrics_from_genius(title: str, artist: str, require_artist=True):
    if not title or not GENIUS_API_TOKEN:
        return None

    title = norm_text(title)
    artist = norm_text(artist)
    require_artist = bool(
        require_artist
        and artist
        and artist.casefold() != "unknown"
    )
    query = f"{artist} {title}".strip() if require_artist else title
    search_label = "title+artist" if require_artist else "title-only"
    headers = {"Authorization": f"Bearer {GENIUS_API_TOKEN}"}

    try:
        resp = requests.get(
            f"{GENIUS_API_URL}/search",
            params={"q": query},
            headers=headers,
            timeout=20,
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        hits = data.get("response", {}).get("hits", [])
        if not hits:
            return None

        ranked_hits = []
        for hit in hits:
            result = hit.get("result") or {}
            if not result:
                continue

            if genius_hit_is_lyrics_only_match(hit):
                result_title = result.get("title") or result.get("full_title") or ""
                print(
                    "  Genius hit rejected because the search matched lyrics, "
                    f"not the song title: {result_title}"
                )
                continue

            result_artist = (
                result.get("artist_names")
                or result.get("primary_artist", {}).get("name")
                or ""
            )

            title_score, title_overlap, matched_title = genius_title_match_score(
                title,
                result,
            )
            artist_score = (
                artist_token_overlap(artist, result_artist)
                if require_artist
                else 0.0
            )

            minimum_title_score = 0.84 if require_artist else 0.90
            if title_score < minimum_title_score:
                continue
            if title_overlap < 0.80:
                print(
                    "  Genius hit rejected because the title/name field did "
                    f"not contain the expected song title: {matched_title}"
                )
                continue

            if require_artist and artist_score < 0.45:
                continue

            total = (
                0.70 * title_score + 0.30 * artist_score
                if require_artist
                else title_score
            )
            ranked_hits.append((total, result))

        ranked_hits.sort(key=lambda row: row[0], reverse=True)

        if not ranked_hits:
            print(f"  Genius returned no identity-safe {search_label} hit.")
            return None

        best_total, best_hit = ranked_hits[0]

        minimum_total_score = 0.78 if require_artist else 0.90
        if best_total < minimum_total_score:
            print(
                f"  Genius {search_label} best match score too low: "
                f"{best_total:.3f}"
            )
            return None

        if (
            len(ranked_hits) > 1
            and best_total - ranked_hits[1][0] < 0.06
        ):
            print(f"  Genius {search_label} results are ambiguous; refusing to choose.")
            return None

        print(
            f"  Genius {search_label} match accepted "
            f"with score={best_total:.3f}"
        )

        song_url = best_hit.get("url")
        if not song_url:
            return None

        page_resp = requests.get(song_url, timeout=20)
        if page_resp.status_code != 200:
            return None

        html_text = page_resp.text
        lyrics_parts = re.findall(
            r'<div[^>]*data-lyrics-container="true"[^>]*>(.*?)</div>',
            html_text,
            re.DOTALL,
        )
        if not lyrics_parts:
            return None

        lyrics_lines = []
        for part in lyrics_parts:
            part = re.sub(r'<br\s*/?>', "\n", part)
            part = re.sub(r"<[^>]+>", "", part)
            part = html.unescape(part)
            lyrics_lines.append(part.strip())

        return "\n\n".join(lyrics_lines).strip()
    except Exception:
        return None


YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_youtube_id_from_url(value):
    value = norm_text(value)
    if not value:
        return None

    try:
        parsed = urlparse(value)
    except Exception:
        return None

    query = parse_qs(parsed.query)
    for key in ("v", "vi"):
        for candidate in query.get(key, []):
            if YOUTUBE_VIDEO_ID_RE.fullmatch(candidate):
                return candidate

    host = parsed.netloc.casefold()
    path_parts = [part for part in parsed.path.split("/") if part]
    if host.endswith("youtu.be") and path_parts and YOUTUBE_VIDEO_ID_RE.fullmatch(path_parts[0]):
        return path_parts[0]

    for marker in ("shorts", "embed", "live", "v"):
        if marker in path_parts:
            index = path_parts.index(marker)
            if index + 1 < len(path_parts) and YOUTUBE_VIDEO_ID_RE.fullmatch(path_parts[index + 1]):
                return path_parts[index + 1]

    return None


def extract_youtube_id_from_comment_text(value):
    value = norm_text(value)
    if not value:
        return None

    match = re.search(
        r"\bYouTube\s*(?:video\s*)?ID\s*[:=]\s*([A-Za-z0-9_-]{11})\b",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1)

    return extract_youtube_id_from_url(value)


def extract_youtube_id_from_audio(file_path):
    try:
        tags = ID3(file_path)
    except Exception:
        tags = None

    if tags:
        for frame_id in ("WOAS", "WXXX"):
            for frame in tags.getall(frame_id):
                youtube_id = extract_youtube_id_from_url(getattr(frame, "url", ""))
                if youtube_id:
                    return youtube_id

        for frame_id in ("COMM", "TXXX"):
            for frame in tags.getall(frame_id):
                desc = str(getattr(frame, "desc", "") or "")
                text = first_id3_frame_text(frame)
                if "youtube" in desc.casefold() or "youtube" in text.casefold():
                    youtube_id = extract_youtube_id_from_comment_text(text)
                    if youtube_id:
                        return youtube_id

    try:
        audio = File(file_path)
    except Exception:
        audio = None

    if audio and audio.tags:
        for key, value in audio.tags.items():
            key_text = str(key).casefold()
            if "youtube" not in key_text and "comment" not in key_text and "url" not in key_text:
                continue
            text = first_tag_text(value)
            youtube_id = (
                extract_youtube_id_from_comment_text(text)
                or extract_youtube_id_from_url(text)
            )
            if youtube_id:
                return youtube_id

    return None


def extract_youtube_id_from_name(file_name):
    base = os.path.splitext(os.path.basename(file_name))[0]
    matches = re.findall(r"\[([A-Za-z0-9_-]{11})\]", base)
    return matches[-1] if matches else None


def text_from_metadata_value(value):
    if isinstance(value, list):
        parts = [norm_text(str(item)) for item in value if norm_text(str(item))]
        return ", ".join(parts)
    return norm_text(str(value or ""))


def clean_topic_artist_name(value):
    value = text_from_metadata_value(value)
    return re.sub(r"\s+-\s+Topic$", "", value, flags=re.IGNORECASE).strip()


def youtube_extract_opts(use_cookies=False):
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 5,
        "extractor_retries": 5,
        "socket_timeout": 20,
    }

    if use_cookies:
        opts["cookiesfrombrowser"] = ("firefox",)
    else:
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android_vr", "web_safari", "web", "tv"],
            }
        }

    return opts


def get_youtube_info(video_id):
    if video_id in SUBTITLE_INFO_CACHE:
        return SUBTITLE_INFO_CACHE[video_id]

    url = YOUTUBE_WATCH_URL.format(video_id=video_id)
    last_error = None

    for use_cookies in (False, True):
        try:
            with YoutubeDL(youtube_extract_opts(use_cookies=use_cookies)) as ydl:
                info = ydl.extract_info(url, download=False)
            SUBTITLE_INFO_CACHE[video_id] = info
            return info
        except KeyboardInterrupt:
            raise
        except Exception as e:
            last_error = e
            mode = "with cookies" if use_cookies else "without cookies"
            print(f"  Could not fetch YouTube subtitle metadata {mode}: {e}")

    print(f"  No YouTube subtitle metadata available for {video_id}: {last_error}")
    SUBTITLE_INFO_CACHE[video_id] = None
    return None


def parse_youtube_art_track_description(description):
    description = description or ""
    lower_description = description.lower()
    if (
        "provided to youtube by" not in lower_description
        and "auto-generated by youtube" not in lower_description
    ):
        return None

    for raw_line in description.splitlines():
        line = norm_text(raw_line)
        if not line or " ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· " not in line:
            continue

        lowered = line.lower()
        if lowered.startswith(("provided to youtube by", "released on:", "auto-generated by youtube")):
            continue
        if lowered.startswith(("composer", "lyricist", "vocalist", "producer", "writer")):
            continue
        if line.startswith(("ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¾ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â", "ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â©")):
            continue

        title_part, artist_part = line.split(" ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· ", 1)
        title = norm_text(title_part)
        artist = clean_topic_artist_name(artist_part)
        if title and artist:
            return title, artist

    return None


def get_youtube_music_metadata(video_id):
    if not video_id:
        return None

    info = get_youtube_info(video_id)
    if not info:
        return None

    track = text_from_metadata_value(info.get("track"))
    artist = text_from_metadata_value(info.get("artists")) or text_from_metadata_value(info.get("artist"))
    source = "youtube_music_fields"

    if track and not artist:
        channel_artist = clean_topic_artist_name(info.get("channel") or info.get("uploader"))
        channel = text_from_metadata_value(info.get("channel") or info.get("uploader"))
        if channel.lower().endswith(" - topic"):
            artist = channel_artist

    if not (track and artist):
        parsed = parse_youtube_art_track_description(info.get("description") or "")
        if parsed:
            track, artist = parsed
            source = "youtube_art_track_description"

    if not (track and artist):
        return None

    track = norm_text(track)
    artist = clean_topic_artist_name(artist)
    album = text_from_metadata_value(info.get("album"))

    if looks_bad(track) or looks_bad(artist):
        return None

    return {
        "title": track,
        "artist": artist,
        "album": album,
        "source": source,
    }


def choose_subtitle_format(formats):
    if not formats:
        return None

    preferred_exts = ("json3", "srv3", "vtt", "ttml", "srv2", "srv1")
    for ext in preferred_exts:
        for item in formats:
            if item.get("url") and str(item.get("ext", "")).lower() == ext:
                return item

    for item in formats:
        if item.get("url"):
            return item

    return None


def subtitle_track_display_name(language_code, track):
    name = track.get("name") or track.get("format") or ""
    if isinstance(name, dict):
        name = name.get("simpleText") or ""
    return f"{language_code} {name}".strip()


def subtitle_url_hints(url):
    try:
        query = parse_qs(urlparse(url or "").query)
    except Exception:
        return "", ""

    source_language = (query.get("lang") or [""])[0]
    translated_to = (query.get("tlang") or [""])[0]
    return source_language, translated_to


def subtitle_language_root(language_code):
    language_code = str(language_code or "").strip().lower()
    if not language_code:
        return ""
    return re.split(r"[-_]", language_code, maxsplit=1)[0]


def is_live_chat_subtitle(language_code, display_name):
    text = f"{language_code or ''} {display_name or ''}".strip().lower()
    text = re.sub(r"\s+", "_", text)
    return "live_chat" in text


def iter_subtitle_candidates(info, automatic=False):
    key = "automatic_captions" if automatic else "subtitles"
    tracks_by_language = info.get(key) or {}

    for language_code, formats in tracks_by_language.items():
        chosen = choose_subtitle_format(formats)
        if not chosen:
            continue

        display_name = subtitle_track_display_name(language_code, chosen)
        if is_live_chat_subtitle(language_code, display_name):
            continue

        source_language, translated_to = subtitle_url_hints(chosen.get("url"))
        if translated_to:
            continue

        normalized_track_language = normalize_language_code(
            source_language or language_code
        )
        if not normalized_track_language:
            continue

        yield {
            "language_code": language_code,
            "normalized_language_code": normalized_track_language,
            "name": display_name,
            "url": chosen.get("url"),
            "ext": str(chosen.get("ext", "")).lower(),
            "automatic": automatic,
            "source_language_code": source_language,
            "translated_to_language_code": translated_to,
            "is_translated": False,
            "http_headers": dict(
                chosen.get("http_headers")
                or info.get("http_headers")
                or {}
            ),
        }


def clean_subtitle_lines(lines):
    cleaned = []
    previous = None

    for line in lines:
        line = html.unescape(line or "")
        line = re.sub(r"<[^>]+>", " ", line)
        line = re.sub(r"\{\\.*?\}", " ", line)
        line = remove_square_bracket_content(line)
        line = re.sub(r"[\u266a\u266b]", " ", line)
        line = norm_text(line)

        if not line:
            continue
        if re.match(r"^\d+$", line):
            continue
        if "-->" in line:
            continue
        if line.upper() == "WEBVTT":
            continue
        if line == previous:
            continue

        cleaned.append(line)
        previous = line

    return "\n".join(cleaned).strip()


def parse_json3_subtitle(text):
    data = json.loads(text)
    lines = []

    for event in data.get("events") or []:
        pieces = []
        for segment in event.get("segs") or []:
            value = segment.get("utf8") or ""
            if value:
                pieces.append(value)
        if pieces:
            lines.append("".join(pieces))

    return clean_subtitle_lines(lines)


def parse_xml_subtitle(text):
    try:
        root = ElementTree.fromstring(text)
    except Exception:
        return ""

    lines = []
    for element in root.iter():
        if element.text and element.text.strip():
            lines.append(element.text)

    return clean_subtitle_lines(lines)


def parse_text_subtitle(text):
    lines = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            continue
        lines.append(stripped)

    return clean_subtitle_lines(lines)


def parse_subtitle_text(text, ext):
    ext = (ext or "").lower()

    try:
        if ext == "json3":
            parsed = parse_json3_subtitle(text)
        elif ext in {"srv1", "srv2", "srv3", "ttml", "xml"}:
            parsed = parse_xml_subtitle(text)
        else:
            parsed = parse_text_subtitle(text)
    except Exception as e:
        print(f"  Subtitle parse failed for {ext or 'unknown'} format: {e}")
        parsed = parse_text_subtitle(text)

    return parsed


def download_subtitle_text(candidate):
    global YOUTUBE_SUBTITLES_DISABLED_FOR_RUN

    candidate.pop("download_error_status", None)

    if YOUTUBE_SUBTITLES_DISABLED_FOR_RUN:
        print("  YouTube subtitles are disabled for the rest of this run.")
        return None

    url = candidate.get("url")
    if not url:
        return None

    headers = dict(candidate.get("http_headers") or {})
    last_error = None

    for use_cookies in (False, True):
        try:
            opts = youtube_extract_opts(use_cookies=use_cookies)
            opts["sleep_interval_requests"] = 1
            opts["sleep_interval_subtitles"] = 1

            with YoutubeDL(opts) as ydl:
                request = YtDlpRequest(url, headers=headers)
                response = ydl.urlopen(request)
                try:
                    raw = response.read()
                finally:
                    close_response = getattr(response, "close", None)
                    if close_response:
                        close_response()

            text = raw.decode("utf-8-sig", errors="replace")
            parsed = parse_subtitle_text(text, candidate.get("ext"))

            if not parsed:
                print(f"  Subtitle {candidate.get('name')} was empty after parsing.")
                return None

            return parsed

        except Exception as error:
            last_error = error
            error_text = str(error)
            status = getattr(error, "status", None)

            if status is None:
                response = getattr(error, "response", None)
                status = getattr(response, "status", None)
                if status is None:
                    status = getattr(response, "status_code", None)

            if status == 429 or "429" in error_text:
                candidate["download_error_status"] = 429
                YOUTUBE_SUBTITLES_DISABLED_FOR_RUN = True
                print(
                    "  YouTube subtitle request returned 429. "
                    "YouTube subtitles are disabled for the rest of this run; "
                    "the resolver will use LRCLIB/Genius or leave lyrics blank."
                )
                return None

            if status == 403 or "403" in error_text:
                candidate["download_error_status"] = 403
            else:
                candidate["download_error_status"] = status

    print(
        f"  Could not download subtitle {candidate.get('name')} "
        f"through yt-dlp: {last_error}"
    )
    return None



def get_lyrics_from_youtube_subtitles(
    video_id,
    video_title,
    title="",
    artist="",
    library_lyrics=None,
    explicit_language="",
):
    if ARGS.disable_youtube_subtitles:
        print("  YouTube subtitle lookup disabled by command-line option.")
        return None, None

    if YOUTUBE_SUBTITLES_DISABLED_FOR_RUN:
        print("  YouTube subtitle lookup disabled after an earlier 429.")
        return None, None

    info = get_youtube_info(video_id)
    if not info:
        return None, None

    expected_language, expected_source = infer_expected_language(
        title=title,
        artist=artist,
        library_lyrics=library_lyrics or "",
        explicit_language=explicit_language,
    )

    manual_candidates = list(iter_subtitle_candidates(info, automatic=False))
    automatic_candidates = list(iter_subtitle_candidates(info, automatic=True))

    print(
        "  Original YouTube subtitles after translation filtering: "
        f"{len(manual_candidates)} manual, "
        f"{len(automatic_candidates)} automatic."
    )

    if expected_language:
        print(
            f"  Expected lyrics language={expected_language} "
            f"from {expected_source}."
        )
        candidates = [
            candidate
            for candidate in manual_candidates + automatic_candidates
            if candidate.get("normalized_language_code") == expected_language
        ]
    else:
        if len(manual_candidates) == 1:
            candidates = manual_candidates
            print(
                "  Expected language is unknown, but exactly one original "
                "manual subtitle exists; validating its actual text."
            )
        elif len(manual_candidates) > 1:
            print(
                "  Expected language is unknown and multiple original manual "
                "subtitle languages exist. Refusing to guess."
            )
            return None, None
        elif len(automatic_candidates) == 1:
            candidates = automatic_candidates
            print(
                "  Expected language is unknown, but exactly one original "
                "automatic subtitle exists; validating its actual text."
            )
        else:
            print(
                "  Expected language is unknown and the original subtitle "
                "choice is ambiguous. Refusing to guess."
            )
            return None, None

    candidates.sort(
        key=lambda candidate: (
            bool(candidate.get("automatic")),
            candidate.get("name") or "",
        )
    )

    if not candidates:
        print("  No original subtitle track matches the expected lyrics language.")
        return None, None

    for index, candidate in enumerate(candidates[:3], start=1):
        kind = "automatic" if candidate.get("automatic") else "manual"
        print(
            f"  Validating {kind} subtitle #{index}: "
            f"{candidate.get('name')}"
        )

        text = download_subtitle_text(candidate)
        if not text:
            if YOUTUBE_SUBTITLES_DISABLED_FOR_RUN:
                break
            continue

        validation = validate_subtitle_language(
            candidate=candidate,
            subtitle_text=text,
            expected_language=expected_language,
        )

        detected = validation.get("detected") or {}
        print(
            "  Local subtitle language validation: "
            f"approved={validation.get('approved')}, "
            f"track={candidate.get('normalized_language_code')}, "
            f"detected={detected.get('code')}, "
            f"confidence={detected.get('confidence', 0.0):.3f}, "
            f"margin={detected.get('margin', 0.0):.3f}, "
            f"reason={validation.get('reason')}"
        )

        if not validation.get("approved"):
            continue

        approved_candidate = dict(candidate)
        approved_candidate["language_validation"] = validation
        approved_candidate["language_match_approved"] = True
        approved_candidate["language_match_source"] = "local_lingua_full_text"
        return text, approved_candidate

    print("  No YouTube subtitle passed deterministic language validation.")
    return None, None


def normalize_lyrics_for_similarity(text):
    text = remove_square_bracket_content(text or "")
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"\([^)]*(official|lyrics?|video|audio|remix|cover)[^)]*\)", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def lyric_similarity(left, right):
    left_norm = normalize_lyrics_for_similarity(left)
    right_norm = normalize_lyrics_for_similarity(right)

    if not left_norm or not right_norm:
        return 0.0

    sequence_ratio = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
    left_words = set(left_norm.split())
    right_words = set(right_norm.split())

    if not left_words or not right_words:
        return sequence_ratio

    overlap_ratio = len(left_words & right_words) / max(len(left_words), len(right_words))
    return max(sequence_ratio, overlap_ratio)


def choose_best_lyrics(
    library_lyrics,
    subtitle_lyrics,
    subtitle_candidate,
    library_source="lrclib",
    title="",
    artist="",
):
    library_source_label = (
        "Genius" if library_source == "genius" else "LRCLIB"
    )
    library_lyrics = clean_lyrics_for_tag(library_lyrics or "")
    subtitle_lyrics = clean_lyrics_for_tag(subtitle_lyrics or "")

    subtitle_validation = (
        subtitle_candidate.get("language_validation")
        if subtitle_candidate
        else None
    )
    subtitle_is_approved = bool(
        subtitle_validation
        and subtitle_validation.get("approved")
    )

    if library_lyrics and subtitle_lyrics:
        similarity = lyric_similarity(library_lyrics, subtitle_lyrics)
        print(
            f"  {library_source_label}/subtitle lyric similarity: "
            f"{similarity:.2%}"
        )

        if not subtitle_is_approved:
            print(
                f"  Rejecting subtitle; using verified "
                f"{library_source_label} result."
            )
            return library_lyrics, library_source, similarity

        library_language = detect_text_language(library_lyrics)
        subtitle_language = subtitle_validation.get("detected") or {}

        if (
            library_language.get("approved")
            and subtitle_language.get("approved")
            and library_language.get("code") != subtitle_language.get("code")
        ):
            print(
                "  Library/subtitle languages conflict. "
                "Rejecting subtitle and using the strict library result."
            )
            return library_lyrics, library_source, similarity

        if similarity >= 0.30:
            print(
                f"  Sources agree sufficiently; using "
                f"{library_source_label} lyrics."
            )
        else:
            print(
                "  Sources have low text similarity. This is a conflict, "
                "not a reason to prefer the subtitle. Using the strict "
                "library result."
            )

        return library_lyrics, library_source, similarity

    if library_lyrics:
        print(
            f"  Using strict {library_source_label} lyrics; "
            "no approved YouTube subtitle was found."
        )
        return library_lyrics, library_source, None

    if subtitle_lyrics and subtitle_is_approved:
        print(
            "  Using YouTube subtitle only because it passed track-code, "
            "full-text language, expected-language, and length gates."
        )
        return subtitle_lyrics, "youtube_subtitles_validated", None

    if subtitle_lyrics and not subtitle_is_approved:
        print("  Subtitle text exists but failed validation; not writing it.")

    return None, None, None


def write_tags(file_path: str, title: str, artist: str, album: str = ""):
    title = norm_text(title)
    artist = norm_text(artist)
    album = norm_text(album)

    if looks_bad(title):
        title = ""
    if artist_is_invalid(artist):
        artist = "Unknown"

    audio = get_audio_object(file_path)
    if not audio:
        raise RuntimeError("Unsupported or unreadable audio format")

    try:
        if isinstance(audio, MP4):
            if audio.tags is None:
                audio.add_tags()
            if title:
                audio["\xa9nam"] = [title]
            if artist:
                audio["\xa9ART"] = [artist]
            if album:
                audio["\xa9alb"] = [album]
            retry_file_operation(
                f"saving MP4 title/artist tags for {file_path}",
                audio.save,
            )
            return

        if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            if title:
                audio["title"] = [title]
            if artist:
                audio["artist"] = [artist]
            if album:
                audio["album"] = [album]
            retry_file_operation(
                f"saving Vorbis/FLAC title/artist tags for {file_path}",
                audio.save,
            )
            return

        if isinstance(audio, (MP3, WAVE, AIFF)):
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                tags = ID3()

            if title:
                tags.delall("TIT2")
                tags.add(TIT2(encoding=3, text=title))
            if artist:
                tags.delall("TPE1")
                tags.add(TPE1(encoding=3, text=[artist]))
            if album:
                tags.delall("TALB")
                tags.add(TALB(encoding=3, text=album))

            retry_file_operation(
                f"saving ID3 title/artist tags for {file_path}",
                lambda: tags.save(file_path, v2_version=3),
            )
            return

        # Generic fallback
        if audio.tags is None:
            try:
                audio.add_tags()
            except Exception:
                pass

        if audio.tags is None:
            raise RuntimeError(f"Tag writing not supported for this format: {type(audio).__name__}")

        if title:
            audio.tags["title"] = [title]
        if artist:
            audio.tags["artist"] = [artist]
        if album:
            audio.tags["album"] = [album]
        retry_file_operation(
            f"saving generic title/artist tags for {file_path}",
            audio.save,
        )

    except Exception as e:
        raise RuntimeError(f"Tag write failed: {e}")


def write_lyrics(file_path: str, lyrics: str, replace_existing=False):
    lyrics = clean_lyrics_for_tag(lyrics)
    if not lyrics:
        return

    audio = get_audio_object(file_path)
    if not audio:
        raise RuntimeError("Unsupported or unreadable audio format")

    try:
        if isinstance(audio, MP4):
            if audio.tags is None:
                audio.add_tags()

            existing = first_mp4_lyrics(audio.tags)
            if not replace_existing and existing:
                return

            if replace_existing:
                for key in MP4_LYRICS_KEYS:
                    try:
                        del audio.tags[key]
                    except KeyError:
                        pass

            audio["\xa9lyr"] = [lyrics]
            retry_file_operation(
                f"saving MP4 lyrics for {file_path}",
                audio.save,
            )
            return

        if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            if not replace_existing:
                for key in ("lyrics", "unsyncedlyrics", "lyric"):
                    existing = audio.tags.get(key)
                    if isinstance(existing, list):
                        if any(str(x).strip() for x in existing):
                            return
                    elif existing and str(existing).strip():
                        return

            audio["lyrics"] = [lyrics]
            retry_file_operation(
                f"saving Vorbis/FLAC lyrics for {file_path}",
                audio.save,
            )
            return

        if isinstance(audio, (MP3, WAVE, AIFF)):
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                tags = ID3()

            if replace_existing:
                tags.delall("USLT")
            else:
                for frame in tags.getall("USLT"):
                    text = frame.text
                    if isinstance(text, list):
                        text = " ".join(str(x) for x in text)
                    if str(text).strip():
                        return

            tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
            retry_file_operation(
                f"saving ID3 lyrics for {file_path}",
                lambda: tags.save(file_path, v2_version=3),
            )
            return

        raise RuntimeError(f"Lyrics writing not supported for this format: {type(audio).__name__}")

    except Exception as e:
        raise RuntimeError(f"Lyrics write failed: {e}")


def clear_lyrics(file_path: str):
    audio = get_audio_object(file_path)
    if not audio:
        return

    try:
        if isinstance(audio, MP4):
            if audio.tags is None:
                return

            changed = False
            for key in MP4_LYRICS_KEYS:
                if key in audio.tags:
                    del audio.tags[key]
                    changed = True

            if changed:
                retry_file_operation(
                    f"saving MP4 lyrics cleanup for {file_path}",
                    audio.save,
                )
            return

        if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            if audio.tags is None:
                return

            changed = False
            for key in ("lyrics", "unsyncedlyrics", "lyric"):
                if key in audio.tags:
                    del audio.tags[key]
                    changed = True

            if changed:
                retry_file_operation(
                    f"saving Vorbis/FLAC lyrics cleanup for {file_path}",
                    audio.save,
                )
            return

        if isinstance(audio, (MP3, WAVE, AIFF)):
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                return

            if tags.getall("USLT"):
                tags.delall("USLT")
                retry_file_operation(
                    f"saving ID3 lyrics cleanup for {file_path}",
                    lambda: tags.save(file_path, v2_version=3),
                )
    except Exception as e:
        raise RuntimeError(f"Lyrics clear failed: {e}")


def has_usable_title_and_artist(title, artist):
    title = norm_text(title)
    artist = norm_text(artist)
    return bool(title) and not artist_is_invalid(artist)


def current_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_lyrics_not_found_history():
    if not os.path.exists(LYRICS_NOT_FOUND_HISTORY_PATH):
        return {"version": LYRICS_NOT_FOUND_HISTORY_VERSION, "items": {}}

    try:
        with open(LYRICS_NOT_FOUND_HISTORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Could not read lyrics not-found history; starting fresh: {e}")
        return {"version": LYRICS_NOT_FOUND_HISTORY_VERSION, "items": {}}

    if not isinstance(data, dict):
        return {"version": LYRICS_NOT_FOUND_HISTORY_VERSION, "items": {}}

    items = data.get("items")
    if not isinstance(items, dict):
        items = {}

    return {
        "version": LYRICS_NOT_FOUND_HISTORY_VERSION,
        "items": items,
        "updated_at": data.get("updated_at"),
    }


def save_lyrics_not_found_history(history):
    history["version"] = LYRICS_NOT_FOUND_HISTORY_VERSION
    history["updated_at"] = current_timestamp()
    temp_path = LYRICS_NOT_FOUND_HISTORY_PATH + ".tmp"

    def write_history():
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(temp_path, LYRICS_NOT_FOUND_HISTORY_PATH)

    retry_file_operation("saving lyrics not-found history", write_history)


def lyrics_not_found_cache_key(file_path):
    return os.path.normcase(os.path.abspath(file_path))


def build_lyrics_not_found_signature(file_path, original_file_name, title, artist, album, video_id):
    try:
        file_size = os.path.getsize(file_path)
    except Exception:
        file_size = None

    return {
        "file_path": lyrics_not_found_cache_key(file_path),
        "file_name": os.path.basename(file_path),
        "original_file_name": original_file_name,
        "title": norm_text(title).casefold(),
        "artist": norm_text(artist).casefold(),
        "album": norm_text(album).casefold(),
        "video_id": video_id or "",
        "file_size": file_size,
        "resolver_version": LYRICS_NOT_FOUND_RESOLVER_VERSION,
    }


def lyrics_not_found_signature_matches(record, signature):
    if not isinstance(record, dict):
        return False

    # File size and album can change when tags or thumbnails are written later.
    # They are kept in history for debugging, but not used to invalidate the
    # no-lyrics cache.
    for field in ("title", "artist", "video_id", "resolver_version"):
        if record.get(field) != signature.get(field):
            return False

    return True


def lyrics_not_found_cache_hit(history, file_path, signature):
    if ARGS.retry_missing_lyrics:
        return False

    record = history.get("items", {}).get(lyrics_not_found_cache_key(file_path))
    return lyrics_not_found_signature_matches(record, signature)


def remember_lyrics_not_found(history, signature):
    key = signature["file_path"]
    existing = history.setdefault("items", {}).get(key, {})
    attempt_count = int(existing.get("attempt_count") or 0) + 1
    record = dict(signature)
    record["attempt_count"] = attempt_count
    record["last_attempted_at"] = current_timestamp()
    history["items"][key] = record
    save_lyrics_not_found_history(history)


def forget_lyrics_not_found(history, file_path):
    key = lyrics_not_found_cache_key(file_path)
    if key in history.get("items", {}):
        history["items"].pop(key, None)
        save_lyrics_not_found_history(history)


all_metadata = []
lyrics_not_found_history = load_lyrics_not_found_history()

for idx, (original_file_name, file_path) in enumerate(file_entries, start=1):
    existing_metadata = get_existing_audio_metadata(file_path)
    existing_title = existing_metadata["title"]
    existing_artist = existing_metadata["artist"]
    existing_album = existing_metadata["album"]
    existing_lyrics_before = existing_metadata.get("lyrics") or ""
    already_has_lyrics = (
        existing_metadata["has_lyrics"]
        and not ARGS.recheck_existing_lyrics
    )
    skipped_tag_update_existing = has_usable_title_and_artist(existing_title, existing_artist)
    video_id = (
        extract_youtube_id_from_audio(file_path)
        or extract_youtube_id_from_name(original_file_name)
    )
    tag_source = None
    did_expensive_lookup = False

    cleaned_name_for_ai = clean_input_filename(original_file_name)

    print(f"[{idx}/{len(file_entries)}] Reading: {original_file_name}")

    if skipped_tag_update_existing:
        title = existing_title
        artist = existing_artist
        tag_source = "existing"
        print(f"  Skipped tag update: existing title='{title}' | artist='{artist}'")
    else:
        did_expensive_lookup = True
        youtube_music_metadata = get_youtube_music_metadata(video_id)

        if youtube_music_metadata:
            title = youtube_music_metadata["title"]
            artist = youtube_music_metadata["artist"]
            if not existing_album and youtube_music_metadata.get("album"):
                existing_album = youtube_music_metadata["album"]
            tag_source = youtube_music_metadata["source"]
            print(
                "  Used YouTube music metadata "
                f"({tag_source}) -> title='{title}' | artist='{artist}'"
            )

            try:
                write_tags(file_path, title=title, artist=artist, album=existing_album)
                print(f"  Updated tags -> title='{title}' | artist='{artist}'")
            except Exception as e:
                print(f"  Error updating tags: {e}")
        else:
            messages = [
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": build_single_prompt(idx, cleaned_name_for_ai)},
            ]

            try:
                one = call_and_parse(messages, max_tokens=220, json_mode=True)
                title = norm_text(str(one.get("title") or ""))
                artist = norm_text(str(one.get("artist") or "Unknown")) or "Unknown"
                tag_source = "groq"

                if looks_bad(title):
                    print(f"  Suspicious title detected, using filename fallback: {title}")
                    title = conservative_filename_title(original_file_name)
                    tag_source = "filename_fallback"

                if artist_is_invalid(artist):
                    print(f"  Suspicious artist detected, using Unknown: {artist}")
                    artist = "Unknown"

            except Exception as e:
                print(f"  AI parse failed: {e}")
                title = conservative_filename_title(original_file_name)
                artist = existing_artist or "Unknown"
                tag_source = "filename_fallback"

            if not title:
                title = conservative_filename_title(original_file_name)
                tag_source = tag_source or "filename_fallback"

            try:
                write_tags(file_path, title=title, artist=artist, album=existing_album)
                print(f"  Updated tags -> title='{title}' | artist='{artist}'")
            except Exception as e:
                print(f"  Error updating tags: {e}")

    lyrics_found = already_has_lyrics
    lyrics_source = "existing" if already_has_lyrics else None
    subtitle_track = None
    lyric_source_similarity = None
    existing_lyrics_cleaned_for_noise = False
    lyrics_lookup_skipped_not_found_cache = False

    if already_has_lyrics:
        forget_lyrics_not_found(lyrics_not_found_history, file_path)
        print("  Skipped lyrics: already present")
    else:
        lyrics_not_found_signature = build_lyrics_not_found_signature(
            file_path=file_path,
            original_file_name=original_file_name,
            title=title,
            artist=artist,
            album=existing_album,
            video_id=video_id,
        )

        if lyrics_not_found_cache_hit(lyrics_not_found_history, file_path, lyrics_not_found_signature):
            lyrics_lookup_skipped_not_found_cache = True
            lyrics_source = "lyrics_not_found_cache"
            print(
                "  Skipped lyrics lookup: previously attempted and not found "
                "for this same title/artist/video id."
            )
        else:
            did_expensive_lookup = True

            try:
                lyrics = None
                selected_lyrics_source = None
                library_lyrics = None
                library_source = "lrclib"
                subtitle_lyrics = None
                subtitle_candidate = None

                genius_lyrics = None
                if has_usable_title_and_artist(title, artist):
                    genius_lyrics = get_lyrics_from_genius(
                        title=title,
                        artist=artist,
                        require_artist=True,
                    )

                if not genius_lyrics:
                    genius_lyrics = get_lyrics_from_genius(
                        title=title,
                        artist="",
                        require_artist=False,
                    )

                if genius_lyrics:
                    lyrics = genius_lyrics
                    selected_lyrics_source = "genius"
                    print("  Genius lyrics found; skipping LRCLIB and YouTube subtitles.")
                else:
                    duration = read_duration_seconds(file_path)
                    library_lyrics = get_lyrics_from_lrclib(
                        title=title,
                        artist=artist,
                        duration=duration,
                        album=existing_album,
                    )

                    if video_id:
                        subtitle_lyrics, subtitle_candidate = get_lyrics_from_youtube_subtitles(
                            video_id=video_id,
                            video_title=cleaned_name_for_ai,
                            title=title,
                            artist=artist,
                            library_lyrics=library_lyrics,
                            explicit_language=ARGS.preferred_language,
                        )
                    else:
                        print("  No YouTube video id found in filename; cannot look up subtitle lyrics.")

                    lyrics, selected_lyrics_source, lyric_source_similarity = choose_best_lyrics(
                        library_lyrics=library_lyrics,
                        subtitle_lyrics=subtitle_lyrics,
                        subtitle_candidate=subtitle_candidate,
                        library_source=library_source,
                        title=title,
                        artist=artist,
                    )

                if lyrics:
                    cleaned_lyrics = clean_lyrics_for_tag(lyrics)
                    if cleaned_lyrics:
                        write_lyrics(file_path, cleaned_lyrics, replace_existing=True)
                        lyrics_found = True
                        lyrics_source = selected_lyrics_source
                        subtitle_track = subtitle_candidate.get("name") if subtitle_candidate else None
                        print(f"  Wrote lyrics from {lyrics_source} ({len(cleaned_lyrics)} chars)")
                    else:
                        print("  No lyrics found after square-bracket cleanup")
                else:
                    print("  No lyrics found")

                if lyrics_found:
                    forget_lyrics_not_found(lyrics_not_found_history, file_path)
                else:
                    remember_lyrics_not_found(lyrics_not_found_history, lyrics_not_found_signature)
            except Exception as e:
                print(f"  Lyrics error: {e}")

    all_metadata.append({
        "file_name": original_file_name,
        "title": title,
        "artist": artist,
        "tag_update_skipped_existing": skipped_tag_update_existing,
        "tag_source": tag_source,
        "lyrics_found": lyrics_found,
        "lyrics_had_existing_before": bool(existing_lyrics_before.strip()),
        "lyrics_skipped_existing": (
            bool(existing_lyrics_before.strip())
            and not ARGS.recheck_existing_lyrics
        ),
        "lyrics_lookup_skipped_not_found_cache": lyrics_lookup_skipped_not_found_cache,
        "lyrics_existing_cleaned": existing_lyrics_cleaned_for_noise,
        "lyrics_source": lyrics_source,
        "subtitle_track": subtitle_track,
        "lrclib_subtitle_similarity": lyric_source_similarity,
    })

    if did_expensive_lookup:
        time.sleep(0.4)

print("\nDone.")
print(json.dumps(all_metadata, ensure_ascii=False, indent=2))

if ARGS.no_move:
    print("\nNO MOVE requested; processed files in place.")
else:
    try:
        from trinity import FINISHED_DIR, move_working_items_to_finished

        moved_paths = move_working_items_to_finished()
        print(f"\nMOVED {len(moved_paths)} WORKING ITEM(S) TO: {FINISHED_DIR}")
    except Exception as e:
        raise SystemExit(f"Could not move working files to finished: {e}")
