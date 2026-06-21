import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time

try:
    from mutagen import File as MutagenFile
    from mutagen.aiff import AIFF
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis
    from mutagen.wave import WAVE
except Exception:
    MutagenFile = None
    AIFF = FLAC = ID3 = MP4 = OggOpus = OggVorbis = WAVE = None

# Path to Python interpreter
PYTHON_EXE = r"C:\Users\qacer\AppData\Local\Python\pythoncore-3.14-64\python.exe"

def current_app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# Folder where all laptop scripts and runtime files live.
SCRIPTS_DIR = current_app_dir()
WORKING_DOWNLOADS_DIR = os.path.join(SCRIPTS_DIR, "_working_downloads")
FINISHED_DIR = os.path.join(SCRIPTS_DIR, "finished")
LOG_FILE = os.path.join(SCRIPTS_DIR, "trinity_run_log.txt")
DOWNLOAD_MANIFEST_FILE = os.path.join(SCRIPTS_DIR, "_last_downloaded_manifest.json")
TEST_MANIFEST_FILE = os.path.join(SCRIPTS_DIR, "_last_test_manifest.json")
UPLOAD_SUMMARY_FILE = os.path.join(SCRIPTS_DIR, "_last_upload_summary.json")
DRIVE_UPLOAD_MANIFEST_FILE = os.path.join(SCRIPTS_DIR, "_last_drive_upload_delete_manifest.json")
DRIVE_UPLOAD_SUMMARY_FILE = os.path.join(SCRIPTS_DIR, "_last_drive_upload_delete_summary.json")
TEST_PROCESSED_HISTORY_FILE = os.path.join(SCRIPTS_DIR, "_test_processed_history.json")
UPLOADED_HISTORY_FILE = os.path.join(SCRIPTS_DIR, "_android_uploaded_history.json")
DRIVE_UPLOADED_HISTORY_FILE = os.path.join(SCRIPTS_DIR, "_drive_uploaded_history.json")
DRIVE_FOLDER_ID = "1qbVH_yaNn1aagSrMGvZIggCjSvRzZRSs"
DRIVE_SERVICE_ACCOUNT_EMAIL = "trinitydrive@wavestack2.iam.gserviceaccount.com"
DRIVE_SERVICE_ACCOUNT_JSON_FILE = os.path.join(SCRIPTS_DIR, "wavestack2-cbe4e94e8a01.json")
DRIVE_OAUTH_CLIENT_JSON_FILE = os.path.join(SCRIPTS_DIR, "google_drive_oauth_client.json")
DRIVE_OAUTH_TOKEN_FILE = os.path.join(SCRIPTS_DIR, "_google_drive_oauth_token.json")
SUPPORTED_AUDIO_EXTENSIONS = {
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
MP4_LYRICS_KEYS = ("\xa9lyr", "\xc2\xa9lyr")

_LOG_HANDLE = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def open_run_log():
    global _LOG_HANDLE
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    _LOG_HANDLE = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    log("")
    log("=" * 72)
    log(f"RUN START: {datetime.datetime.now().isoformat(timespec='seconds')}")
    log(f"Scripts folder: {SCRIPTS_DIR}")
    log(f"Working folder: {WORKING_DOWNLOADS_DIR}")
    log(f"Finished folder: {FINISHED_DIR}")


def close_run_log():
    global _LOG_HANDLE
    if _LOG_HANDLE:
        log(f"RUN END: {datetime.datetime.now().isoformat(timespec='seconds')}")
        _LOG_HANDLE.close()
        _LOG_HANDLE = None


def write_log_raw(text):
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))
    sys.stdout.flush()

    if _LOG_HANDLE:
        _LOG_HANDLE.write(text)
        _LOG_HANDLE.flush()


def log(message=""):
    write_log_raw(f"{message}\n")


def retry_file_operation(description, operation, attempts=8, initial_delay=0.75):
    delay = initial_delay
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (PermissionError, OSError) as e:
            last_error = e
            if attempt >= attempts:
                break

            log(
                f"File busy while {description}; retrying in {delay:.1f}s "
                f"({attempt}/{attempts}): {e}"
            )
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)

    raise last_error


def remove_file_if_exists(path, description):
    if not os.path.exists(path):
        return

    retry_file_operation(description, lambda: os.remove(path))


def prepare_output_dirs():
    os.makedirs(WORKING_DOWNLOADS_DIR, exist_ok=True)
    os.makedirs(FINISHED_DIR, exist_ok=True)

    if os.name == "nt":
        subprocess.run(
            ["attrib", "+h", WORKING_DOWNLOADS_DIR],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def unique_destination(path):
    if not os.path.exists(path):
        return path

    root, ext = os.path.splitext(path)
    counter = 1
    while True:
        candidate = f"{root} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1


def expand_files(paths):
    files = []
    for path in paths:
        if os.path.isfile(path):
            files.append(os.path.abspath(path))
        elif os.path.isdir(path):
            for root, _, names in os.walk(path):
                for name in names:
                    files.append(os.path.abspath(os.path.join(root, name)))
    return files


def move_working_items_to_finished():
    moved_paths = []

    if not os.path.isdir(WORKING_DOWNLOADS_DIR):
        return moved_paths

    for item_name in sorted(os.listdir(WORKING_DOWNLOADS_DIR)):
        source_path = os.path.join(WORKING_DOWNLOADS_DIR, item_name)
        destination_path = unique_destination(os.path.join(FINISHED_DIR, item_name))
        try:
            retry_file_operation(
                f"moving {source_path} to finished/",
                lambda s=source_path, d=destination_path: shutil.move(s, d),
            )
            moved_paths.append(destination_path)
            log(f"Moved to finished: {destination_path}")
        except Exception as e:
            log(f"WARNING: Could not move working item to finished/ after retries: {source_path} ({e})")

    return expand_files(moved_paths)


def snapshot_finished_files():
    if not os.path.isdir(FINISHED_DIR):
        return set()

    found = set()
    for root, _, names in os.walk(FINISHED_DIR):
        for name in names:
            found.add(os.path.abspath(os.path.join(root, name)))
    return found


def is_supported_audio_file(path):
    return os.path.splitext(path)[1].lower() in SUPPORTED_AUDIO_EXTENSIONS


def has_working_audio_files():
    if not os.path.isdir(WORKING_DOWNLOADS_DIR):
        return False

    for name in os.listdir(WORKING_DOWNLOADS_DIR):
        path = os.path.join(WORKING_DOWNLOADS_DIR, name)
        if not os.path.isfile(path):
            continue
        if is_supported_audio_file(path):
            return True

    return False


def relative_finished_name(path):
    try:
        return os.path.relpath(path, FINISHED_DIR)
    except ValueError:
        return os.path.basename(path)


def uploaded_history_key(path):
    abs_path = os.path.abspath(path)
    name = relative_finished_name(abs_path)
    size = os.path.getsize(abs_path) if os.path.exists(abs_path) else 0
    return f"{name}|{size}"


def history_key_from_item(item):
    path = safe_finished_path(item.get("path", ""))
    name = relative_finished_name(path) if path else item.get("name", "")
    size = item.get("size")

    if size is None and path and os.path.exists(path):
        size = os.path.getsize(path)

    try:
        size = int(size)
    except (TypeError, ValueError):
        size = 0

    return f"{name}|{size}"


def load_history_file(path, purpose):
    if not os.path.exists(path):
        return set()

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log(f"Could not read {purpose} history; treating matching finished files as pending: {e}")
        return set()

    if isinstance(data, list):
        return {str(item) for item in data}

    return set()


def save_history_file(path, history):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(history), f, indent=2, ensure_ascii=False)


def load_uploaded_history():
    return load_history_file(UPLOADED_HISTORY_FILE, "uploaded")


def save_uploaded_history(history):
    save_history_file(UPLOADED_HISTORY_FILE, history)


def load_drive_uploaded_history():
    return load_history_file(DRIVE_UPLOADED_HISTORY_FILE, "Drive uploaded")


def save_drive_uploaded_history(history):
    save_history_file(DRIVE_UPLOADED_HISTORY_FILE, history)


def test_processed_history_key(path):
    return uploaded_history_key(path)


def load_test_processed_history():
    return load_history_file(TEST_PROCESSED_HISTORY_FILE, "test.py processed")


def save_test_processed_history(history):
    save_history_file(TEST_PROCESSED_HISTORY_FILE, history)


def pending_finished_upload_files():
    uploaded_history = load_uploaded_history()
    pending = []

    for path in sorted(snapshot_finished_files()):
        if not is_supported_audio_file(path):
            continue
        if uploaded_history_key(path) not in uploaded_history:
            pending.append(path)

    return pending


def first_tag_value(value):
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    if value is None:
        return ""
    return str(value).strip()


def audio_missing_title_or_artist(path):
    if MutagenFile is None:
        log("Mutagen is not available in trinity.py; relying on test.py processed history only.")
        return False

    try:
        audio = MutagenFile(path, easy=True)
    except Exception as e:
        log(f"Could not inspect audio tags, will reprocess with test.py: {path} ({e})")
        return True

    if not audio or not audio.tags:
        return True

    title = first_tag_value(audio.tags.get("title"))
    artist = first_tag_value(audio.tags.get("artist"))
    return not title or not artist or artist.lower() == "unknown"


def text_value_present(value):
    if isinstance(value, list):
        return any(str(item).strip() for item in value)
    return bool(value and str(value).strip())


def iter_tag_text_values(value):
    if isinstance(value, list):
        for item in value:
            text = str(item).strip()
            if text:
                yield text
        return

    if value and str(value).strip():
        yield str(value).strip()


def text_has_square_bracket_content(text):
    return bool(re.search(r"\[[^\[\]\r\n]*\]", text or ""))


def id3_has_lyrics(path):
    if ID3 is None:
        return False

    try:
        tags = ID3(path)
    except Exception:
        return False

    for frame in tags.getall("USLT"):
        text = frame.text
        if isinstance(text, list):
            text = " ".join(str(item) for item in text)
        if str(text).strip():
            return True

    return False


def id3_lyrics_need_square_bracket_cleanup(path):
    if ID3 is None:
        return False

    try:
        tags = ID3(path)
    except Exception:
        return False

    for frame in tags.getall("USLT"):
        text = frame.text
        if isinstance(text, list):
            text = " ".join(str(item) for item in text)
        if text_has_square_bracket_content(str(text)):
            return True

    return False


def audio_has_lyrics(path):
    if MutagenFile is None:
        log("Mutagen is not available in trinity.py; cannot inspect lyrics.")
        return False

    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        return id3_has_lyrics(path)

    try:
        audio = MutagenFile(path)
    except Exception as e:
        log(f"Could not inspect lyrics, will reprocess with test.py: {path} ({e})")
        return False

    if not audio or audio.tags is None:
        return False

    if MP4 is not None and isinstance(audio, MP4):
        return any(text_value_present(audio.tags.get(key)) for key in MP4_LYRICS_KEYS)

    if (
        (FLAC is not None and isinstance(audio, FLAC))
        or (OggVorbis is not None and isinstance(audio, OggVorbis))
        or (OggOpus is not None and isinstance(audio, OggOpus))
    ):
        for key in ("lyrics", "unsyncedlyrics", "lyric"):
            if text_value_present(audio.tags.get(key)):
                return True
        return False

    if (
        (WAVE is not None and isinstance(audio, WAVE))
        or (AIFF is not None and isinstance(audio, AIFF))
    ):
        return id3_has_lyrics(path)

    for key in ("lyrics", "unsyncedlyrics", "lyric", *MP4_LYRICS_KEYS):
        try:
            if text_value_present(audio.tags.get(key)):
                return True
        except Exception:
            pass

    return False


def audio_lyrics_need_square_bracket_cleanup(path):
    if MutagenFile is None:
        return False

    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        return id3_lyrics_need_square_bracket_cleanup(path)

    try:
        audio = MutagenFile(path)
    except Exception:
        return False

    if not audio or audio.tags is None:
        return False

    if MP4 is not None and isinstance(audio, MP4):
        for key in MP4_LYRICS_KEYS:
            for text in iter_tag_text_values(audio.tags.get(key)):
                if text_has_square_bracket_content(text):
                    return True
        return False

    if (
        (FLAC is not None and isinstance(audio, FLAC))
        or (OggVorbis is not None and isinstance(audio, OggVorbis))
        or (OggOpus is not None and isinstance(audio, OggOpus))
    ):
        for key in ("lyrics", "unsyncedlyrics", "lyric"):
            for text in iter_tag_text_values(audio.tags.get(key)):
                if text_has_square_bracket_content(text):
                    return True
        return False

    if (
        (WAVE is not None and isinstance(audio, WAVE))
        or (AIFF is not None and isinstance(audio, AIFF))
    ):
        return id3_lyrics_need_square_bracket_cleanup(path)

    for key in ("lyrics", "unsyncedlyrics", "lyric", *MP4_LYRICS_KEYS):
        try:
            for text in iter_tag_text_values(audio.tags.get(key)):
                if text_has_square_bracket_content(text):
                    return True
        except Exception:
            pass

    return False


def pending_finished_test_files():
    return [
        path
        for path in sorted(snapshot_finished_files())
        if is_supported_audio_file(path)
    ]


def pending_finished_drive_upload_files():
    uploaded_history = load_uploaded_history()
    drive_uploaded_history = load_drive_uploaded_history()
    pending = []

    for path in sorted(snapshot_finished_files()):
        if not is_supported_audio_file(path):
            continue

        history_key = uploaded_history_key(path)
        if history_key not in uploaded_history:
            continue
        if history_key in drive_uploaded_history:
            continue

        pending.append(path)

    return pending


def remember_successful_uploads(successful_items):
    if not successful_items:
        return

    uploaded_history = load_uploaded_history()

    for item in successful_items:
        path = safe_finished_path(item.get("path", ""))
        if path and os.path.exists(path):
            uploaded_history.add(uploaded_history_key(path))

    save_uploaded_history(uploaded_history)
    log(f"Recorded {len(successful_items)} successful upload(s) in uploaded history.")


def remember_successful_drive_uploads(summary):
    successful_items = summary.get("successful") or []
    if not successful_items:
        return

    drive_uploaded_history = load_drive_uploaded_history()

    for item in successful_items:
        if isinstance(item, dict):
            history_key = history_key_from_item(item)
            if history_key:
                drive_uploaded_history.add(history_key)

    save_drive_uploaded_history(drive_uploaded_history)
    log(f"Recorded {len(successful_items)} successful Drive upload(s) in Drive history.")


def remember_test_processed_files(paths):
    paths = [path for path in paths if path and os.path.exists(path) and is_supported_audio_file(path)]
    if not paths:
        return

    processed_history = load_test_processed_history()

    for path in paths:
        processed_history.add(test_processed_history_key(path))

    save_test_processed_history(processed_history)
    log(f"Recorded {len(paths)} file(s) in test.py processed history.")


def write_download_manifest(paths):
    payload = {
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "finished_dir": FINISHED_DIR,
        "files": sorted(os.path.abspath(path) for path in paths),
    }

    with open(DOWNLOAD_MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    log(f"Wrote download manifest: {DOWNLOAD_MANIFEST_FILE}")


def write_test_manifest(paths):
    payload = {
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "finished_dir": FINISHED_DIR,
        "files": sorted(os.path.abspath(path) for path in paths),
    }

    with open(TEST_MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    log(f"Wrote test.py manifest: {TEST_MANIFEST_FILE}")


def load_upload_summary(downloaded_paths):
    if os.path.exists(UPLOAD_SUMMARY_FILE):
        try:
            with open(UPLOAD_SUMMARY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"Could not read upload summary: {e}")

    items = [{"path": path, "name": relative_finished_name(path)} for path in downloaded_paths]
    return {
        "attempted": items,
        "successful": [],
        "failed": items,
        "error": "Upload summary was not created.",
    }


def run_script(script_name, extra_args=None, check=True):
    script_path = os.path.join(SCRIPTS_DIR, script_name)
    args = extra_args or []

    log("")
    log(f"RUNNING: {script_path}")
    log("-" * 50)

    if not os.path.exists(script_path):
        raise SystemExit(f"Missing script: {script_path}")

    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUNBUFFERED"] = "1"

    process = subprocess.Popen(
        [PYTHON_EXE, "-u", script_path, *args],
        cwd=SCRIPTS_DIR,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=child_env,
    )

    try:
        if process.stdout:
            for line in process.stdout:
                write_log_raw(line)
    except KeyboardInterrupt:
        log("")
        log(f"Interrupted while running {script_name}; terminating child process...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log(f"{script_name} did not exit after terminate; killing it...")
            process.kill()
            process.wait()
        raise

    return_code = process.wait()

    if return_code == 0:
        log(f"FINISHED: {script_name}")
    else:
        log(f"ERROR: {script_name} exited with code {return_code}")
        if check:
            raise SystemExit(return_code)

    return return_code


def run_upload_step(downloaded_paths):
    if not downloaded_paths:
        log("")
        log("No finished/ files need Android upload.")
        return {
            "attempted": [],
            "successful": [],
            "failed": [],
            "error": None,
        }, 0

    write_download_manifest(downloaded_paths)

    remove_file_if_exists(UPLOAD_SUMMARY_FILE, "removing old Android upload summary")

    return_code = run_script(
        "find_lan_upload.py",
        [
            "--manifest",
            DOWNLOAD_MANIFEST_FILE,
            "--summary-json",
            UPLOAD_SUMMARY_FILE,
        ],
        check=False,
    )

    return load_upload_summary(downloaded_paths), return_code


def process_finished_files_before_upload():
    pending = pending_finished_test_files()
    if not pending:
        log("")
        log("No finished/ audio files found before upload.")
        return []

    log("")
    log(f"Running test.py on all {len(pending)} finished/ audio file(s).")
    print_numbered_files("Running test.py on finished/ files:", pending)
    write_test_manifest(pending)

    run_script(
        "test.py",
        [
            "--manifest",
            TEST_MANIFEST_FILE,
            "--no-move",
        ],
    )

    remember_test_processed_files(pending)
    return pending


def prepare_upload_candidates_after_test(finished_before, moved_after_test, mark_downloaded_as_test_processed):
    new_finished = snapshot_finished_files() - finished_before
    downloaded_paths = sorted(set(moved_after_test) | new_finished)

    if mark_downloaded_as_test_processed:
        remember_test_processed_files(downloaded_paths)

    process_finished_files_before_upload()

    upload_candidates = sorted(
        path
        for path in set(downloaded_paths) | set(pending_finished_upload_files())
        if is_supported_audio_file(path)
    )

    return downloaded_paths, upload_candidates


def is_nonfatal_upload_failure(upload_summary):
    error = (upload_summary.get("error") or "").strip().lower()
    return error in {
        "no server found.",
        "no server found",
    }


def summary_items(summary, key):
    items = summary.get(key) or []
    normalized = []

    for item in items:
        if isinstance(item, dict):
            path = item.get("path") or ""
            name = item.get("name") or (relative_finished_name(path) if path else "")
            normalized.append({"path": path, "name": name})
        else:
            path = str(item)
            normalized.append({"path": path, "name": relative_finished_name(path)})

    return normalized


def print_numbered_files(title, paths_or_items):
    log(title)

    if not paths_or_items:
        log("  (none)")
        return

    for index, item in enumerate(paths_or_items, start=1):
        if isinstance(item, dict):
            name = item.get("name") or relative_finished_name(item.get("path", ""))
        else:
            name = relative_finished_name(item)
        log(f"  {index}. {name}")


def safe_finished_path(path):
    if not path:
        return None

    abs_path = os.path.abspath(path)
    finished_abs = os.path.abspath(FINISHED_DIR)

    try:
        if os.path.commonpath([finished_abs, abs_path]) != finished_abs:
            return None
    except ValueError:
        return None

    return abs_path


def write_drive_upload_manifest(successful_items):
    files = []

    for item in successful_items:
        if isinstance(item, dict):
            raw_path = item.get("path", "")
        else:
            raw_path = str(item)

        path = safe_finished_path(raw_path)
        if not path:
            log(f"Skipped Drive manifest item outside finished/: {item}")
            continue
        if not os.path.exists(path):
            log(f"Skipped missing Drive manifest item: {path}")
            continue
        if not is_supported_audio_file(path):
            log(f"Skipped non-audio Drive manifest item: {path}")
            continue

        files.append(os.path.abspath(path))

    payload = {
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "finished_dir": FINISHED_DIR,
        "drive_folder_id": DRIVE_FOLDER_ID,
        "files": sorted(files),
    }

    with open(DRIVE_UPLOAD_MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    log(f"Wrote Drive upload/delete manifest: {DRIVE_UPLOAD_MANIFEST_FILE}")
    return files


def load_drive_upload_summary():
    if not os.path.exists(DRIVE_UPLOAD_SUMMARY_FILE):
        return {
            "attempted": [],
            "successful": [],
            "failed": [],
            "deleted": [],
            "delete_failed": [],
            "error": "Drive upload/delete summary was not created.",
        }

    try:
        with open(DRIVE_UPLOAD_SUMMARY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {
            "attempted": [],
            "successful": [],
            "failed": [],
            "deleted": [],
            "delete_failed": [],
            "error": f"Could not read Drive upload/delete summary: {e}",
        }


def print_drive_upload_summary(summary):
    attempted = summary.get("attempted") or []
    successful = summary.get("successful") or []
    failed = summary.get("failed") or []
    deleted = summary.get("deleted") or []
    delete_failed = summary.get("delete_failed") or []

    log("")
    log("GOOGLE DRIVE UPLOAD/DELETE SUMMARY")
    log("-" * 50)
    log(f"Attempted Drive upload: {len(attempted)} file(s)")
    log(f"Successfully uploaded to Drive: {len(successful)} file(s)")
    print_numbered_files("Drive upload successes:", successful)
    log(f"Failed Drive upload: {len(failed)} file(s)")
    print_numbered_files("Drive upload failures:", failed)
    log(f"Deleted from finished/ after Drive upload: {len(deleted)} file(s)")
    print_numbered_files("Deleted local files:", deleted)
    log(f"Uploaded to Drive but failed local delete: {len(delete_failed)} file(s)")
    print_numbered_files("Local delete failures:", delete_failed)

    if summary.get("error"):
        log(f"Drive upload/delete error: {summary['error']}")


def prompt_drive_upload_then_delete(drive_candidates):
    if not drive_candidates:
        log("")
        log("No finished/ files are waiting for Drive upload/delete.")
        return

    log("")
    log("Finished files already sent to Android and waiting for Drive upload/delete:")
    print_numbered_files("Pending Drive files:", drive_candidates)

    log("")
    log(f"Google Drive target folder ID: {DRIVE_FOLDER_ID}")
    log(f"Using OAuth client JSON: {DRIVE_OAUTH_CLIENT_JSON_FILE}")
    log(f"OAuth token cache: {DRIVE_OAUTH_TOKEN_FILE}")
    log("First OAuth run prints a login URL to copy into your chosen browser.")
    log("Future runs reuse the cached token.")
    log("")
    log(
        "Upload these files to Google Drive, "
        "then delete each local finished/ file only after its Drive upload succeeds?"
    )
    log("Type yes to upload to Drive and delete confirmed Drive uploads, or anything else to keep them locally.")
    answer = input("> ").strip()
    log(f"Drive upload/delete prompt answer: {answer}")

    if answer.lower() not in {"y", "yes"}:
        log("Kept successfully Android-uploaded files in finished/.")
        return

    files_to_upload = write_drive_upload_manifest(drive_candidates)
    if not files_to_upload:
        log("No valid finished/ files were available for Drive upload/delete.")
        return

    if not os.path.exists(DRIVE_OAUTH_CLIENT_JSON_FILE):
        log(f"OAuth client JSON does not exist: {DRIVE_OAUTH_CLIENT_JSON_FILE}")
        log("Create a Google Cloud OAuth Client ID with application type Desktop app,")
        log("download its JSON, and save it as google_drive_oauth_client.json inside ytb/.")
        log("The service-account JSON cannot upload into a normal My Drive shared folder because")
        log("Google service accounts do not have Drive storage quota.")
        log("Kept files in finished/.")
        return

    log("")
    log("DRIVE UPLOAD/DELETE PERMISSION GRANTED")
    log("-" * 50)
    log(f"This will upload {len(files_to_upload)} file(s) to Google Drive folder:")
    log(f"  {DRIVE_FOLDER_ID}")
    log("")
    log("After each file uploads successfully to Drive, that same local file will be deleted from:")
    log(f"  {FINISHED_DIR}")
    log("")
    log("Files that fail Drive upload will NOT be deleted.")

    remove_file_if_exists(DRIVE_UPLOAD_SUMMARY_FILE, "removing old Drive upload/delete summary")

    return_code = run_script(
        "drive_upload_then_delete.py",
        [
            "--manifest",
            DRIVE_UPLOAD_MANIFEST_FILE,
            "--oauth-client-json",
            DRIVE_OAUTH_CLIENT_JSON_FILE,
            "--oauth-token-json",
            DRIVE_OAUTH_TOKEN_FILE,
            "--folder-id",
            DRIVE_FOLDER_ID,
            "--summary-json",
            DRIVE_UPLOAD_SUMMARY_FILE,
            "--delete-after-upload",
        ],
        check=False,
    )

    summary = load_drive_upload_summary()
    print_drive_upload_summary(summary)
    remember_successful_drive_uploads(summary)

    if return_code == 0:
        log("Drive upload/delete completed.")
    else:
        log(
            "Drive upload/delete finished with errors. "
            "Any file that failed Drive upload was kept in finished/."
        )


def print_run_summary(downloaded_paths, upload_summary, upload_candidates):
    attempted = summary_items(upload_summary, "attempted")
    successful = summary_items(upload_summary, "successful")
    failed = summary_items(upload_summary, "failed")

    log("")
    log("RUN SUMMARY")
    log("-" * 50)
    log(f"Downloaded this run: {len(downloaded_paths)} file(s)")
    print_numbered_files("Downloaded files:", downloaded_paths)
    log(f"Pending finished/ files selected for Android upload: {len(upload_candidates)} file(s)")
    print_numbered_files("Pending upload files:", upload_candidates)
    log(f"Attempted Android upload: {len(attempted)} file(s)")
    log(f"Successfully uploaded to Android: {len(successful)} file(s)")
    print_numbered_files("Successful uploads:", successful)
    log(f"Failed Android uploads: {len(failed)} file(s)")
    print_numbered_files("Failed uploads:", failed)

    if upload_summary.get("error"):
        log(f"Upload error: {upload_summary['error']}")

    return successful


def main():
    prepare_output_dirs()
    open_run_log()

    exit_code = 0
    upload_summary = {"attempted": [], "successful": [], "failed": [], "error": None}
    downloaded_paths = []
    upload_candidates = []
    finished_before = snapshot_finished_files()
    upload_return_code = 0

    scripts_before_upload = [
        "thumbnail.py",
        "mediahuman.py",
        "prepforaichat.py",
        "test.py",
    ]

    try:
        for script in scripts_before_upload:
            if script == "test.py" and not has_working_audio_files():
                log("")
                log("No new working audio files found; skipping normal _working_downloads test.py.")
                moved_after_test = move_working_items_to_finished()
                downloaded_paths, upload_candidates = prepare_upload_candidates_after_test(
                    finished_before,
                    moved_after_test,
                    mark_downloaded_as_test_processed=False,
                )
                upload_summary, upload_return_code = run_upload_step(upload_candidates)
                if upload_return_code != 0:
                    if is_nonfatal_upload_failure(upload_summary):
                        log("")
                        log(
                            "No Android LAN upload URL was found. Downloaded files "
                            "were already moved to finished/, so the program will exit normally."
                        )
                    else:
                        exit_code = upload_return_code
                continue

            run_script(script)

            if script == "test.py":
                moved_after_test = move_working_items_to_finished()
                downloaded_paths, upload_candidates = prepare_upload_candidates_after_test(
                    finished_before,
                    moved_after_test,
                    mark_downloaded_as_test_processed=True,
                )
                upload_summary, upload_return_code = run_upload_step(upload_candidates)
                if upload_return_code != 0:
                    if is_nonfatal_upload_failure(upload_summary):
                        log("")
                        log(
                            "No Android LAN upload URL was found. Downloaded files "
                            "were already moved to finished/, so the program will exit normally."
                        )
                    else:
                        exit_code = upload_return_code

    except KeyboardInterrupt:
        log("")
        log("Interrupted. Moving staged files before closing...")
        exit_code = 130
    except SystemExit as e:
        exit_code = e.code if isinstance(e.code, int) else 1
    finally:
        try:
            cleanup_moved = move_working_items_to_finished()
            if cleanup_moved:
                downloaded_paths = sorted(set(downloaded_paths) | set(cleanup_moved))
                upload_candidates = sorted(
                    path
                    for path in set(upload_candidates) | set(cleanup_moved)
                    if is_supported_audio_file(path)
                )
        except Exception as e:
            log(f"ERROR: Could not move working files to {FINISHED_DIR}: {e}")
            exit_code = 1

    successful_items = print_run_summary(downloaded_paths, upload_summary, upload_candidates)
    remember_successful_uploads(successful_items)
    drive_candidates = pending_finished_drive_upload_files()

    if exit_code == 0:
        log("")
        log("ALL SCRIPTS COMPLETED SUCCESSFULLY")
    else:
        log("")
        log(f"STOPPED WITH EXIT CODE {exit_code}")

    try:
        prompt_drive_upload_then_delete(drive_candidates)
    finally:
        close_run_log()

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
