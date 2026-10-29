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
import os
import sys
import time
from pathlib import Path

from upload_identity import calculate_file_hash, upload_history_key
from shared_history import load_set, save_set, flush_pending
from drive_upload_then_delete import send_file_to_recycle_bin

ROOT = Path(__file__).resolve().parent
FINISHED = ROOT / 'finished'
TOKEN_PATH = ROOT / '_google_drive_oauth_token.json'
PAUSE_PATH = ROOT / '_drive_storage_full.json'
SHEETS_TOKEN = ROOT / '_google_sheets_history_token.json'
ALLOWED_TYPES = {'.mp3', '.m4a', '.mp4', '.flac', '.ogg', '.opus', '.wav', '.wave', '.aiff', '.aif', '.aac'}
SCOPES = ['https://www.googleapis.com/auth/drive']


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


def oauth_app():
    # No new credentials download: reuse the Desktop OAuth app already used
    # for the shared Google Sheet if available.
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
    name_patterns = ['google_drive_oauth_client.json', 'credentials.json', '*client_secret*.json']
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
        'Browser authorization requires an OAuth client ID. Trinity can reuse '
        '_google_sheets_history_token.json or your existing Desktop OAuth client; '
        'you do not need a separate WaveStack service-account key.'
    )


def authenticate(force=False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if not force and TOKEN_PATH.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
            if not set(SCOPES).issubset(set(creds.scopes or [])):
                creds = None
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
        except Exception:
            creds = None
    if not creds or not creds.valid:
        config = oauth_app()
        flow = InstalledAppFlow.from_client_config(config, SCOPES)
        print('Opening browser. Select the Google account with Editor access to the target Drive folder.', flush=True)
        creds = flow.run_local_server(
            port=0, open_browser=True,
            prompt='select_account',
            authorization_prompt_message='Google Drive sign-in URL: {url}',
        )
    atomic_json(TOKEN_PATH, json.loads(creds.to_json()))
    try:
        TOKEN_PATH.chmod(0o600)
    except OSError:
        pass
    service = build('drive', 'v3', credentials=creds, cache_discovery=False)
    about = service.about().get(fields='user(emailAddress)').execute(num_retries=2)
    email = str((about.get('user') or {}).get('emailAddress') or '(unknown account)')
    return service, email


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
    if (remote.get('trashed') or folder_id not in (remote.get('parents') or []) or
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
        response = service.files().list(q=q, fields='nextPageToken,files(id,name,size,md5Checksum,parents,trashed)', pageToken=page, pageSize=100, supportsAllDrives=True, includeItemsFromAllDrives=True).execute(num_retries=2)
        for f in response.get('files', []):
            if int(f.get('size', -1)) == item['size'] and str(f.get('md5Checksum', '')).lower() == item['md5'].lower():
                return verified_remote_entry(service, f, item, folder_id)
        page = response.get('nextPageToken')
        if not page:
            break
    return None


def upload(service, folder_id, path, item):
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(path), mimetype='audio/mpeg' if path.suffix.lower() == '.mp3' else 'application/octet-stream', resumable=True)
    response = service.files().create(body={'name': path.name, 'parents': [folder_id]}, media_body=media, fields='id', supportsAllDrives=True).execute(num_retries=2)
    return verified_remote_entry(service, response, item, folder_id)


def commit_one(item, drive_history):
    # Crash journal -> Google Sheet -> remote readback. The shared_history
    # layer reconciles its WAL with sheet rows, including partial overlap.
    key = item['history_key']
    if key not in drive_history:
        drive_history.add(key)
        save_set('drive_uploaded', drive_history)
    flush_pending()
    # Force a fresh remote read in the next process or after a cache timeout.
    # Only a successfully verified flush allows local deletion.


def safely_trash(item, android_history, drive_history):
    path = safe_source(item['path'])
    if item['history_key'] not in android_history or item['history_key'] not in drive_history:
        raise RuntimeError('Not recorded as confirmed in BOTH Android and Drive histories')
    # Must be current content, not a same-name replacement or mutated file.
    if item_data(path)['history_key'] != item['history_key'] or calculate_file_hash(path, 'md5') != item['md5']:
        raise RuntimeError('File was modified since upload; not removing local original')
    send_file_to_recycle_bin(path)


def save_summary(path, summary):
    atomic_json(path, summary)


def prompt_rotate():
    if not sys.stdin.isatty():
        return False
    # Parent Trinity streams child stdout line-by-line; finish the line before
    # reading stdin or the user never sees the prompt.
    print('Sign in to a DIFFERENT Google account with folder access and resume Drive uploads now? [y/N]', flush=True)
    response = input().strip().lower()
    return response in ('y', 'yes')


def run(args):
    files = load_manifest(args.manifest)
    summary = {'timestamp': dt.datetime.now().isoformat(), 'folder_id': args.folder_id,
               'attempted': [], 'successful': [], 'failed': [], 'deleted': [], 'delete_failed': [],
               'quota_paused': False, 'accounts_used': [], 'error': None}
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
    service, account = None, None
    remaining = list(files)
    attempted_accounts = set()

    while remaining:
        if service is None:
            if PAUSE_PATH.is_file():
                paused = load_json(PAUSE_PATH, {})
                print('\nDRIVE STORAGE FULL: previous upload account was paused.', flush=True)
                print('Confirmed previous uploads are recorded in the shared Google Sheet. Unuploaded originals are preserved.', flush=True)
                if not prompt_rotate():
                    summary['quota_paused'] = True
                    save_summary(args.summary_json, summary)
                    print('Drive transfer paused. Trinity will continue its normal shutdown; rerun to resume.', flush=True)
                    return 0
                TOKEN_PATH.unlink(missing_ok=True)
            try:
                service, account = authenticate(force=PAUSE_PATH.is_file())
                verify_folder(service, args.folder_id)
            except Exception as exc:
                summary['error'] = 'Drive sign-in/folder validation failed: ' + str(exc)
                save_summary(args.summary_json, summary)
                print(summary['error'])
                return 1
            if account in attempted_accounts:
                summary['error'] = ('The selected storage account was already exhausted in this run. '
                                    'Choose another account with access to the folder.')
                save_summary(args.summary_json, summary)
                print(summary['error'])
                TOKEN_PATH.unlink(missing_ok=True)
                atomic_json(PAUSE_PATH, {'account': account, 'reason': 'already_exhausted_in_run'})
                return 1
            PAUSE_PATH.unlink(missing_ok=True)
            summary['quota_paused'] = False
            summary['accounts_used'].append(account)
            save_summary(args.summary_json, summary)
            print('Drive account: ' + account + ' (Sheets history remains under its own account)', flush=True)

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
        try:
            if item['history_key'] in drive:
                print('[HISTORY CONFIRMED] ' + item['name'], flush=True)
                remote = {'id': 'confirmed-by-shared-sheet'}
                found_before = True
            else:
                remote = find_identical(service, args.folder_id, item)
                found_before = remote is not None
                if not found_before:
                    print('[UPLOADING] ' + item['name'], flush=True)
                    remote = upload(service, args.folder_id, path, item)
                else:
                    print('[ALREADY ON DRIVE] ' + item['name'], flush=True)
                commit_one(item, drive)
            successful = dict(item, drive_file_id=remote.get('id'), drive_already_present=found_before,
                              drive_account=account)
            summary['successful'].append(successful)
            save_summary(args.summary_json, summary)
            try:
                safely_trash(item, android, drive)
                summary['deleted'].append(item)
                print('[CONFIRMED BOTH + RECYCLED] ' + item['name'], flush=True)
            except Exception as trash_exc:
                summary['delete_failed'].append(dict(item, error=str(trash_exc)))
                print('[LOCAL RETAINED] ' + item['name'] + ': ' + str(trash_exc), flush=True)
            save_summary(args.summary_json, summary)
            remaining.pop(0)
        except Exception as exc:
            if is_storage_full(exc):
                print('\nGOOGLE DRIVE STORAGE EXHAUSTED for: ' + account, flush=True)
                print('Uploaded successes are committed to the shared Google Sheet. Keeping unuploaded originals.', flush=True)
                # Explicitly discard ONLY the Drive uploader token, never Sheets credentials.
                attempted_accounts.add(account)
                TOKEN_PATH.unlink(missing_ok=True)
                atomic_json(PAUSE_PATH, {'account': account, 'time': dt.datetime.now().isoformat(),
                                         'reason': 'storageQuotaExceeded'})
                summary['quota_paused'] = True
                save_summary(args.summary_json, summary)
                service = None
                continue
            summary['failed'].append(dict(item, error=str(exc), google_reason=error_reason(exc)))
            save_summary(args.summary_json, summary)
            print('[DRIVE FAILED; LOCAL RETAINED] ' + item['name'] + ': ' + str(exc), flush=True)
            # A Sheets failure is a safety-stop, not an upload failure: continuing
            # and trashing anything without verified Sheet history is prohibited.
            if item['history_key'] in drive:
                summary['error'] = 'Drive was successful, but history/cleanup validation failed.'
                save_summary(args.summary_json, summary)
                return 1
            remaining.pop(0)
    if summary['failed']:
        summary['error'] = str(len(summary['failed'])) + ' Drive item(s) failed; originals retained.'
    save_summary(args.summary_json, summary)
    print('Drive result: %d successful, %d recycled, %d failed.' % (
        len(summary['successful']), len(summary['deleted']), len(summary['failed'])), flush=True)
    return 1 if summary['failed'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--folder-id', required=True)
    parser.add_argument('--summary-json', type=Path, default=ROOT / '_last_drive_upload_delete_summary.json')
    args = parser.parse_args()
    try:
        return run(args)
    except Exception as exc:
        print('Drive uploader safely stopped; originals retained:', exc, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
