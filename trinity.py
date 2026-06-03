import datetime
import json
import os
import shutil
import subprocess
import sys

# Path to Python interpreter
PYTHON_EXE = r"C:\Users\qacer\AppData\Local\Python\pythoncore-3.14-64\python.exe"

# Folder where all scripts live
SCRIPTS_DIR = r"C:\Users\qacer\Downloads\ytb"
WORKING_DOWNLOADS_DIR = os.path.join(SCRIPTS_DIR, "_working_downloads")
FINISHED_DIR = os.path.join(SCRIPTS_DIR, "finished")
LOG_FILE = os.path.join(SCRIPTS_DIR, "trinity_run_log.txt")
DOWNLOAD_MANIFEST_FILE = os.path.join(SCRIPTS_DIR, "_last_downloaded_manifest.json")
TEST_MANIFEST_FILE = os.path.join(SCRIPTS_DIR, "_last_test_manifest.json")
UPLOAD_SUMMARY_FILE = os.path.join(SCRIPTS_DIR, "_last_upload_summary.json")
TEST_PROCESSED_HISTORY_FILE = os.path.join(SCRIPTS_DIR, "_test_processed_history.json")
UPLOADED_HISTORY_FILE = os.path.join(SCRIPTS_DIR, "_android_uploaded_history.json")
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
        shutil.move(source_path, destination_path)
        moved_paths.append(destination_path)
        log(f"Moved to finished: {destination_path}")

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


def pending_finished_test_files():
    processed_history = load_test_processed_history()
    pending = []

    for path in sorted(snapshot_finished_files()):
        if not is_supported_audio_file(path):
            continue
        if test_processed_history_key(path) not in processed_history:
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

    process = subprocess.Popen(
        [PYTHON_EXE, script_path, *args],
        cwd=SCRIPTS_DIR,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=child_env,
    )

    if process.stdout:
        for line in process.stdout:
            write_log_raw(line)

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
        log("No newly downloaded files to upload.")
        return {
            "attempted": [],
            "successful": [],
            "failed": [],
            "error": None,
        }, 0

    write_download_manifest(downloaded_paths)

    if os.path.exists(UPLOAD_SUMMARY_FILE):
        os.remove(UPLOAD_SUMMARY_FILE)

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


def process_unprocessed_finished_files():
    pending = pending_finished_test_files()
    if not pending:
        log("")
        log("No unprocessed finished/ audio files found before upload.")
        return []

    log("")
    log(f"Found {len(pending)} finished/ audio file(s) not yet processed by test.py.")
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

    process_unprocessed_finished_files()

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


def prompt_delete_successful_uploads(successful_items):
    if not successful_items:
        log("")
        log("No successful uploads; nothing to delete from finished/.")
        return

    log("")
    log("Successfully uploaded files:")
    for index, item in enumerate(successful_items, start=1):
        log(f"  {index}. {item.get('name') or relative_finished_name(item.get('path', ''))}")

    log("")
    log("Delete successfully uploaded files from finished/? Type yes to delete, or anything else to keep them.")
    answer = input("> ").strip()
    log(f"Delete prompt answer: {answer}")

    if answer.lower() not in {"y", "yes"}:
        log("Kept successfully uploaded files in finished/.")
        return

    deleted_count = 0
    for item in successful_items:
        path = safe_finished_path(item.get("path", ""))
        if not path:
            log(f"Skipped delete outside finished/: {item}")
            continue
        if not os.path.exists(path):
            log(f"Already missing, could not delete: {path}")
            continue

        os.remove(path)
        deleted_count += 1
        log(f"Deleted uploaded file: {path}")

    log(f"Deleted {deleted_count} uploaded file(s) from finished/.")


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

    if exit_code == 0:
        log("")
        log("ALL SCRIPTS COMPLETED SUCCESSFULLY")
    else:
        log("")
        log(f"STOPPED WITH EXIT CODE {exit_code}")

    try:
        prompt_delete_successful_uploads(successful_items)
    finally:
        close_run_log()

    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
