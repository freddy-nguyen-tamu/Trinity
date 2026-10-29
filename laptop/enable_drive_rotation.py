#!/usr/bin/env python3
"""Install the quota-aware Drive uploader without overwriting other Trinity changes.

One-time source patch, backed up. Only replaces Trinity's existing Drive step.
This is compatible with both the original Oct 28 Trinity and the previous
Google Drive-for-desktop staging patch.
"""
from __future__ import annotations
import datetime
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / 'trinity.py'
GITIGNORE = HERE.parent / '.gitignore'
START = 'def auto_drive_upload_then_delete(drive_candidates):\n'
END = '\ndef print_run_summary('
NEW = '''def auto_drive_upload_then_delete(drive_candidates):
    """Run verified, quota-aware Drive API uploads with independent account rotation.

    Android success must already be journaled in the shared Google Sheet.
    The child handles each Drive success, Sheet confirmation and safe local
    trash independently. On storage exhaustion it pauses Drive transfers and
    optionally opens browser sign-in for a different account, without failing
    Trinity's unrelated download, test or history processing work.
    """
    if not drive_candidates:
        log("")
        log("No finished/ files are waiting for Drive upload/delete.")
        return

    log("")
    log("DRIVE UPLOAD: verified completion, partial history sync and quota rotation")
    print_numbered_files("Android-confirmed candidates:", drive_candidates)
    files_to_upload = write_drive_upload_manifest(drive_candidates)
    if not files_to_upload:
        return

    # All successful Android transfers have already been queued with the
    # crash-recoverable Google Sheets history journal. Confirm it remotely
    # BEFORE Drive-side deletions can be considered.
    try:
        flush_pending()
    except Exception as error:
        log(f"Google Sheets history sync failed; Drive cleanup is blocked: {error}")
        log("Unconfirmed history and all local originals are preserved.")
        return

    result = run_script(
        "drive_quota_rotation.py",
        [
            "--manifest", DRIVE_UPLOAD_MANIFEST_FILE,
            "--folder-id", DRIVE_FOLDER_ID,
            "--summary-json", DRIVE_UPLOAD_SUMMARY_FILE,
        ],
        check=False,
    )
    summary = load_drive_upload_summary()
    print_drive_upload_summary(summary)
    if summary.get("quota_paused"):
        log("Google Drive storage is full; Drive uploading is paused.")
        log("All completed Android/Drive histories are preserved in Google Sheets.")
        log("Sign into another account with access to the same folder to resume.")
    elif result:
        log("Drive upload had errors. Local originals not confirmed on BOTH sides were retained.")
    else:
        log("Drive upload finished; only doubly confirmed files were recycled.")

'''


def main():
    if not TARGET.is_file():
        raise SystemExit('Missing trinity.py; extract this ZIP into the existing project.')
    if not (HERE / 'drive_quota_rotation.py').is_file():
        raise SystemExit('Missing drive_quota_rotation.py; extract the entire ZIP.')
    original = TARGET.read_text(encoding='utf-8')
    if 'DRIVE UPLOAD: verified completion, partial history sync and quota rotation' in original:
        print('Drive quota rotation already installed; no changes needed.')
        return
    start = original.find(START)
    end = original.find(END, start + len(START)) if start != -1 else -1
    if start < 0 or end < 0 or original.count(START) != 1:
        raise SystemExit('Unexpected Trinity version. No source files were modified.')
    updated = original[:start] + NEW + original[end + 1:]
    import ast
    ast.parse(updated, filename='trinity.py')
    timestamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = TARGET.with_name('trinity.py.backup-pre-quota-' + timestamp)
    backup.write_text(original, encoding='utf-8')
    temp = TARGET.with_name('trinity.py.tmp-quota')
    temp.write_text(updated, encoding='utf-8')
    os.replace(temp, TARGET)
    if GITIGNORE.is_file():
        content = GITIGNORE.read_text(encoding='utf-8')
        block = ('\n# Drive quota rotation runtime tokens, state and backups\n'
                 'laptop/_drive_storage_full.json\n'
                 'laptop/_drive_storage_full.json.tmp\n'
                 'laptop/_google_drive_oauth_token.json.tmp\n'
                 'laptop/trinity.py.backup-pre-quota-*\n')
        if 'laptop/_drive_storage_full.json' not in content:
            GITIGNORE.write_text(content + block, encoding='utf-8')
    print('Installed quota-aware Drive uploader.')
    print('Backed up previous source:', backup)
    print('Existing Google Sheets OAuth is kept separate from rotating Drive accounts.')
    print('Run Trinity normally to start or resume pending files.')


if __name__ == '__main__':
    main()
