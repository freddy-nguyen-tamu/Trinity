import argparse
import datetime
import http.client
import ipaddress
import json
import socket
import sys
import urllib.parse
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

PORT = 1234
TIMEOUT = 0.6
MAX_WORKERS = 128
UPLOAD_CHUNK_SIZE = 1024 * 1024

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_UPLOAD_DIR = str(BASE_DIR / "finished")

# Keep the old Android-server guesses as a fallback, but prefer this laptop's
# actual LAN subnets first so 10.0.73.x, 192.168.x.x, etc. are covered.
LEGACY_PREFERRED_THIRDS = [24, 33]
RECEIVER_APP_NAMES = {"Trinity", "YtbLanReceiver"}


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


def probe_url(ip_or_url):
    if re_starts_with_scheme(ip_or_url):
        base_url = normalize_base_url(ip_or_url)
    else:
        base_url = f"http://{ip_or_url}:{PORT}/"

    for endpoint in ("ping", "health"):
        probe = urllib.parse.urljoin(base_url, endpoint)
        try:
            req = urllib.request.Request(probe, headers={"User-Agent": "trinity-lan-uploader/1.0"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                body = response.read(1024).decode("utf-8", errors="replace")
                if response.status == 200 and response_is_receiver(body):
                    return base_url
        except Exception:
            pass
    return None


def find_first_server():
    ip_iterator = iter(generate_ips())

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        pending = set()

        for _ in range(MAX_WORKERS):
            try:
                ip = next(ip_iterator)
            except StopIteration:
                break
            pending.add(executor.submit(probe_url, ip))

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)

            for future in done:
                result = future.result()

                if result:
                    executor.shutdown(wait=False, cancel_futures=True)
                    return result

                try:
                    ip = next(ip_iterator)
                    pending.add(executor.submit(probe_url, ip))
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
    files = [str(path) for path in iterator if path.is_file() and path.suffix.lower() == ".mp3"]

    if not files:
        raise FileNotFoundError(f"No MP3 files found in: {folder}")

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
        if path.is_file() and path.suffix.lower() == ".mp3":
            files.append(str(path))
        elif path.is_file():
            print(f"Skipping non-MP3 manifest file: {path}")
        else:
            print(f"Skipping missing manifest file: {path}")

    if not files:
        raise FileNotFoundError(f"No existing MP3 files found from manifest: {manifest}")

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
    path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def upload_one_file(base_url, file_path):
    file_path = Path(file_path).resolve()
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http":
        raise ValueError("The Trinity Android LAN receiver uses plain HTTP on your Wi-Fi LAN.")

    host = parsed.hostname
    port = parsed.port or PORT
    remote_path = "/upload-file?filename=" + urllib.parse.quote(file_path.name, safe="")
    size = file_path.stat().st_size

    conn = http.client.HTTPConnection(host, port=port, timeout=60)
    try:
        conn.putrequest("POST", remote_path, skip_accept_encoding=True)
        conn.putheader("Host", f"{host}:{port}")
        conn.putheader("User-Agent", "trinity-lan-uploader/1.0")
        conn.putheader("Content-Type", "audio/mpeg")
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
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Android receiver returned HTTP {response.status}: {body}")

        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": True, "raw_response": body}
    finally:
        conn.close()


def upload_files(base_url, files):
    successful = []
    failed = []

    for index, file_path in enumerate(files, start=1):
        item = file_items([file_path])[0]
        print(f"[{index}/{len(files)}] Uploading: {item['name']} ({item['size']} bytes)")
        try:
            response = upload_one_file(base_url, file_path)
            item["receiver_response"] = response
            successful.append(item)
            print(f"    OK: {item['name']}")
        except Exception as error:
            item["error"] = str(error)
            failed.append(item)
            print(f"    FAILED: {item['name']}: {error}")

    return successful, failed


def main():
    parser = argparse.ArgumentParser(
        description="Find the Trinity Android LAN receiver and upload local MP3 files directly."
    )
    parser.add_argument(
        "--dir",
        default=DEFAULT_UPLOAD_DIR,
        help="Folder whose MP3 files should be uploaded.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="JSON manifest listing the exact MP3 files to upload.",
    )
    parser.add_argument(
        "--summary-json",
        default=None,
        help="Write upload result summary to this JSON file.",
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
        help="Upload MP3 files inside subfolders too.",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only find and print the Android receiver URL; do not upload.",
    )
    args = parser.parse_args()

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
            url = normalize_base_url(args.url) if args.url else find_first_server()
            if args.url:
                url = probe_url(url) or url
        except Exception as e:
            summary["error"] = str(e)
            write_summary(args.summary_json, summary)
            print(f"Upload setup failed: {e}")
            sys.exit(1)

        if not url:
            summary["error"] = "No server found."
            write_summary(args.summary_json, summary)
            print("No server found. Open the Trinity app on the same Wi-Fi and tap Start LAN Server.")
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

    try:
        url = normalize_base_url(args.url) if args.url else find_first_server()
        if args.url:
            url = probe_url(url) or url
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
        print("No server found. Open the Trinity app on the same Wi-Fi and tap Start LAN Server.")
        sys.exit(1)

    summary["url"] = url
    print(f"Found Android receiver: {url}")
    print(f"Uploading {len(files)} MP3 file(s) from: {source_label}")

    successful, failed = upload_files(url, files)
    summary["successful"] = successful
    summary["failed"] = failed

    if failed:
        summary["error"] = f"{len(failed)} file(s) failed to upload."
        write_summary(args.summary_json, summary)
        print(summary["error"])
        sys.exit(1)

    write_summary(args.summary_json, summary)
    print("Upload finished.")


if __name__ == "__main__":
    main()
