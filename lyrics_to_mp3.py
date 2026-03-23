import os
import json
import re
import time
import unicodedata
import requests

from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, USLT, ID3NoHeaderError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MP3_FOLDER = os.path.join(BASE_DIR, "downloads")

# xAI / Grok
XAI_API_KEY = os.getenv("XAI_API_KEY")
XAI_API_URL = "https://api.x.ai/v1/chat/completions"
XAI_MODEL = "grok-3-mini"

# LRCLIB
LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
LRCLIB_GET_URL = "https://lrclib.net/api/get"

if not XAI_API_KEY:
    raise SystemExit("XAI_API_KEY is not set in the environment.")

if not os.path.isdir(MP3_FOLDER):
    raise SystemExit(f"Folder not found: {MP3_FOLDER}")

file_names = sorted(
    f for f in os.listdir(MP3_FOLDER)
    if os.path.isfile(os.path.join(MP3_FOLDER, f)) and f.lower().endswith(".mp3")
)
if not file_names:
    raise SystemExit("No MP3 files found in folder.")

ai_headers = {
    "Authorization": f"Bearer {XAI_API_KEY}",
    "Content-Type": "application/json",
}

lyrics_headers = {
    "User-Agent": "mp3-lyrics-tagger/1.0"
}


def norm_text(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def clean_input_filename(name: str) -> str:
    name = re.sub(r"\.mp3$", "", name, flags=re.IGNORECASE)
    name = name.replace("_", " ")
    name = norm_text(name)
    return name


def clean_filename_fallback_title(name: str) -> str:
    """
    Conservative fallback: keep original text as much as possible.
    Removes only obvious junk but preserves Vietnamese exactly.
    """
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


def xai_chat(messages, max_tokens=220, temperature=0, timeout=60, max_retries=8):
    payload = {
        "model": XAI_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    backoff = 1.0

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(XAI_API_URL, headers=ai_headers, json=payload, timeout=timeout)
        except requests.RequestException as e:
            if attempt == max_retries:
                raise SystemExit(f"Network error calling xAI: {e}")
            wait_s = backoff + 0.25
            print(f"[NET] Sleeping {wait_s:.2f}s (attempt {attempt}/{max_retries})")
            time.sleep(wait_s)
            backoff = min(backoff * 1.6, 20.0)
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            wait_s = max(parse_retry_after_seconds(resp), backoff) + 0.25
            print(f"[429] Rate limited. Sleeping {wait_s:.2f}s (attempt {attempt}/{max_retries})")
            time.sleep(wait_s)
            backoff = min(backoff * 1.6, 20.0)
            continue

        raise SystemExit(f"xAI API error: {resp.status_code} {resp.text}")

    raise SystemExit("xAI API error: too many retries.")


def build_single_prompt(item_id, file_name):
    return (
        "Extract title and artist from this music filename.\n\n"
        f"Input:\n{json.dumps({'id': item_id, 'file_name': file_name}, ensure_ascii=False)}\n\n"
        "Return EXACTLY this JSON object:\n"
        "{\n"
        f'  "id": {item_id},\n'
        '  "title": "title",\n'
        '  "artist": "artist or Unknown",\n'
        '  "confidence": 0.0\n'
        "}\n\n"
        "Rules:\n"
        "- Preserve Vietnamese and all Unicode characters exactly.\n"
        "- Do NOT transliterate, normalize differently, or rewrite accented characters.\n"
        "- Do NOT invent words.\n"
        "- Only remove obvious suffix junk such as: .mp3, Official Video, Official MV, Lyrics, Lyric Video, Visualizer, Audio, HD, 4K.\n"
        "- If the filename already looks like a valid song title, keep it almost unchanged.\n"
        "- If artist is unclear, use Unknown.\n"
        "- confidence is between 0 and 1.\n"
        "- Output JSON only."
    )


def call_and_parse(messages, max_tokens=220):
    data_json = xai_chat(messages, max_tokens=max_tokens, temperature=0)
    content = data_json["choices"][0]["message"]["content"]
    return extract_json_object(content)


def looks_bad(text: str) -> bool:
    if not text:
        return True
    bad_markers = ["�", "CÑA", "TÙN", "\\u", "???"]
    return any(x in text for x in bad_markers)


def fold_for_compare(s: str) -> str:
    """
    Compare strings while ignoring accent composition differences.
    """
    s = norm_text(s).casefold()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


def title_seems_suspicious(model_title: str, original_cleaned_filename: str) -> bool:
    """
    Reject titles that are too different from the source filename after
    accent-insensitive folding. This helps avoid weird Vietnamese rendering.
    """
    if not model_title:
        return True
    if looks_bad(model_title):
        return True

    a = fold_for_compare(model_title)
    b = fold_for_compare(original_cleaned_filename)

    if not a or not b:
        return True

    if a in b or b in a:
        return False

    # crude similarity gate
    overlap = sum(1 for ch1, ch2 in zip(a, b) if ch1 == ch2)
    similarity = overlap / max(len(a), len(b))
    return similarity < 0.45


def read_duration_seconds(file_path: str):
    try:
        audio = MP3(file_path)
        if audio.info and audio.info.length:
            return int(round(audio.info.length))
    except Exception:
        pass
    return None


def has_lyrics(file_path: str) -> bool:
    try:
        tags = ID3(file_path)
        for frame in tags.getall("USLT"):
            text = frame.text
            if isinstance(text, list):
                text = " ".join(str(x) for x in text)
            if str(text).strip():
                return True
    except Exception:
        pass
    return False


def get_existing_basic_tags(file_path: str):
    title = ""
    artist = ""
    album = ""

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

    return title, artist, album


def get_lyrics_from_lrclib(title: str, artist: str, duration=None, album=""):
    if not title:
        return None

    try:
        params = {
            "track_name": title,
            "artist_name": artist if artist and artist != "Unknown" else "",
        }
        if duration:
            params["duration"] = duration
        if album:
            params["album_name"] = album

        resp = requests.get(LRCLIB_GET_URL, params=params, headers=lyrics_headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            lyrics = data.get("plainLyrics") or data.get("syncedLyrics")
            if lyrics and lyrics.strip():
                return lyrics.strip()
    except Exception:
        pass

    try:
        q = f"{artist} {title}".strip() if artist and artist != "Unknown" else title
        resp = requests.get(LRCLIB_SEARCH_URL, params={"q": q}, headers=lyrics_headers, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                best = None
                normalized_title = norm_text(title).lower()
                normalized_artist = norm_text(artist).lower()

                for item in data:
                    item_title = norm_text(item.get("trackName", "")).lower()
                    item_artist = norm_text(item.get("artistName", "")).lower()

                    if item_title == normalized_title:
                        if artist == "Unknown" or not artist:
                            best = item
                            break
                        if item_artist == normalized_artist:
                            best = item
                            break

                if best is None:
                    best = data[0]

                lyrics = best.get("plainLyrics") or best.get("syncedLyrics")
                if lyrics and lyrics.strip():
                    return lyrics.strip()
    except Exception:
        pass

    return None


def write_tags(file_path: str, title: str, artist: str, album: str = ""):
    title = norm_text(title)
    artist = norm_text(artist)
    album = norm_text(album)

    if looks_bad(title):
        title = ""
    if looks_bad(artist):
        artist = "Unknown"

    try:
        try:
            audio = MP3(file_path, ID3=EasyID3)
        except Exception:
            audio = MP3(file_path)
            if audio.tags is None:
                audio.add_tags()
            audio.save()
            audio = MP3(file_path, ID3=EasyID3)

        if title:
            audio["title"] = [title]
        if artist:
            audio["artist"] = [artist]
        if album:
            audio["album"] = [album]

        audio.save()
    except Exception:
        pass

    try:
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
    except Exception as e:
        raise RuntimeError(f"ID3 write failed: {e}")


def write_lyrics(file_path: str, lyrics: str):
    lyrics = lyrics.strip()
    if not lyrics:
        return

    try:
        try:
            tags = ID3(file_path)
        except ID3NoHeaderError:
            tags = ID3()

        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="XXX", desc="", text=lyrics))
        tags.save(file_path, v2_version=3)
    except Exception as e:
        raise RuntimeError(f"Lyrics write failed: {e}")


all_metadata = []

for idx, original_file_name in enumerate(file_names, start=1):
    file_path = os.path.join(MP3_FOLDER, original_file_name)
    existing_title, existing_artist, existing_album = get_existing_basic_tags(file_path)
    already_has_lyrics = has_lyrics(file_path)

    print(f"[{idx}/{len(file_names)}] Reading: {original_file_name}")

    title = existing_title
    artist = existing_artist or "Unknown"
    parsed_by_ai = False

    # Only use AI to fill missing basic tags
    if not title or not existing_artist:
        cleaned_name_for_ai = clean_input_filename(original_file_name)
        filename_fallback_title = clean_filename_fallback_title(original_file_name)

        messages = [
            {"role": "system", "content": "Return valid JSON only."},
            {"role": "user", "content": build_single_prompt(idx, cleaned_name_for_ai)},
        ]

        try:
            one = call_and_parse(messages, max_tokens=220)
            ai_title = norm_text(str(one.get("title") or ""))
            ai_artist = norm_text(str(one.get("artist") or "Unknown")) or "Unknown"
            confidence = float(one.get("confidence", 0.0) or 0.0)

            if title_seems_suspicious(ai_title, cleaned_name_for_ai) or confidence < 0.45:
                print(f"  Title looked suspicious or low-confidence. Using filename fallback.")
                ai_title = filename_fallback_title

            if looks_bad(ai_artist):
                ai_artist = "Unknown"

            if not title:
                title = ai_title
            if not existing_artist:
                artist = ai_artist

            parsed_by_ai = True

        except Exception as e:
            print(f"  AI parse failed: {e}")
            if not title:
                title = clean_filename_fallback_title(original_file_name)
            if not artist:
                artist = "Unknown"

    # Write tags if we have something useful
    try:
        write_tags(file_path, title=title, artist=artist, album=existing_album)
        if parsed_by_ai:
            print(f"  Updated tags -> title='{title}' | artist='{artist}'")
        else:
            print(f"  Kept existing tags -> title='{title}' | artist='{artist}'")
    except Exception as e:
        print(f"  Error updating tags: {e}")

    # Skip lyrics if already present
    lyrics_found = already_has_lyrics
    if already_has_lyrics:
        print("  Skipped lyrics: already present")
    else:
        try:
            duration = read_duration_seconds(file_path)
            lyrics = get_lyrics_from_lrclib(title=title, artist=artist, duration=duration, album=existing_album)
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