
#!/usr/bin/env python3
"""Verified Drive uploads with browser account rotation and crash-safe Sheet history.

No service-account keys. Reuse an existing installed-app OAuth client (including
metadata in Trinity's Sheets OAuth token) to authorize each Drive account.
Google Drive for desktop staging is intentionally NOT used: it cannot establish
server-side completion, storage-quota errors, or safe deletion.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import hashlib
import os
import sys
import time
from pathlib import Path

from upload_identity import calculate_file_hash, upload_history_key, history_upload_match
from shared_history import load_set, save_set, flush_pending
from drive_upload_then_delete import send_file_to_recycle_bin

ROOT = Path(__file__).resolve().parent
FINISHED = ROOT / 'finished'
TOKEN_PATH = ROOT / '_google_drive_oauth_token.json'
PAUSE_PATH = ROOT / '_drive_storage_full.json'
SHEETS_TOKEN = ROOT / '_google_sheets_history_token.json'
ALLOWED_TYPES = {'.mp3', '.m4a', '.mp4', '.flac', '.ogg', '.opus', '.wav', '.wave', '.aiff', '.aif', '.aac'}
SCOPES = ['https://www.googleapis.com/auth/drive']
# Google API's default resumable chunk is the ENTIRE file (-1). A smaller
# chunk avoids waiting for a single large MP3 upload response.
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
DRIVE_HTTP_TIMEOUT_SECONDS = 300
DRIVE_FILE_RETRIES = 3  # Additional fresh sessions after the initial attempt.
# History receipts are persisted locally after EACH confirmed Drive upload,
# but committed to Google Sheets together to avoid per-MP3 API reads/writes.
DRIVE_HISTORY_BATCH_SIZE = 100
# Reserved IDs are an on-disk idempotency journal, separate from shared Sheet
# history. A lost HTTP response can NOT cause the same media to be recreated
# with a different Drive ID. This file is private runtime state, not a backup.
RESERVED_IDS_PATH = ROOT / '_drive_reserved_upload_ids.json'
ID_BATCH_SIZE = 100
# New reservations in THIS process have never been uploaded; avoid a needless
# files.get (404) for each, while probing saved reservations after restart.
RECENTLY_RESERVED_IDS = set()


class IndeterminateDriveUpload(RuntimeError):
    """Google may have committed the upload; never allocate a different ID."""


def _ledger_key(folder_id, item):
    return hashlib.sha256((str(folder_id) + '\0' + item['history_key']).encode('utf-8')).hexdigest()


def _load_reserved_ids():
    data = load_json(RESERVED_IDS_PATH, {'version': 1, 'entries': {}})
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('entries'), dict):
        raise RuntimeError('Drive idempotency journal is damaged; retaining local files to prevent duplicates: ' + str(RESERVED_IDS_PATH))
    return data


def reserved_drive_id(folder_id, item):
    record = _load_reserved_ids()['entries'].get(_ledger_key(folder_id, item))
    return record.get('id') if isinstance(record, dict) else None


def reserve_drive_ids(service, folder_id, items, account):
    """Reserve IDs for multiple MP3s in ONE API call and fsync before upload.

    Existing IDs are NEVER replaced after a timeout. Reservations survive
    crashes and reuse across different attempts and process launches.
    """
    data = _load_reserved_ids()
    missing = []
    result = {}
    missing_keys = set()
    for item in items:
        key = _ledger_key(folder_id, item)
        record = data['entries'].get(key)
        if record is not None:
            if (record.get('name') != item['name'] or
                    record.get('size') != item['size'] or
                    record.get('md5') != item['md5'] or not record.get('id')):
                raise RuntimeError('Drive idempotency journal identity mismatch: ' + item['name'])
            result[key] = record['id']
        elif key not in result and key not in missing_keys:
            missing.append((key, item))
            missing_keys.add(key)
    for start in range(0, len(missing), ID_BATCH_SIZE):
        group = missing[start:start + ID_BATCH_SIZE]
        # Google Drive generates up to 1000 unique IDs per request; request a
        # small batch to avoid per-file API calls and unused reservations.
        response = service.files().generateIds(count=len(group), space='drive', type='files').execute(num_retries=2)
        ids = response.get('ids', [])
        if len(ids) != len(group) or len(set(ids)) != len(ids) or not all(isinstance(i,str) and i for i in ids):
            raise RuntimeError('Drive returned invalid pre-generated file IDs; no upload started')
        for (key, item), file_id in zip(group, ids):
            data['entries'][key] = {
                'id': file_id, 'name': item['name'], 'size': item['size'],
                'md5': item['md5'], 'account_when_reserved': account,
            }
            result[key] = file_id
            RECENTLY_RESERVED_IDS.add(file_id)
        # A crash between generateIds and fsync wastes reserved IDs, never
        # creates duplicates because no media has been uploaded yet.
        atomic_json(RESERVED_IDS_PATH, data)
    return result


def forget_reserved_id(folder_id, item):
    # Only used AFTER both Sheet history receipts have been confirmed and the
    # local original is recycled; never clear a still-uncertain reservation.
    data = _load_reserved_ids()
    if data['entries'].pop(_ledger_key(folder_id, item), None) is not None:
        atomic_json(RESERVED_IDS_PATH, data)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as output:
        json.dump(value, output, indent=2, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(tmp, path)


def load_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding='utf-8-sig'))


def safe_source(path):
    p = Path(path).resolve()
    if not p.is_file() or FINISHED.resolve() not in p.parents or p.suffix.lower() not in ALLOWED_TYPES:
        raise ValueError('Invalid/unsafe source (must be an audio file inside finished/): ' + str(p))
    return p


def load_manifest(path):
    data = load_json(path)
    items = data.get('files', []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError('Manifest files must be a list')
    out, seen = [], set()
    for item in items:
        p = safe_source(item.get('path') if isinstance(item, dict) else item)
        if str(p).casefold() not in seen:
            out.append(p)
            seen.add(str(p).casefold())
    return out


def item_data(path):
    path = safe_source(path)
    return {
        'path': str(path), 'name': path.name, 'size': path.stat().st_size,
        'md5': calculate_file_hash(path, 'md5'),
        'history_key': upload_history_key(path, base_dir=FINISHED),
    }


def from_client_json(data):
    if not isinstance(data, dict):
        return None
    item = data.get('installed') or data.get('web')
    if not isinstance(item, dict) or not item.get('client_id'):
        return None
    # Native / Desktop OAuth application only. Web clients can require a
    # registered redirect URI and cannot be assumed to work with loopback.
    if 'installed' not in data:
        return None
    return {'installed': {
        'client_id': item['client_id'],
        'client_secret': item.get('client_secret', ''),
        'auth_uri': item.get('auth_uri', 'https://accounts.google.com/o/oauth2/auth'),
        'token_uri': item.get('token_uri', 'https://oauth2.googleapis.com/token'),
        'redirect_uris': ['http://localhost'],
    }}


def oauth_app(client_json=None):
    # Prefer the existing, explicitly configured Drive Desktop OAuth client.
    # Do not accidentally use a different restricted Sheets OAuth application.
    candidates = []
    if client_json:
        candidates.append(Path(client_json).expanduser())
    else:
        candidates.append(ROOT / 'google_drive_oauth_client.json')

    for path in candidates:
        if not path.is_file():
            if client_json:
                raise FileNotFoundError('Drive OAuth client JSON not found: ' + str(path))
            continue
        config = from_client_json(load_json(path))
        if config:
            return config
        raise ValueError('Drive OAuth client must be a Desktop/installed application: ' + str(path))

    # Fallback for installations that only have the Sheets Desktop OAuth app.
    token = load_json(SHEETS_TOKEN, {})
    if isinstance(token, dict) and token.get('client_id'):
        return {'installed': {
            'client_id': token['client_id'],
            'client_secret': token.get('client_secret', ''),
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': ['http://localhost'],
        }}
    locations = [ROOT, ROOT.parent, Path.cwd(), Path.home() / 'Downloads']
    profile = os.getenv('USERPROFILE')
    if profile:
        locations.append(Path(profile) / 'Downloads')
    name_patterns = ['credentials.json', '*client_secret*.json']
    for folder in locations:
        for pat in name_patterns:
            for path in folder.glob(pat):
                try:
                    config = from_client_json(load_json(path))
                except (ValueError, OSError):
                    continue
                if config:
                    return config
    raise RuntimeError(
        'No installed-app Google OAuth client configuration found. '
        'Browser authorization requires a Desktop OAuth client. '
        'Your Google Sheets credentials and token are separate.'
    )


def new_drive_service(credentials):
    """Construct an independent Drive transport, with no pooled sockets reused.

    Recreating the discovery client alone is insufficient when an old httplib2
    transport is passed back in. Each attempt must own a new HTTP connection
    pool, but the same approved OAuth credentials/account can be reused.
    """
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=DRIVE_HTTP_TIMEOUT_SECONDS))
    return build('drive', 'v3', http=http, cache_discovery=False)


def close_drive_connection(service):
    """Discard potentially broken pooled sockets without revoking OAuth."""
    authorized_http = getattr(service, '_http', None)
    transport = getattr(authorized_http, 'http', None)
    for candidate in (transport, authorized_http):
        close = getattr(candidate, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
            break


def authenticate(force=False, token_path=None, client_json=None):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = Path(token_path or TOKEN_PATH)
    creds = None
    if not force and token_path.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if not set(SCOPES).issubset(set(creds.scopes or [])):
                creds = None
            if creds and expired_and_refreshable(creds):
                creds.refresh(Request())
        except Exception as error:
            print('Cached Drive login needs reauthorization: ' + str(error), flush=True)
            creds = None
    if not creds or not creds.valid:
        config = oauth_app(client_json)
        flow = InstalledAppFlow.from_client_config(config, SCOPES)
        print('Select a Google account with AVAILABLE storage and Editor access to the same Drive folder.', flush=True)
        print('A browser sign-in should open; the authorization URL is also printed below.', flush=True)
        creds = flow.run_local_server(
            port=0, open_browser=True,
            prompt='select_account consent',
            authorization_prompt_message='Google Drive account-selection URL: {url}',
        )
    # Do not cache the newly selected account until its identity and folder
    # permissions have been verified by the caller.
    service = new_drive_service(creds)
    about = service.about().get(fields='user(emailAddress),storageQuota(limit,usage)').execute(num_retries=2)
    email = str((about.get('user') or {}).get('emailAddress') or '(unknown account)')
    return service, email, creds, about.get('storageQuota') or {}


def expired_and_refreshable(credentials):
    return bool(credentials.expired and credentials.refresh_token)


def save_accepted_drive_token(token_path, credentials):
    token_path = Path(token_path)
    atomic_json(token_path, json.loads(credentials.to_json()))
    try:
        token_path.chmod(0o600)
    except OSError:
        pass


def account_is_full(quota):
    try:
        limit = int(quota.get('limit') or 0)
        usage = int(quota.get('usage') or 0)
        return limit > 0 and usage >= limit
    except (ValueError, TypeError, AttributeError):
        return False


def error_reason(exc):
    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError):
            data = json.loads(exc.content.decode('utf-8', errors='replace'))
            reasons = [str(e.get('reason', '')) for e in data.get('error', {}).get('errors', [])]
            return reasons[0] if reasons else ''
    except (ImportError, TypeError, ValueError, AttributeError):
        pass
    return ''


def is_storage_full(exc):
    # Do NOT conflate API rate limits, permissions, or daily upload limits
    # with actual account storage exhaustion.
    return error_reason(exc) == 'storageQuotaExceeded'


def is_retryable_drive_failure(exc):
    """Retry connection issues and temporary server/rate-limit responses.

    Never retry permanent authorization, invalid data, quota exhaustion, or
    checksum/folder validation failures as though they were network failures.
    """
    if is_storage_full(exc):
        return False
    if isinstance(exc, IndeterminateDriveUpload):
        return True
    try:
        from googleapiclient.errors import HttpError
        if isinstance(exc, HttpError):
            status = int(getattr(exc.resp, 'status', 0))
            return status in (408, 429, 500, 502, 503, 504) or (
                status == 403 and error_reason(exc) in (
                    'rateLimitExceeded', 'userRateLimitExceeded', 'backendError',
                )
            )
    except ImportError:
        pass
    if isinstance(exc, (TimeoutError, ConnectionError, BrokenPipeError)):
        return True
    message = str(exc).casefold()
    return any(marker in message for marker in (
        'read operation timed out', 'timed out', 'connection reset',
        'connection aborted', 'connection refused', 'remote end closed',
        'server disconnected', 'incomplete read', 'broken pipe',
        'temporarily unavailable', 'ssl eof occurred', 'getaddrinfo failed',
        'unable to find the server at',
    ))


def ensure_drive_file(service, credentials, folder_id, path, item, *, account=None,
                      reservation_id=None, inventory=None):
    """Every upload attempt gets a clean HTTP session; every create uses ONE ID.

    A lost response may hide a successful upload. Reusing Google's pre-generated
    ID makes a second create idempotent (409 rather than duplicate). A 409 is
    success ONLY after the exact uploaded contents are verified by Drive.
    """
    reserved_id = reservation_id or reserved_drive_id(folder_id, item)
    for attempt in range(DRIVE_FILE_RETRIES + 1):
        if attempt:
            service = new_drive_service(credentials)
        try:
            # Bulk inventory is the initial authoritative folder snapshot;
            # retries query Drive anew because late completions may appear.
            existing = (matching_inventory_item(inventory, item, folder_id)
                        if attempt == 0 and inventory is not None
                        else find_identical(service, folder_id, item))
            if existing is not None:
                print('[ALREADY ON DRIVE; EXACT SIZE + MD5] ' + item['name'], flush=True)
                return service, existing, True
            if reserved_id and (attempt or reserved_id not in RECENTLY_RESERVED_IDS):
                # A reserved ID is stronger than a filename query: Drive can
                # finish the POST before the response reaches the client.
                try:
                    exact = verified_remote_entry(service, {'id': reserved_id}, item, folder_id)
                except Exception as check_error:
                    if not is_drive_not_found(check_error):
                        raise
                else:
                    print('[FOUND COMPLETED DRIVE UPLOAD BY RESERVED ID] ' + item['name'], flush=True)
                    return service, exact, True
            if not reserved_id:
                reserved_id = reserve_drive_ids(service, folder_id, [item], account)[_ledger_key(folder_id, item)]
            print('[UPLOADING; RESERVED ID] ' + item['name'], flush=True)
            uploaded = upload(service, folder_id, path, item, reserved_id=reserved_id)
            return service, uploaded, False
        except Exception as exc:
            if attempt >= DRIVE_FILE_RETRIES or not is_retryable_drive_failure(exc):
                raise
            delay = min(2 ** attempt, 8)
            print('[DRIVE RETRY %d/%d; FRESH CONNECTION; SAME FILE ID] %s: %s; in %ds' % (
                attempt + 1, DRIVE_FILE_RETRIES, item['name'], exc, delay), flush=True)
            close_drive_connection(service)
            time.sleep(delay)
    raise RuntimeError('Unreachable: all Drive attempts exhausted')


def is_drive_not_found(exc):
    try:
        from googleapiclient.errors import HttpError
        return isinstance(exc, HttpError) and int(exc.resp.status) == 404
    except (ImportError, AttributeError, ValueError):
        return False


def is_drive_conflict(exc):
    try:
        from googleapiclient.errors import HttpError
        return isinstance(exc, HttpError) and int(exc.resp.status) == 409
    except (ImportError, AttributeError, ValueError):
        return False


def load_drive_inventory(service, folder_id):
    """Read entire Drive folder with 1000-item pages, once per batch.

    Avoid one files.list request for EVERY audio file. Page boundaries are
    Google API limitations, not extra per-file requests.
    """
    escaped = folder_id.replace('\\', '\\\\').replace("'", "\\'")
    query = "'%s' in parents and trashed = false" % escaped
    inventory = {}
    page = None
    while True:
        response = service.files().list(
            q=query, fields='nextPageToken,files(id,name,size,md5Checksum,parents,trashed)',
            pageToken=page, pageSize=1000, supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=2)
        for file in response.get('files', []):
            inventory.setdefault(file.get('name'), []).append(file)
        page = response.get('nextPageToken')
        if not page:
            break
        if not isinstance(page, str):
            raise RuntimeError('Google Drive returned invalid inventory pagination token')
    return inventory


def matching_inventory_item(inventory, item, folder_id=None):
    if inventory is None:
        return None
    for file in inventory.get(item['name'], []):
        if (file.get('id') and not file.get('trashed') and
                (folder_id is None or folder_id in (file.get('parents') or [])) and
                int(file.get('size', -1)) == item['size'] and
                str(file.get('md5Checksum', '')).lower() == item['md5'].lower()):
            return file
    return None


def verify_folder(service, folder_id):
    folder = service.files().get(fileId=folder_id, fields='id,name,mimeType,capabilities', supportsAllDrives=True).execute(num_retries=2)
    if folder.get('mimeType') != 'application/vnd.google-apps.folder':
        raise ValueError('Drive target ID is not a folder')
    if (folder.get('capabilities') or {}).get('canAddChildren') is False:
        raise PermissionError('This Google account cannot upload to the target folder. Share it as Editor first.')
    return folder


def verified_remote_entry(service, remote, item, folder_id):
    if not remote or not remote.get('id'):
        raise RuntimeError('Drive did not return a file ID')
    remote = service.files().get(fileId=remote['id'], fields='id,name,size,md5Checksum,parents,trashed', supportsAllDrives=True).execute(num_retries=2)
    if (remote.get('trashed') or remote.get('name') != item['name'] or
            folder_id not in (remote.get('parents') or []) or
            int(remote.get('size', -1)) != item['size'] or
            str(remote.get('md5Checksum', '')).lower() != item['md5'].lower()):
        raise RuntimeError('Drive confirmation failed (folder/size/checksum mismatch) for ' + item['name'])
    return remote


def find_identical(service, folder_id, item):
    q = "'%s' in parents and name = '%s' and trashed = false" % (
        folder_id.replace('\\', '\\\\').replace("'", "\\'"),
        item['name'].replace('\\', '\\\\').replace("'", "\\'"),
    )
    page = None
    while True:
        response = service.files().list(
            q=q, fields='nextPageToken,files(id,name,size,md5Checksum,parents,trashed)',
            pageToken=page, pageSize=1000, supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=2)
        found = matching_inventory_item({item['name']: response.get('files', [])}, item, folder_id)
        if found is not None:
            return found
        page = response.get('nextPageToken')
        if not page:
            break
        if not isinstance(page, str):
            raise RuntimeError('Google Drive returned invalid listing pagination token')
    return None


def upload(service, folder_id, path, item, *, reserved_id):
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(
        str(path),
        mimetype='audio/mpeg' if path.suffix.lower() == '.mp3' else 'application/octet-stream',
        resumable=True,
        chunksize=UPLOAD_CHUNK_SIZE,
    )
    try:
        try:
            # The ID is persisted to disk BEFORE any media bytes are sent.
            response = service.files().create(
                body={'id': reserved_id, 'name': path.name, 'parents': [folder_id]},
                media_body=media, fields='id', supportsAllDrives=True,
            ).execute(num_retries=0)
        except Exception as error:
            if not is_drive_conflict(error):
                raise
            # Google's 409 is exactly what an already-committed first request
            # returns. Never call files.create with another ID here.
            try:
                return verified_remote_entry(service, {'id': reserved_id}, item, folder_id)
            except Exception as check_error:
                if is_drive_not_found(check_error) or is_retryable_drive_failure(check_error):
                    raise IndeterminateDriveUpload(
                        'Drive reports an ID conflict, but its metadata is not yet visible; '
                        'keeping the same reserved ID for a later retry') from check_error
                raise
    finally:
        media.stream().close()
    if response.get('id') != reserved_id:
        raise RuntimeError('Drive returned a different file ID; local original retained')
    return verified_remote_entry(service, response, item, folder_id)


def commit_one(item, drive_history):
    """Durably queue a Drive receipt; Google Sheet writes happen in batches.

    save_set() appends to shared_history's fsynced local WAL, not to Google.
    A subsequent flush verifies all queued receipts together on the remote
    Sheet. Never recycle the source until that verification completes.
    """
    key = item['history_key']
    if key not in drive_history:
        drive_history.add(key)
        save_set('drive_uploaded', drive_history)


def safely_trash(item, android_history, drive_history):
    path = safe_source(item['path'])
    phone_receipt = history_upload_match(
        android_history, path, history_key=item['history_key'], size=item['size'])
    drive_receipt = history_upload_match(
        drive_history, path, history_key=item['history_key'], size=item['size'])
    if not phone_receipt or not drive_receipt:
        raise RuntimeError('Not recorded as confirmed in BOTH Android and Drive histories')
    if 'legacy' in (phone_receipt, drive_receipt):
        print('[LEGACY HISTORY] Using earlier filename-and-size upload receipt(s) for ' + item['name'], flush=True)
    # Must be current content, not a same-name replacement or mutated file.
    if item_data(path)['history_key'] != item['history_key'] or calculate_file_hash(path, 'md5') != item['md5']:
        raise RuntimeError('File was modified since upload; not removing local original')
    send_file_to_recycle_bin(path)


def save_summary(path, summary):
    atomic_json(path, summary)


def prompt_rotate():
    # The parent may pipe stdout, so always print a complete, flushed line
    # before asking for input. Redirected stdin can still supply a response;
    # EOF safely pauses the uploader instead of hanging or deleting anything.
    print('Sign in to a DIFFERENT Google account with free space and folder access, then resume? [y/N]', flush=True)
    try:
        response = input().strip().lower()
    except (EOFError, OSError):
        print('No interactive response received; retaining local originals.', flush=True)
        return False
    return response in ('y', 'yes')


def run(args):
    files = load_manifest(args.manifest)
    summary = {'timestamp': dt.datetime.now().isoformat(), 'folder_id': args.folder_id,
               'attempted': [], 'successful': [], 'failed': [], 'deleted': [], 'delete_failed': [],
               'awaiting_android': [], 'quota_paused': False, 'accounts_used': [], 'error': None}
    save_summary(args.summary_json, summary)

    # IMPORTANT: Sheets auth is intentionally independent from the Drive auth
    # token; switching storage accounts must not change the history sheet.
    print('Reconciling locally journaled history with the authoritative shared Google Sheet...', flush=True)
    try:
        flush_pending()
        android = load_set('android_uploaded')
        drive = load_set('drive_uploaded')
    except Exception as exc:
        summary['error'] = 'Google Sheets reconciliation failed; all local originals retained: ' + str(exc)
        save_summary(args.summary_json, summary)
        print(summary['error'], flush=True)
        return 1
    service, account, remote_inventory = None, None, None
    remaining = list(files)
    attempted_accounts = set()
    token_path = Path(getattr(args, 'oauth_token_json', None) or TOKEN_PATH).expanduser().resolve()
    client_json = getattr(args, 'oauth_client_json', None)
    awaiting_sheet_confirmation = []

    def flush_verified_batch():
        """One Sheet write/readback for many Drive completions, then cleanup.

        An exception leaves all local originals untouched; successfully
        uploaded files are recoverable from the Drive identity lookup and
        the durable local Google Sheets write-ahead journal.
        """
        if not awaiting_sheet_confirmation:
            return
        # The shared-history layer batches mixed-namespace receipts in
        # <=1.9 MB requests and verifies all of them in one remote read.
        flush_pending()
        for confirmed in awaiting_sheet_confirmation:
            confirmed_path = Path(confirmed['path'])
            if history_upload_match(android, confirmed_path,
                                    history_key=confirmed['history_key'],
                                    size=confirmed['size']):
                try:
                    safely_trash(confirmed, android, drive)
                    summary['deleted'].append(confirmed)
                    forget_reserved_id(args.folder_id, confirmed)
                    print('[CONFIRMED BOTH + RECYCLED] ' + confirmed['name'], flush=True)
                except Exception as trash_exc:
                    summary['delete_failed'].append(dict(confirmed, error=str(trash_exc)))
                    print('[LOCAL RETAINED] ' + confirmed['name'] + ': ' + str(trash_exc), flush=True)
            else:
                summary['awaiting_android'].append(confirmed)
                print('[DRIVE CONFIRMED; WAITING FOR PHONE] ' + confirmed['name'], flush=True)
        awaiting_sheet_confirmation.clear()
        save_summary(args.summary_json, summary)

    while remaining:
        path = remaining[0]
        try:
            item = item_data(path)
        except Exception as exc:
            summary['failed'].append({'path': str(path), 'error': str(exc)})
            save_summary(args.summary_json, summary)
            remaining.pop(0)
            continue
        if not any(x['history_key'] == item['history_key'] for x in summary['attempted']):
            summary['attempted'].append(item)
        save_summary(args.summary_json, summary)

        # Already-confirmed Drive files require NO Drive login/network/session.
        # This also allows cleanup after a later successful phone transfer,
        # even when Drive storage is full or the selected account is offline.
        drive_receipt = history_upload_match(
            drive, path, history_key=item['history_key'], size=item['size'])
        phone_receipt = history_upload_match(
            android, path, history_key=item['history_key'], size=item['size'])
        if drive_receipt:
            print('[DRIVE HISTORY CONFIRMED: ' + drive_receipt + '] ' + item['name'], flush=True)
            summary['successful'].append(dict(item, drive_already_present=True,
                                               drive_history_format=drive_receipt,
                                               drive_file_id='confirmed-by-shared-sheet'))
            if phone_receipt:
                try:
                    safely_trash(item, android, drive)
                    summary['deleted'].append(item)
                    forget_reserved_id(args.folder_id, item)
                    print('[CONFIRMED BOTH + RECYCLED] ' + item['name'], flush=True)
                except Exception as trash_exc:
                    summary['delete_failed'].append(dict(item, error=str(trash_exc)))
                    print('[LOCAL RETAINED] ' + item['name'] + ': ' + str(trash_exc), flush=True)
            else:
                summary['awaiting_android'].append(item)
                print('[DRIVE CONFIRMED; WAITING FOR PHONE] ' + item['name'], flush=True)
            save_summary(args.summary_json, summary)
            remaining.pop(0)
            continue

        if service is None:
            rotating = PAUSE_PATH.is_file()
            if rotating:
                paused = load_json(PAUSE_PATH, {}) or {}
                print('\nDRIVE STORAGE FULL / ACCOUNT ROTATION', flush=True)
                print('Previous account: ' + str(paused.get('account') or '(unknown)'), flush=True)
                print('Completed uploads remain in Sheets history; unuploaded originals are retained.', flush=True)
                if not prompt_rotate():
                    summary['quota_paused'] = True
                    save_summary(args.summary_json, summary)
                    print('Drive transfers paused. Rerun Trinity to try another account.', flush=True)
                    return 0
            try:
                service, account, creds, quota = authenticate(
                    force=rotating, token_path=token_path, client_json=client_json,
                )
                print('Selected Drive account: ' + account, flush=True)
                if account.casefold() in attempted_accounts or account_is_full(quota):
                    reason = ('already exhausted during this run' if account.casefold() in attempted_accounts
                              else 'Drive reports no remaining storage')
                    print('Account cannot be used: ' + reason + '. Select a different account.', flush=True)
                    attempted_accounts.add(account.casefold())
                    atomic_json(PAUSE_PATH, {'account': account, 'time': dt.datetime.now().isoformat(),
                                             'reason': 'storageQuotaExceeded'})
                    service = None
                    summary['quota_paused'] = True
                    save_summary(args.summary_json, summary)
                    continue
                try:
                    verify_folder(service, args.folder_id)
                except Exception as folder_error:
                    print('The selected account cannot use the original Drive folder: ' + str(folder_error), flush=True)
                    print('Share that SAME folder with the new account as Editor, or choose another account.', flush=True)
                    atomic_json(PAUSE_PATH, {'account': account, 'time': dt.datetime.now().isoformat(),
                                             'reason': 'folderAccessDenied'})
                    summary['quota_paused'] = True
                    save_summary(args.summary_json, summary)
                    service = None
                    continue
                save_accepted_drive_token(token_path, creds)
                try:
                    remote_inventory = load_drive_inventory(service, args.folder_id)
                except Exception as inventory_error:
                    remote_inventory = None
                    print('[BULK DRIVE INVENTORY UNAVAILABLE; CHECKING FILES INDIVIDUALLY] '
                          + str(inventory_error), flush=True)
            except Exception as exc:
                summary['error'] = 'Drive sign-in/folder validation failed: ' + str(exc)
                save_summary(args.summary_json, summary)
                print(summary['error'], flush=True)
                if rotating:
                    print('Account was not switched; local originals are safe. Rerun to retry sign-in.', flush=True)
                    summary['quota_paused'] = True
                    save_summary(args.summary_json, summary)
                    return 0
                return 1
            PAUSE_PATH.unlink(missing_ok=True)
            summary['quota_paused'] = False
            summary['error'] = None
            summary['accounts_used'].append(account)
            save_summary(args.summary_json, summary)
            print('Drive account: ' + account + ' (Sheets history remains under its own account)', flush=True)

        try:
            reserved_id = reserved_drive_id(args.folder_id, item)
            # Pre-generate up to 100 IDs in one Drive request, not one request
            # per file. A record already on Drive may use an unused ID safely.
            if not reserved_id:
                to_reserve = [item]
                for next_path in remaining[1:ID_BATCH_SIZE]:
                    next_item = item_data(next_path)
                    if not history_upload_match(drive, next_path,
                                                history_key=next_item['history_key'],
                                                size=next_item['size']):
                        to_reserve.append(next_item)
                reserve_drive_ids(service, args.folder_id, to_reserve, account)
                reserved_id = reserved_drive_id(args.folder_id, item)
            service, remote, found_before = ensure_drive_file(
                service, creds, args.folder_id, path, item,
                account=account, reservation_id=reserved_id,
                inventory=remote_inventory,
            )
            # Persist the verified remote result locally immediately. This
            # summary is also a recovery receipt if journaling is interrupted.
            successful = dict(item, drive_file_id=remote.get('id'),
                              drive_already_present=found_before,
                              drive_account=account)
            summary['successful'].append(successful)
            save_summary(args.summary_json, summary)
            commit_one(item, drive)  # durable local WAL, NO Google API request
            awaiting_sheet_confirmation.append(item)
            remaining.pop(0)
        except Exception as exc:
            if is_storage_full(exc):
                # Flush all earlier successful uploads together before moving
                # to another account or pausing due to its storage quota.
                try:
                    flush_verified_batch()
                except Exception as history_exc:
                    summary['error'] = ('Google Sheets batch confirmation failed; '
                                        'local originals retained: ' + str(history_exc))
                    save_summary(args.summary_json, summary)
                    return 1
                print('\nGOOGLE DRIVE STORAGE EXHAUSTED for: ' + account, flush=True)
                print('Uploaded successes are committed to the shared Google Sheet. Keeping unuploaded originals.', flush=True)
                attempted_accounts.add(account.casefold())
                token_path.unlink(missing_ok=True)
                atomic_json(PAUSE_PATH, {'account': account, 'time': dt.datetime.now().isoformat(),
                                         'reason': 'storageQuotaExceeded'})
                summary['quota_paused'] = True
                save_summary(args.summary_json, summary)
                close_drive_connection(service)
                service = None
                remote_inventory = None
                continue
            summary['failed'].append(dict(item, error=str(exc), google_reason=error_reason(exc)))
            save_summary(args.summary_json, summary)
            print('[DRIVE UNCONFIRMED; LOCAL RETAINED; RESERVED ID KEPT] ' + item['name'] + ': ' + str(exc), flush=True)
            # A Sheets failure is a safety-stop, not an upload failure: continuing
            # and trashing anything without verified Sheet history is prohibited.
            if history_upload_match(drive, path, history_key=item['history_key'], size=item['size']):
                summary['error'] = 'Drive was successful, but history/cleanup validation failed.'
                save_summary(args.summary_json, summary)
                return 1
            # The caller's transport may be the SAME broken socket that began
            # this failure; ensure the NEXT MP3 also starts with fresh HTTP.
            close_drive_connection(service)
            try:
                service = new_drive_service(creds)
            except Exception as renewal_error:
                summary['error'] = 'Could not safely replace failed Drive connection: ' + str(renewal_error)
                save_summary(args.summary_json, summary)
                return 1
            remaining.pop(0)
        else:
            if len(awaiting_sheet_confirmation) >= DRIVE_HISTORY_BATCH_SIZE:
                try:
                    flush_verified_batch()
                except Exception as history_exc:
                    summary['error'] = ('Google Sheets batch confirmation failed; '
                                        'local originals retained: ' + str(history_exc))
                    save_summary(args.summary_json, summary)
                    print(summary['error'], flush=True)
                    return 1
    # A lost response can arrive only after OTHER MP3s finish. Check the whole
    # folder ONCE more at the end instead of retrying an apparently missing file
    # with a fresh (and potentially duplicating) ID. Existing failures remain
    # recoverable on the next run by their persisted reserved IDs.
    if summary['failed'] and service is not None:
        try:
            recovered_inventory = load_drive_inventory(service, args.folder_id)
            failures_not_found = []
            for failed in summary['failed']:
                remote = matching_inventory_item(recovered_inventory, failed, args.folder_id)
                if remote is None:
                    failures_not_found.append(failed)
                    continue
                print('[LATE DRIVE SUCCESS RECOVERED AFTER BATCH] ' + failed['name'], flush=True)
                summary['successful'].append(dict(failed, drive_file_id=remote['id'],
                                                   drive_already_present=True,
                                                   drive_account=account))
                commit_one(failed, drive)
                awaiting_sheet_confirmation.append(failed)
            summary['failed'] = failures_not_found
        except Exception as recovery_error:
            print('[LATE DRIVE RECONCILIATION INCOMPLETE; FILES RETAINED] ' + str(recovery_error), flush=True)
            summary['error'] = 'Drive/Sheet final verification failed; originals retained: ' + str(recovery_error)
            save_summary(args.summary_json, summary)
            return 1
    # One final batch for both ordinary and late-recovered completions.
    try:
        flush_verified_batch()
    except Exception as history_exc:
        summary['error'] = ('Google Sheets batch confirmation failed; '
                            'local originals retained: ' + str(history_exc))
        save_summary(args.summary_json, summary)
        print(summary['error'], flush=True)
        return 1
    if summary['failed']:
        summary['error'] = str(len(summary['failed'])) + (
            ' Drive item(s) could not be confirmed. Originals and persistent '
            'upload IDs retained to avoid duplicate uploads.')
    save_summary(args.summary_json, summary)
    print('Drive result: %d confirmed, %d recycled, %d waiting for phone, %d failed.' % (
        len(summary['successful']), len(summary['deleted']),
        len(summary['awaiting_android']), len(summary['failed'])), flush=True)
    return 1 if summary['failed'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--folder-id', required=True)
    parser.add_argument('--summary-json', type=Path, default=ROOT / '_last_drive_upload_delete_summary.json')
    parser.add_argument('--oauth-client-json', type=Path, default=None)
    parser.add_argument('--oauth-token-json', type=Path, default=None)
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:
        print('Drive uploader safely stopped; originals retained:', exc, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
