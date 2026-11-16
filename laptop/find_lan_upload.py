import argparse
import datetime
import http.client
import ipaddress
import json
import mimetypes
import os
import socket
import sys
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from shared_history import flush_pending, load_set, save_set
from upload_identity import upload_history_key, history_upload_match

PORT = 1234
TIMEOUT = 0.6
MAX_WORKERS = 128
UPLOAD_CHUNK_SIZE = 1024 * 1024
UPLOAD_TIMEOUT = 300

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

AUDIO_MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
    ".aac": "audio/aac",
}

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_UPLOAD_DIR = str(BASE_DIR / "finished")
DEFAULT_TOKEN_FILE = str(BASE_DIR / "_trinity_android_token.json")
TOKEN_HEADER = "X-Trinity-Token"

# Keep the old Android-server guesses as a fallback, but prefer this laptop's
# actual LAN subnets first so 10.0.73.x, 192.168.x.x, etc. are covered.
LEGACY_PREFERRED_THIRDS = [24, 33]
RECEIVER_APP_NAMES = {"Trinity", "YtbLanReceiver"}


class UploadAuthError(RuntimeError):
    pass


def is_supported_audio_file(path):
    return Path(path).suffix.lower() in SUPPORTED_AUDIO_EXTENSIONS


def audio_content_type(path):
    path = Path(path)
    return (
        AUDIO_MIME_TYPES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )


def android_transport_filename(path):
    """Keep original bytes while satisfying the installed receiver's MP3 suffix rule."""
    path = Path(path)
    if path.suffix.lower() == ".mp3":
        return path.name
    return path.name + ".mp3"


def local_ipv4_addresses():
    found = set()

    try:
        host_name = socket.gethostname()
        for info in socket.getaddrinfo(host_name, None, socket.AF_INET):
            ip = info[4][0]
            addr = ipaddress.ip_address(ip)
            if addr.version == 4 and not addr.is_loopback and addr.is_private:
                found.add(ip)
    except Exception:
        pass

    # This does not send packets; it asks the OS which source address it would
    # use for a normal route, which is often the most reliable Windows answer.
    for probe in ("8.8.8.8", "1.1.1.1", "10.255.255.255"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.2)
                s.connect((probe, 80))
                ip = s.getsockname()[0]
                addr = ipaddress.ip_address(ip)
                if addr.version == 4 and not addr.is_loopback and addr.is_private:
                    found.add(ip)
        except Exception:
            pass

    return sorted(found)


def subnet_candidates_from_local_ips():
    seen = set()

    for local_ip in local_ipv4_addresses():
        try:
            network = ipaddress.ip_network(f"{local_ip}/24", strict=False)
        except ValueError:
            continue

        for ip in network.hosts():
            ip_text = str(ip)
            if ip_text == local_ip or ip_text in seen:
                continue
            seen.add(ip_text)
            yield ip_text


def fallback_candidates():
    seen = set()

    ranges = []
    for third in LEGACY_PREFERRED_THIRDS:
        ranges.append(f"10.0.{third}.0/24")
    ranges.extend(
        [
            "192.168.0.0/24",
            "192.168.1.0/24",
            "192.168.4.0/24",
            "10.0.0.0/24",
            "10.0.1.0/24",
            "172.16.0.0/24",
        ]
    )

    for cidr in ranges:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        for ip in network.hosts():
            ip_text = str(ip)
            if ip_text in seen:
                continue
            seen.add(ip_text)
            yield ip_text

    for third in range(10, 100):
        try:
            network = ipaddress.ip_network(f"10.0.{third}.0/24", strict=False)
        except ValueError:
            continue
        for ip in network.hosts():
            ip_text = str(ip)
            if ip_text in seen:
                continue
            seen.add(ip_text)
            yield ip_text


def generate_ips():
    seen = set()
    for ip in subnet_candidates_from_local_ips():
        if ip not in seen:
            seen.add(ip)
            yield ip
    for ip in fallback_candidates():
        if ip not in seen:
            seen.add(ip)
            yield ip


def re_starts_with_scheme(url):
    return url.lower().startswith(("http://", "https://"))


def normalize_token(raw):
    return "".join(ch.lower() for ch in str(raw or "") if ch.isalnum())


def load_pairing_token(token_file):
    path = Path(token_file)
    if not path.exists():
        return ""

    try:
        with path.open("r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return normalize_token(data.get("token", ""))
        return normalize_token(data)
    except Exception as e:
        print(f"Could not read Trinity token file, will ask again: {path} ({e})")
        return ""


def save_pairing_token(token_file, token):
    token = normalize_token(token)
    if not token:
        return ""

    path = Path(token_file)
    path.write_text(
        json.dumps(
            {
                "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "token": token,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return token


def prompt_for_pairing_token(token_file):
    print("")
    print("No Trinity Android pairing token is saved yet.")
    print("Open the Trinity app on Android and type the Laptop pairing token shown there.")
    print(f"It will be saved for future runs at: {token_file}")
    try:
        token = input().strip()
    except EOFError:
        return ""
    return save_pairing_token(token_file, token)


def request_headers(token=""):
    headers = {"User-Agent": "trinity-lan-uploader/1.0"}
    token = normalize_token(token)
    if token:
        headers[TOKEN_HEADER] = token
    return headers


def normalize_base_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    if not re_starts_with_scheme(url):
        url = "http://" + url
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc:
        raise ValueError(f"Invalid URL: {url}")
    if not parsed.port:
        parsed = parsed._replace(netloc=f"{parsed.hostname}:{PORT}")
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")) + "/"


def response_is_receiver(body):
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return any(name in body for name in RECEIVER_APP_NAMES)

    app_name = str(data.get("app") or data.get("receiver") or "")
    return app_name in RECEIVER_APP_NAMES


def probe_url(ip_or_url, token=""):
    if re_starts_with_scheme(ip_or_url):
        base_url = normalize_base_url(ip_or_url)
    else:
        base_url = f"http://{ip_or_url}:{PORT}/"

    for endpoint in ("ping", "health"):
        probe = urllib.parse.urljoin(base_url, endpoint)
        try:
            req = urllib.request.Request(probe, headers=request_headers(token))
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                body = response.read(1024).decode("utf-8", errors="replace")
                if response.status == 200 and response_is_receiver(body):
                    return base_url
        except Exception:
            pass
    return None


def find_first_server(token=""):
    ip_iterator = iter(generate_ips())

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        pending = set()

        for _ in range(MAX_WORKERS):
            try:
                ip = next(ip_iterator)
            except StopIteration:
                break
            pending.add(executor.submit(probe_url, ip, token))

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)

            for future in done:
                result = future.result()

                if result:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return result

                try:
                    ip = next(ip_iterator)
                    pending.add(executor.submit(probe_url, ip, token))
                except StopIteration:
                    pass

    return None


def collect_files(upload_dir, recursive=False):
    folder = Path(upload_dir)

    if not folder.exists():
        raise FileNotFoundError(f"Upload folder does not exist: {folder}")

    if not folder.is_dir():
        raise NotADirectoryError(f"Upload path is not a folder: {folder}")

    iterator = folder.rglob("*") if recursive else folder.iterdir()
    files = [str(path) for path in iterator if path.is_file() and is_supported_audio_file(path)]

    if not files:
        raise FileNotFoundError(f"No supported audio files found in: {folder}")

    return files


def collect_manifest_files(manifest_path):
    manifest = Path(manifest_path)

    if not manifest.exists():
        raise FileNotFoundError(f"Manifest file does not exist: {manifest}")

    with manifest.open("r", encoding="utf-8-sig") as f:
        data = json.load(f)

    raw_files = data.get("files") if isinstance(data, dict) else data
    if not isinstance(raw_files, list):
        raise ValueError("Manifest must be a list of files or an object with a files list.")

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
        if path.is_file() and is_supported_audio_file(path):
            files.append(str(path))
        elif path.is_file():
            print(f"Skipping unsupported audio manifest file: {path}")
        else:
            print(f"Skipping missing manifest file: {path}")

    if not files:
        raise FileNotFoundError(
            f"No existing supported audio files found from manifest: {manifest}"
        )

    return files


def file_items(files):
    items = []
    for path_text in files:
        path = Path(path_text).resolve()
        items.append(
            {
                "path": str(path),
                "name": path.name,
                "size": path.stat().st_size if path.exists() else 0,
            }
        )
    return items


def write_summary(summary_path, summary):
    if not summary_path:
        return

    path = Path(summary_path)
    # Persist each acknowledged phone receipt before attempting another file.
    # Replacing atomically means a parent crash cannot leave a truncated summary.
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def upload_one_file(base_url, file_path, token=""):
    file_path = Path(file_path).resolve()
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http":
        raise ValueError("The Trinity Android LAN receiver uses plain HTTP on your Wi-Fi LAN.")

    host = parsed.hostname
    port = parsed.port or PORT
    transport_name = android_transport_filename(file_path)
    remote_path = "/upload-file?filename=" + urllib.parse.quote(transport_name, safe="")
    size = file_path.stat().st_size

    conn = http.client.HTTPConnection(host, port=port, timeout=UPLOAD_TIMEOUT)
    try:
        conn.putrequest("POST", remote_path, skip_accept_encoding=True)
        conn.putheader("Host", f"{host}:{port}")
        conn.putheader("User-Agent", "trinity-lan-uploader/1.0")
        token = normalize_token(token)
        if token:
            conn.putheader(TOKEN_HEADER, token)
        conn.putheader("Content-Type", audio_content_type(file_path))
        conn.putheader("X-Trinity-Original-Filename", urllib.parse.quote(file_path.name, safe=""))
        conn.putheader("X-Trinity-Original-Extension", file_path.suffix.lower())
        conn.putheader("Content-Length", str(size))
        conn.endheaders()

        with file_path.open("rb") as f:
            while True:
                chunk = f.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                conn.send(chunk)

        response = conn.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        if response.status == 401:
            raise UploadAuthError(f"Android receiver rejected the pairing token: {body}")
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Android receiver returned HTTP {response.status}: {body}")

        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            result = {"ok": True, "raw_response": body}

        if result.get("ok") is False:
            raise RuntimeError(f"Android receiver rejected the upload: {body}")

        received_bytes = result.get("bytes")
        if received_bytes is not None and int(received_bytes) != size:
            raise RuntimeError(
                f"Android receiver byte-count mismatch: sent={size}, "
                f"received={received_bytes}"
            )

        result["original_name"] = file_path.name
        result["transport_name"] = transport_name
        result["content_type"] = audio_content_type(file_path)
        result["original_bytes_preserved"] = True
        return result
    finally:
        conn.close()


def upload_files(base_url, files, token="", on_success=None, on_progress=None):
    successful = []
    failed = []

    for index, file_path in enumerate(files, start=1):
        item = file_items([file_path])[0]
        print(f"[{index}/{len(files)}] Uploading: {item['name']} ({item['size']} bytes)")
        transport_name = android_transport_filename(file_path)
        if transport_name != item["name"]:
            print(
                "    Preserving the original audio bytes; the installed Android "
                f"receiver will store this as: {transport_name}"
            )
        try:
            # Compute identity before the network upload. Once Android sends
            # its success response, the receipt can be saved immediately.
            item["history_key"] = upload_history_key(
                file_path, base_dir=DEFAULT_UPLOAD_DIR,
            )
            response = upload_one_file(base_url, file_path, token=token)
        except Exception as error:
            item["error"] = str(error)
            failed.append(item)
            if on_progress is not None:
                on_progress(item, False)
            print(f"    FAILED: {item['name']}: {error}")
        else:
            item["receiver_response"] = response
            successful.append(item)
            if on_progress is not None:
                on_progress(item, True)
            # Write the separate Android history's crash-safe Sheet journal
            # IMMEDIATELY after the receiver's success acknowledgment. Never
            # reinterpret a completed upload as a phone failure just because
            # history synchronization may fail later.
            if on_success is not None:
                on_success(item)
            print(f"    OK: {item['name']}")

    return successful, failed


def main():
    parser = argparse.ArgumentParser(
        description="Find the Trinity Android LAN receiver and upload local audio files directly."
    )
    parser.add_argument(
        "--dir",
        default=DEFAULT_UPLOAD_DIR,
        help="Folder whose supported audio files should be uploaded.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="JSON manifest listing the exact audio files to upload.",
    )
    parser.add_argument(
        "--summary-json",
        default=None,
        help="Write upload result summary to this JSON file.",
    )
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        help="JSON file where the Trinity Android pairing token is stored.",
    )
    parser.add_argument(
        "--pair-token",
        default=None,
        help="Save this Trinity Android pairing token before scanning/uploading.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Skip scanning and upload to this receiver URL directly, for example http://10.0.73.48:1234/.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Accepted for old commands; direct HTTP upload has no browser window.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Upload supported audio files inside subfolders too.",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only find and print the Android receiver URL; do not upload.",
    )
    args = parser.parse_args()

    token = normalize_token(args.pair_token) if args.pair_token else load_pairing_token(args.token_file)
    if args.pair_token:
        token = save_pairing_token(args.token_file, token)

    summary = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "url": None,
        "attempted": [],
        "successful": [],
        "failed": [],
        "error": None,
    }

    if args.scan_only:
        try:
            url = normalize_base_url(args.url) if args.url else find_first_server(token=token)
            if args.url:
                url = probe_url(url, token=token) or url
        except Exception as e:
            summary["error"] = str(e)
            write_summary(args.summary_json, summary)
            print(f"Upload setup failed: {e}")
            sys.exit(1)

        if not url:
            summary["error"] = "No server found."
            write_summary(args.summary_json, summary)
            print("No server found. Open Trinity once on Android, keep the receiver enabled, and make sure both devices are on the same Wi-Fi.")
            sys.exit(1)

        summary["url"] = url
        print(f"Found Android receiver: {url}")
        write_summary(args.summary_json, summary)
        return

    try:
        if args.manifest:
            files = collect_manifest_files(args.manifest)
        else:
            files = collect_files(args.dir, recursive=args.recursive)
    except Exception as e:
        summary["error"] = str(e)
        write_summary(args.summary_json, summary)
        print(f"Upload setup failed: {e}")
        sys.exit(1)

    attempted = file_items(files)
    summary["attempted"] = attempted
    source_label = args.manifest or args.dir

    # Check both current and historical Android receipts before probing the
    # phone. Files already sent do not require the receiver to be online.
    # Android and Drive are different namespaces in Trinity Shared History.
    # Prepare the history snapshot before transferring any bytes, so each
    # acknowledged phone receipt can immediately enter the durable Sheet WAL.
    try:
        android_history = load_set("android_uploaded")
    except Exception as error:
        summary["failed"] = attempted
        summary["error"] = "Could not load shared Android upload history: " + str(error)
        write_summary(args.summary_json, summary)
        print(summary["error"])
        sys.exit(1)

    # Do not re-send current or historical already-confirmed phone files.
    # Keep these separate from the current run's freshly uploaded successes.
    pending = []
    for path in files:
        key = upload_history_key(path, base_dir=DEFAULT_UPLOAD_DIR)
        prior = history_upload_match(android_history, path, history_key=key)
        if prior:
            label = "LEGACY HISTORY" if prior == "legacy" else "CONFIRMED HISTORY"
            print(f"[ALREADY SENT TO PHONE - {label}] " + Path(path).name)
        else:
            pending.append(path)

    write_summary(args.summary_json, summary)
    if not pending:
        # shared_history may have just imported legacy JSON into the Sheet.
        # Confirm that the import and any unfinished WAL records are committed.
        try:
            flush_pending()
        except Exception as error:
            summary["history_sync_error"] = str(error)
            write_summary(args.summary_json, summary)
            print("Android history synchronization failed: " + str(error))
            sys.exit(1)
        print("All selected files have already been sent to Android; no LAN connection needed.")
        return

    if not token:
        token = prompt_for_pairing_token(args.token_file)

    if not token:
        summary["failed"] = attempted
        summary["error"] = "No Trinity Android pairing token was provided."
        write_summary(args.summary_json, summary)
        print(summary["error"])
        sys.exit(1)

    try:
        url = normalize_base_url(args.url) if args.url else find_first_server(token=token)
        if args.url:
            url = probe_url(url, token=token) or url
    except Exception as e:
        summary["failed"] = attempted
        summary["error"] = str(e)
        write_summary(args.summary_json, summary)
        print(f"Upload setup failed: {e}")
        sys.exit(1)

    if not url:
        summary["failed"] = attempted
        summary["error"] = "No server found."
        write_summary(args.summary_json, summary)
        print("No server found. Open Trinity once on Android, keep the receiver enabled, and make sure both devices are on the same Wi-Fi.")
        sys.exit(1)

    summary["url"] = url
    print(f"Found Android receiver: {url}")
    print(f"Uploading {len(files)} audio file(s) from: {source_label}")

    def persist_result(item, is_success):
        if is_success:
            summary["successful"].append(item)
        else:
            summary["failed"].append(item)
        write_summary(args.summary_json, summary)

    def journal_success(item):
        key = item["history_key"]
        if key not in android_history:
            android_history.add(key)
            save_set("android_uploaded", android_history)

    successful, failed = upload_files(
        url, pending, token=token,
        on_success=journal_success, on_progress=persist_result,
    )
    auth_failed = [item for item in failed if "pairing token" in item.get("error", "").lower()]
    if auth_failed and len(auth_failed) == len(failed):
        print("")
        print("The saved Trinity Android pairing token did not work.")
        print("Type the current token shown in the Trinity Android app to retry these files once.")
        token = prompt_for_pairing_token(args.token_file)
        if token:
            retry_files = [item["path"] for item in failed]
            # Replace the first round's failure entries as retries complete.
            summary["failed"] = []
            write_summary(args.summary_json, summary)
            retry_successful, retry_failed = upload_files(
                url, retry_files, token=token,
                on_success=journal_success, on_progress=persist_result,
            )
            successful.extend(retry_successful)
            failed = retry_failed

    summary["successful"] = successful
    summary["failed"] = failed
    if failed:
        summary["error"] = f"{len(failed)} file(s) failed to upload."
    write_summary(args.summary_json, summary)

    try:
        flush_pending()
    except Exception as error:
        summary["history_sync_error"] = str(error)
        write_summary(args.summary_json, summary)
        print("Android transfers were retained, but Google Sheets history synchronization failed: " + str(error))
        print("Trinity's durable history journal will be replayed on the next run.")
        sys.exit(1)

    if failed:
        print(summary["error"])
        sys.exit(1)
    print("Upload finished.")


if __name__ == "__main__":
    main()
