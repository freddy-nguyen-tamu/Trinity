#!/usr/bin/env python3
import argparse
import math
import os
import shutil
import subprocess
import sys

"""
python speed_mp3.py input.mp3 output_1_25x.mp3 1.25
python speed_mp3.py input.mp3 output_2x.mp3 2.0
python speed_mp3.py input.mp3 output_0_8x.mp3 0.8
python speed_mp3.py input.mp3 output_3x.mp3 3.0 --method atempo
python speed_mp3.py input.mp3 output_1_35x.mp3 1.35 --method rubberband
"""

def split_speed_for_atempo(speed: float) -> list[float]:
    """
    Split a speed factor into multiple atempo stages so every stage stays
    in FFmpeg's safer [0.5, 2.0] range.
    """
    if speed <= 0:
        raise ValueError("speed must be greater than 0")

    if 0.5 <= speed <= 2.0:
        return [speed]

    if speed > 2.0:
        stages = math.ceil(math.log2(speed))
    else:
        stages = math.ceil(math.log2(1.0 / speed))

    base = speed ** (1.0 / stages)
    factors = [base] * (stages - 1)

    # Correct floating-point drift in the final stage so the exact product
    # equals the requested speed.
    remaining = speed / math.prod(factors) if factors else speed
    factors.append(remaining)

    if not all(0.5 <= f <= 2.0 for f in factors):
        raise RuntimeError(
            f"Could not split speed={speed} into valid atempo stages: {factors}"
        )

    return factors


def ffmpeg_has_filter(ffmpeg_path: str, filter_name: str) -> bool:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False

    output = (result.stdout + "\n" + result.stderr).lower()
    return filter_name.lower() in output


def build_filter_expression(speed: float, method: str, ffmpeg_path: str) -> str:
    chosen_method = method

    if method == "auto":
        chosen_method = "rubberband" if ffmpeg_has_filter(ffmpeg_path, "rubberband") else "atempo"

    if chosen_method == "rubberband":
        if not ffmpeg_has_filter(ffmpeg_path, "rubberband"):
            raise RuntimeError(
                "FFmpeg does not have the rubberband filter enabled. "
                "Use --method atempo or install an FFmpeg build with librubberband."
            )
        # pitch=1 keeps pitch unchanged while tempo changes
        return f"rubberband=tempo={speed:.12g}:pitch=1"

    factors = split_speed_for_atempo(speed)
    return ",".join(f"atempo={factor:.12g}" for factor in factors)


def speed_change_mp3(
    input_path: str,
    output_path: str,
    speed: float,
    bitrate: str = "192k",
    method: str = "auto",
    ffmpeg_path: str = "ffmpeg",
) -> None:
    if speed <= 0:
        raise ValueError("speed must be greater than 0")

    input_abs = os.path.abspath(os.path.expanduser(input_path))
    output_abs = os.path.abspath(os.path.expanduser(output_path))

    if input_abs == output_abs:
        raise ValueError("input and output must be different files")

    if not os.path.isfile(input_abs):
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if shutil.which(ffmpeg_path) is None:
        raise FileNotFoundError(
            f"Could not find FFmpeg executable: {ffmpeg_path}"
        )

    filter_expr = build_filter_expression(speed, method, ffmpeg_path)

    cmd = [
        ffmpeg_path,
        "-y",
        "-i", input_abs,
        "-vn",                    # ignore album art / video streams
        "-map_metadata", "0",     # keep metadata when possible
        "-filter:a", filter_expr,
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        output_abs,
    ]

    print("Running:")
    print(" ".join(f'"{part}"' if " " in part else part for part in cmd))
    print(f"Audio filter: {filter_expr}")

    result = subprocess.run(cmd, check=False)

    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed with exit code {result.returncode}")

    print(f"\nDone: {output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a speed-changed MP3 while preserving pitch."
    )
    parser.add_argument("input", help="Input MP3 path")
    parser.add_argument("output", help="Output MP3 path")
    parser.add_argument("speed", type=float, help="Playback speed, e.g. 1.25, 0.8, 2.0")
    parser.add_argument(
        "--bitrate",
        default="192k",
        help="Output MP3 bitrate (default: 192k)",
    )
    parser.add_argument(
        "--method",
        choices=["auto", "atempo", "rubberband"],
        default="auto",
        help="Processing method (default: auto)",
    )
    parser.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="FFmpeg executable path/name (default: ffmpeg)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        speed_change_mp3(
            input_path=args.input,
            output_path=args.output,
            speed=args.speed,
            bitrate=args.bitrate,
            method=args.method,
            ffmpeg_path=args.ffmpeg,
        )
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())