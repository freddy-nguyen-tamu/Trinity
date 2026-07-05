import argparse
import copy
import shutil
import subprocess
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError, TLEN
from mutagen.mp3 import MP3


def build_atempo_chain(speed: float) -> str:
    """
    FFmpeg atempo changes speed while preserving pitch.
    For compatibility, split values so each atempo filter stays between 0.5 and 2.0.
    """
    if speed <= 0:
        raise ValueError("Speed must be greater than 0.")

    filters = []

    while speed > 2.0:
        filters.append("atempo=2.0")
        speed /= 2.0

    while speed < 0.5:
        filters.append("atempo=0.5")
        speed /= 0.5

    filters.append(f"atempo={speed:.8f}")
    return ",".join(filters)


def format_speed_for_filename(speed: float) -> str:
    text = str(speed).rstrip("0").rstrip(".")
    return text.replace(".", "_")


def copy_id3_tags(source_mp3: Path, output_mp3: Path) -> None:
    """
    Copy original ID3 tags exactly-ish from source to output.

    This preserves common MP3 attributes like:
    - title
    - artist
    - album
    - year/date
    - genre
    - track number
    - album art / cover image
    - embedded lyrics
    - custom ID3 frames
    """
    try:
        original_tags = ID3(source_mp3)
    except ID3NoHeaderError:
        print("No ID3 metadata found in original file.")
        return

    copied_tags = copy.deepcopy(original_tags)

    # If the original had a TLEN duration tag, update it to the new duration.
    if "TLEN" in copied_tags:
        try:
            new_audio = MP3(output_mp3)
            new_length_ms = int(new_audio.info.length * 1000)
            copied_tags.delall("TLEN")
            copied_tags.add(TLEN(encoding=3, text=str(new_length_ms)))
        except Exception as exc:
            print(f"Warning: could not update TLEN duration tag: {exc}")

    # Remove whatever FFmpeg wrote, then write the copied original tags.
    try:
        existing_tags = ID3(output_mp3)
        existing_tags.delete(output_mp3)
    except ID3NoHeaderError:
        pass

    # Preserve original ID3v2.3 vs ID3v2.4 when possible.
    # Clamp to supported versions (3 or 4) because v2.2 is not writable.
    version_number = original_tags.version[1] if len(original_tags.version) > 1 else 4
    if version_number not in (3, 4):
        version_number = 4

    copied_tags.save(output_mp3, v2_version=version_number)

    print("Copied original MP3 metadata:")
    print("- title / artist / album tags")
    print("- cover art")
    print("- lyrics")
    print("- custom ID3 frames")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Speed up or slow down an MP3 while preserving pitch and metadata."
    )
    parser.add_argument("input", help="Input MP3 file")
    parser.add_argument("speed", type=float, help="Speed factor, e.g. 2 for twice as fast")
    parser.add_argument(
        "-o",
        "--output",
        help="Output filename (optional). The file will always be placed in the same directory as the input MP3.",
    )

    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg was not found. Install ffmpeg and make sure it is in PATH.")

    input_path = Path(args.input)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if input_path.suffix.lower() != ".mp3":
        print("Warning: input file does not end with .mp3, but continuing anyway.")

    # Determine output path: always in the same directory as the input.
    if args.output:
        # Use only the filename part of the given output, discard any directory.
        output_filename = Path(args.output).name
        output_path = input_path.parent / output_filename
    else:
        speed_text = format_speed_for_filename(args.speed)
        output_path = input_path.with_name(f"{input_path.stem}_{speed_text}x.mp3")

    if input_path.resolve() == output_path.resolve():
        raise ValueError("Output file cannot be the same as the input file.")

    atempo_chain = build_atempo_chain(args.speed)

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(input_path),
        "-filter:a",
        atempo_chain,
        "-vn",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(output_path),
    ]

    print("Speed filter:")
    print(atempo_chain)
    print()
    print("Running FFmpeg...")
    subprocess.run(cmd, check=True)

    print()
    print("Restoring original metadata...")
    copy_id3_tags(input_path, output_path)

    print()
    print(f"Done: {output_path}")


if __name__ == "__main__":
    main()