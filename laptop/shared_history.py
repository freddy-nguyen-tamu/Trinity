"""Trinity's shared Google Sheets history store.

All devices signing into the SAME Google account discover one spreadsheet
named 'Trinity Shared History'. For multiple Google accounts, share that Sheet
and optionally set TRINITY_HISTORY_SHEET_ID to its URL/ID on each device.

No local JSON history is used at runtime. Existing JSON history files are
non-destructively imported into the single 'History' worksheet as needed.
Writers journal deltas locally and bulk-append them at tool completion,
without overwriting remote records with stale snapshots.
"""

from __future__ import annotations

import atexit
import copy
import datetime as dt
import json
import os
import random
import re
import sys
import threading
import uuid
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SCRIPT_DIR
SHEET_TITLE = "Trinity Shared History"
TAB = "History"
HEADER = ["timestamp_utc", "namespace", "entry_key", "operation", "value_json"]
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]
TOKEN_NAME = "_google_sheets_history_token.json"
CONFIG_NAME = "history_sheet_config.json"
LOCK = threading.RLock()
# A process-scoped, durable write-ahead journal is NOT a history database. It
# holds only unconfirmed changes until they are appended to the shared Sheet.
_PENDING = {}                # (namespace, key) -> (operation, JSON value)
_JOURNAL_ID = f"{os.getpid()}_{uuid.uuid4().hex}"
_JOURNAL = APP_DIR / f"_pending_sheet_history_{_JOURNAL_ID}.json"
_JOURNAL_LOCK = APP_DIR / f"_pending_sheet_history_{_JOURNAL_ID}.lock"
_JOURNAL_HANDLE = None
_RECOVERY_DONE = False
_REMOTE_CACHE = None
_REMOTE_CACHE_AT = 0.0
REMOTE_CACHE_SECONDS = 20
_SERVICES = None
_SPREADSHEET_ID = None
_SNAPSHOTS = {}
_IMPORTING = False
_MIGRATED = False
_READY_TABS = set()

# A namespace is shared between each script that used the corresponding file.
LEGACY = {
    "download_history.json": ("download_history", "set"),
    "_android_uploaded_history.json": ("android_uploaded", "set"),
    "_drive_uploaded_history.json": ("drive_uploaded", "set"),
    "_test_processed_history.json": ("test_processed", "set"),
    "_lyrics_not_found_history.json": ("lyrics_not_found", "map"),
}


def _possible_locations(filename):
    """Try the project, working directory and user Downloads (both laptops)."""
    locations = [APP_DIR, SCRIPT_DIR, APP_DIR.parent, SCRIPT_DIR.parent,
                 Path.cwd(), Path.home() / "Downloads"]
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        locations.append(Path(userprofile) / "Downloads")
    visited = set()
    for folder in locations:
        path = folder / filename
        key = str(path).casefold()
        if key not in visited:
            visited.add(key)
            yield path


def _config_id():
    value = os.environ.get("TRINITY_HISTORY_SHEET_ID", "").strip()
    if not value:
        for candidate in _possible_locations(CONFIG_NAME):
            if candidate.is_file():
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
                value = str(data.get("spreadsheet_id", "")).strip()
                if value:
                    break
    if not value:
        return None
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    if match:
        value = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{15,}", value):
        raise ValueError("TRINITY_HISTORY_SHEET_ID must be a Google Sheets URL or ID")
    return value


def _credentials():
    """Use Downloads/credentials.json automatically; also accept Trinity keys."""
    service_env = os.environ.get("TRINITY_HISTORY_SERVICE_ACCOUNT_JSON", "").strip()
    client_env = os.environ.get("TRINITY_HISTORY_OAUTH_CLIENT_JSON", "").strip()
    if service_env:
        candidates = [Path(service_env).expanduser()]
    elif client_env:
        candidates = [Path(client_env).expanduser()]
    else:
        candidates = []
        for name in ("credentials.json", "google_drive_oauth_client.json"):
            candidates.extend(_possible_locations(name))
        # Google Cloud Console often downloads an OAuth client with a long
        # client_secret_*.json filename; accept that without renaming it.
        folders = [APP_DIR, SCRIPT_DIR, APP_DIR.parent, SCRIPT_DIR.parent,
                   Path.cwd(), Path.home() / "Downloads"]
        profile = os.environ.get("USERPROFILE")
        if profile:
            folders.append(Path(profile) / "Downloads")
        for folder in folders:
            for pattern in ("*client_secret*.json", "*Client_Secret*.json"):
                candidates.extend(sorted(folder.glob(pattern)))
        candidates.extend(_possible_locations("wavestack2-cbe4e94e8a01.json"))

    # Prefer an interactive OAuth Desktop client for regular Google Drive users.
    # Some machines have unrelated service-account credentials.json files; those
    # cannot create files in a personal My Drive and must not silently win.
    options = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            content = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            if service_env or client_env:
                raise ValueError(f"Invalid Google JSON credentials: {candidate}: {exc}") from exc
            continue
        if "installed" in content:
            options.append((0, candidate, content))
        elif "web" in content:
            options.append((1, candidate, content))
        elif content.get("type") == "service_account":
            options.append((2, candidate, content))
    if not options:
        raise FileNotFoundError(
            "No Google OAuth client found. Expected Downloads/credentials.json "
            "or a client_secret*.json file (Google OAuth Desktop app)."
        )
    _, client, data = min(options, key=lambda option: option[0])
    if data.get("type") == "service_account":
        # An explicitly shared existing Sheet works with a service account.
        # Personal My Drive does not grant service accounts storage quota.
        if not _config_id():
            raise RuntimeError(
                "Service-account credentials need an existing spreadsheet ID "
                "shared with that account. For personal Google Drive, use "
                "the Desktop OAuth client JSON in Downloads."
            )
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_file(str(client), scopes=SCOPES)

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    # The file stores an OAuth refresh token, not history. Never commit it.
    token_path = APP_DIR / TOKEN_NAME
    auth = None
    if token_path.is_file():
        try:
            auth = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if not set(SCOPES).issubset(set(auth.scopes or [])):
                auth = None
        except (ValueError, KeyError, json.JSONDecodeError):
            auth = None
    if auth and auth.expired and auth.refresh_token:
        try:
            auth.refresh(Request())
        except Exception:
            auth = None
    if not auth or not auth.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES)
        auth = flow.run_local_server(port=0, open_browser=True)
    token_path.write_text(auth.to_json(), encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    return auth


def _service(name):
    global _SERVICES
    if _SERVICES is None:
        from googleapiclient.discovery import build
        creds = _credentials()
        _SERVICES = {
            "sheets": build("sheets", "v4", credentials=creds, cache_discovery=False),
            "drive": build("drive", "v3", credentials=creds, cache_discovery=False),
        }
    return _SERVICES[name]


def _execute(request):
    return request.execute(num_retries=5)


def _sheet_id():
    global _SPREADSHEET_ID
    if _SPREADSHEET_ID:
        return _SPREADSHEET_ID
    explicit = _config_id()
    if explicit:
        _SPREADSHEET_ID = explicit
        return explicit

    # Drive discovery makes a shared ID unnecessary when all laptops authenticate
    # as the same Google user. No machine-specific local pointer is required.
    drive = _service("drive")
    escaped = SHEET_TITLE.replace("'", "\\'")
    query = ("name = '" + escaped + "' and trashed = false and "
             "mimeType = 'application/vnd.google-apps.spreadsheet'")
    found = []
    page_token = None
    while True:
        listing = _execute(drive.files().list(
            q=query, fields="nextPageToken,files(id,name,createdTime)",
            pageSize=1000, pageToken=page_token,
        ))
        found.extend(listing.get("files", []))
        page_token = listing.get("nextPageToken")
        if not page_token:
            break
    if len(found) > 1:
        raise RuntimeError(
            f"Found {len(found)} spreadsheets named '{SHEET_TITLE}'. "
            "Use TRINITY_HISTORY_SHEET_ID to choose one; refusing to split history."
        )
    if found:
        _SPREADSHEET_ID = found[0]["id"]
        return _SPREADSHEET_ID

    # Creating through Sheets API works with the spreadsheet OAuth scope.
    sheet = _execute(_service("sheets").spreadsheets().create(body={
        "properties": {"title": SHEET_TITLE},
        "sheets": [{"properties": {"title": TAB}}],
    }, fields="spreadsheetId"))
    created = sheet["spreadsheetId"]
    # Detect the exceptional concurrent-first-start race and fail rather than
    # silently maintaining two diverging histories.
    siblings = _execute(drive.files().list(
        q=query, fields="nextPageToken,files(id)", pageSize=1000
    )).get("files", [])
    if len(siblings) > 1:
        raise RuntimeError(
            f"Two devices created '{SHEET_TITLE}' concurrently. "
            "Choose the same Sheet ID on all devices using TRINITY_HISTORY_SHEET_ID."
        )
    _SPREADSHEET_ID = created
    print(f"Created shared Trinity history Sheet: "
          f"https://docs.google.com/spreadsheets/d/{created}/edit", flush=True)
    return created


def _ensure_tab(sid):
    if sid in _READY_TABS:
        return
    sheets = _service("sheets")
    meta = _execute(sheets.spreadsheets().get(
        spreadsheetId=sid, fields="sheets(properties(title))"))
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if TAB not in titles:
        try:
            _execute(sheets.spreadsheets().batchUpdate(
                spreadsheetId=sid, body={"requests": [
                    {"addSheet": {"properties": {"title": TAB}}}
                ]}))
        except Exception:
            meta = _execute(sheets.spreadsheets().get(
                spreadsheetId=sid, fields="sheets(properties(title))"))
            if TAB not in [s["properties"]["title"] for s in meta.get("sheets", [])]:
                raise
    first = _execute(sheets.spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{TAB}'!A1:E1")).get("values", [])
    if not first:
        _execute(sheets.spreadsheets().values().update(
            spreadsheetId=sid, range=f"'{TAB}'!A1:E1",
            valueInputOption="RAW", body={"values": [HEADER]}))
    elif first[0] != HEADER:
        raise RuntimeError(f"The existing '{TAB}' tab is not a Trinity history tab; "
                           "refusing to overwrite its data")
    _READY_TABS.add(sid)


def _read_remote(force=False):
    global _REMOTE_CACHE, _REMOTE_CACHE_AT
    if not force and _REMOTE_CACHE is not None and time.monotonic() - _REMOTE_CACHE_AT < REMOTE_CACHE_SECONDS:
        return copy.deepcopy(_REMOTE_CACHE)
    sid = _sheet_id()
    _ensure_tab(sid)
    rows = _execute(_service("sheets").spreadsheets().values().get(
        spreadsheetId=sid, range=f"'{TAB}'!A2:E",
        valueRenderOption="UNFORMATTED_VALUE"),).get("values", [])
    records, seen = {}, {}
    for row_number, row in enumerate(rows, start=2):
        if not row or len(row) < 4:
            continue
        namespace, key, operation = (str(item) for item in row[1:4])
        if not namespace or not key:
            continue
        seen.setdefault(namespace, set()).add(key)
        group = records.setdefault(namespace, {})
        if operation == "delete":
            group.pop(key, None)
        elif operation == "put":
            try:
                group[key] = json.loads(str(row[4]))
            except (IndexError, ValueError) as exc:
                raise ValueError(f"Invalid history JSON at row {row_number}: {exc}") from exc
        else:
            raise ValueError(f"Invalid history operation at row {row_number}: {operation}")
    _REMOTE_CACHE = copy.deepcopy((records, seen))
    _REMOTE_CACHE_AT = time.monotonic()
    return records, seen


def _append(namespace, changes):
    """Upload a legacy/import transaction as size-bounded bulk rows."""
    if not changes:
        return 0
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rows = [_make_row(namespace, key, op, value, timestamp)
            for key, op, value in changes]
    _append_bulk_rows(rows)
    return len(rows)


def _make_row(namespace, key, op, value, timestamp=None):
    if timestamp is None:
        timestamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if op == "put" else ""
    if len(payload) > 45000:
        raise ValueError(f"History record too large: {namespace}:{key}")
    return [timestamp, str(namespace), str(key), op, payload]



# A one-time playlist import can contain tens of thousands of rows. Google
# recommends keeping Sheets request bodies around or below 2 MB. One append
# request for ALL history namespaces avoids the 60 writes/minute/user quota
# that was hit by calling save_set five times with 200-row writes.
BULK_MAX_BODY_BYTES = 1_900_000


def _bulk_row_bytes(row):
    return len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


def _row_already_applied(row, remote):
    """Check the effective current value, including updates and tombstones."""
    _, namespace, key, op, payload = row
    group = remote.get(namespace, {})
    if op == "delete":
        return key not in group
    return key in group and group[key] == json.loads(payload)


def _append_bulk_rows(rows, max_body_bytes=BULK_MAX_BODY_BYTES):
    """Append mixed-namespace rows in a few <2 MB API requests.

    A failed quota request is retried after increasing delays. For ambiguous
    server errors the remote sheet is consulted before attempting another
    write, preventing duplicate inserts when the response was lost.
    """
    if not rows:
        return 0
    sid = _sheet_id()
    batches = []
    current = []
    current_size = len('{"values":[]}')
    for row in rows:
        row_size = _bulk_row_bytes(row)
        if row_size + len('{"values":[]}') > max_body_bytes:
            raise ValueError("One history row exceeds the safe Sheets request size")
        if current and current_size + row_size > max_body_bytes:
            batches.append(current)
            current = []
            current_size = len('{"values":[]}')
        current.append(row)
        current_size += row_size
    if current:
        batches.append(current)

    print(f"Uploading {len(rows):,} history rows in {len(batches)} Google Sheets "
          f"write request(s) (<= {max_body_bytes / 1_000_000:.1f} MB each)...", flush=True)
    sheets = _service("sheets")
    for batch_number, original in enumerate(batches, 1):
        pending = list(original)
        for attempt in range(8):
            if not pending:
                break
            try:
                # Disable the library's hidden retries; we handle quota delays
                # ourselves so the importer can show useful progress.
                request = sheets.spreadsheets().values().append(
                    spreadsheetId=sid,
                    range=f"'{TAB}'!A:E",
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": pending},
                )
                request.execute(num_retries=0)
                pending = []
                break
            except Exception as exc:
                from googleapiclient.errors import HttpError
                if not isinstance(exc, HttpError) or exc.resp.status not in (429, 500, 502, 503, 504):
                    raise
                status = exc.resp.status
                if status != 429:
                    # Some upstream failures can happen AFTER an append commits.
                    # Read the Sheet before retrying to avoid duplicate rows.
                    try:
                        remote_values, _ = _read_remote(force=True)
                        pending = [r for r in pending if not _row_already_applied(r, remote_values)]
                    except Exception:
                        # Without an authoritative read, don't blindly duplicate.
                        raise RuntimeError(
                            "Google failed while confirming whether a bulk append "
                            "committed. Rerun the importer to safely resume."
                        ) from exc
                if not pending:
                    break
                if attempt == 7:
                    raise RuntimeError(
                        f"Google Sheets HTTP {status} persisted after 8 attempts. "
                        "Rerun this command; already-written IDs are skipped."
                    ) from exc
                header_delay = exc.resp.get("retry-after") if hasattr(exc.resp, "get") else None
                try:
                    advised = float(header_delay) if header_delay else 0
                except ValueError:
                    advised = 0
                delay = min(64, max(advised, 2 ** (attempt + 1))) + random.random()
                print(f"Google Sheets HTTP {status}; retrying the bulk write "
                      f"after {delay:.1f}s (attempt {attempt + 2}/8)...", flush=True)
                time.sleep(delay)
        if pending:
            raise RuntimeError("Bulk upload did not finish; rerun to safely resume")
        print(f"Bulk write {batch_number}/{len(batches)} complete.", flush=True)
    global _REMOTE_CACHE
    _REMOTE_CACHE = None
    return len(batches)


def merge_many_histories(entries_by_namespace):
    """Read once, append only missing keys from all namespaces, verify once.

    Unlike save_set/save_map, this operation never interprets absent values as
    deletions. Previously deleted keys are NOT resurrected. It is safe to rerun
    after an interrupted quota-limited import.

    Args:
        entries_by_namespace: mapping namespace -> mapping key -> JSON value.
    Returns:
        (per_namespace_stats, number_of_write_requests)
    """
    allowed = {namespace for namespace, _ in LEGACY.values()}
    if not isinstance(entries_by_namespace, dict):
        raise TypeError("Expected namespace-to-records mapping")
    for namespace, entries in entries_by_namespace.items():
        if namespace not in allowed:
            raise ValueError(f"Unknown history namespace: {namespace}")
        if not isinstance(entries, dict):
            raise TypeError(f"{namespace} must be a mapping of keys to values")

    with LOCK:
        remote, ever_seen = _read_remote(force=True)  # ONE remote scan for all namespaces
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        additions, stats = [], {}
        for namespace, entries in entries_by_namespace.items():
            existing = remote.get(namespace, {})
            seen = ever_seen.get(namespace, set())
            already_present = 0
            deleted_before = 0
            missing = 0
            for key, value in sorted(entries.items()):
                key = str(key)
                if key in existing:
                    already_present += 1
                    continue
                if key in seen:
                    deleted_before += 1
                    continue
                payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                if len(payload) > 45000:
                    raise ValueError(f"History record too large: {namespace}:{key}")
                additions.append([now, namespace, key, "put", payload])
                missing += 1
            stats[namespace] = {
                "already_present": already_present,
                "previously_deleted": deleted_before,
                "added": missing,
            }

        for namespace, info in stats.items():
            print(f"{namespace}: {info['already_present']:,} already present, "
                  f"{info['added']:,} to insert" +
                  (f", {info['previously_deleted']:,} deliberately deleted (left deleted)"
                   if info['previously_deleted'] else ""), flush=True)

        writes = _append_bulk_rows(additions)
        # A single authoritative post-write read verifies ALL FIVE namespaces,
        # rather than five independent load requests per invocation.
        if additions:
            final, _ = _read_remote(force=True)
            missing = [(row[1], row[2]) for row in additions
                       if row[2] not in final.get(row[1], {})]
            if missing:
                raise RuntimeError(
                    f"Bulk verification failed for {len(missing)} rows "
                    f"(e.g. {missing[0]}); rerun safely to repair missing entries."
                )
        return stats, writes


def _legacy_dirs():
    folders = [APP_DIR, SCRIPT_DIR, APP_DIR.parent, SCRIPT_DIR.parent,
               Path.cwd(), Path.home() / "Downloads"]
    profile = os.environ.get("USERPROFILE")
    if profile:
        folders.append(Path(profile) / "Downloads")
    other = os.environ.get("TRINITY_HISTORY_IMPORT_DIRS", "")
    for part in other.split(os.pathsep):
        if part.strip():
            folders.append(Path(part.strip()).expanduser())
    paths = {}
    for folder in folders:
        for name in LEGACY:
            path = folder / name
            if path.is_file():
                paths[str(path.resolve()).casefold()] = path
    return list(paths.values())


def _migrate_legacy(records, seen):
    """Merge legacy JSON files in ONE bulk Sheets write, without resurrection."""
    global _MIGRATED
    if _MIGRATED:
        return records, seen
    pending_rows = []
    pending_by_namespace = {}
    imported = []
    for path in _legacy_dirs():
        namespace, kind = LEGACY[path.name]
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if kind == "set":
                if not isinstance(data, list):
                    raise TypeError("expected JSON list")
                entries = {str(item): True for item in data}
            else:
                if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
                    raise TypeError("expected JSON object with an items mapping")
                entries = {str(key): value for key, value in data["items"].items()}
                for value in data["items"].values():
                    if not isinstance(value, dict):
                        continue
                    video = str(value.get("video_id") or "").strip()
                    title = str(value.get("title") or "").strip().casefold()
                    artist = str(value.get("artist") or "").strip().casefold()
                    if video:
                        portable = "youtube:" + video
                    elif title and artist:
                        portable = "song:" + json.dumps([title, artist], ensure_ascii=False,
                                                         separators=(",", ":"))
                    else:
                        continue
                    entries.setdefault(portable, value)
        except Exception as exc:
            raise RuntimeError(f"Cannot migrate {path}; no local data was changed: {exc}") from exc
        stage = pending_by_namespace.setdefault(namespace, {})
        count = 0
        for key, value in sorted(entries.items()):
            if key in seen.get(namespace, set()) or key in stage:
                continue
            stage[key] = value
            pending_rows.append(_make_row(namespace, key, "put", value))
            count += 1
        if count:
            imported.append((path, namespace, count))
    if pending_rows:
        _append_bulk_rows(pending_rows)
        for namespace, entries in pending_by_namespace.items():
            records.setdefault(namespace, {}).update(entries)
            seen.setdefault(namespace, set()).update(entries)
        for path, namespace, count in imported:
            print(f"Imported {count} previous {namespace} entries from {path}", flush=True)
    _MIGRATED = True
    return records, seen


def _acquire_file_lock(path, blocking=False):
    """Hold an OS file lock so a second Trinity process cannot steal live work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return handle
    except (OSError, IOError):
        handle.close()
        return None


def _release_file_lock(handle):
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _ensure_journal_lock():
    global _JOURNAL_HANDLE
    if _JOURNAL_HANDLE is None:
        _JOURNAL_HANDLE = _acquire_file_lock(_JOURNAL_LOCK, blocking=True)
        if _JOURNAL_HANDLE is None:
            raise RuntimeError("Could not lock Trinity's pending Google history journal")


def _append_journal(namespace, changes):
    """O(number of new changes), not O(total historical changes) per save."""
    if not changes:
        return
    _ensure_journal_lock()
    with _JOURNAL.open("a", encoding="utf-8", newline="\n") as stream:
        for key, op, value in changes:
            stream.write(json.dumps([namespace, str(key), op, value],
                                    ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _persist_pending():
    """Compact recovered orphan events atomically; delete after confirmation."""
    if not _PENDING:
        _JOURNAL.unlink(missing_ok=True)
        return
    _ensure_journal_lock()
    tmp = _JOURNAL.with_name(_JOURNAL.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            for (namespace, key), (op, value) in _PENDING.items():
                stream.write(json.dumps([namespace, key, op, value],
                                        ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, _JOURNAL)
    finally:
        tmp.unlink(missing_ok=True)


def _recover_orphans():
    """Recover writes from crashed Trinity processes, skipping live processes."""
    global _RECOVERY_DONE
    if _RECOVERY_DONE:
        return
    _RECOVERY_DONE = True
    try:
        for path in sorted(APP_DIR.glob("_pending_sheet_history_*.json")):
            if path == _JOURNAL:
                continue
            lock_path = path.with_suffix(".lock")
            handle = _acquire_file_lock(lock_path)
            if handle is None:  # another currently-running script owns it
                continue
            try:
                raw_lines = path.read_text(encoding="utf-8").splitlines()
                changes = []
                for position, line in enumerate(raw_lines):
                    try:
                        record = json.loads(line)
                        if not isinstance(record, list) or len(record) != 4:
                            raise ValueError("Malformed history journal record")
                        changes.append(record)
                    except ValueError:
                        if position != len(raw_lines) - 1:
                            raise ValueError(f"Corrupt pending history journal: {path}")
                        print(f"Ignoring an incomplete final journal entry in {path}", file=sys.stderr)
                for namespace, key, op, value in changes:
                    if namespace not in {v[0] for v in LEGACY.values()} or op not in ("put", "delete"):
                        raise ValueError(f"Invalid pending history change in {path}")
                original = dict(_PENDING)
                try:
                    for namespace, key, op, value in changes:
                        _PENDING.setdefault((namespace, str(key)), (op, value))
                    _persist_pending()  # durable before deleting orphan
                except Exception:
                    _PENDING.clear()
                    _PENDING.update(original)
                    raise
                path.unlink()
                if changes:
                    print(f"Recovered {len(changes)} queued history changes from {path.name}", flush=True)
            finally:
                _release_file_lock(handle)
                try:
                    if not path.exists():
                        lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:
        _RECOVERY_DONE = False  # retry on next call; never silently lose the journal
        raise


def _overlay_pending(records):
    for (namespace, key), (op, value) in _PENDING.items():
        group = records.setdefault(namespace, {})
        if op == "put":
            group[key] = value
        else:
            group.pop(key, None)
    return records


def _records():
    _recover_orphans()
    records, seen = _read_remote()
    records, seen = _migrate_legacy(records, seen)
    return _overlay_pending(records), seen


def _queue_changes(namespace, changes):
    if not changes:
        return 0
    _recover_orphans()
    previous = dict(_PENDING)
    try:
        for key, op, value in changes:
            _make_row(namespace, key, op, value)  # validate before journaling
            _PENDING[(namespace, str(key))] = (op, value)
        _append_journal(namespace, changes)
    except Exception:
        _PENDING.clear()
        _PENDING.update(previous)
        raise
    return len(changes)


def flush_pending():
    """Bulk-write all queued histories once; call at program/phase end.

    Safely rerunnable after a failed write. Data remains on disk until a
    successful verified append. Other processes' active journals are skipped.
    """
    global _JOURNAL_HANDLE
    with LOCK:
        _recover_orphans()
        if not _PENDING:
            return 0
        remote, _ = _read_remote(force=True)
        changes = [_make_row(ns, key, op, value)
                   for (ns, key), (op, value) in _PENDING.items()]
        missing = [row for row in changes if not _row_already_applied(row, remote)]
        if missing:
            writes = _append_bulk_rows(missing)
            final, _ = _read_remote(force=True)
            failures = [row for row in changes if not _row_already_applied(row, final)]
            if failures:
                raise RuntimeError(f"Google Sheets did not confirm {len(failures)} queued "
                                   "history changes; local pending journal preserved")
        else:
            writes = 0
        _PENDING.clear()
        _persist_pending()  # delete durable journal only after confirmation
        if _JOURNAL_HANDLE is not None:
            _release_file_lock(_JOURNAL_HANDLE)
            _JOURNAL_HANDLE = None
            try:
                _JOURNAL_LOCK.unlink(missing_ok=True)
            except OSError:
                pass
        return writes


def _flush_on_exit():
    if _PENDING:
        try:
            flush_pending()
        except Exception as exc:
            print(f"TRINITY HISTORY SYNC FAILED: {exc}. "
                  f"Changes remain in {_JOURNAL} for the next run.", file=sys.stderr)


atexit.register(_flush_on_exit)


def load_set(namespace):
    with LOCK:
        records, _ = _records()
        result = set(records.get(namespace, {}))
        _SNAPSHOTS[namespace] = {key: True for key in result}
        return result


def save_set(namespace, values):
    wanted = {str(item) for item in values}
    with LOCK:
        if namespace not in _SNAPSHOTS:
            raise RuntimeError(f"Load {namespace} from Google Sheets before saving")
        previous = set(_SNAPSHOTS[namespace])
        changes = [(key, "put", True) for key in sorted(wanted - previous)]
        changes += [(key, "delete", None) for key in sorted(previous - wanted)]
        count = _queue_changes(namespace, changes)
        _SNAPSHOTS[namespace] = {key: True for key in wanted}
        return count


def load_map(namespace):
    with LOCK:
        records, _ = _records()
        result = json.loads(json.dumps(records.get(namespace, {}), ensure_ascii=False))
        _SNAPSHOTS[namespace] = json.loads(json.dumps(result, ensure_ascii=False))
        return result


def save_map(namespace, values):
    if not isinstance(values, dict):
        raise TypeError("Expected a dictionary")
    wanted = {str(key): value for key, value in values.items()}
    with LOCK:
        if namespace not in _SNAPSHOTS:
            raise RuntimeError(f"Load {namespace} from Google Sheets before saving")
        previous = _SNAPSHOTS[namespace]
        changes = [(key, "put", val) for key, val in sorted(wanted.items())
                   if key not in previous or previous[key] != val]
        changes += [(key, "delete", None) for key in sorted(set(previous) - set(wanted))]
        count = _queue_changes(namespace, changes)
        _SNAPSHOTS[namespace] = json.loads(json.dumps(wanted, ensure_ascii=False))
        return count


def migrate_set(namespace, values):
    with LOCK:
        _, seen = _records()
        changes = [(key, "put", True) for key in sorted({str(v) for v in values})
                   if key not in seen.get(namespace, set())]
        return _append(namespace, changes)


def migrate_map(namespace, values):
    # Optional manual imports must produce the same cross-device song aliases
    # as the automatic import from the default laptop/ directory.
    entries = {str(key): val for key, val in values.items()}
    if namespace == "lyrics_not_found":
        for value in values.values():
            if not isinstance(value, dict):
                continue
            video = str(value.get("video_id") or "").strip()
            title = str(value.get("title") or "").strip().casefold()
            artist = str(value.get("artist") or "").strip().casefold()
            if video:
                portable = "youtube:" + video
            elif title and artist:
                portable = "song:" + json.dumps([title, artist], ensure_ascii=False,
                                                 separators=(",", ":"))
            else:
                continue
            entries.setdefault(portable, value)
    with LOCK:
        _, seen = _records()
        changes = [(key, "put", val) for key, val in sorted(entries.items())
                   if key not in seen.get(namespace, set())]
        return _append(namespace, changes)


def check_connection():
    with LOCK:
        records, _ = _records()
        return {k: len(v) for k, v in sorted(records.items())}


def shared_sheet_url():
    with LOCK:
        return f"https://docs.google.com/spreadsheets/d/{_sheet_id()}/edit"
