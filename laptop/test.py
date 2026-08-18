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
from html.parser import HTMLParser
from xml.etree import ElementTree
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL
from yt_dlp.networking import Request as YtDlpRequest

from groq_dynamic import groq_chat_try_all_models, load_groq_keys
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
    LANGUAGE_BY_CODE,
    detect_text_language,
    infer_expected_language,
    normalize_language_code,
    validate_subtitle_language,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AUDIO_FOLDER = os.path.join(BASE_DIR, "_working_downloads")
LYRICS_NOT_FOUND_HISTORY_PATH = os.path.join(BASE_DIR, "_lyrics_not_found_history.json")
LYRICS_NOT_FOUND_HISTORY_VERSION = 2
LYRICS_NOT_FOUND_RESOLVER_VERSION = 10
MIN_LYRIC_SOURCE_AGREEMENT = 0.80


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

GROQ_API_KEYS = load_groq_keys()

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

ID3_LANGUAGE_BY_CODE = {
    "en": "eng",
    "vi": "vie",
    "ko": "kor",
    "ja": "jpn",
    "zh": "zho",
    "th": "tha",
    "id": "ind",
    "ms": "msa",
    "tl": "tgl",
    "fr": "fra",
    "es": "spa",
    "pt": "por",
    "de": "deu",
    "it": "ita",
    "ru": "rus",
    "uk": "ukr",
    "ar": "ara",
    "hi": "hin",
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


class GeniusLyricsHTMLParser(HTMLParser):
    BLOCK_TAGS = {"div", "p", "section", "article", "li", "h1", "h2", "h3"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.container_tag = ""
        self.container_depth = 0
        self.current_parts = []
        self.lyrics_containers = []

    def append_break(self):
        if self.current_parts and self.current_parts[-1] != "\n":
            self.current_parts.append("\n")

    def handle_starttag(self, tag, attrs):
        tag = tag.casefold()
        attrs = {str(key).casefold(): value for key, value in attrs}

        if not self.container_depth:
            if str(attrs.get("data-lyrics-container", "")).casefold() == "true":
                self.container_tag = tag
                self.container_depth = 1
                self.current_parts = []
            return

        if tag == self.container_tag:
            self.container_depth += 1
        if tag == "br" or tag in self.BLOCK_TAGS:
            self.append_break()

    def handle_startendtag(self, tag, attrs):
        if self.container_depth and tag.casefold() == "br":
            self.append_break()

    def handle_endtag(self, tag):
        if not self.container_depth:
            return

        tag = tag.casefold()
        if tag in self.BLOCK_TAGS:
            self.append_break()

        if tag == self.container_tag:
            self.container_depth -= 1
            if not self.container_depth:
                text = "".join(self.current_parts).strip()
                if text:
                    self.lyrics_containers.append(text)
                self.container_tag = ""
                self.current_parts = []

    def handle_data(self, data):
        if self.container_depth:
            self.current_parts.append(data)


def clean_genius_lyrics_for_tag(lyrics: str, title: str = "") -> str:
    cleaned = clean_lyrics_for_tag(lyrics)
    if not cleaned:
        return ""

    lines = cleaned.splitlines()
    title_lyrics = norm_text(f"{title} Lyrics").casefold()

    while lines:
        first = norm_text(lines[0])
        folded = first.casefold()
        contributor_prefix = re.match(
            r"^\d+\s+contributors?\s*",
            first,
            flags=re.IGNORECASE,
        )
        if contributor_prefix:
            remainder = first[contributor_prefix.end():].strip()
            if remainder:
                lines[0] = remainder
            else:
                lines.pop(0)
            continue
        if folded in {"lyrics", title_lyrics}:
            lines.pop(0)
            continue
        break

    # Translation and romanization pages can prepend navigation links followed
    # by a longer page-title header, for example "DARA - Kiss ... Lyrics".
    # Remove that entire preamble, but only when the header contains every
    # token from the expected song title.
    title_tokens = set(
        re.findall(r"[^\W_]+", title.casefold(), flags=re.UNICODE)
    )
    if title_tokens:
        for index, line in enumerate(lines[:12]):
            folded = norm_text(line).casefold()
            line_tokens = set(
                re.findall(r"[^\W_]+", folded, flags=re.UNICODE)
            )
            if folded.endswith(" lyrics") and title_tokens <= line_tokens:
                lines = lines[index + 1:]
                break

    while lines and re.fullmatch(r"\d*\s*embed", lines[-1], flags=re.IGNORECASE):
        lines.pop()

    return clean_lyrics_for_tag("\n".join(lines))


def extract_genius_lyrics_from_html(html_text: str, title: str = "") -> str:
    parser = GeniusLyricsHTMLParser()
    parser.feed(html_text or "")
    parser.close()
    return clean_genius_lyrics_for_tag(
        "\n\n".join(parser.lyrics_containers),
        title=title,
    )


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
    return groq_chat_try_all_models(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        max_retry_rounds=max_retries,
        json_mode=json_mode,
    )


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


def lyrics_body_looks_legit(text, source="lyrics", title=""):
    cleaned = clean_lyrics_for_tag(text)
    if not lyrics_text_looks_usable(cleaned):
        return False

    nonempty_lines = [
        line.strip()
        for line in cleaned.splitlines()
        if line.strip()
    ]
    compact = re.sub(r"\s+", " ", cleaned).strip(" \"'").casefold()

    placeholder_patterns = (
        r"(lyrics\s*)?(\d+\s+)?contributors?.{0,160}\blyrics\b",
        r"\d+\s+contributor[s]?\s+.+\s+lyrics",
        r".+\s+lyrics\s+you might also like",
        r"you might also like\s+.+",
    )
    if any(re.fullmatch(pattern, compact) for pattern in placeholder_patterns):
        return False

    if (
        "contributor" in compact
        and compact.endswith("lyrics")
        and len(nonempty_lines) <= 4
    ):
        return False
    if "you might also like" in compact and len(nonempty_lines) <= 8:
        return False
    if compact in {"lyrics", "instrumental", "no lyrics"}:
        return False

    word_tokens = re.findall(r"[^\W\d_]+", compact, flags=re.UNICODE)
    unique_words = set(word_tokens)
    if len(word_tokens) < 25 or len(unique_words) < 10:
        return False

    return True


def genius_lyrics_text_looks_usable(text, title=""):
    return lyrics_body_looks_legit(text, source="Genius", title=title)


def clean_legit_lyrics_candidate(text, source="lyrics", title=""):
    cleaned = clean_lyrics_for_tag(text or "")
    if not cleaned:
        return ""

    if lyrics_body_looks_legit(cleaned, source=source, title=title):
        return cleaned

    preview = re.sub(r"\s+", " ", cleaned).strip()
    if len(preview) > 120:
        preview = preview[:117] + "..."
    print(
        f"  {source} lyrics rejected because the text did not look "
        f"like a complete lyrics body: {preview!r}"
    )
    return ""


def id3_lyrics_language(language_code=""):
    normalized = normalize_language_code(language_code)
    return ID3_LANGUAGE_BY_CODE.get(normalized, "eng")


def detect_lyrics_language_code(lyrics):
    detected = detect_text_language(lyrics or "")
    if detected.get("approved"):
        return normalize_language_code(detected.get("code"))
    return ""


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
        "has_lyrics": lyrics_body_looks_legit(lyrics, source="existing"),
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
    value = value.replace("_", " ")
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
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


GENIUS_MANAGED_LYRICS_ACCOUNT_MARKERS = (
    "translation",
    "translations",
    "romanization",
    "romanizations",
    "traduccion",
    "traducciones",
    "traducao",
    "traducoes",
    "traduction",
    "traductions",
    "traduzione",
    "traduzioni",
    "ubersetzung",
    "ubersetzungen",
)


def genius_managed_variant_kind(result_artist, result):
    """Identify Genius-managed translation/romanization result pages."""
    folded_artist = fold_match_text(result_artist)
    if folded_artist != "genius" and not folded_artist.startswith("genius "):
        return ""

    title_text = fold_match_text(
        result.get("title_with_featured")
        or result.get("title")
        or result.get("full_title")
        or ""
    )
    if "romaniz" in folded_artist or "romaniz" in title_text:
        return "romanization"
    if any(
        marker in f"{folded_artist} {title_text}"
        for marker in GENIUS_MANAGED_LYRICS_ACCOUNT_MARKERS
    ):
        return "translation"

    # Some official Genius language communities use localized account names
    # without an English "translation" marker. The caller still requires both
    # the expected original artist and full song title inside the result title
    # before this generic managed-account result can be accepted.
    return "managed"


def genius_artist_score_from_title(expected_artist, result):
    title_values = (
        result.get("title"),
        result.get("title_with_featured"),
        result.get("full_title"),
        result.get("name"),
    )
    return max(
        (artist_token_overlap(expected_artist, value or "") for value in title_values),
        default=0.0,
    )


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
        best_lyrics_identity = normalize_lyrics_for_similarity(best_lyrics)
        distinct_ranked = [
            row
            for row in ranked[1:]
            if normalize_lyrics_for_similarity(row[3]) != best_lyrics_identity
        ]
        second_total = distinct_ranked[0][0] if distinct_ranked else 0.0

        if best_total < 0.78:
            print(f"  LRCLIB search best score too low: {best_total:.3f}")
            return None

        if distinct_ranked and best_total - second_total < 0.06:
            print("  LRCLIB search is ambiguous; top candidates are too close.")
            return None

        duplicate_count = len(ranked) - len(distinct_ranked) - 1
        if duplicate_count:
            print(
                "  LRCLIB ignored "
                f"{duplicate_count} duplicate result(s) with identical lyrics."
            )

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


def get_lyrics_from_lrclib(
    title,
    artist,
    duration=None,
    album="",
    title_only_query=False,
):
    if not title:
        return None

    title = norm_text(title)
    artist = norm_text(artist)
    album = norm_text(album)

    if artist and artist != "Unknown" and not title_only_query:
        lyrics = _lrclib_get(title=title, artist=artist, duration=duration, album=album)
        if lyrics:
            return lyrics

    query = (
        f"{artist} {title}".strip()
        if artist and artist != "Unknown" and not title_only_query
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


def get_lyrics_from_genius(
    title: str,
    artist: str,
    require_artist=True,
    title_only_query=False,
):
    if not title or not GENIUS_API_TOKEN:
        return None

    title = norm_text(title)
    artist = norm_text(artist)
    require_artist = bool(
        require_artist
        and artist
        and artist.casefold() != "unknown"
    )
    query = (
        f"{artist} {title}".strip()
        if require_artist and not title_only_query
        else title
    )
    if require_artist and title_only_query:
        search_label = "title-only query with artist validation"
    elif require_artist:
        search_label = "title+artist"
    else:
        search_label = "title-only"
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
            managed_variant = (
                genius_managed_variant_kind(result_artist, result)
                if require_artist
                else ""
            )

            # Genius translation/romanization pages are credited to a managed
            # Genius account. Accept that proxy credit only when the page title
            # itself contains both the expected song title and original artist.
            if managed_variant:
                embedded_artist_score = genius_artist_score_from_title(artist, result)
                if title_overlap >= 0.80 and embedded_artist_score >= 0.80:
                    title_score = max(title_score, title_overlap)
                    artist_score = embedded_artist_score
                else:
                    managed_variant = ""

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
            ranked_hits.append((total, result, managed_variant))

        variant_priority = {
            "": 3,
            "romanization": 2,
            "translation": 1,
            "managed": 1,
        }
        ranked_hits.sort(
            key=lambda row: (row[0], variant_priority.get(row[2], 0)),
            reverse=True,
        )

        if not ranked_hits:
            print(f"  Genius returned no identity-safe {search_label} hit.")
            return None

        best_total, best_hit, best_variant = ranked_hits[0]

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
            second_variant = ranked_hits[1][2]
            direct_artist_over_proxy = not best_variant and bool(second_variant)
            managed_variant_pair = (
                best_variant == "romanization"
                and second_variant in {"translation", "managed"}
            )
            if direct_artist_over_proxy:
                print(
                    "  Genius returned directly credited and managed-language "
                    "pages for the same identity; preferring the directly "
                    "credited artist page."
                )
            elif managed_variant_pair:
                print(
                    "  Genius returned matching managed romanization and "
                    "translation pages; preferring romanization for exact-text "
                    "corroboration."
                )
            else:
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

        lyrics = extract_genius_lyrics_from_html(page_resp.text, title=title)
        if not lyrics:
            return None
        if not genius_lyrics_text_looks_usable(lyrics, title=title):
            print(
                "  Genius lyrics rejected because the extracted page text "
                "looked like metadata/header text, not a lyrics body."
            )
            return None

        return lyrics
    except Exception:
        return None


def find_genius_lyrics_with_no_result_retry(title, artist):
    has_title_and_artist = has_usable_title_and_artist(title, artist)

    if has_title_and_artist:
        lyrics = get_lyrics_from_genius(
            title=title,
            artist=artist,
            require_artist=True,
        )
        if lyrics:
            return lyrics, True, False

        print(
            "  Genius title+artist search returned no accepted result; "
            "retrying with a title-only query and the same artist validation."
        )
        lyrics = get_lyrics_from_genius(
            title=title,
            artist=artist,
            require_artist=True,
            title_only_query=True,
        )
        return lyrics, False, True

    lyrics = get_lyrics_from_genius(
        title=title,
        artist="",
        require_artist=False,
        title_only_query=True,
    )
    return lyrics, False, True


def find_lrclib_lyrics_with_no_result_retry(
    title,
    artist,
    duration=None,
    album="",
):
    has_title_and_artist = has_usable_title_and_artist(title, artist)
    lyrics = get_lyrics_from_lrclib(
        title=title,
        artist=artist,
        duration=duration,
        album=album,
    )

    if lyrics or not has_title_and_artist:
        return lyrics, bool(lyrics and has_title_and_artist), not has_title_and_artist

    print(
        "  LRCLIB title+artist search returned no accepted result; "
        "retrying with a title-only query and the same artist validation."
    )
    lyrics = get_lyrics_from_lrclib(
        title=title,
        artist=artist,
        duration=duration,
        album=album,
        title_only_query=True,
    )
    return lyrics, False, True


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


def compact_subtitle_sample(text, max_chars=1800):
    lines = [
        line.strip()
        for line in (text or "").splitlines()
        if line.strip()
    ]
    if not lines:
        return ""

    if len("\n".join(lines)) <= max_chars:
        return "\n".join(lines)

    head = lines[:18]
    middle_start = max(0, len(lines) // 2 - 6)
    middle = lines[middle_start:middle_start + 12]
    tail = lines[-18:]
    sample = "\n".join(head + ["..."] + middle + ["..."] + tail)
    return sample[:max_chars]


def groq_confirm_subtitle_language(
    candidate,
    subtitle_text,
    expected_language="",
    video_title="",
    title="",
    artist="",
):
    if not GROQ_API_KEYS:
        return None

    payload = {
        "video_title": video_title,
        "song_title": title,
        "artist": artist,
        "expected_language": normalize_language_code(expected_language),
        "track_language": candidate.get("normalized_language_code") or "",
        "track_name": candidate.get("name") or "",
        "track_type": "automatic" if candidate.get("automatic") else "manual",
        "subtitle_sample": compact_subtitle_sample(subtitle_text),
    }
    messages = [
        {
            "role": "system",
            "content": (
                "Return JSON only. Decide whether the subtitle text is the "
                "right lyrics language for this YouTube music video. Use the "
                "expected_language when present. Return exactly: "
                '{"approved": true/false, "language": "iso-639-1", "reason": "short reason"}.'
            ),
        },
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False),
        },
    ]

    try:
        result = call_and_parse(
            messages,
            max_tokens=120,
            json_mode=True,
            temperature=0,
        )
    except Exception as error:
        print(f"  Groq subtitle language validation unavailable: {error}")
        return None

    approved = bool(result.get("approved"))
    detected_language = normalize_language_code(result.get("language"))
    expected = normalize_language_code(expected_language)

    if expected and approved and detected_language and detected_language != expected:
        approved = False

    return {
        "approved": approved,
        "reason": norm_text(result.get("reason") or "groq_response"),
        "detected": {
            "approved": approved,
            "code": detected_language,
            "confidence": 1.0 if approved else 0.0,
            "margin": 1.0 if approved else 0.0,
            "letters": sum(1 for char in subtitle_text if char.isalpha()),
            "reason": "groq_confirmed" if approved else "groq_rejected",
        },
        "source": "groq",
    }


def validate_subtitle_candidate_language(
    candidate,
    subtitle_text,
    expected_language="",
    video_title="",
    title="",
    artist="",
):
    local_validation = validate_subtitle_language(
        candidate=candidate,
        subtitle_text=subtitle_text,
        expected_language=expected_language,
    )

    # Every subtitle must reach Groq for the title/language decision before it
    # can be written or used as the automatic-caption comparison baseline.
    groq_validation = groq_confirm_subtitle_language(
        candidate=candidate,
        subtitle_text=subtitle_text,
        expected_language=expected_language,
        video_title=video_title,
        title=title,
        artist=artist,
    )
    if groq_validation is not None:
        return groq_validation

    local_validation["source"] = "local_lingua_full_text_groq_unavailable"
    return local_validation



def empty_youtube_subtitle_result():
    return {
        "manual_lyrics": None,
        "manual_candidate": None,
        "automatic_lyrics": None,
        "automatic_candidate": None,
    }


def validate_subtitle_candidates(
    candidates,
    expected_language="",
    video_title="",
    title="",
    artist="",
    candidate_limit=3,
):
    approved = []

    for index, candidate in enumerate(candidates[:candidate_limit], start=1):
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

        validation = validate_subtitle_candidate_language(
            candidate=candidate,
            subtitle_text=text,
            expected_language=expected_language,
            video_title=video_title,
            title=title,
            artist=artist,
        )

        detected = validation.get("detected") or {}
        print(
            f"  Subtitle language validation ({validation.get('source') or 'local'}): "
            f"approved={validation.get('approved')}, "
            f"track={candidate.get('normalized_language_code')}, "
            f"detected={detected.get('code')}, "
            f"confidence={detected.get('confidence', 0.0):.3f}, "
            f"margin={detected.get('margin', 0.0):.3f}, "
            f"reason={validation.get('reason')}"
        )

        if not validation.get("approved"):
            continue
        if validation.get("source") != "groq":
            print(
                f"  {kind.capitalize()} subtitle was not accepted because Groq did not "
                "confirm its language against the video title."
            )
            continue

        approved_candidate = dict(candidate)
        approved_candidate["language_validation"] = validation
        approved_candidate["language_match_approved"] = True
        approved_candidate["language_match_source"] = (
            validation.get("source") or "language_validation"
        )
        approved.append((text, approved_candidate))

    return approved


def choose_unambiguous_approved_subtitle(approved, kind):
    if not approved:
        return None, None
    if len(approved) == 1:
        return approved[0]

    approved_languages = {
        item[1].get("normalized_language_code")
        for item in approved
        if item[1].get("normalized_language_code")
    }
    if len(approved_languages) == 1:
        print(
            f"  Multiple {kind} tracks passed validation in the same "
            "language; using the first track reported by YouTube."
        )
        return approved[0]

    non_english = [
        item
        for item in approved
        if item[1].get("normalized_language_code") != "en"
    ]
    if len(non_english) == 1:
        print(
            f"  Multiple {kind} subtitles validated; using the single "
            f"approved non-English track: {non_english[0][1].get('name')}"
        )
        return non_english[0]

    print(
        f"  Multiple {kind} subtitle tracks passed language validation; "
        "the original language is still ambiguous."
    )
    return None, None


def get_lyrics_from_youtube_subtitles(
    video_id,
    video_title,
    title="",
    artist="",
    explicit_language="",
):
    result = empty_youtube_subtitle_result()

    if ARGS.disable_youtube_subtitles:
        print("  YouTube subtitle lookup disabled by command-line option.")
        return result

    if YOUTUBE_SUBTITLES_DISABLED_FOR_RUN:
        print("  YouTube subtitle lookup disabled after an earlier 429.")
        return result

    info = get_youtube_info(video_id)
    if not info:
        return result

    expected_language, expected_source = infer_expected_language(
        title=title,
        artist=artist,
        library_lyrics="",
        explicit_language=explicit_language,
    )

    manual_candidates = list(iter_subtitle_candidates(info, automatic=False))
    automatic_candidates = list(iter_subtitle_candidates(info, automatic=True))
    all_candidates = manual_candidates + automatic_candidates

    if not expected_language:
        video_language = normalize_language_code(info.get("language"))
        if (
            video_language in LANGUAGE_BY_CODE
            and any(
                candidate.get("normalized_language_code") == video_language
                for candidate in all_candidates
            )
        ):
            expected_language = video_language
            expected_source = "youtube_video_language"

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
        manual_pool = [
            candidate
            for candidate in manual_candidates
            if candidate.get("normalized_language_code") == expected_language
        ]
        automatic_pool = [
            candidate
            for candidate in automatic_candidates
            if candidate.get("normalized_language_code") == expected_language
        ]

        approved_manual = validate_subtitle_candidates(
            manual_pool,
            expected_language=expected_language,
            video_title=video_title,
            title=title,
            artist=artist,
            candidate_limit=len(manual_pool),
        )
        if approved_manual:
            result["manual_lyrics"], result["manual_candidate"] = approved_manual[0]

        approved_automatic = validate_subtitle_candidates(
            automatic_pool,
            expected_language=expected_language,
            video_title=video_title,
            title=title,
            artist=artist,
            candidate_limit=len(automatic_pool),
        )
        if approved_automatic:
            result["automatic_lyrics"], result["automatic_candidate"] = (
                approved_automatic[0]
            )

        if not manual_pool and not automatic_pool:
            print("  No original subtitle track matches the expected lyrics language.")
        elif not result["manual_lyrics"] and not result["automatic_lyrics"]:
            print("  No YouTube subtitle passed language validation.")
        return result

    manual_choice = (None, None)
    if len(manual_candidates) == 1:
        print(
            "  Expected language is unknown, but exactly one original "
            "manual subtitle exists; validating its actual text."
        )
        approved_manual = validate_subtitle_candidates(
            manual_candidates,
            video_title=video_title,
            title=title,
            artist=artist,
        )
        manual_choice = choose_unambiguous_approved_subtitle(
            approved_manual,
            "manual",
        )
    elif len(manual_candidates) > 1:
        print(
            "  Expected language is unknown and multiple original manual "
            "subtitle languages exist. Validating all manual tracks."
        )
        approved_manual = validate_subtitle_candidates(
            manual_candidates,
            video_title=video_title,
            title=title,
            artist=artist,
            candidate_limit=len(manual_candidates),
        )
        manual_choice = choose_unambiguous_approved_subtitle(
            approved_manual,
            "manual",
        )

    result["manual_lyrics"], result["manual_candidate"] = manual_choice

    automatic_pool = automatic_candidates
    automatic_expected_language = ""
    if result["manual_candidate"]:
        automatic_expected_language = subtitle_language_code(
            result["manual_candidate"]
        )
        automatic_pool = [
            candidate
            for candidate in automatic_candidates
            if candidate.get("normalized_language_code")
            == automatic_expected_language
        ]
        print(
            "  Searching for an automatic-caption baseline in the approved "
            f"manual subtitle language={automatic_expected_language}."
        )

    if not automatic_pool:
        if result["manual_lyrics"]:
            print("  No automatic subtitle is available in the approved manual language.")
        elif not manual_candidates:
            print("  No original manual or automatic subtitles are available.")
        return result

    approved_automatic = validate_subtitle_candidates(
        automatic_pool,
        expected_language=automatic_expected_language,
        video_title=video_title,
        title=title,
        artist=artist,
        candidate_limit=(
            3
            if automatic_expected_language
            else len(automatic_pool)
        ),
    )
    automatic_choice = (
        approved_automatic[0]
        if automatic_expected_language and approved_automatic
        else choose_unambiguous_approved_subtitle(
            approved_automatic,
            "automatic",
        )
    )
    result["automatic_lyrics"], result["automatic_candidate"] = (
        automatic_choice
    )

    if not result["manual_lyrics"] and not result["automatic_lyrics"]:
        print(
            "  Expected language is unknown and no unambiguous subtitle "
            "passed language validation."
        )

    return result


def normalize_lyrics_for_similarity(text):
    text = remove_square_bracket_content(text or "")
    text = unicodedata.normalize("NFKD", text).lower()

    folded_chars = []
    for char in text:
        if (
            unicodedata.combining(char)
            and folded_chars
            and "LATIN" in unicodedata.name(folded_chars[-1], "")
        ):
            continue
        folded_chars.append(char)

    text = unicodedata.normalize("NFC", "".join(folded_chars))
    text = text.replace("đ", "d")
    text = re.sub(r"\([^)]*(official|lyrics?|video|audio|remix|cover)[^)]*\)", " ", text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def lyric_similarity(left, right):
    left_norm = normalize_lyrics_for_similarity(left)
    right_norm = normalize_lyrics_for_similarity(right)

    if not left_norm or not right_norm:
        return 0.0

    sequence_ratio = difflib.SequenceMatcher(
        None,
        left_norm,
        right_norm,
        autojunk=False,
    ).ratio()
    left_words = set(left_norm.split())
    right_words = set(right_norm.split())

    if not left_words or not right_words:
        return sequence_ratio

    overlap_ratio = len(left_words & right_words) / max(len(left_words), len(right_words))
    return max(sequence_ratio, overlap_ratio)


def subtitle_candidate_is_approved(subtitle_candidate):
    subtitle_validation = (
        subtitle_candidate.get("language_validation")
        if subtitle_candidate
        else None
    )
    return bool(
        subtitle_validation
        and subtitle_validation.get("approved")
        and subtitle_validation.get("source") == "groq"
    )


def youtube_subtitle_source_name(subtitle_candidate):
    if subtitle_candidate and subtitle_candidate.get("automatic"):
        return "youtube_auto_subtitles_validated"
    return "youtube_manual_subtitles_validated"


def subtitle_language_code(subtitle_candidate):
    if not subtitle_candidate:
        return ""
    return normalize_language_code(
        subtitle_candidate.get("normalized_language_code")
        or subtitle_candidate.get("source_language_code")
        or subtitle_candidate.get("language_code")
    )


def choose_ranked_lyrics(
    genius_lyrics=None,
    lrclib_lyrics=None,
    manual_subtitle_lyrics=None,
    manual_subtitle_candidate=None,
    automatic_subtitle_lyrics=None,
    automatic_subtitle_candidate=None,
    title="",
    artist="",
    finalize_fallback=True,
):
    genius_lyrics = clean_legit_lyrics_candidate(
        genius_lyrics,
        source="Genius",
        title=title,
    )
    lrclib_lyrics = clean_legit_lyrics_candidate(
        lrclib_lyrics,
        source="LRCLIB",
        title=title,
    )
    manual_subtitle_lyrics = clean_legit_lyrics_candidate(
        manual_subtitle_lyrics,
        source="YouTube manual subtitle",
        title=title,
    )
    automatic_subtitle_lyrics = clean_legit_lyrics_candidate(
        automatic_subtitle_lyrics,
        source="YouTube automatic subtitle",
        title=title,
    )

    manual_is_approved = subtitle_candidate_is_approved(
        manual_subtitle_candidate
    )
    automatic_is_approved = subtitle_candidate_is_approved(
        automatic_subtitle_candidate
    )
    if manual_subtitle_lyrics and not manual_is_approved:
        print(
            "  Manual subtitle text exists but failed language validation; "
            "not writing it."
        )
        manual_subtitle_lyrics = ""
    if automatic_subtitle_lyrics and not automatic_is_approved:
        print(
            "  Automatic subtitle text exists but failed language validation; "
            "it cannot be used as lyrics or as the comparison baseline."
        )
        automatic_subtitle_lyrics = ""

    manual_auto_similarity = None
    genius_auto_similarity = None
    lrclib_auto_similarity = None

    if manual_subtitle_lyrics and automatic_subtitle_lyrics:
        manual_auto_similarity = lyric_similarity(
            manual_subtitle_lyrics,
            automatic_subtitle_lyrics,
        )
        print(
            "  YouTube manual/automatic subtitle similarity: "
            f"{manual_auto_similarity:.2%}"
        )
    if genius_lyrics and automatic_subtitle_lyrics:
        genius_auto_similarity = lyric_similarity(
            genius_lyrics,
            automatic_subtitle_lyrics,
        )
        print(
            "  Genius/YouTube automatic subtitle similarity: "
            f"{genius_auto_similarity:.2%}"
        )
    if lrclib_lyrics and automatic_subtitle_lyrics:
        lrclib_auto_similarity = lyric_similarity(
            lrclib_lyrics,
            automatic_subtitle_lyrics,
        )
        print(
            "  LRCLIB/YouTube automatic subtitle similarity: "
            f"{lrclib_auto_similarity:.2%}"
        )

    # A human-created subtitle whose language was validated against the video
    # title is authoritative even when automatic captions disagree with it.
    if manual_subtitle_lyrics and manual_is_approved:
        print(
            "  Using the approved human-created YouTube subtitle as the "
            "highest-priority lyrics source."
        )
        return (
            manual_subtitle_lyrics,
            youtube_subtitle_source_name(manual_subtitle_candidate),
            manual_auto_similarity,
        )

    # When YouTube has no usable Groq-approved automatic caption, there is no
    # caption baseline to score against. Library candidates have already passed
    # title-field identity checks and complete-lyrics validation, so use the
    # requested Genius-first, LRCLIB-second fallback directly.
    if not automatic_subtitle_lyrics:
        if genius_lyrics:
            print(
                "  No Groq-approved YouTube automatic subtitle baseline is "
                "available; using identity-safe Genius lyrics without the "
                "80% caption-agreement requirement."
            )
            return genius_lyrics, "genius", None
        if lrclib_lyrics:
            print(
                "  No Groq-approved YouTube automatic subtitle baseline or "
                "Genius lyrics are available; using identity-safe LRCLIB "
                "lyrics without the 80% caption-agreement requirement."
            )
            return lrclib_lyrics, "lrclib", None

    if (
        genius_lyrics
        and genius_auto_similarity is not None
        and genius_auto_similarity >= MIN_LYRIC_SOURCE_AGREEMENT
    ):
        print(
            "  Genius lyrics match YouTube automatic subtitles at "
            f"{genius_auto_similarity:.2%}; using Genius."
        )
        return genius_lyrics, "genius", genius_auto_similarity

    if (
        lrclib_lyrics
        and lrclib_auto_similarity is not None
        and lrclib_auto_similarity >= MIN_LYRIC_SOURCE_AGREEMENT
    ):
        print(
            "  LRCLIB lyrics match YouTube automatic subtitles at "
            f"{lrclib_auto_similarity:.2%}; using LRCLIB."
        )
        return lrclib_lyrics, "lrclib", lrclib_auto_similarity

    if not finalize_fallback:
        return None, None, None

    if automatic_subtitle_lyrics and automatic_is_approved:
        print(
            "  Using approved automatic YouTube subtitles because no "
            "human subtitle exists and no library source reached 80% "
            "agreement with the automatic captions."
        )
        return (
            automatic_subtitle_lyrics,
            youtube_subtitle_source_name(automatic_subtitle_candidate),
            None,
        )

    low_confidence_sources = []
    if genius_lyrics:
        low_confidence_sources.append("Genius")
    if lrclib_lyrics:
        low_confidence_sources.append("LRCLIB")

    if low_confidence_sources:
        print(
            "  Leaving lyrics blank rather than writing low-confidence "
            f"{'/'.join(low_confidence_sources)} lyrics without 80% agreement "
            "against YouTube automatic subtitles."
        )

    return None, None, None


def choose_ranked_lyrics_with_rejected_candidate_retries(
    genius_lyrics=None,
    lrclib_lyrics=None,
    manual_subtitle_lyrics=None,
    manual_subtitle_candidate=None,
    automatic_subtitle_lyrics=None,
    automatic_subtitle_candidate=None,
    title="",
    artist="",
    genius_found_with_artist=False,
    lrclib_found_with_artist=False,
    genius_title_only_attempted=False,
    lrclib_title_only_attempted=False,
    genius_title_only_lookup=None,
    lrclib_title_only_lookup=None,
):
    provisional_choice = choose_ranked_lyrics(
        genius_lyrics=genius_lyrics,
        lrclib_lyrics=lrclib_lyrics,
        manual_subtitle_lyrics=manual_subtitle_lyrics,
        manual_subtitle_candidate=manual_subtitle_candidate,
        automatic_subtitle_lyrics=automatic_subtitle_lyrics,
        automatic_subtitle_candidate=automatic_subtitle_candidate,
        title=title,
        artist=artist,
        finalize_fallback=False,
    )
    lyrics, selected_source, similarity = provisional_choice

    if selected_source == "genius":
        return lyrics, selected_source, similarity
    if selected_source and selected_source.startswith("youtube_manual_"):
        return lyrics, selected_source, similarity

    if (
        genius_found_with_artist
        and not genius_title_only_attempted
        and callable(genius_title_only_lookup)
    ):
        print(
            "  Genius title+artist candidate was rejected by final lyric "
            "evaluation; retrying with a title-only query and the same "
            "artist validation."
        )
        fallback_genius_lyrics = genius_title_only_lookup()
        if fallback_genius_lyrics:
            fallback_choice = choose_ranked_lyrics(
                genius_lyrics=fallback_genius_lyrics,
                lrclib_lyrics=lrclib_lyrics,
                manual_subtitle_lyrics=manual_subtitle_lyrics,
                manual_subtitle_candidate=manual_subtitle_candidate,
                automatic_subtitle_lyrics=automatic_subtitle_lyrics,
                automatic_subtitle_candidate=automatic_subtitle_candidate,
                title=title,
                artist=artist,
                finalize_fallback=False,
            )
            if fallback_choice[1] == "genius":
                return fallback_choice
            if selected_source != "lrclib":
                genius_lyrics = fallback_genius_lyrics
                lyrics, selected_source, similarity = fallback_choice

    if selected_source == "lrclib":
        return lyrics, selected_source, similarity

    if (
        lrclib_found_with_artist
        and not lrclib_title_only_attempted
        and callable(lrclib_title_only_lookup)
    ):
        print(
            "  LRCLIB title+artist candidate was rejected by final lyric "
            "evaluation; retrying with a title-only query and the same "
            "artist validation."
        )
        fallback_lrclib_lyrics = lrclib_title_only_lookup()
        if fallback_lrclib_lyrics:
            lrclib_lyrics = fallback_lrclib_lyrics

    return choose_ranked_lyrics(
        genius_lyrics=genius_lyrics,
        lrclib_lyrics=lrclib_lyrics,
        manual_subtitle_lyrics=manual_subtitle_lyrics,
        manual_subtitle_candidate=manual_subtitle_candidate,
        automatic_subtitle_lyrics=automatic_subtitle_lyrics,
        automatic_subtitle_candidate=automatic_subtitle_candidate,
        title=title,
        artist=artist,
        finalize_fallback=True,
    )


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


def write_lyrics(file_path: str, lyrics: str, replace_existing=False, language_code=""):
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

            tags.add(
                USLT(
                    encoding=3,
                    lang=id3_lyrics_language(language_code),
                    desc="",
                    text=lyrics,
                )
            )
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
                title = (
                    existing_title
                    if existing_title and not looks_bad(existing_title)
                    else conservative_filename_title(original_file_name)
                )
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
                lyrics_language_code = ""
                lrclib_lyrics = None
                manual_subtitle_lyrics = None
                manual_subtitle_candidate = None
                automatic_subtitle_lyrics = None
                automatic_subtitle_candidate = None
                selected_subtitle_candidate = None

                has_title_and_artist = has_usable_title_and_artist(title, artist)
                (
                    genius_lyrics,
                    genius_found_with_artist,
                    genius_title_only_attempted,
                ) = find_genius_lyrics_with_no_result_retry(title, artist)

                duration = read_duration_seconds(file_path)
                (
                    lrclib_lyrics,
                    lrclib_found_with_artist,
                    lrclib_title_only_attempted,
                ) = find_lrclib_lyrics_with_no_result_retry(
                    title=title,
                    artist=artist,
                    duration=duration,
                    album=existing_album,
                )

                if video_id:
                    youtube_subtitles = get_lyrics_from_youtube_subtitles(
                        video_id=video_id,
                        video_title=cleaned_name_for_ai,
                        title=title,
                        artist=artist,
                        explicit_language=ARGS.preferred_language,
                    )
                    manual_subtitle_lyrics = youtube_subtitles["manual_lyrics"]
                    manual_subtitle_candidate = youtube_subtitles["manual_candidate"]
                    automatic_subtitle_lyrics = youtube_subtitles["automatic_lyrics"]
                    automatic_subtitle_candidate = youtube_subtitles["automatic_candidate"]
                else:
                    print("  No YouTube video id found in filename; cannot look up subtitle lyrics.")

                lyrics, selected_lyrics_source, lyric_source_similarity = (
                    choose_ranked_lyrics_with_rejected_candidate_retries(
                        genius_lyrics=genius_lyrics,
                        lrclib_lyrics=lrclib_lyrics,
                        manual_subtitle_lyrics=manual_subtitle_lyrics,
                        manual_subtitle_candidate=manual_subtitle_candidate,
                        automatic_subtitle_lyrics=automatic_subtitle_lyrics,
                        automatic_subtitle_candidate=automatic_subtitle_candidate,
                        title=title,
                        artist=artist,
                        genius_found_with_artist=genius_found_with_artist,
                        lrclib_found_with_artist=lrclib_found_with_artist,
                        genius_title_only_attempted=genius_title_only_attempted,
                        lrclib_title_only_attempted=lrclib_title_only_attempted,
                        genius_title_only_lookup=(
                            lambda: get_lyrics_from_genius(
                                title=title,
                                artist=artist,
                                require_artist=True,
                                title_only_query=True,
                            )
                            if has_title_and_artist
                            else None
                        ),
                        lrclib_title_only_lookup=(
                            lambda: get_lyrics_from_lrclib(
                                title=title,
                                artist=artist,
                                duration=duration,
                                album=existing_album,
                                title_only_query=True,
                            )
                            if has_title_and_artist
                            else None
                        ),
                    )
                )

                if selected_lyrics_source == "youtube_manual_subtitles_validated":
                    selected_subtitle_candidate = manual_subtitle_candidate
                elif selected_lyrics_source == "youtube_auto_subtitles_validated":
                    selected_subtitle_candidate = automatic_subtitle_candidate

                if selected_subtitle_candidate:
                    lyrics_language_code = subtitle_language_code(
                        selected_subtitle_candidate
                    )
                elif lyrics:
                    lyrics_language_code = detect_lyrics_language_code(lyrics)

                if lyrics:
                    cleaned_lyrics = clean_lyrics_for_tag(lyrics)
                    if cleaned_lyrics:
                        write_lyrics(
                            file_path,
                            cleaned_lyrics,
                            replace_existing=True,
                            language_code=lyrics_language_code,
                        )
                        lyrics_found = True
                        lyrics_source = selected_lyrics_source
                        subtitle_track = (
                            selected_subtitle_candidate.get("name")
                            if selected_subtitle_candidate
                            else None
                        )
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
        "selected_source_auto_subtitle_similarity": lyric_source_similarity,
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
