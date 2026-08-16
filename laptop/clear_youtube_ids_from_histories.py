import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_IDS_FILE = SCRIPT_DIR / "youtube_ids_to_clear_from_histories.txt"
YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_ID_ANYWHERE_RE = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
YOUTUBE_PATH_ID_RE = re.compile(
    r"(?:youtu\.be/|/(?:shorts|embed|live|v)/)([A-Za-z0-9_-]{11})(?=[/?#&\s\"'>)]|$)"
)
YOUTUBE_QUERY_ID_RE = re.compile(r"(?:[?&#]|^)v=([A-Za-z0-9_-]{11})(?=[&#\s]|$)")
YOUTUBE_RADIO_LIST_RE = re.compile(r"^(?:RD|RDMM)([A-Za-z0-9_-]{11})$")


def append_unique(items, item):
    if item and item not in items:
        items.append(item)


def extract_ids_from_url_like_text(text):
    ids = []
    parsed = urlparse(text)

    query = parse_qs(parsed.query)
    for key in ("v", "vi"):
        for value in query.get(key, []):
            if YOUTUBE_ID_RE.fullmatch(value):
                append_unique(ids, value)

    for value in query.get("list", []):
        match = YOUTUBE_RADIO_LIST_RE.fullmatch(value)
        if match:
            append_unique(ids, match.group(1))

    path_parts = [part for part in parsed.path.split("/") if part]
    host = parsed.netloc.lower()

    if host.endswith("youtu.be") and path_parts and YOUTUBE_ID_RE.fullmatch(path_parts[0]):
        append_unique(ids, path_parts[0])

    for marker in ("shorts", "embed", "live", "v"):
        if marker in path_parts:
            index = path_parts.index(marker)
            if index + 1 < len(path_parts) and YOUTUBE_ID_RE.fullmatch(path_parts[index + 1]):
                append_unique(ids, path_parts[index + 1])

    return ids


def extract_youtube_ids(line):
    text = line.strip()
    if not text or text.startswith("#"):
        return []

    text = re.split(r"\s+#", text, maxsplit=1)[0].strip()

    if YOUTUBE_ID_RE.fullmatch(text):
        return [text]

    ids = []
    texts_to_scan = [text]
    decoded_text = unquote(text)
    if decoded_text != text:
        texts_to_scan.append(decoded_text)

    for candidate in texts_to_scan:
        for youtube_id in extract_ids_from_url_like_text(candidate):
            append_unique(ids, youtube_id)

        for match in YOUTUBE_QUERY_ID_RE.finditer(candidate):
            append_unique(ids, match.group(1))

        for match in YOUTUBE_PATH_ID_RE.finditer(candidate):
            append_unique(ids, match.group(1))

    if ids:
        return ids

    for match in YOUTUBE_ID_ANYWHERE_RE.finditer(text):
        append_unique(ids, match.group(1))

    return ids


def load_ids(ids_file):
    if not ids_file.exists():
        raise FileNotFoundError(f"IDs file does not exist: {ids_file}")

    ids = []
    invalid_lines = []

    for line_number, line in enumerate(ids_file.read_text(encoding="utf-8-sig").splitlines(), start=1):
        youtube_ids = extract_youtube_ids(line)
        if youtube_ids:
            for youtube_id in youtube_ids:
                append_unique(ids, youtube_id)
            continue

        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            invalid_lines.append((line_number, stripped))

    return ids, invalid_lines


def item_matches_ids(item, ids):
    if not isinstance(item, str):
        return False

    # YouTube video IDs are case-sensitive. Do not clear a different video whose
    # ID differs only by letter case.
    return any(youtube_id in item for youtube_id in ids)


def value_matches_ids(value, ids):
    if isinstance(value, str):
        return item_matches_ids(value, ids)

    if isinstance(value, list):
        return any(value_matches_ids(item, ids) for item in value)

    if isinstance(value, dict):
        return any(
            item_matches_ids(str(key), ids) or value_matches_ids(item, ids)
            for key, item in value.items()
        )

    return False


def dict_has_direct_id_trace(data, ids):
    """Return whether a mapping directly identifies one requested video."""
    return any(
        item_matches_ids(str(key), ids)
        or (isinstance(value, str) and item_matches_ids(value, ids))
        for key, value in data.items()
    )


def filter_json_data(data, ids):
    """Remove ID-bearing records while preserving an arbitrary JSON shape."""
    removed = []

    if isinstance(data, list):
        kept = []
        for item in data:
            if value_matches_ids(item, ids):
                removed.append(str(item))
            else:
                kept.append(item)
        return kept, removed

    if isinstance(data, dict):
        changed = False
        filtered = {}
        for key, value in data.items():
            if item_matches_ids(str(key), ids):
                changed = True
                removed.append(str(key))
                continue

            # Dictionary-of-record histories commonly use an unrelated path or
            # filename as the key and put video_id in a direct field. Remove the
            # whole record so it cannot remain as stale processed/uploaded state.
            if isinstance(value, dict) and dict_has_direct_id_trace(value, ids):
                changed = True
                removed.append(str(key))
            elif isinstance(value, (list, dict)):
                new_value, nested_removed = filter_json_data(value, ids)
                filtered[key] = new_value
                if nested_removed:
                    changed = True
                    removed.extend(f"{key}: {item}" for item in nested_removed)
            elif value_matches_ids(value, ids):
                changed = True
                removed.append(f"{key}: {value}")
            else:
                filtered[key] = value
        return filtered if changed else data, removed

    if isinstance(data, str) and item_matches_ids(data, ids):
        return None, [data]

    return data, removed


# Keep the old function name available for callers that imported this script.
filter_history_data = filter_json_data


def load_json(path):
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_json(path, data):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            temporary_path = Path(f.name)
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def find_json_files(root_dir=SCRIPT_DIR):
    root_dir = Path(root_dir).resolve()
    return sorted(
        (
            path
            for path in root_dir.rglob("*")
            if path.is_file() and path.suffix.casefold() == ".json"
        ),
        key=lambda path: str(path).casefold(),
    )


def clear_ids_from_histories(ids, dry_run=False, root_dir=SCRIPT_DIR):
    root_dir = Path(root_dir).resolve()
    json_files = find_json_files(root_dir)
    results = []

    for path in json_files:
        try:
            data = load_json(path)
        except Exception as e:
            results.append((path, None, [f"ERROR reading JSON: {e}"]))
            continue

        filtered, removed = filter_json_data(data, ids)
        if removed and value_matches_ids(filtered, ids):
            results.append(
                (
                    path,
                    None,
                    ["ERROR: an ID trace remained after filtering; file was not changed"],
                )
            )
            continue

        if removed and not dry_run:
            try:
                save_json(path, filtered)
            except Exception as e:
                results.append((path, None, [f"ERROR writing JSON: {e}"]))
                continue

        results.append((path, len(removed), removed))

    return results


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Recursively remove listed YouTube video IDs from every JSON file "
            "under the laptop folder."
        )
    )
    parser.add_argument(
        "--ids-file",
        default=str(DEFAULT_IDS_FILE),
        help="Text file containing one YouTube ID or URL per line.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without changing history files.",
    )
    args = parser.parse_args()

    ids_file = Path(args.ids_file).expanduser().resolve()
    ids, invalid_lines = load_ids(ids_file)

    print(f"IDs file: {ids_file}")
    if invalid_lines:
        print("Ignored lines that did not contain a YouTube video ID:")
        for line_number, text in invalid_lines:
            print(f"  {line_number}: {text}")

    if not ids:
        print("No YouTube IDs found to clear.")
        return

    print(f"Clearing {len(ids)} YouTube ID(s):")
    for youtube_id in ids:
        print(f"  {youtube_id}")

    if args.dry_run:
        print("")
        print("DRY RUN: no files will be changed.")

    json_files = find_json_files(SCRIPT_DIR)
    print(f"Scanning {len(json_files)} JSON file(s) recursively under: {SCRIPT_DIR}")
    results = clear_ids_from_histories(ids, dry_run=args.dry_run)

    print("")
    print("JSON cleanup summary")
    print("-" * 50)

    total_removed = 0
    for path, removed_count, removed_items in results:
        try:
            display_path = path.relative_to(SCRIPT_DIR)
        except ValueError:
            display_path = path

        if removed_count is None:
            print(f"{display_path}: {removed_items[0]}")
            continue

        total_removed += removed_count
        print(
            f"{display_path}: removed {removed_count} "
            f"entr{'y' if removed_count == 1 else 'ies'}"
        )
        for item in removed_items:
            print(f"  {item}")

    print("-" * 50)
    print(f"Total removed: {total_removed}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("")
        print("Interrupted.")
        sys.exit(130)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)
