import os
import json
import re
import time
import unicodedata
import requests

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
AUDIO_FOLDER = r"C:\ytb\_working_downloads"

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

if not os.path.isdir(AUDIO_FOLDER):
    raise SystemExit(f"Folder not found: {AUDIO_FOLDER}")

file_names = sorted(
    f for f in os.listdir(AUDIO_FOLDER)
    if os.path.isfile(os.path.join(AUDIO_FOLDER, f))
    and os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
)

if not file_names:
    raise SystemExit("No supported audio files found in folder.")

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


def call_and_parse(messages, max_tokens=220, json_mode=True):
    data_json = groq_chat(messages, max_tokens=max_tokens, temperature=0, json_mode=json_mode)
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
            lyr = audio.tags.get("©lyr")
            if isinstance(lyr, list):
                return any(str(x).strip() for x in lyr)
            return bool(str(lyr).strip()) if lyr else False

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


def write_lyrics(file_path: str, lyrics: str):
    lyrics = lyrics.strip()
    if not lyrics:
        return

    audio = get_audio_object(file_path)
    if not audio:
        raise RuntimeError("Unsupported or unreadable audio format")

    try:
        if isinstance(audio, MP4):
            if audio.tags is None:
                audio.add_tags()

            existing = audio.tags.get("©lyr")
            if isinstance(existing, list) and any(str(x).strip() for x in existing):
                return

            audio["©lyr"] = [lyrics]
            audio.save()
            return

        if isinstance(audio, (FLAC, OggVorbis, OggOpus)):
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


all_metadata = []

for idx, original_file_name in enumerate(file_names, start=1):
    file_path = os.path.join(AUDIO_FOLDER, original_file_name)

    existing_title, existing_artist, existing_album = get_existing_basic_tags(file_path)
    already_has_lyrics = has_lyrics(file_path)

    cleaned_name_for_ai = clean_input_filename(original_file_name)

    print(f"[{idx}/{len(file_names)}] Reading: {original_file_name}")

    messages = [
        {"role": "system", "content": "Return valid JSON only."},
        {"role": "user", "content": build_single_prompt(idx, cleaned_name_for_ai)},
    ]

    try:
        one = call_and_parse(messages, max_tokens=220, json_mode=True)
        title = norm_text(str(one.get("title") or ""))
        artist = norm_text(str(one.get("artist") or "Unknown")) or "Unknown"

        if looks_bad(title):
            print(f"  Suspicious title detected, using filename fallback: {title}")
            title = conservative_filename_title(original_file_name)

        if looks_bad(artist):
            print(f"  Suspicious artist detected, using Unknown: {artist}")
            artist = "Unknown"

    except Exception as e:
        print(f"  AI parse failed: {e}")
        title = conservative_filename_title(original_file_name)
        artist = existing_artist or "Unknown"

    if not title:
        title = conservative_filename_title(original_file_name)

    try:
        write_tags(file_path, title=title, artist=artist, album=existing_album)
        print(f"  Updated tags -> title='{title}' | artist='{artist}'")
    except Exception as e:
        print(f"  Error updating tags: {e}")

    lyrics_found = already_has_lyrics
    if already_has_lyrics:
        print("  Skipped lyrics: already present")
    else:
        try:
            duration = read_duration_seconds(file_path)
            lyrics = get_lyrics_from_lrclib(
                title=title,
                artist=artist,
                duration=duration,
                album=existing_album
            )
            if lyrics:
                write_lyrics(file_path, lyrics)
                lyrics_found = True
                print(f"  Added lyrics ({len(lyrics)} chars)")
            else:
                print("  No lyrics found")
        except Exception as e:
            print(f"  Lyrics error: {e}")

    all_metadata.append({
        "file_name": original_file_name,
        "title": title,
        "artist": artist,
        "lyrics_found": lyrics_found,
        "lyrics_skipped_existing": already_has_lyrics,
    })

    time.sleep(0.4)

print("\nDone.")
print(json.dumps(all_metadata, ensure_ascii=False, indent=2))
