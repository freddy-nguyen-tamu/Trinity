import argparse
import difflib
import html
import os
import json
import re
import time
import unicodedata
import requests
from xml.etree import ElementTree
from urllib.parse import parse_qs, urlparse

from yt_dlp import YoutubeDL

from mutagen import File
from mutagen.mp3 import MP3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.id3 import ID3, TIT2, TPE1, TALB, USLT, ID3NoHeaderError
from mutagen.wave import WAVE
from mutagen.aiff import AIFF
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AUDIO_FOLDER = os.path.join(BASE_DIR, "_working_downloads")


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
YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
SUBTITLE_INFO_CACHE = {}

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

if not GROQ_API_KEYS:
    raise SystemExit(
        "No Groq API keys found. Set GROQ_API_KEY and optionally "
        "GROQ_API_KEY1 through GROQ_API_KEY9 in the environment."
    )

print(f"Loaded {len(GROQ_API_KEYS)} Groq API key(s): " + ", ".join(name for name, _ in GROQ_API_KEYS))


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
    repaired = repaired.replace("“", '"').replace("”", '"').replace("’", "'")
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
    bad_markers = ["�", "CÑA", "TÙN", "\\u", "???"]
    return any(x in text for x in bad_markers)


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


MP4_LYRICS_KEYS = ("\xa9lyr", "\xc2\xa9lyr")


def tag_value_has_text(value) -> bool:
    if isinstance(value, list):
        return any(str(item).strip() for item in value)
    return bool(value and str(value).strip())


def first_tag_text(value):
    if isinstance(value, list):
        for item in value:
            if str(item).strip():
                return str(item).strip()
        return None
    if value and str(value).strip():
        return str(value).strip()
    return None


def first_mp4_lyrics(tags):
    if not tags:
        return None

    for key in MP4_LYRICS_KEYS:
        text = first_tag_text(tags.get(key))
        if text:
            return text

    return None


def has_lyrics(file_path: str) -> bool:
    ext = get_file_ext(file_path)

    try:
        if ext == ".mp3":
            tags = ID3(file_path)
            for frame in tags.getall("USLT"):
                text = frame.text
                if isinstance(text, list):
                    text = " ".join(str(x) for x in text)
                if str(text).strip():
                    return True
            return False

        audio = get_audio_object(file_path)
        if not audio or audio.tags is None:
            return False

        if isinstance(audio, MP4):
            return first_mp4_lyrics(audio.tags) is not None

        if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            for key in ("lyrics", "unsyncedlyrics", "lyric"):
                val = audio.tags.get(key)
                if isinstance(val, list):
                    if any(str(x).strip() for x in val):
                        return True
                elif val and str(val).strip():
                    return True
            return False

        if isinstance(audio, (WAVE, AIFF)):
            try:
                tags = ID3(file_path)
                for frame in tags.getall("USLT"):
                    text = frame.text
                    if isinstance(text, list):
                        text = " ".join(str(x) for x in text)
                    if str(text).strip():
                        return True
            except Exception:
                return False

    except Exception:
        pass

    return False


def read_existing_lyrics(file_path: str):
    ext = get_file_ext(file_path)

    try:
        if ext == ".mp3":
            tags = ID3(file_path)
            for frame in tags.getall("USLT"):
                text = frame.text
                if isinstance(text, list):
                    text = " ".join(str(x) for x in text)
                if str(text).strip():
                    return str(text).strip()
            return None

        audio = get_audio_object(file_path)
        if not audio or audio.tags is None:
            return None

        if isinstance(audio, MP4):
            return first_mp4_lyrics(audio.tags)

        if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            for key in ("lyrics", "unsyncedlyrics", "lyric"):
                val = audio.tags.get(key)
                if isinstance(val, list):
                    for item in val:
                        if str(item).strip():
                            return str(item).strip()
                elif val and str(val).strip():
                    return str(val).strip()
            return None

        if isinstance(audio, (WAVE, AIFF)):
            try:
                tags = ID3(file_path)
                for frame in tags.getall("USLT"):
                    text = frame.text
                    if isinstance(text, list):
                        text = " ".join(str(x) for x in text)
                    if str(text).strip():
                        return str(text).strip()
            except Exception:
                return None

    except Exception:
        pass

    return None


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
            title = _first_tag_value(audio.tags.get("©nam") if audio.tags else "")
            artist = _first_tag_value(audio.tags.get("©ART") if audio.tags else "")
            album = _first_tag_value(audio.tags.get("©alb") if audio.tags else "")

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


def _lrclib_get(title: str, artist: str = "", duration=None, album: str = ""):
    params = {"track_name": title}

    if artist and artist != "Unknown":
        params["artist_name"] = artist
    if duration:
        params["duration"] = duration
    if album:
        params["album_name"] = album

    try:
        resp = requests.get(LRCLIB_GET_URL, params=params, headers=lyrics_headers, timeout=20)
        if resp.status_code == 200:
            return _extract_lyrics_from_item(resp.json())
    except Exception:
        pass

    return None


def _lrclib_search(query: str, expected_title: str = "", expected_artist: str = ""):
    try:
        resp = requests.get(LRCLIB_SEARCH_URL, params={"q": query}, headers=lyrics_headers, timeout=20)
        if resp.status_code != 200:
            return None

        data = resp.json()
        if not isinstance(data, list) or not data:
            return None

        normalized_title = norm_text(expected_title).lower()
        normalized_artist = norm_text(expected_artist).lower()

        if normalized_title and normalized_artist and normalized_artist != "unknown":
            for item in data:
                item_title = norm_text(item.get("trackName", "")).lower()
                item_artist = norm_text(item.get("artistName", "")).lower()
                if item_title == normalized_title and item_artist == normalized_artist:
                    lyrics = _extract_lyrics_from_item(item)
                    if lyrics:
                        return lyrics

        if normalized_title:
            for item in data:
                item_title = norm_text(item.get("trackName", "")).lower()
                if item_title == normalized_title:
                    lyrics = _extract_lyrics_from_item(item)
                    if lyrics:
                        return lyrics

        for item in data:
            lyrics = _extract_lyrics_from_item(item)
            if lyrics:
                return lyrics

    except Exception:
        pass

    return None


def get_lyrics_from_lrclib(title: str, artist: str, duration=None, album=""):
    if not title:
        return None

    title = norm_text(title)
    artist = norm_text(artist)
    album = norm_text(album)

    if artist and artist != "Unknown":
        lyrics = _lrclib_get(title=title, artist=artist, duration=duration, album=album)
        if lyrics:
            return lyrics

    lyrics = _lrclib_get(title=title, artist="", duration=duration, album=album)
    if lyrics:
        return lyrics

    if artist and artist != "Unknown":
        combined_query = f"{artist} {title}".strip()
        lyrics = _lrclib_search(
            query=combined_query,
            expected_title=title,
            expected_artist=artist,
        )
        if lyrics:
            return lyrics

    lyrics = _lrclib_search(
        query=title,
        expected_title=title,
        expected_artist="",
    )
    if lyrics:
        return lyrics

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
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr", "web_safari", "web", "tv"],
            }
        },
        "retries": 5,
        "extractor_retries": 5,
        "socket_timeout": 20,
    }

    if use_cookies:
        opts["cookiesfrombrowser"] = ("firefox",)

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
        if not line or " · " not in line:
            continue

        lowered = line.lower()
        if lowered.startswith(("provided to youtube by", "released on:", "auto-generated by youtube")):
            continue
        if lowered.startswith(("composer", "lyricist", "vocalist", "producer", "writer")):
            continue
        if line.startswith(("℗", "©")):
            continue

        title_part, artist_part = line.split(" · ", 1)
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


def iter_subtitle_candidates(info, automatic=False):
    key = "automatic_captions" if automatic else "subtitles"
    tracks_by_language = info.get(key) or {}

    for language_code, formats in tracks_by_language.items():
        chosen = choose_subtitle_format(formats)
        if not chosen:
            continue
        source_language, translated_to = subtitle_url_hints(chosen.get("url"))

        yield {
            "language_code": language_code,
            "name": subtitle_track_display_name(language_code, chosen),
            "url": chosen.get("url"),
            "ext": str(chosen.get("ext", "")).lower(),
            "automatic": automatic,
            "source_language_code": source_language,
            "translated_to_language_code": translated_to,
            "is_translated": bool(translated_to),
        }


def clean_subtitle_lines(lines):
    cleaned = []
    previous = None

    for line in lines:
        line = html.unescape(line or "")
        line = re.sub(r"<[^>]+>", " ", line)
        line = re.sub(r"\{\\.*?\}", " ", line)
        line = remove_square_bracket_content(line)
        line = re.sub(r"♪", " ", line)
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
    try:
        response = requests.get(candidate["url"], timeout=30)
        response.raise_for_status()
    except Exception as e:
        print(f"  Could not download subtitle {candidate.get('name')}: {e}")
        return None

    parsed = parse_subtitle_text(response.text, candidate.get("ext"))
    if not parsed:
        print(f"  Subtitle {candidate.get('name')} was empty after parsing.")
        return None

    return parsed


def groq_same_language(video_title, subtitle_text, candidate):
    sample = subtitle_text[:2500]
    prompt = {
        "video_title": video_title,
        "subtitle_language_code": candidate.get("language_code"),
        "subtitle_track_name": candidate.get("name"),
        "subtitle_sample": sample,
    }

    messages = [
        {
            "role": "system",
            "content": (
                "You detect language match. Return JSON only. "
                "same_language must be true only when the YouTube video title and "
                "subtitle sample are primarily the same human language."
            ),
        },
        {
            "role": "user",
            "content": (
                "Are the video title and subtitle sample primarily the same language?\n\n"
                f"{json.dumps(prompt, ensure_ascii=False)}\n\n"
                "Return exactly this JSON object:\n"
                '{ "same_language": true, "title_language": "language", '
                '"subtitle_language": "language", "confidence": 0.0, "reason": "short reason" }'
            ),
        },
    ]

    try:
        result = call_and_parse(messages, max_tokens=180, json_mode=True)
    except Exception as e:
        print(f"  Groq subtitle language check failed for {candidate.get('name')}: {e}")
        return False

    same_language = bool(result.get("same_language"))
    title_language = result.get("title_language") or "unknown"
    subtitle_language = result.get("subtitle_language") or "unknown"
    confidence = result.get("confidence")
    print(
        "  Subtitle language check: "
        f"title={title_language}, subtitle={subtitle_language}, "
        f"same={same_language}, confidence={confidence}"
    )
    return same_language


def subtitle_candidate_kind(candidate):
    if not candidate.get("automatic"):
        return "manual_human"
    if candidate.get("is_translated"):
        return "automatic_translated"
    return "automatic_original"


def default_subtitle_candidate_order(manual_candidates, automatic_candidates):
    automatic_order = sorted(
        automatic_candidates,
        key=lambda item: (
            bool(item.get("is_translated")),
            item.get("language_code") or "",
            item.get("name") or "",
        ),
    )
    return manual_candidates + automatic_order


def subtitle_candidate_prompt_item(candidate):
    return {
        "id": candidate.get("rank_id"),
        "kind": subtitle_candidate_kind(candidate),
        "language_code": candidate.get("language_code"),
        "name": candidate.get("name"),
        "source_language_code": candidate.get("source_language_code"),
        "translated_to_language_code": candidate.get("translated_to_language_code"),
    }


def rank_subtitle_candidates_with_groq(video_title, candidates, info):
    if not candidates:
        return []

    for index, candidate in enumerate(candidates, start=1):
        prefix = "auto" if candidate.get("automatic") else "manual"
        candidate["rank_id"] = f"{prefix}-{index}"

    prompt = {
        "youtube_title": video_title,
        "youtube_track": info.get("track"),
        "youtube_artist": info.get("artist") or info.get("artists"),
        "instructions": (
            "Rank subtitle tracks for song lyrics extraction. Prefer the track whose language "
            "matches the YouTube title/song language. Prefer manual_human over automatic tracks "
            "only when it matches the title language. Prefer automatic_original over "
            "automatic_translated when no matching manual track exists. Avoid translated tracks "
            "to unrelated languages."
        ),
        "candidates": [subtitle_candidate_prompt_item(candidate) for candidate in candidates],
    }

    messages = [
        {
            "role": "system",
            "content": (
                "You choose subtitle priority for lyric extraction. Return JSON only. "
                "Use exact candidate ids from the provided list."
            ),
        },
        {
            "role": "user",
            "content": (
                f"{json.dumps(prompt, ensure_ascii=False)}\n\n"
                "Return exactly this JSON shape:\n"
                '{ "detected_title_language": "language", '
                '"priority_ids": ["candidate-id-1", "candidate-id-2"], '
                '"reason": "short reason" }'
            ),
        },
    ]

    try:
        result = call_and_parse(messages, max_tokens=900, json_mode=True)
    except Exception as e:
        print(f"  Groq subtitle priority ranking failed; using safe fallback order: {e}")
        return candidates

    priority_ids = result.get("priority_ids") or []
    if isinstance(priority_ids, str):
        priority_ids = re.split(r"[\s,]+", priority_ids)

    id_map = {candidate["rank_id"]: candidate for candidate in candidates}
    ranked = []
    seen = set()

    for candidate_id in priority_ids:
        candidate_id = str(candidate_id).strip()
        candidate = id_map.get(candidate_id)
        if candidate and candidate_id not in seen:
            ranked.append(candidate)
            seen.add(candidate_id)

    for candidate in candidates:
        candidate_id = candidate["rank_id"]
        if candidate_id not in seen:
            ranked.append(candidate)
            seen.add(candidate_id)

    detected = result.get("detected_title_language") or "unknown"
    preview = ", ".join(candidate.get("name") or candidate.get("rank_id") for candidate in ranked[:8])
    print(f"  Groq subtitle priority language={detected}; first choices: {preview}")
    return ranked


def get_lyrics_from_youtube_subtitles(video_id, video_title):
    info = get_youtube_info(video_id)
    if not info:
        return None, None

    title_for_language_check = info.get("track") or info.get("title") or video_title or video_id
    manual_candidates = list(iter_subtitle_candidates(info, automatic=False))
    automatic_candidates = list(iter_subtitle_candidates(info, automatic=True))
    ranked_candidates = default_subtitle_candidate_order(manual_candidates, automatic_candidates)

    print(
        f"  YouTube subtitles: {len(manual_candidates)} manual track(s), "
        f"{len(automatic_candidates)} automatic/translated track(s)."
    )

    ranked_candidates = rank_subtitle_candidates_with_groq(
        video_title=title_for_language_check,
        candidates=ranked_candidates,
        info=info,
    )
    first_parseable = None

    for index, candidate in enumerate(ranked_candidates, start=1):
        kind = subtitle_candidate_kind(candidate).replace("_", " ")
        print(f"  Checking ranked subtitle #{index}/{len(ranked_candidates)} ({kind}): {candidate['name']}")
        text = download_subtitle_text(candidate)
        if not text:
            continue
        if first_parseable is None:
            first_parseable = (text, candidate)
        if groq_same_language(title_for_language_check, text, candidate):
            return text, candidate

        print(f"  Subtitle language did not match title: {candidate['name']}")

    if first_parseable:
        text, candidate = first_parseable
        print(
            "  No subtitle passed the second language check; using highest-ranked "
            f"parseable subtitle: {candidate['name']}"
        )
        return text, candidate

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


def lyric_vote_sample(text, max_chars=4500):
    text = clean_lyrics_for_tag(text or "")
    if len(text) <= max_chars:
        return text

    edge = max_chars // 2
    return text[:edge].rstrip() + "\n...\n" + text[-edge:].lstrip()


def normalize_lyric_vote_choice(choice):
    choice = norm_text(str(choice or "")).lower()
    choice = choice.replace("-", "_").replace(" ", "_")

    if choice in {"lrclib", "library", "library_search", "library_lyrics"}:
        return "lrclib"
    if choice in {
        "subtitle",
        "subtitles",
        "youtube_subtitle",
        "youtube_subtitles",
        "caption",
        "captions",
    }:
        return "youtube_subtitles"

    return None


def groq_choose_lyric_source_once(title, artist, library_lyrics, subtitle_lyrics, subtitle_candidate, similarity, vote_number):
    subtitle_source = subtitle_candidate.get("name") if subtitle_candidate else "YouTube subtitles"
    prompt = {
        "vote_number": vote_number,
        "song_title": title,
        "artist": artist,
        "lrclib_subtitle_similarity": similarity,
        "subtitle_source": subtitle_source,
        "subtitle_language_code": subtitle_candidate.get("language_code") if subtitle_candidate else None,
        "subtitle_kind": subtitle_candidate_kind(subtitle_candidate) if subtitle_candidate else None,
        "task": (
            "Choose which lyrics should be saved for this song. Prefer the candidate that "
            "looks like the real complete song lyrics in the song's original language. "
            "Reject unrelated translations, wrong-language captions, noisy transcript text, "
            "or text that does not match the title/artist."
        ),
        "choices": {
            "lrclib": lyric_vote_sample(library_lyrics),
            "youtube_subtitles": lyric_vote_sample(subtitle_lyrics),
        },
    }

    messages = [
        {
            "role": "system",
            "content": (
                "You judge competing lyric candidates. Return JSON only. "
                "The choice must be exactly lrclib or youtube_subtitles."
            ),
        },
        {
            "role": "user",
            "content": (
                f"{json.dumps(prompt, ensure_ascii=False)}\n\n"
                "Return exactly this JSON shape:\n"
                '{ "choice": "lrclib", "confidence": 0.0, "reason": "short reason" }'
            ),
        },
    ]

    result = call_and_parse(messages, max_tokens=220, json_mode=True, temperature=0.35)
    return normalize_lyric_vote_choice(result.get("choice")), result.get("reason") or ""


def groq_vote_lyric_source(title, artist, library_lyrics, subtitle_lyrics, subtitle_candidate, similarity):
    votes = {"lrclib": 0, "youtube_subtitles": 0}
    valid_votes = 0

    print("  LRCLIB/subtitle similarity is below 80%; asking Groq to vote on which lyrics to keep.")

    for vote_number in range(1, 12):
        try:
            choice, reason = groq_choose_lyric_source_once(
                title=title,
                artist=artist,
                library_lyrics=library_lyrics,
                subtitle_lyrics=subtitle_lyrics,
                subtitle_candidate=subtitle_candidate,
                similarity=similarity,
                vote_number=vote_number,
            )
        except Exception as e:
            print(f"  Groq lyric vote {vote_number}/11 failed: {e}")
            continue

        if not choice:
            print(f"  Groq lyric vote {vote_number}/11 returned no usable choice.")
            continue

        votes[choice] += 1
        valid_votes += 1
        reason_suffix = f" ({reason})" if reason else ""
        print(
            f"  Groq lyric vote {vote_number}/11: {choice} "
            f"[LRCLIB {votes['lrclib']}, subtitles {votes['youtube_subtitles']}]" + reason_suffix
        )

        if votes[choice] >= 6:
            print(f"  Groq lyric vote stopped early: {choice} reached 6 votes.")
            break

    if votes["lrclib"] > votes["youtube_subtitles"]:
        winner = "lrclib"
    elif votes["youtube_subtitles"] > votes["lrclib"]:
        winner = "youtube_subtitles"
    else:
        winner = "youtube_subtitles"
        if valid_votes:
            print("  Groq lyric vote tied; keeping subtitle lyrics as fallback.")
        else:
            print("  No valid Groq lyric votes; keeping subtitle lyrics as fallback.")

    print(
        "  Groq lyric vote result: "
        f"{winner} wins [LRCLIB {votes['lrclib']}, subtitles {votes['youtube_subtitles']}]."
    )
    return winner, votes


def choose_best_lyrics(library_lyrics, subtitle_lyrics, subtitle_candidate, title="", artist=""):
    if library_lyrics and subtitle_lyrics:
        similarity = lyric_similarity(library_lyrics, subtitle_lyrics)
        print(f"  LRCLIB/subtitle lyric similarity: {similarity:.2%}")
        if similarity >= 0.80:
            print("  Using LRCLIB lyrics because they match subtitles by at least 80%.")
            return library_lyrics, "lrclib", similarity

        source = subtitle_candidate.get("name") if subtitle_candidate else "YouTube subtitles"
        winner, votes = groq_vote_lyric_source(
            title=title,
            artist=artist,
            library_lyrics=library_lyrics,
            subtitle_lyrics=subtitle_lyrics,
            subtitle_candidate=subtitle_candidate,
            similarity=similarity,
        )
        if winner == "lrclib":
            print(f"  Using LRCLIB lyrics after Groq vote against subtitle source {source}.")
            return library_lyrics, "lrclib_groq_vote", similarity

        print(f"  Using subtitle lyrics from {source} after Groq vote.")
        return subtitle_lyrics, "youtube_subtitles_groq_vote", similarity

    if library_lyrics:
        print("  Using LRCLIB lyrics; no usable YouTube subtitle lyrics were found.")
        return library_lyrics, "lrclib", None

    if subtitle_lyrics:
        source = subtitle_candidate.get("name") if subtitle_candidate else "YouTube subtitles"
        print(f"  Using subtitle lyrics from {source}; LRCLIB returned no lyrics.")
        return subtitle_lyrics, "youtube_subtitles", None

    return None, None, None


def write_tags(file_path: str, title: str, artist: str, album: str = ""):
    title = norm_text(title)
    artist = norm_text(artist)
    album = norm_text(album)

    if looks_bad(title):
        title = ""
    if looks_bad(artist):
        artist = "Unknown"

    audio = get_audio_object(file_path)
    if not audio:
        raise RuntimeError("Unsupported or unreadable audio format")

    try:
        if isinstance(audio, MP4):
            if audio.tags is None:
                audio.add_tags()
            if title:
                audio["©nam"] = [title]
            if artist:
                audio["©ART"] = [artist]
            if album:
                audio["©alb"] = [album]
            audio.save()
            return

        if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
            if title:
                audio["title"] = [title]
            if artist:
                audio["artist"] = [artist]
            if album:
                audio["album"] = [album]
            audio.save()
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

            tags.save(file_path, v2_version=3)
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
        audio.save()

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
            audio.save()
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
            audio.save()
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
            tags.save(file_path, v2_version=3)
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
                audio.save()
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
                audio.save()
            return

        if isinstance(audio, (MP3, WAVE, AIFF)):
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                return

            if tags.getall("USLT"):
                tags.delall("USLT")
                tags.save(file_path, v2_version=3)
    except Exception as e:
        raise RuntimeError(f"Lyrics clear failed: {e}")


def has_usable_title_and_artist(title, artist):
    title = norm_text(title)
    artist = norm_text(artist)
    return bool(title) and bool(artist) and artist.lower() != "unknown"


all_metadata = []

for idx, (original_file_name, file_path) in enumerate(file_entries, start=1):
    existing_title, existing_artist, existing_album = get_existing_basic_tags(file_path)
    already_has_lyrics = has_lyrics(file_path)
    skipped_tag_update_existing = has_usable_title_and_artist(existing_title, existing_artist)
    video_id = extract_youtube_id_from_name(original_file_name)
    tag_source = None

    cleaned_name_for_ai = clean_input_filename(original_file_name)

    print(f"[{idx}/{len(file_entries)}] Reading: {original_file_name}")
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
    elif skipped_tag_update_existing:
        title = existing_title
        artist = existing_artist
        tag_source = "existing"
        print(f"  Skipped tag update: existing title='{title}' | artist='{artist}'")
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

            if looks_bad(artist):
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
    existing_lyrics = read_existing_lyrics(file_path) if already_has_lyrics else None
    existing_lyrics_cleaned_for_noise = False

    if existing_lyrics:
        try:
            cleaned_existing_lyrics = clean_lyrics_for_tag(existing_lyrics)
            if cleaned_existing_lyrics != existing_lyrics.strip():
                if cleaned_existing_lyrics:
                    write_lyrics(file_path, cleaned_existing_lyrics, replace_existing=True)
                    existing_lyrics = cleaned_existing_lyrics
                    existing_lyrics_cleaned_for_noise = True
                    print("  Removed square-bracket content from existing lyrics.")
                else:
                    clear_lyrics(file_path)
                    existing_lyrics = None
                    lyrics_found = False
                    lyrics_source = None
                    existing_lyrics_cleaned_for_noise = True
                    print("  Removed existing lyrics because they only contained square-bracket noise.")
        except Exception as e:
            print(f"  Existing lyrics cleanup error: {e}")

    try:
        duration = read_duration_seconds(file_path)
        library_lyrics = get_lyrics_from_lrclib(
            title=title,
            artist=artist,
            duration=duration,
            album=existing_album
        )

        if existing_lyrics:
            print("  Existing lyrics found; still checking LRCLIB for possible upgrade.")
            if library_lyrics:
                lyric_source_similarity = lyric_similarity(library_lyrics, existing_lyrics)
                print(
                    "  LRCLIB/existing lyric similarity: "
                    f"{lyric_source_similarity:.2%} "
                    "(normalized text ratio plus word-overlap ratio; using the higher score)"
                )

                if lyric_source_similarity >= 0.80:
                    write_lyrics(file_path, library_lyrics, replace_existing=True)
                    lyrics_source = "lrclib"
                    lyrics_found = True
                    print(
                        "  Replaced existing lyrics with LRCLIB lyrics because they "
                        "matched by at least 80%."
                    )
                else:
                    lyrics_source = "existing"
                    print("  Kept existing lyrics because LRCLIB did not match by at least 80%.")
            else:
                lyrics_source = "existing"
                print("  Kept existing lyrics because LRCLIB returned no lyrics.")
        else:
            subtitle_lyrics = None
            subtitle_candidate = None

            if video_id:
                subtitle_lyrics, subtitle_candidate = get_lyrics_from_youtube_subtitles(
                    video_id=video_id,
                    video_title=cleaned_name_for_ai,
                )
            else:
                print("  No YouTube video id found in filename; skipping subtitle lyrics.")

            lyrics, lyrics_source, lyric_source_similarity = choose_best_lyrics(
                library_lyrics=library_lyrics,
                subtitle_lyrics=subtitle_lyrics,
                subtitle_candidate=subtitle_candidate,
                title=title,
                artist=artist,
            )

            if lyrics:
                cleaned_lyrics = clean_lyrics_for_tag(lyrics)
                if cleaned_lyrics:
                    write_lyrics(file_path, cleaned_lyrics, replace_existing=already_has_lyrics)
                    lyrics_found = True
                    subtitle_track = subtitle_candidate.get("name") if subtitle_candidate else None
                    print(f"  Added lyrics from {lyrics_source} ({len(cleaned_lyrics)} chars)")
                elif existing_lyrics_cleaned_for_noise:
                    print("  Lyrics candidate only contained square-bracket noise; not writing lyrics.")
                else:
                    print("  No lyrics found after square-bracket cleanup")
            else:
                print("  No lyrics found")
    except Exception as e:
        print(f"  Lyrics error: {e}")

    all_metadata.append({
        "file_name": original_file_name,
        "title": title,
        "artist": artist,
        "tag_update_skipped_existing": skipped_tag_update_existing,
        "tag_source": tag_source,
        "lyrics_found": lyrics_found,
        "lyrics_skipped_existing": already_has_lyrics,
        "lyrics_source": lyrics_source,
        "subtitle_track": subtitle_track,
        "lrclib_subtitle_similarity": lyric_source_similarity,
    })

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
