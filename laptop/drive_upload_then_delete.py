import argparse
import datetime
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DRIVE_FOLDER_ID = "1qbVH_yaNn1aagSrMGvZIggCjSvRzZRSs"
SERVICE_ACCOUNT_EMAIL = "trinitydrive@wavestack2.iam.gserviceaccount.com"
SCRIPT_DIR = Path(__file__).resolve().parent
FINISHED_DIR = SCRIPT_DIR / "finished"
DEFAULT_ANDROID_UPLOADED_HISTORY_JSON = SCRIPT_DIR / "_android_uploaded_history.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive",
]

SERVICE_ACCOUNT_QUOTA_MARKERS = (
    "Service Accounts do not have storage quota",
    "storageQuotaExceeded",
)

try:
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
        write_through=True,
    )
    sys.stderr.reconfigure(
        encoding="utf-8",
        errors="replace",
        line_buffering=True,
        write_through=True,
    )
except Exception:
    pass


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


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

            print(
                f"File busy while {description}; retrying in {delay:.1f}s "
                f"({attempt}/{attempts}): {e}",
                flush=True,
            )
            time.sleep(delay)
            delay = min(delay * 1.5, 5.0)

    raise last_error


def send_file_to_recycle_bin(path):
    path = Path(path).resolve()
    if not path.exists():
        return

    send2trash_error = None
    try:
        from send2trash import send2trash
    except ImportError:
        send2trash = None

    if send2trash is not None:
        try:
            send2trash(str(path))
            return
        except Exception as e:
            send2trash_error = e

    if os.name != "nt":
        trash_errors = []

        if sys.platform == "darwin" and shutil.which("osascript"):
            escaped_path = str(path).replace("\\", "\\\\").replace('"', '\\"')
            script = f'tell application "Finder" to delete POSIX file "{escaped_path}"'
            try:
                subprocess.run(
                    ["osascript", "-e", script],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return
            except Exception as e:
                trash_errors.append(f"osascript: {e}")

        for command in (
            ["gio", "trash", str(path)],
            ["kioclient6", "move", str(path), "trash:/"],
            ["kioclient5", "move", str(path), "trash:/"],
            ["trash-put", str(path)],
        ):
            if not shutil.which(command[0]):
                continue
            try:
                subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                return
            except Exception as e:
                trash_errors.append(f"{command[0]}: {e}")

        details = "; ".join(trash_errors) if trash_errors else "no trash command found"
        if send2trash_error:
            raise RuntimeError(f"Could not move file to system trash: {path}; {details}") from send2trash_error
        raise RuntimeError(f"No system trash backend is available for {path}; {details}")

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", wintypes.LPVOID),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    FO_DELETE = 0x0003
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_NOERRORUI = 0x0400
    FOF_SILENT = 0x0004

    file_list = str(path) + "\0\0"
    operation = SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = FO_DELETE
    operation.pFrom = file_list
    operation.pTo = None
    operation.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    operation.fAnyOperationsAborted = False
    operation.hNameMappings = None
    operation.lpszProgressTitle = None

    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0:
        raise OSError(result, f"Could not move file to Recycle Bin: {path}")
    if operation.fAnyOperationsAborted:
        raise OSError(f"Recycle Bin operation was aborted: {path}")


def recycle_file_if_exists(path, description):
    if not Path(path).exists():
        return

    retry_file_operation(description, lambda: send_file_to_recycle_bin(path))


def load_manifest_files(manifest_path):
    manifest = Path(manifest_path)

    if not manifest.exists():
        raise FileNotFoundError(f"Manifest file does not exist: {manifest}")

    with manifest.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)

    raw_files = data.get("files") if isinstance(data, dict) else data

    if not isinstance(raw_files, list):
        raise ValueError("Manifest must be a list or an object with a files list.")

    files = []
    for raw_file in raw_files:
        if isinstance(raw_file, dict):
            raw_path = raw_file.get("path") or raw_file.get("file") or ""
        else:
            raw_path = str(raw_file)

        if not raw_path:
            print(f"Skipping manifest item without path: {raw_file}")
            continue

        path = Path(raw_path).expanduser().resolve()
        if path.is_file():
            files.append(path)
        else:
            print(f"Skipping missing file: {path}")

    if not files:
        raise FileNotFoundError(f"No existing files found from manifest: {manifest}")

    return files


def file_item(path):
    path = Path(path)
    return {
        "path": str(path),
        "name": path.name,
        "size": path.stat().st_size if path.exists() else 0,
    }


def load_history_file(path, purpose):
    history_path = Path(path).expanduser().resolve()

    if not history_path.exists():
        print(f"{purpose} history does not exist: {history_path}")
        return set()

    try:
        with history_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Could not read {purpose} history: {e}")
        return set()

    if isinstance(data, list):
        return {str(item) for item in data}

    print(f"Ignoring {purpose} history because it is not a JSON list: {history_path}")
    return set()


def relative_finished_name(path):
    path = Path(path).resolve()

    try:
        return os.path.relpath(str(path), str(FINISHED_DIR.resolve()))
    except ValueError:
        return path.name


def uploaded_history_key(path):
    path = Path(path).resolve()
    size = path.stat().st_size if path.exists() else 0
    return f"{relative_finished_name(path)}|{size}"


def write_summary(summary_path, summary):
    if not summary_path:
        return

    path = Path(summary_path)
    retry_file_operation(
        f"writing summary {path}",
        lambda: path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        ),
    )


def validate_service_account_file(json_path):
    try:
        data = json.loads(json_path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise RuntimeError(f"Could not read service account JSON: {e}")

    client_email = data.get("client_email")
    if client_email != SERVICE_ACCOUNT_EMAIL:
        raise RuntimeError(
            "Service account JSON is for the wrong account. "
            f"Expected {SERVICE_ACCOUNT_EMAIL}, found {client_email or '(missing client_email)'}."
        )


def build_drive_service(service_account_json):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError(
            "Google Drive packages are missing. Run:\n"
            "py -m pip install google-api-python-client google-auth google-auth-httplib2\n"
            r"C:\Users\qacer\AppData\Local\Python\pythoncore-3.14-64\python.exe "
            "-m pip install google-api-python-client google-auth google-auth-httplib2"
        )

    json_path = Path(service_account_json).expanduser().resolve()

    if not json_path.exists():
        raise FileNotFoundError(f"Service account JSON does not exist: {json_path}")

    validate_service_account_file(json_path)

    credentials = service_account.Credentials.from_service_account_file(
        str(json_path),
        scopes=SCOPES,
    )

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def build_oauth_drive_service(oauth_client_json, oauth_token_json):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise RuntimeError(
            "Google OAuth packages are missing. Run:\n"
            "py -m pip install google-api-python-client google-auth google-auth-httplib2 google-auth-oauthlib\n"
            r"C:\Users\qacer\AppData\Local\Python\pythoncore-3.14-64\python.exe "
            "-m pip install google-api-python-client google-auth google-auth-httplib2 google-auth-oauthlib"
        )

    client_path = Path(oauth_client_json).expanduser().resolve()
    token_path = Path(oauth_token_json).expanduser().resolve()

    if not client_path.exists():
        raise FileNotFoundError(f"OAuth client JSON does not exist: {client_path}")

    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
        print("Starting Google OAuth local callback server...", flush=True)
        credentials = flow.run_local_server(
            port=0,
            open_browser=False,
            authorization_prompt_message=(
                "\n"
                "GOOGLE DRIVE OAUTH LOGIN URL\n"
                "-" * 50
                + "\n"
                "Copy this URL into the browser you want to use, then sign in and approve access:\n\n"
                "{url}\n\n"
                "Waiting for Google to redirect back to this computer...\n"
            ),
        )

    retry_file_operation(
        f"writing OAuth token {token_path}",
        lambda: token_path.write_text(credentials.to_json(), encoding="utf-8"),
    )

    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def validate_drive_folder(service, folder_id):
    folder = (
        service.files()
        .get(
            fileId=folder_id,
            fields="id,name,mimeType,capabilities",
            supportsAllDrives=True,
        )
        .execute()
    )

    mime_type = folder.get("mimeType")
    if mime_type != "application/vnd.google-apps.folder":
        raise RuntimeError(f"Drive ID is not a folder: {folder_id}")

    capabilities = folder.get("capabilities") or {}
    can_add_children = capabilities.get("canAddChildren")

    if can_add_children is False:
        raise RuntimeError(
            "The service account can see the folder but cannot upload into it. "
            f"Make sure {SERVICE_ACCOUNT_EMAIL} has Editor permission."
        )

    return folder


def upload_one_file(service, file_path, folder_id):
    from googleapiclient.http import MediaFileUpload

    file_path = Path(file_path)
    mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"

    media = MediaFileUpload(
        str(file_path),
        mimetype=mime_type,
        resumable=True,
    )

    metadata = {
        "name": file_path.name,
        "parents": [folder_id],
    }

    uploaded = (
        service.files()
        .create(
            body=metadata,
            media_body=media,
            fields="id,name,size,md5Checksum,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )

    expected_size = file_path.stat().st_size
    returned_size = uploaded.get("size")

    if returned_size is not None:
        returned_size_int = int(returned_size)
        if returned_size_int != expected_size:
            raise RuntimeError(
                f"Drive uploaded size mismatch for {file_path.name}: "
                f"local={expected_size}, drive={returned_size_int}"
            )

    return uploaded


def is_service_account_quota_error(error):
    text = str(error)
    return any(marker in text for marker in SERVICE_ACCOUNT_QUOTA_MARKERS)


def main():
    parser = argparse.ArgumentParser(
        description="Upload files to a shared Google Drive folder, then move local files to system trash after successful upload."
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="JSON manifest containing files to upload.",
    )
    parser.add_argument(
        "--service-account-json",
        default=None,
        help="Path to the downloaded Google service account JSON key.",
    )
    parser.add_argument(
        "--oauth-client-json",
        default=None,
        help="Path to a downloaded Desktop OAuth client JSON.",
    )
    parser.add_argument(
        "--oauth-token-json",
        default=None,
        help="Path where the OAuth token should be cached.",
    )
    parser.add_argument(
        "--folder-id",
        default=DRIVE_FOLDER_ID,
        help="Google Drive folder ID.",
    )
    parser.add_argument(
        "--summary-json",
        default=None,
        help="Write upload/delete summary to this JSON file.",
    )
    parser.add_argument(
        "--delete-after-upload",
        action="store_true",
        help=(
            "Move each local file to system trash only after that file uploads successfully "
            "and is recorded in the Android uploaded history."
        ),
    )
    parser.add_argument(
        "--android-uploaded-history-json",
        default=str(DEFAULT_ANDROID_UPLOADED_HISTORY_JSON),
        help="JSON history proving which finished/ files were already uploaded to Android.",
    )

    args = parser.parse_args()

    summary = {
        "timestamp": now_iso(),
        "folder_id": args.folder_id,
        "service_account_email": SERVICE_ACCOUNT_EMAIL,
        "auth_mode": "oauth" if args.oauth_client_json else "service_account",
        "attempted": [],
        "successful": [],
        "failed": [],
        "deleted": [],
        "delete_failed": [],
        "error": None,
    }

    try:
        files = load_manifest_files(args.manifest)
        summary["attempted"] = [file_item(path) for path in files]
        android_uploaded_history = set()
        if args.delete_after_upload:
            android_uploaded_history = load_history_file(
                args.android_uploaded_history_json,
                "Android uploaded",
            )

        print(f"Using Drive folder: {args.folder_id}")
        print(f"Uploading {len(files)} file(s) to Google Drive...")

        if args.oauth_client_json:
            if not args.oauth_token_json:
                raise RuntimeError("--oauth-token-json is required with --oauth-client-json.")

            print(f"Using OAuth client: {Path(args.oauth_client_json).resolve()}")
            print(f"OAuth token cache: {Path(args.oauth_token_json).resolve()}")
            service = build_oauth_drive_service(args.oauth_client_json, args.oauth_token_json)
        elif args.service_account_json:
            print(f"Expected service account: {SERVICE_ACCOUNT_EMAIL}")
            service = build_drive_service(args.service_account_json)
        else:
            raise RuntimeError(
                "No Drive credentials were provided. Use --oauth-client-json with --oauth-token-json, "
                "or --service-account-json for a real Google Shared Drive."
            )

        folder = validate_drive_folder(service, args.folder_id)

        print(f"Confirmed Drive folder: {folder.get('name')}")

        for index, file_path in enumerate(files, start=1):
            file_path = Path(file_path)

            try:
                print(f"[{index}/{len(files)}] Uploading: {file_path.name}")
                uploaded = upload_one_file(service, file_path, args.folder_id)

                success_item = file_item(file_path)
                success_item.update(
                    {
                        "drive_file_id": uploaded.get("id"),
                        "drive_name": uploaded.get("name"),
                        "drive_web_view_link": uploaded.get("webViewLink"),
                    }
                )
                summary["successful"].append(success_item)

                print(f"Uploaded: {file_path.name}")

                if args.delete_after_upload:
                    history_key = uploaded_history_key(file_path)
                    if history_key not in android_uploaded_history:
                        delete_failed_item = dict(success_item)
                        delete_failed_item["delete_error"] = (
                            "Not moved to trash because this file is not recorded as uploaded to Android."
                        )
                        summary["delete_failed"].append(delete_failed_item)
                        print(
                            "Kept local file after Drive upload because it is not recorded "
                            f"as uploaded to Android: {file_path}"
                        )
                        continue

                    try:
                        retry_file_operation(
                            f"moving uploaded local file to system trash {file_path}",
                            lambda p=file_path: send_file_to_recycle_bin(p),
                        )
                        deleted_item = dict(success_item)
                        summary["deleted"].append(deleted_item)
                        print(f"Moved local file to system trash after Drive upload: {file_path}")
                    except Exception as delete_error:
                        delete_failed_item = dict(success_item)
                        delete_failed_item["delete_error"] = str(delete_error)
                        summary["delete_failed"].append(delete_failed_item)
                        print(
                            "WARNING: Uploaded but could not move local file to system trash "
                            f"{file_path}: {delete_error}"
                        )

            except Exception as upload_error:
                failed_item = file_item(file_path)
                failed_item["error"] = str(upload_error)
                summary["failed"].append(failed_item)
                print(f"FAILED: {file_path.name}: {upload_error}")

                if is_service_account_quota_error(upload_error):
                    summary["error"] = (
                        "Google rejected service-account upload because service accounts do not "
                        "have Drive storage quota for normal shared folders. Use a Desktop OAuth "
                        "client for My Drive folders, or upload into a Google Workspace Shared Drive."
                    )
                    write_summary(args.summary_json, summary)
                    print(summary["error"])
                    sys.exit(1)

    except Exception as error:
        summary["error"] = str(error)
        write_summary(args.summary_json, summary)
        print(f"Drive upload setup failed: {error}")
        sys.exit(1)

    write_summary(args.summary_json, summary)

    failed_count = len(summary["failed"])
    delete_failed_count = len(summary["delete_failed"])

    print("")
    print("Drive upload/delete summary")
    print("-" * 50)
    print(f"Attempted: {len(summary['attempted'])}")
    print(f"Uploaded: {len(summary['successful'])}")
    print(f"Moved to system trash locally: {len(summary['deleted'])}")
    print(f"Upload failed: {failed_count}")
    print(f"Trash move failed: {delete_failed_count}")

    if failed_count or delete_failed_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
