# Trinity Drive quota rotation (2026-10-28)

**Purpose**: Continue normal Trinity processing if the current Google Drive
storage account runs out of space. Preserve its partial work in the single
Google Sheets history, offer browser sign-in to another account, and resume the
remaining verified Drive transfers.

## Install

1. Extract the ZIP so `drive_quota_rotation.py` and
   `enable_drive_rotation.py` sit beside `trinity.py` in `Trinity/laptop/`.
2. From Windows CMD:

   ```bat
   cd /d "%USERPROFILE%\Downloads\Trinity\laptop"
   python enable_drive_rotation.py
   ```

   The installer changes only Trinity's Drive step, saves a timestamped backup,
   and refuses unknown source shapes. Compatible with the older Drive-for-desktop
   patch: this NEW uploader replaces local staging with independently verified
   Google Drive API uploads. Do not use the old Drive desktop staging mode to
   decide when originals are safe to delete.

3. Install dependencies if needed:

   ```bat
   python -m pip install google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2 send2trash
   ```
4. Run `python trinity.py` normally. If no Drive OAuth cache exists, your
   browser opens; choose an account with **Editor** access to the target folder.

## Google authorization: no new WaveStack JSON needed

Trinity reuses the OAuth **Desktop client configuration** saved in its existing
`_google_sheets_history_token.json`, or in an existing Desktop OAuth JSON used
by Sheets. It asks Google for a separate Drive scope in a new browser flow.
**Google still requires a registered OAuth client ID**. If neither a Sheets
OAuth cache nor an existing Desktop OAuth client can be found, browser-only
sign-in is NOT possible and the uploader stops without changing files. A
service-account JSON is not used for personal Drive storage.

Some unpublished Google OAuth apps restrict which Google accounts may sign in.
If the browser says the new account is unauthorized, add it as a test user or
publish the OAuth app appropriately in Google Cloud.

The Sheets auth cache and target sheet ID remain distinct from Drive tokens.
When using different accounts, each must be given Editor access to the **same
Drive folder**; the history account must retain access to the **same Sheet**.
The uploader uses the existing folder ID in `trinity.py`. Do not switch to a
new folder silently: that would split verification/duplicate checks.

## Storage-full behavior

- On a Drive API HTTP error with the precise reason `storageQuotaExceeded`,
  STOP Drive requests for that account. Do not confuse this with an API
  rate limit, daily transfer limit, or permissions error.
- Successful Android and Drive file histories are committed via the existing
  crash-recoverable Sheets WAL. A completed Drive upload is checked on Drive
  by folder, byte size and MD5 checksum before recording success.
- Only after Android history **and** Drive history have been confirmed on
  Google Sheets does Trinity move the unchanged local original to the system
  Recycle Bin. This is not permanent deletion. Any error retains the original.
- The depleted account's *local Drive OAuth token* is removed. The separate
  Sheets token is never removed or revoked.
- The terminal announces storage exhaustion and asks whether to select a
  different account now. Answer `y` to sign in and continue remaining files;
  `n` (default) pauses only Drive uploads and lets Trinity exit normally.
- When paused, run Trinity again to resume. The `_drive_storage_full.json`
  marker causes the terminal to ask about switching before signing in again.

## Conflicting / overlapping histories

Trinity uses the **existing `shared_history.py` WAL**, not a second copy of
history. On resume it first reconciles queued local records with authoritative
Sheet records. Entries already present remotely are acknowledged rather than
re-appended. Only missing entries are written; each batch is re-read and
confirmed before its pending local journal entries are removed. Current
runtime records are unioned with prior remote records, not used to overwrite
Sheet contents. A failed reconciliation prevents all local cleanup.

Each file is keyed by Trinity's versioned content/identity key. If a Drive
upload succeeded but the process crashed before committing history, a restart
checks Google Drive for an existing identical filename, byte size and MD5 to
avoid reuploading. A successful token switch does not change the key.

## Safety limitations

- No real Google account or Android receiver was used in local tests.
- If the Google Sheets auth account or spreadsheet becomes unwritable, history
  synchronization fails closed; local files are retained until resolved.
- If the next account does **not** have Editor access to the original folder,
  Trinity refuses to upload; grant access before retrying.
- Drive quotas can be charged to a different owner in some sharing setups.
  Switching storage accounts is not guaranteed to increase capacity in those
  cases, and is never treated as proof of capacity.
- A partial Drive upload is not a success unless Drive's final metadata confirms
  folder, size and checksum; a server-side success before a crash is recovered
  by checking for the existing file on the next run.
- JSON files `_google_drive_oauth_token.json` and `_drive_storage_full.json`
  are private local runtime data and must not be committed to GitHub.
