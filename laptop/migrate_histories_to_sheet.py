"""One-time, non-destructive import from Trinity's old local JSON histories.

Usage:
    python migrate_histories_to_sheet.py --check
    python migrate_histories_to_sheet.py --migrate
    python migrate_histories_to_sheet.py --migrate --additional-dir D:\\old-trinity\\laptop

Re-running is safe: an existing or previously deleted Sheets key is never
re-imported from a stale local history. All original JSON files remain intact.
"""

import argparse
import json
from pathlib import Path

from shared_history import check_connection, migrate_map, migrate_set

NAMES = {
    "download_history.json": ("download_history", "set"),
    "_android_uploaded_history.json": ("android_uploaded", "set"),
    "_drive_uploaded_history.json": ("drive_uploaded", "set"),
    "_test_processed_history.json": ("test_processed", "set"),
    "_lyrics_not_found_history.json": ("lyrics_not_found", "map"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Verify access to the shared Sheet")
    mode.add_argument("--migrate", action="store_true", help="Merge old local histories into the Sheet")
    parser.add_argument("--additional-dir", action="append", type=Path, default=[],
                        help="Also migrate histories from another laptop/ directory")
    args = parser.parse_args()
    counts = check_connection()
    print("Connected to shared Google Sheet. Current record counts:", counts)
    if args.check:
        return

    dirs = [Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent,
            *args.additional_dir]
    total = 0
    already_seen_paths = set()
    for folder in dirs:
        for filename, (namespace, kind) in NAMES.items():
            path = folder.expanduser().resolve() / filename
            if not path.is_file() or path in already_seen_paths:
                continue
            already_seen_paths.add(path)
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if kind == "set":
                    if not isinstance(data, list):
                        raise ValueError("expected a JSON list")
                    count = migrate_set(namespace, [str(item) for item in data])
                    source_size = len(data)
                else:
                    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
                        raise ValueError("expected a JSON object containing an items mapping")
                    count = migrate_map(namespace, data["items"])
                    source_size = len(data["items"])
            except Exception as exc:
                raise RuntimeError(f"Migration stopped at {path}; original file untouched: {exc}") from exc
            print(f"{path}: {source_size} local entries, {count} newly imported")
            total += count
    print(f"Migration complete: {total} new record(s). Originals preserved.")
    print("Remote record counts:", check_connection())


if __name__ == "__main__":
    main()
