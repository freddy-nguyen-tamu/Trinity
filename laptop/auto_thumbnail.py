import argparse
import os
import json
import time
import requests

from ddgs import DDGS
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.mp4 import MP4
from mutagen.flac import FLAC

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FINISHED_DIR = os.path.join(BASE_DIR, "finished")
WORKING_DOWNLOADS_DIR = os.path.join(BASE_DIR, "_working_downloads")
MAX_IMAGE_ATTEMPTS = 10
TMP_IMAGE = os.path.join(BASE_DIR, "_auto_thumb_tmp.jpg")

SUPPORTED_EXTENSIONS = {".mp3", ".m4a", ".mp4", ".flac"}


def parse_args():
    parser = argparse.ArgumentParser(description="Embed missing album art via DuckDuckGo image search.")
    parser.add_argument(
        "--audio-folder",
        help="Folder to scan when --manifest is not provided (default: finished/).",
    )
    parser.add_argument(
        "--manifest",
        help="JSON manifest containing a files array of exact audio paths to process.",
    )
    return parser.parse_args()


ARGS = parse_args()


def is_supported_audio_path(path):
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS


def load_manifest_entries(manifest_path):
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
            print(f"  Skipping manifest item without path: {item}")
            continue

        if not os.path.isabs(raw_path):
            raw_path = os.path.join(BASE_DIR, raw_path)

        file_path = os.path.abspath(raw_path)
        if file_path in seen:
            continue
        seen.add(file_path)

        if not os.path.isfile(file_path):
            print(f"  Skipping missing manifest file: {file_path}")
            continue

        if not is_supported_audio_path(file_path):
            continue

        entries.append((display_name or os.path.basename(file_path), file_path))

    return sorted(entries, key=lambda item: item[0].casefold())


def load_folder_entries(folder):
    if not os.path.isdir(folder):
        return []

    return sorted(
        (f, os.path.join(folder, f))
        for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f))
        and is_supported_audio_path(f)
    )


def has_thumbnail(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == ".mp3":
            tags = ID3(file_path)
            return bool(tags.getall("APIC"))
        audio = MutagenFile(file_path)
        if audio is None or audio.tags is None:
            return False
        if isinstance(audio, MP4):
            return bool(audio.tags.get("covr"))
        if isinstance(audio, FLAC):
            return bool(audio.pictures)
    except Exception:
        pass
    return False


def get_audio_tags(file_path):
    title = ""
    artist = ""
    try:
        audio = MutagenFile(file_path, easy=True)
        if audio and audio.tags:
            title = str(audio.tags.get("title", [""])[0]).strip()
            artist = str(audio.tags.get("artist", [""])[0]).strip()
    except Exception:
        pass
    return title, artist


def search_and_download_thumbnail(query):
    print(f"  Searching images for: {query}")
    with DDGS() as ddgs:
        image_urls = [
            r["image"]
            for r in ddgs.images(query, max_results=MAX_IMAGE_ATTEMPTS)
            if r.get("image")
        ]

    if not image_urls:
        print("  No image results found.")
        return False

    print(f"  Got {len(image_urls)} candidate(s). Trying each...")

    for idx, url in enumerate(image_urls, 1):
        print(f"    [{idx}/{len(image_urls)}] {url} ... ", end="")
        try:
            resp = requests.get(url, stream=True, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"FAIL ({e})")
            continue

        content_type = resp.headers.get("Content-Type", "")
        if not content_type.startswith("image/"):
            print(f"SKIP (not an image: {content_type})")
            resp.close()
            continue

        try:
            with open(TMP_IMAGE, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            print("SUCCESS")
            resp.close()
            return True
        except Exception as e:
            print(f"WRITE ERROR ({e})")
            resp.close()
            continue

    print(f"  Could not download any valid image from the first {MAX_IMAGE_ATTEMPTS} results.")
    return False


def embed_thumbnail(file_path):
    if not os.path.exists(TMP_IMAGE):
        return False

    ext = os.path.splitext(file_path)[1].lower()

    with open(TMP_IMAGE, "rb") as f:
        img_data = f.read()

    mime = "image/png" if TMP_IMAGE.lower().endswith(".png") else "image/jpeg"

    try:
        if ext == ".mp3":
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                tags = ID3()

            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=img_data))
            tags.save(file_path, v2_version=3)
            return True

        audio = MutagenFile(file_path)
        if audio is None:
            return False

        if isinstance(audio, MP4):
            if audio.tags is None:
                audio.add_tags()
            from mutagen.mp4 import MP4Cover
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(img_data, fmt)]
            audio.save()
            return True

        if isinstance(audio, FLAC):
            from mutagen.flac import Picture
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.data = img_data
            audio.add_picture(pic)
            audio.save()
            return True

        print(f"  Thumbnail embedding not supported for {ext}.")
        return False

    except Exception as e:
        print(f"  Embed error: {e}")
        return False


def process_entries(entries):
    if not entries:
        print("No supported audio files found.")
        return

    processed = 0
    embedded = 0
    skipped = 0
    failed = 0

    for name, path in entries:
        processed += 1
        print(f"[{processed}/{len(entries)}] {name}")

        if has_thumbnail(path):
            print("  Thumbnail already present, skipping.")
            skipped += 1
            continue

        title, artist = get_audio_tags(path)
        basename = os.path.splitext(name)[0]
        query_parts = [p for p in [artist, title, basename] if p]
        query = "  ".join(query_parts)

        if not query:
            print("  No search terms available, skipping.")
            failed += 1
            continue

        if not search_and_download_thumbnail(query):
            print("  No suitable image found.")
            failed += 1
            continue

        if embed_thumbnail(path):
            print(f"  Thumbnail embedded: {name}")
            embedded += 1
        else:
            print(f"  Failed to embed thumbnail: {name}")
            failed += 1

        if os.path.exists(TMP_IMAGE):
            os.remove(TMP_IMAGE)

        time.sleep(0.3)

    print("")
    print("AUTO THUMBNAIL SUMMARY")
    print("-" * 40)
    print(f"  Total processed: {processed}")
    print(f"  Already had thumbnail: {skipped}")
    print(f"  Embedded: {embedded}")
    print(f"  Failed: {failed}")

    if os.path.exists(TMP_IMAGE):
        os.remove(TMP_IMAGE)


def gather_entries():
    if ARGS.manifest:
        return load_manifest_entries(ARGS.manifest)

    folder = ARGS.audio_folder
    if not folder:
        entries = load_folder_entries(WORKING_DOWNLOADS_DIR)
        finished_entries = load_folder_entries(FINISHED_DIR)
        seen = set()
        result = []
        for name, path in entries + finished_entries:
            if path not in seen:
                seen.add(path)
                result.append((name, path))
        return sorted(result, key=lambda item: item[0].casefold())

    return load_folder_entries(folder)


def main():
    entries = gather_entries()
    process_entries(entries)


if __name__ == "__main__":
    main()
