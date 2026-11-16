#!/usr/bin/env python3
"""Audio Trimmer & Crop Queue — based on the supplied mp3trimmer.py.

Requirements: Python 3.9+, PyQt5, numpy, mutagen, FFmpeg and FFprobe in PATH.
Install Python packages: pip install PyQt5 numpy mutagen

Crops store ONLY source-file paths and edit ranges; no cropped audio is saved.
The queue is auto-restored between launches. Source audio files must remain in place.
The temporary WAV used for joined preview is deleted when it is no longer needed.
Per-crop optional speed uses pitch-preserving FFmpeg atempo at PLAY/EXPORT time.
In joined editing, changing a rate modifies only the virtual crop model and timeline.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import traceback
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PyQt5 import QtCore, QtGui, QtMultimedia, QtWidgets
from mutagen import File as MutagenFile
from mutagen.id3 import (
    APIC, COMM, ID3, ID3NoHeaderError, TALB, TBPM, TCOM, TCON, TCOP, TDRC,
    TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, USLT,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

SAMPLE_RATE = 44100
# Joined editing changes only the stored Crop range/rate. Preview waveforms
# use the source PCM without audio processing; FFmpeg pitch-preserving atempo
# is applied only when playing/exporting a speed-adjusted crop. No fades,
# crossfades, pitch shifting, or changes to the original files are introduced.
MIN_SPEED_RATE = 0.05
MAX_SPEED_RATE = 32.0
PREVIEW_ROW_HEIGHT = 86
DEFAULT_DIR = r"C:\Users\qacer\Downloads\ytb\laptop\finished"
OPEN_FILTER = (
    "Audio files (*.mp3 *.m4a *.m4u *.m4b *.aac *.flac *.wav *.ogg "
    "*.opus *.wma *.aif *.aiff *.mp4);;All files (*)"
)
SAVE_FILTERS = (
    "MP3 - high quality (*.mp3)",
    "M4A - AAC 256 kbps (*.m4a)",
    "M4A - ALAC lossless (*.m4a)",
)
META_FIELDS = ("title", "artist", "lyrics", "artwork")
LABELS = {"title": "song title", "artist": "artist", "lyrics": "lyrics", "artwork": "thumbnail / cover art"}


def format_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    return f"{int(seconds // 60):02d}:{seconds % 60:06.3f}"


def subprocess_ok(args: Sequence[str]) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(list(args), capture_output=True, text=True, errors="replace", creationflags=flags)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Unknown FFmpeg error")[-6000:])
    return result


def ffprobe_duration(path: str) -> float:
    p = subprocess_ok(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=noprint_wrappers=1:nokey=1", path])
    return float(p.stdout.strip())


def temporary_path(suffix: str, directory: Optional[str] = None) -> str:
    fd, path = tempfile.mkstemp(prefix="audiocrop_", suffix=suffix, dir=directory)
    os.close(fd)
    return path


def safe_unlink(path: Optional[str]) -> None:
    if path:
        try:
            os.unlink(path)
        except OSError:
            pass


def decode_mono_samples(path: str) -> np.ndarray:
    """Waveform-only PCM cache. Actual export always uses ORIGINAL source audio."""
    tmp = temporary_path(".pcm")
    try:
        subprocess_ok(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                       "-i", path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                       "-f", "s16le", "-acodec", "pcm_s16le", tmp])
        samples = np.fromfile(tmp, dtype="<i2")
        if not len(samples):
            raise ValueError("No decodable audio found in the selected file")
        return samples
    finally:
        safe_unlink(tmp)


def write_wav_fast(path: str, samples: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    import struct
    data = np.ascontiguousarray(samples, dtype="<i2")
    n = data.nbytes
    if n > 0xFFFFFFFF - 36:
        raise ValueError("Preview segment exceeds the 4 GB WAV limit")
    with open(path, "wb") as f:
        f.write(struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + n, b"WAVE",
                            b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16, b"data", n))
        data.tofile(f)


def validate_speed_rate(rate: Any) -> float:
    try:
        value = float(rate)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Speed must be a valid number")
    if not math.isfinite(value) or not MIN_SPEED_RATE <= value <= MAX_SPEED_RATE:
        raise ValueError(f"Speed must be between {MIN_SPEED_RATE:g}× and {MAX_SPEED_RATE:g}×")
    return value


def atempo_factors(rate: float) -> List[float]:
    """Decompose tempo into high-quality .5–2 stages (avoids high-rate skips).

    FFmpeg `atempo` time-stretches without changing pitch. Chaining bounded
    stages avoids the documented audio sample skipping at extreme values.
    """
    remaining = validate_speed_rate(rate)
    if abs(remaining - 1) < 1e-9:
        return []
    factors: List[float] = []
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-8:
        factors.append(remaining)
    return factors


@dataclass
class Crop:
    uid: str
    name: str
    source: str
    start: float
    end: float
    keep: bool
    duration: float
    file_size: int = 0
    file_mtime_ns: int = 0
    speed_enabled: bool = False
    speed_rate: float = 1.0

    def playback_rate(self) -> float:
        return validate_speed_rate(self.speed_rate) if self.speed_enabled else 1.0

    @classmethod
    def from_record(cls, value: Dict[str, Any]) -> "Crop":
        return cls(uid=str(value["uid"]), name=str(value["name"]),
                   source=str(value["source"]), start=float(value["start"]),
                   end=float(value["end"]), keep=bool(value["keep"]),
                   duration=float(value["duration"]),
                   file_size=int(value.get("file_size", 0)),
                   file_mtime_ns=int(value.get("file_mtime_ns", 0)),
                   speed_enabled=bool(value.get("speed_enabled", False)),
                   speed_rate=validate_speed_rate(value.get("speed_rate", 1.0)))

    def parts(self) -> List[Tuple[float, float]]:
        """'Remove' keeps BOTH sides of the removed interval as one logical crop."""
        end = max(0.0, self.duration)
        a = max(0.0, min(self.start, end))
        b = max(a, min(self.end, end))
        choices = [(a, b)] if self.keep else [(0.0, a), (b, end)]
        return [(a, b) for a, b in choices if b - a >= 0.001]

    def effective_duration(self) -> float:
        return sum(b - a for a, b in self.parts()) / self.playback_rate()


@dataclass(frozen=True)
class PreviewSegment:
    """One audible source interval in the joined timeline (all times in seconds)."""
    crop_uid: str
    crop_name: str
    source: str
    part_index: int  # 0=first kept piece, 1=second piece of a Remove crop
    source_start: float
    source_end: float
    preview_start: float
    preview_end: float
    start_editable: bool
    end_editable: bool
    speed_rate: float = 1.0

    def boundary_time(self, side: str) -> float:
        return self.preview_start if side == "start" else self.preview_end

    def original_time(self, side: str) -> float:
        return self.source_start if side == "start" else self.source_end

    def boundary_editable(self, side: str) -> bool:
        return self.start_editable if side == "start" else self.end_editable


def preview_parts(crop: Crop) -> List[Tuple[int, float, float]]:
    """Retain the logical part index even if one Remove crop side is empty."""
    limit = max(0.0, crop.duration)
    a = max(0.0, min(crop.start, limit))
    b = max(a, min(crop.end, limit))
    pieces = [(0, a, b)] if crop.keep else [(0, 0.0, a), (1, b, limit)]
    return [(index, start, end) for index, start, end in pieces if end - start >= .001]


def describe_joined_preview(crops: Sequence[Crop],
                            source_samples: Dict[str, np.ndarray]) -> Tuple[List[PreviewSegment], int]:
    """Map original source timestamps onto a virtual playback timeline.

    This creates NO joined PCM, no clipped source files, and no audio effects.
    Called on every boundary edit so its sole output is updated timeline data.
    """
    segments: List[PreviewSegment] = []
    position = 0
    for crop in crops:
        if crop.source not in source_samples:
            raise ValueError("Missing preview samples for " + crop.source)
        source = source_samples[crop.source]
        rate = crop.playback_rate()
        for part_index, a, b in preview_parts(crop):
            first = max(0, min(len(source), round(a * SAMPLE_RATE)))
            last = max(first, min(len(source), round(b * SAMPLE_RATE)))
            if last <= first:
                continue
            start = position / SAMPLE_RATE
            position += max(1, round((last - first) / rate))
            end = position / SAMPLE_RATE
            segments.append(PreviewSegment(
                crop_uid=crop.uid, crop_name=crop.name, source=crop.source,
                part_index=part_index, source_start=first / SAMPLE_RATE,
                source_end=last / SAMPLE_RATE, preview_start=start, preview_end=end,
                start_editable=crop.keep or part_index == 1,
                end_editable=crop.keep or part_index == 0,
                speed_rate=rate,
            ))
    if not segments:
        raise ValueError("None of the crops contain audible audio")
    return segments, position


def assemble_joined_preview(crops: Sequence[Crop],
                            source_samples: Dict[str, np.ndarray]) -> Tuple[np.ndarray, List[PreviewSegment]]:
    """Create playback PCM ONLY on demand, from unmodified source samples.

    The interactive editor is virtual: it calls describe_joined_preview instead.
    This function does not use any DSP/fades/normalization or hard cut source
    files; it simply presents selected intervals consecutively to the player.
    """
    if any(abs(crop.playback_rate() - 1.0) > 1e-8 for crop in crops):
        raise ValueError("Time-stretched audio must be rendered via pitch-preserving FFmpeg atempo")
    segments, sample_count = describe_joined_preview(crops, source_samples)
    parts = [source_samples[seg.source][round(seg.source_start * SAMPLE_RATE):
                                         round(seg.source_end * SAMPLE_RATE)]
             for seg in segments]
    joined = np.concatenate(parts)
    if len(joined) != sample_count:
        raise RuntimeError("Joined audio differs from the source timestamp map")
    return joined, segments


def nudge_preview_boundary(crop: Crop, part_index: int, side: str,
                           delta: float) -> bool:
    """Change the real source timestamp behind a preview marker.

    For a Remove crop, only the two edges bordering its removed area are
    editable; the source-file start/end are immovable. Nonzero pieces cannot
    be accidentally inverted or eliminated by a keyboard repeat.
    """
    if side not in ("start", "end"):
        raise ValueError("Unknown boundary side")
    if crop.keep:
        field = side
    elif part_index == 0 and side == "end":
        field = "start"
    elif part_index == 1 and side == "start":
        field = "end"
    else:
        return False
    # The preview renderer excludes pieces shorter than 1 ms. After sample
    # quantization, 0.001 s can round DOWN to 44 samples at 44.1 kHz; use
    # ceil to keep even the tiniest legal crop visible and selectable.
    minimum = math.ceil(.001 * SAMPLE_RATE) / SAMPLE_RATE
    if field == "start":
        lower = minimum if not crop.keep else 0.0
        upper = crop.end - minimum if crop.keep else crop.end
    else:
        lower = crop.start + minimum if crop.keep else crop.start
        upper = crop.duration - minimum if not crop.keep else crop.duration
    if upper < lower:
        return False
    old = getattr(crop, field)
    new = max(lower, min(upper, old + float(delta)))
    new = round(new * SAMPLE_RATE) / SAMPLE_RATE
    if abs(new - old) < 0.5 / SAMPLE_RATE:
        return False
    setattr(crop, field, new)
    return True


def source_audio_profile(path: str) -> Tuple[int, int]:
    """Probe original rate/channels so export need not inherit waveform settings."""
    result = subprocess_ok(["ffprobe", "-v", "error", "-select_streams", "a:0",
                           "-show_entries", "stream=sample_rate,channels", "-of", "json", path])
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ValueError("No usable audio stream in " + path)
    return int(streams[0].get("sample_rate") or SAMPLE_RATE), int(streams[0].get("channels") or 2)


def render_audio(crops: Sequence[Crop], path: str, codec: str) -> None:
    """Export original-source sample ranges, respecting the joined editor timestamps.

    Unlike a timestamp-based `atrim=start:...`, `atrim=start_sample:...`
    indexes decoded audio from sample zero. This removes ambiguity from MP3
    encoder delay, container start_time, and non-zero input presentation times.

    Multiple crop pieces from the SAME audio input are branched through `asplit`
    before trimming, making it explicit that every piece independently reads
    the unmodified source. Time stretching is per-piece, using `atempo` only
    for crops whose effective speed differs from 1×. It changes tempo without
    shifting pitch. No fades, gain changes, crossfades or source modifications.
    """
    segments: List[Tuple[str, float, float, float]] = []
    for crop in crops:
        rate = crop.playback_rate()
        for a, b in crop.parts():
            segments.append((crop.source, a, b, rate))
    if not segments:
        raise ValueError("The selected crop(s) contain no audio")

    sources = list(dict.fromkeys(src for src, _, _, _ in segments))
    missing = [src for src in sources if not os.path.isfile(src)]
    if missing:
        raise FileNotFoundError("Source file missing:\n" + "\n".join(missing))

    index = {source: i for i, source in enumerate(sources)}
    profiles = {source: source_audio_profile(source) for source in sources}
    if codec == "preview":
        output_rate, layout, sample_format = SAMPLE_RATE, "mono", "s16"
    else:
        rates = [rate for rate, channels in profiles.values()]
        output_rate = max(rates)
        if codec == "mp3":
            output_rate = min((32000, 44100, 48000),
                              key=lambda rate: abs(rate - min(output_rate, 48000)))
        elif codec == "aac":
            output_rate = min(output_rate, 96000)
        else:
            output_rate = min(output_rate, 192000)
        layout = "mono" if all(channels == 1 for rate, channels in profiles.values()) else "stereo"
        sample_format = "s32p" if codec == "alac" else "fltp"

    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for source in sources:
        args.extend(["-i", source])

    # Give each reuse of a source its own explicitly numbered input branch.
    # Without asplit, reusing the same labelled stream relies on implicit graph
    # behavior, which is fragile for multiple edits on a compressed source.
    used_per_source: Dict[str, int] = {source: 0 for source in sources}
    counts: Dict[str, int] = {source: 0 for source in sources}
    for source, _, _, _ in segments:
        counts[source] += 1
    chains: List[str] = []
    for source in sources:
        if counts[source] > 1:
            pads = "".join(f"[input{index[source]}_{i}]" for i in range(counts[source]))
            chains.append(f"[{index[source]}:a:0]asplit={counts[source]}{pads}")

    for number, (source, start, end, rate) in enumerate(segments):
        branch = used_per_source[source]
        used_per_source[source] += 1
        input_pad = (f"[input{index[source]}_{branch}]" if counts[source] > 1
                     else f"[{index[source]}:a:0]")
        # atrim's *_sample options count decoded audio, independent of PTS.
        # Use the ORIGINAL source's sample rate, not the 44.1-kHz graph rate.
        source_rate, _ = profiles[source]
        first_sample = max(0, round(start * source_rate))
        end_sample = max(first_sample + 1, round(end * source_rate))
        filters = [
            f"atrim=start_sample={first_sample}:end_sample={end_sample}",
            "asetpts=N/SR/TB",
            f"aresample={output_rate}",
        ]
        filters.extend(f"atempo={factor:.12g}" for factor in atempo_factors(rate))
        filters.extend([
            f"aformat=sample_fmts={sample_format}:channel_layouts={layout}",
            "asetpts=N/SR/TB",
        ])
        chains.append(input_pad + ",".join(filters) + f"[s{number}]")

    if len(segments) == 1:
        chains.append("[s0]anull[aout]")
    else:
        chains.append("".join(f"[s{i}]" for i in range(len(segments))) +
                      f"concat=n={len(segments)}:v=0:a=1[aout]")
    args += ["-filter_complex", ";".join(chains), "-map", "[aout]",
             "-map_metadata", "-1", "-vn"]
    if codec == "preview":
        args += ["-c:a", "pcm_s16le", "-f", "wav"]
    elif codec == "mp3":
        args += ["-c:a", "libmp3lame", "-q:a", "2", "-id3v2_version", "3", "-f", "mp3"]
    elif codec == "alac":
        args += ["-c:a", "alac", "-f", "ipod"]
    elif codec == "aac":
        args += ["-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", "-f", "ipod"]
    else:
        raise ValueError("Unknown export codec: " + codec)
    args.append(path)
    subprocess_ok(args)
    if os.path.getsize(path) < 128:
        raise ValueError("FFmpeg produced an unexpectedly small output")


class WaveformWidget(QtWidgets.QWidget):
    # The waveform follows available window WIDTH, but stops growing vertically.
    # Large monitors and maximized windows must not stretch it into a tall graph.
    MAX_WAVEFORM_HEIGHT = 280

    leftHandleMoved = QtCore.pyqtSignal(float)
    rightHandleMoved = QtCore.pyqtSignal(float)
    cursorClicked = QtCore.pyqtSignal(float)
    handleClicked = QtCore.pyqtSignal(str)
    handleFocusLost = QtCore.pyqtSignal()
    dragFinished = QtCore.pyqtSignal(str)
    previewBoundaryClicked = QtCore.pyqtSignal(object)
    previewBoundaryDragged = QtCore.pyqtSignal(object, float)   # key, absolute SOURCE time
    previewDragFinished = QtCore.pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setMinimumSize(200, 115)
        self.setMaximumHeight(self.MAX_WAVEFORM_HEIGHT)
        self.duration = 0.0
        self._base_pos = np.empty(0, dtype=np.float32)
        self._base_neg = np.empty(0, dtype=np.float32)
        self._peaks_pos = np.empty(0, dtype=np.float32)
        self._peaks_neg = np.empty(0, dtype=np.float32)
        self.max_abs = 1.0
        self.left_frac, self.right_frac, self.cursor_frac = 0.0, 1.0, 0.0
        self.keep_mode = True
        self.editable = True
        self.dragging = None
        self.active_handle = None
        # When the handles share a screen pixel, wait for the first pointer
        # movement to select the handle by drag direction instead of always
        # stealing the End handle with the Start handle.
        self._overlap_press_x = None
        self.handle_extend, self.handle_width = 16, 28
        self.cursor_color = QtGui.QColor(255, 200, 0)
        self.preview_segments: List[PreviewSegment] = []
        self._virtual_preview_sources: Dict[str, np.ndarray] = {}
        self.preview_selected: Optional[Tuple[str, int, str]] = None
        self._preview_drag = None
        self._drag_last_emit = 0.0

    def sizeHint(self):
        return QtCore.QSize(750, self.MAX_WAVEFORM_HEIGHT)

    def set_audio_data(self, samples: np.ndarray, duration: float, editable: bool = True) -> None:
        """Cache a width-independent envelope; rebuild visible peaks on EACH resize."""
        self.duration = max(0.0, duration)
        self.editable = editable
        count = len(samples)
        if count:
            bin_count = min(count, min(28000, max(256, count // 1024)))
            edges = np.floor(np.arange(bin_count, dtype=np.float64) * (count / bin_count)).astype(np.int64)
            hi = np.maximum.reduceat(samples, edges).astype(np.float32) / 32768.0
            lo = np.minimum.reduceat(samples, edges).astype(np.float32) / 32768.0
            self._base_pos = np.maximum(hi, 0.0)
            self._base_neg = np.minimum(lo, 0.0)
            self.max_abs = max(float(np.max(self._base_pos)), float(-np.min(self._base_neg)), 0.001)
        else:
            self._base_pos = np.empty(0, dtype=np.float32)
            self._base_neg = np.empty(0, dtype=np.float32)
            self.max_abs = 1.0
        self.left_frac, self.right_frac, self.cursor_frac = 0.0, 1.0, 0.0
        self.active_handle = None
        self.dragging = None
        self._overlap_press_x = None
        # The MainWindow immediately reapplies markers after a preview rebuild.
        self.preview_segments = []
        self._virtual_preview_sources = {}
        self.preview_selected = None
        self._rebuild_visible_peaks()
        self._emit_time_signals()
        self.update()

    def set_preview_segments(self, segments: Sequence[PreviewSegment],
                             selected: Optional[Tuple[str, int, str]] = None,
                             source_samples: Optional[Dict[str, np.ndarray]] = None):
        self.preview_segments = list(segments)
        # The editor draws audio directly from these source arrays: no joined
        # buffer is created until the user requests playback.
        self._virtual_preview_sources = source_samples if source_samples is not None else {}
        if self._virtual_preview_sources:
            self._rebuild_visible_peaks()
        valid = {(s.crop_uid, s.part_index, side)
                 for s in self.preview_segments for side in ("start", "end")}
        self.preview_selected = selected if selected in valid else None
        # One independent lane for each audible piece (Remove crops can have two
        # lanes); a scroll viewport limits visible height to 280 px.
        if not self.editable:
            self.setFixedHeight(max(115, len(self.preview_segments) * PREVIEW_ROW_HEIGHT))
        self.update()

    def restore_editor_height(self):
        self.setMinimumHeight(115)
        self.setMaximumHeight(self.MAX_WAVEFORM_HEIGHT)
        self.updateGeometry()

    def selected_preview_segment(self) -> Optional[PreviewSegment]:
        if not self.preview_selected:
            return None
        uid, part, _ = self.preview_selected
        return next((s for s in self.preview_segments
                     if s.crop_uid == uid and s.part_index == part), None)

    def _preview_boundary_at(self, x: int, y: int) -> Optional[Tuple[str, int, str]]:
        """Row chooses the crop, X chooses the boundary: no join-click ambiguity."""
        if not self.preview_segments or y < 0:
            return None
        row = min(len(self.preview_segments) - 1, y // PREVIEW_ROW_HEIGHT)
        # One-piece preview takes the widget's full height.
        if len(self.preview_segments) == 1:
            row = 0
        if row < 0 or row >= len(self.preview_segments):
            return None
        seg = self.preview_segments[row]
        width = max(1, self.width() - 1)
        candidates = []
        for side in ("start", "end"):
            px = round(seg.boundary_time(side) / max(self.duration, 1e-9) * width)
            dx = abs(x - px)
            if dx > 15:
                continue
            # Even if a tiny segment begins/ends at the same pixel, top and
            # bottom half of its own ROW choose Start vs End independently.
            local_y = y if row == 0 else y - row * PREVIEW_ROW_HEIGHT
            row_height = self.height() if len(self.preview_segments) == 1 else PREVIEW_ROW_HEIGHT
            anchor = 14 if side == "start" else row_height - 14
            dy = abs(local_y - anchor)
            key = (seg.crop_uid, seg.part_index, side)
            candidates.append((dy, dx, 0 if key == self.preview_selected else 1, key))
        return min(candidates)[-1] if candidates else None

    def _preview_drag_target(self, x: int) -> Optional[float]:
        state = self._preview_drag
        if state is None:
            return None
        return state["original"] + ((x - state["start_x"]) * state["duration"] * state["rate"] /
                                    max(1, state["width"] - 1))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rebuild_visible_peaks()
        self.update()

    def _rebuild_visible_peaks(self):
        if self._virtual_preview_sources and self.preview_segments and self.duration > 0:
            self._rebuild_virtual_peaks()
            return
        n, width = len(self._base_pos), max(1, self.width())
        if not n:
            self._peaks_pos = np.zeros(width, dtype=np.float32)
            self._peaks_neg = np.zeros(width, dtype=np.float32)
            return
        if width <= n:
            edges = np.floor(np.arange(width, dtype=np.float64) * n / width).astype(np.int64)
            self._peaks_pos = np.maximum.reduceat(self._base_pos, edges)
            self._peaks_neg = np.minimum.reduceat(self._base_neg, edges)
        else:
            indexes = np.minimum((np.arange(width, dtype=np.int64) * n) // width, n - 1)
            self._peaks_pos = self._base_pos[indexes]
            self._peaks_neg = self._base_neg[indexes]

    def _rebuild_virtual_peaks(self):
        """Paint each crop on its own row using ORIGINAL PCM, never joined PCM.

        `reduceat` creates just a width-sized display envelope for each part.
        This is visual-only and does not change the playback samples or crops.
        """
        width = max(1, self.width())
        self._peaks_pos = np.zeros(width, dtype=np.float32)
        self._peaks_neg = np.zeros(width, dtype=np.float32)
        for seg in self.preview_segments:
            source = self._virtual_preview_sources.get(seg.source)
            if source is None:
                continue
            a = max(0, min(len(source), round(seg.source_start * SAMPLE_RATE)))
            b = max(a, min(len(source), round(seg.source_end * SAMPLE_RATE)))
            if b <= a:
                continue
            start_x = max(0, min(width - 1, round(seg.preview_start / self.duration * (width - 1))))
            end_x = max(start_x, min(width - 1, round(seg.preview_end / self.duration * (width - 1))))
            pixels = end_x - start_x + 1
            data = source[a:b]
            if pixels <= len(data):
                edges = (np.arange(pixels, dtype=np.int64) * len(data)) // pixels
                top = np.maximum.reduceat(data, edges).astype(np.float32) / 32768.0
                bottom = np.minimum.reduceat(data, edges).astype(np.float32) / 32768.0
            else:
                indexes = (np.arange(pixels, dtype=np.int64) * len(data)) // pixels
                top = bottom = data[indexes].astype(np.float32) / 32768.0
            sl = slice(start_x, end_x + 1)
            self._peaks_pos[sl] = np.maximum(self._peaks_pos[sl], top)
            self._peaks_neg[sl] = np.minimum(self._peaks_neg[sl], bottom)
        self.max_abs = max(float(np.max(self._peaks_pos)),
                           float(-np.min(self._peaks_neg)), 0.001)

    def set_keep_mode(self, keep: bool):
        self.keep_mode = bool(keep)
        self.update()

    def set_region(self, left: float, right: float):
        if self.duration <= 0:
            return
        gap = min(self.duration, 0.001)
        left = max(0.0, min(float(left), self.duration - gap))
        right = max(left + gap, min(float(right), self.duration))
        self.left_frac, self.right_frac = left / self.duration, right / self.duration
        self._emit_time_signals()
        self.update()

    def set_left_time(self, sec: float):
        if not self.editable or self.duration <= 0:
            return
        sec = max(0.0, min(float(sec), self.right_frac * self.duration - min(self.duration, 0.001)))
        self.left_frac = sec / self.duration
        self.leftHandleMoved.emit(sec)
        self.update()

    def set_right_time(self, sec: float):
        if not self.editable or self.duration <= 0:
            return
        sec = max(self.left_frac * self.duration + min(self.duration, 0.001),
                  min(float(sec), self.duration))
        self.right_frac = sec / self.duration
        self.rightHandleMoved.emit(sec)
        self.update()

    def set_cursor_time(self, sec: float, notify: bool = False):
        if self.duration <= 0:
            return
        frac = max(0.0, min(float(sec) / self.duration, 1.0))
        if self.editable and not self.keep_mode and self.left_frac < frac < self.right_frac:
            frac = self.left_frac if frac - self.left_frac < self.right_frac - frac else self.right_frac
        self.cursor_frac = frac
        self.update()
        if notify:
            self.cursorClicked.emit(frac * self.duration)

    def _handle_path(self, line_x: int, h: int, direction: int):
        path = QtGui.QPainterPath()
        path.moveTo(line_x, 0)
        half = h / 2.0
        for i in range(1, 25):
            y = h * i / 24.0
            t = (y - half) / half
            path.lineTo(line_x + direction * self.handle_extend * (1 - t * t), y)
        path.closeSubpath()
        return path

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QtGui.QColor(20, 40, 60))
        if self.duration <= 0:
            p.setPen(QtGui.QColor(145, 168, 188))
            p.drawText(self.rect(), QtCore.Qt.AlignCenter, "Open an audio file to begin")
            return
        if self.preview_segments and not self.editable:
            # Professional multitrack-style joined timeline: all rows share a
            # global X axis, but boundaries are NEVER stacked in the same row.
            # They still play as one continuous audio buffer.
            fm = p.fontMetrics()
            for index, seg in enumerate(self.preview_segments):
                top = index * PREVIEW_ROW_HEIGHT
                bottom = (h if len(self.preview_segments) == 1
                          else min(h, top + PREVIEW_ROW_HEIGHT))
                row_h = bottom - top
                if row_h <= 0 or top > event.rect().bottom() or bottom < event.rect().top():
                    continue
                sx = round(seg.preview_start / self.duration * (w - 1))
                ex = round(seg.preview_end / self.duration * (w - 1))
                chosen = self.preview_selected and seg.crop_uid == self.preview_selected[0] and seg.part_index == self.preview_selected[1]
                p.fillRect(0, top, w, row_h - 1, QtGui.QColor(17, 37, 54) if index % 2 else QtGui.QColor(20, 47, 64))
                p.fillRect(sx, top + 3, max(1, ex - sx), max(1, row_h - 7),
                           QtGui.QColor(35, 124, 70) if chosen else QtGui.QColor(29, 92, 62))
                # Waveform for only THIS crop; the other clips are transparent
                # on this lane. Global X mapping preserves real joined time.
                center = top + row_h * .58
                local_hi = self._peaks_pos[max(0, sx):min(w, ex + 1)]
                local_lo = self._peaks_neg[max(0, sx):min(w, ex + 1)]
                local_peak = max(float(np.max(local_hi)) if len(local_hi) else 0.,
                                 float(-np.min(local_lo)) if len(local_lo) else 0., .05)
                amp = max(1, row_h * .28) / local_peak
                p.setPen(QtCore.Qt.NoPen)
                p.setBrush(QtGui.QColor(119, 214, 159))
                for x in range(max(0, sx), min(w - 1, ex) + 1):
                    upper = int(center - float(self._peaks_pos[x]) * amp)
                    lower = int(center - float(self._peaks_neg[x]) * amp)
                    upper = max(top + 22, upper)
                    lower = min(bottom - 3, lower)
                    p.drawRect(x, upper, 1, max(1, lower - upper))
                # A source label at the left of each lane is always visible,
                # even for tiny clips, where in-clip labels would be invisible.
                name = f"{index + 1}. {seg.crop_name}" + (
                    f" · {seg.speed_rate:g}×" if abs(seg.speed_rate - 1.0) > 1e-8 else "")
                if sum(other.crop_uid == seg.crop_uid for other in self.preview_segments) > 1:
                    name += f" · part {seg.part_index + 1}"
                p.setPen(QtGui.QColor(210, 247, 223))
                p.drawText(8, top + 16, fm.elidedText(name + "  ·  " + os.path.basename(seg.source),
                                                     QtCore.Qt.ElideRight, max(30, w - 20)))
                for side in ("start", "end"):
                    key = (seg.crop_uid, seg.part_index, side)
                    active = key == self.preview_selected
                    x = sx if side == "start" else ex
                    color = (QtGui.QColor(255, 241, 109) if active else
                             QtGui.QColor(151, 244, 183) if seg.boundary_editable(side)
                             else QtGui.QColor(114, 144, 130))
                    p.setPen(QtGui.QPen(color, 3 if active else 2))
                    p.drawLine(x, top + 19, x, bottom - 3)
                    p.setPen(QtCore.Qt.NoPen)
                    p.setBrush(color)
                    if side == "start":
                        points = [QtCore.QPoint(x-7, top+20), QtCore.QPoint(x+7, top+20), QtCore.QPoint(x, top+32)]
                    else:
                        points = [QtCore.QPoint(x-7, bottom-3), QtCore.QPoint(x+7, bottom-3), QtCore.QPoint(x, bottom-15)]
                    p.drawPolygon(QtGui.QPolygon(points))
                    if active:
                        p.setPen(QtGui.QColor(255, 255, 220))
                        # Label on the same lane; no need to spam-click joints.
                        p.drawText(min(w-47, max(2, x+7)), top + row_h // 2,
                                   "START" if side == "start" else "END")
            # One continuous playback cursor traverses EVERY lane at once.
            cx = round(self.cursor_frac * (w - 1))
            p.setPen(QtGui.QPen(self.cursor_color, 2))
            p.drawLine(cx, 0, cx, h)
            p.end()
            return

        center = h / 2
        factor = max(1, h / 2 - 8) / self.max_abs
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(120, 180, 220))
        start, stop = max(0, event.rect().left()), min(w - 1, event.rect().right())
        for x in range(start, stop + 1):
            top = int(center - float(self._peaks_pos[x]) * factor)
            bottom = int(center - float(self._peaks_neg[x]) * factor)
            p.drawRect(x, top, 1, max(1, bottom - top))
        if self.editable:
            lx, rx = round(self.left_frac * (w - 1)), round(self.right_frac * (w - 1))
            tint = QtGui.QColor(0, 200, 0, 45) if self.keep_mode else QtGui.QColor(200, 50, 50, 60)
            p.fillRect(lx, 0, max(0, rx - lx), h, tint)
            p.setRenderHint(QtGui.QPainter.Antialiasing, True)
            p.setBrush(QtGui.QColor(255, 20, 147, 235))
            p.setPen(QtCore.Qt.NoPen)
            left_dir = 1 if lx < self.handle_extend else (-1 if self.keep_mode else 1)
            right_dir = -1 if rx > w - self.handle_extend else (1 if self.keep_mode else -1)
            p.drawPath(self._handle_path(lx, h, left_dir))
            p.drawPath(self._handle_path(rx, h, right_dir))
            p.setRenderHint(QtGui.QPainter.Antialiasing, False)
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 200), 2))
            p.drawLine(lx, 0, lx, h)
            p.drawLine(rx, 0, rx, h)
        cx = round(self.cursor_frac * (w - 1))
        p.setPen(QtGui.QPen(self.cursor_color, 2))
        p.drawLine(cx, 0, cx, h)
        p.end()

    def _select_handle(self, name: str) -> None:
        self.dragging = name
        self.active_handle = name
        self._overlap_press_x = None
        self.setCursor(QtCore.Qt.SizeHorCursor)
        self.handleClicked.emit(name)

    def mousePressEvent(self, event):
        if event.button() != QtCore.Qt.LeftButton or self.duration <= 0:
            return
        x = event.x()
        self.setFocus()
        self._overlap_press_x = None
        if not self.editable and self.preview_segments:
            key = self._preview_boundary_at(event.x(), event.y())
            if key:
                self.preview_selected = key
                self.setCursor(QtCore.Qt.SizeHorCursor)
                self.previewBoundaryClicked.emit(key)
                seg = self.selected_preview_segment()
                if seg and seg.boundary_editable(key[2]):
                    self._preview_drag = dict(key=key, start_x=x, original=seg.original_time(key[2]),
                                              width=self.width(), duration=self.duration,
                                              rate=seg.speed_rate, moved=False)
                else:
                    self._preview_drag = None
                self.update()
                return
            self._preview_drag = None
            self.preview_selected = None
            self.previewBoundaryClicked.emit(None)
            self.set_cursor_time(event.x() / max(1, self.width() - 1) * self.duration, notify=True)
            return
        lx = round(self.left_frac * (self.width() - 1))
        rx = round(self.right_frac * (self.width() - 1))
        if self.editable:
            radius = self.handle_width // 2
            left_hit = abs(x - lx) <= radius
            right_hit = abs(x - rx) <= radius
            if left_hit or right_hit:
                if left_hit and right_hit:
                    # Explicit override is available even if the handles are
                    # exactly on top of each other (or a user cannot drag).
                    if event.modifiers() & QtCore.Qt.AltModifier:
                        choice = 'right'
                    elif event.modifiers() & QtCore.Qt.ShiftModifier:
                        choice = 'left'
                    elif lx == rx and x == lx:
                        # A drag to the RIGHT will grab End, a drag to the LEFT
                        # will grab Start. Don't choose early on mouse down.
                        self._overlap_press_x = x
                        self.setCursor(QtCore.Qt.SizeHorCursor)
                        return
                    elif lx == rx:
                        choice = 'right' if x > lx else 'left'
                    elif abs(x - lx) == abs(x - rx):
                        choice = self.active_handle or 'right'
                    else:
                        choice = 'left' if abs(x - lx) < abs(x - rx) else 'right'
                else:
                    choice = 'left' if left_hit else 'right'
                self._select_handle(choice)
                return
        self.dragging = None
        self.active_handle = None
        self.handleFocusLost.emit()
        self.set_cursor_time(x / max(1, self.width() - 1) * self.duration, notify=True)

    def mouseMoveEvent(self, event):
        if not self.editable:
            if self._preview_drag is not None and event.buttons() & QtCore.Qt.LeftButton:
                drag = self._preview_drag
                target = self._preview_drag_target(event.x())
                if abs(event.x() - drag["start_x"]) >= 2 and target is not None:
                    drag["moved"] = True
                    # Rebuilding a long joined buffer is relatively expensive;
                    # cap pointer update frequency and ALWAYS apply final position
                    # on mouse release so millisecond edits are not lost.
                    import time
                    now = time.monotonic()
                    if now - self._drag_last_emit >= 0.04:
                        self._drag_last_emit = now
                        self.previewBoundaryDragged.emit(drag["key"], target)
                return
            key = self._preview_boundary_at(event.x(), event.y())
            self.setCursor(QtCore.Qt.SizeHorCursor if key else QtCore.Qt.ArrowCursor)
            if key:
                seg = next((s for s in self.preview_segments
                            if (s.crop_uid, s.part_index) == key[:2]), None)
                if seg:
                    side = key[2]
                    state = "editable" if seg.boundary_editable(side) else "source edge (locked)"
                    self.setToolTip(f"{seg.crop_name} · {os.path.basename(seg.source)}\n"
                                    f"{side.upper()} at {format_time(seg.original_time(side))}\n"
                                    f"{state} · use ←/→ after selecting")
            else:
                self.setToolTip("Click a green START at the top or END at the bottom to fine tune")
            return
        if self._overlap_press_x is not None:
            dx = event.x() - self._overlap_press_x
            # Ignore tiny motion caused by a normal click. If both bars are
            # in the same pixel, motion direction resolves the ambiguity.
            if abs(dx) < 2:
                return
            self._select_handle('right' if dx > 0 else 'left')
        if self.dragging is None:
            return
        seconds = max(0, min(event.x() / max(1, self.width() - 1), 1)) * self.duration
        if self.dragging == 'left':
            self.set_left_time(seconds)
        else:
            self.set_right_time(seconds)

    def mouseReleaseEvent(self, event):
        if self._preview_drag is not None:
            drag = self._preview_drag
            self._preview_drag = None
            if drag["moved"]:
                target = drag["original"] + ((event.x() - drag["start_x"]) * drag["duration"] * drag["rate"] /
                                              max(1, drag["width"] - 1))
                self.previewBoundaryDragged.emit(drag["key"], target)
                self.previewDragFinished.emit(drag["key"])
            self.setCursor(QtCore.Qt.ArrowCursor)
            return
        if self._overlap_press_x is not None:
            # A click without dragging selects the OTHER handle, so the
            # keyboard arrows can still operate either overlapped bar.
            next_handle = 'right' if self.active_handle != 'right' else 'left'
            self._select_handle(next_handle)
        if self.dragging is not None:
            previous = self.dragging
            self.dragging = None
            self.dragFinished.emit(previous)
        self._overlap_press_x = None
        self.setCursor(QtCore.Qt.ArrowCursor)

    def _emit_time_signals(self):
        self.leftHandleMoved.emit(self.left_frac * self.duration)
        self.rightHandleMoved.emit(self.right_frac * self.duration)


class TaskThread(QtCore.QThread):
    succeeded = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.succeeded.emit(self.fn())
        except Exception as e:
            self.failed.emit(f"{e}\n\n{traceback.format_exc(limit=3)}")


def string_from_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "\n".join(string_from_value(v) for v in value if v is not None).strip()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip("\x00 \n")
    if value is None:
        return ""
    return str(value).strip()


def id3_lyrics(tags: ID3) -> str:
    for frame in tags.getall("USLT"):
        if string_from_value(frame.text):
            return string_from_value(frame.text)
    for frame in tags.getall("SYLT"):
        text = []
        for part in getattr(frame, "text", ()):
            if isinstance(part, tuple) and len(part) == 2:
                line, ms = part
                t = max(0, int(ms))
                text.append(f"[{t // 60000:02d}:{(t % 60000) / 1000:05.2f}]{string_from_value(line)}")
        if text:
            return "\n".join(text)
    for frame in tags.getall("TXXX"):
        if "lyric" in str(getattr(frame, "desc", "")).lower():
            return string_from_value(getattr(frame, "text", ""))
    return ""


def lyric_mp4_keys(tags: Dict[str, Any]) -> List[str]:
    return [key for key in tags if key == "\xa9lyr" or
            (key.startswith("----:") and ("lyric" in key.lower() or "sylt" in key.lower()))]


def image_meta(data: bytes, mime: str) -> Optional[Dict[str, Any]]:
    return {"data": bytes(data), "mime": mime} if data else None


def extract_metadata(path: str) -> Dict[str, Any]:
    """Standard metadata + native container tags for loss-minimized copying."""
    result = {"path": path, "kind": "other", "native": None,
              "title": "", "artist": "", "lyrics": "", "artwork": None,
              "album": "", "albumartist": "", "genre": "", "date": "",
              "composer": "", "comment": "", "copyright": "", "track": "",
              "disc": "", "bpm": ""}
    audio = MutagenFile(path)
    if audio is None:
        return result
    tags = audio.tags
    if isinstance(audio, MP4):
        result["kind"] = "mp4"
        result["native"] = copy.deepcopy(dict(tags or {}))
        tags = tags or {}
        mapping = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
                   "albumartist": "aART", "genre": "\xa9gen", "date": "\xa9day",
                   "composer": "\xa9wrt", "comment": "\xa9cmt", "copyright": "cprt",
                   "track": "trkn", "disc": "disk", "bpm": "tmpo"}
        for dest, key in mapping.items():
            value = tags.get(key)
            if value:
                if dest in ("track", "disc") and isinstance(value[0], tuple):
                    current, total = value[0]
                    result[dest] = f"{current}/{total}" if total else str(current)
                else:
                    result[dest] = string_from_value(value[0])
        for key in lyric_mp4_keys(tags):
            value = string_from_value(tags.get(key))
            if value:
                result["lyrics"] = value
                if key == "\xa9lyr":
                    break
        covers = tags.get("covr", ())
        if covers:
            cover = covers[0]
            mime = "image/png" if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG else "image/jpeg"
            result["artwork"] = image_meta(bytes(cover), mime)
    elif isinstance(tags, ID3):
        result["kind"] = "id3"
        result["native"] = copy.deepcopy(tags)
        frame_map = {"title": "TIT2", "artist": "TPE1", "album": "TALB",
                     "albumartist": "TPE2", "genre": "TCON", "date": "TDRC",
                     "composer": "TCOM", "copyright": "TCOP", "track": "TRCK",
                     "disc": "TPOS", "bpm": "TBPM"}
        for dest, frame_id in frame_map.items():
            frame = tags.get(frame_id)
            if frame is not None:
                result[dest] = string_from_value(getattr(frame, "text", ""))
        result["lyrics"] = id3_lyrics(tags)
        comments = tags.getall("COMM")
        if comments:
            result["comment"] = string_from_value(comments[0].text)
        pictures = tags.getall("APIC")
        if pictures:
            pic = next((p for p in pictures if getattr(p, "type", None) == 3), pictures[0])
            result["artwork"] = image_meta(pic.data, pic.mime)
    else:
        # FLAC/Vorbis/WAV metadata is accepted as input; supported fields are bridged
        # into MP3/MP4, along with a best-effort copy of original custom tags.
        result["kind"] = "generic"
        if tags:
            result["native"] = copy.deepcopy(dict(tags))
            lower = {str(k).lower(): v for k, v in tags.items()}
            for field, variants in {
                "title": ["title"], "artist": ["artist"], "album": ["album"],
                "albumartist": ["albumartist", "album artist"], "genre": ["genre"],
                "date": ["date", "year"], "composer": ["composer"],
                "copyright": ["copyright"], "comment": ["comment", "description"],
                "track": ["tracknumber"], "disc": ["discnumber"], "bpm": ["bpm"],
                "lyrics": ["lyrics", "unsyncedlyrics", "lyric"],
            }.items():
                for key in variants:
                    if lower.get(key):
                        result[field] = string_from_value(lower[key])
                        break
        if hasattr(audio, "pictures") and audio.pictures:
            pic = next((p for p in audio.pictures if p.type == 3), audio.pictures[0])
            result["artwork"] = image_meta(pic.data, pic.mime)
    return result


def artwork_digest(art: Optional[Dict[str, Any]]) -> str:
    return hashlib.sha256(art["data"]).hexdigest() if art else ""


def choose_field_sources(meta_list: Sequence[Dict[str, Any]], field: str) -> List[Tuple[Dict[str, Any], List[str]]]:
    """Group duplicate values. A repeated crop of one file never creates duplicates."""
    groups: Dict[str, Tuple[Dict[str, Any], List[str]]] = {}
    for meta in meta_list:
        value = meta[field]
        key = artwork_digest(value) if field == "artwork" else string_from_value(value)
        if not key:
            continue
        if key not in groups:
            groups[key] = (meta, [])
        groups[key][1].append(os.path.basename(meta["path"]))
    return list(groups.values())


class MetadataChoiceDialog(QtWidgets.QDialog):
    def __init__(self, field: str, options: List[Tuple[Dict[str, Any], List[str]]], parent=None):
        super().__init__(parent)
        self.field = field
        self.options = options
        self.setWindowTitle("Choose output " + LABELS[field])
        self.setModal(True)
        self.setStyleSheet("QDialog {background: #10263c; color: white;}"
                           "QListWidget, QTextEdit {background:#1a354e; color:white; border:1px solid #43617a;}"
                           "QPushButton {padding:6px 15px;}")
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(f"Several sources have different {LABELS[field]} values. Select one:"))
        self.choices = QtWidgets.QListWidget()
        self.choices.setMaximumHeight(148)
        layout.addWidget(self.choices)
        for meta, filenames in options:
            names = ", ".join(filenames)
            value = meta[field]
            excerpt = "Embedded image" if field == "artwork" else string_from_value(value).replace("\n", " ")[:70]
            self.choices.addItem(f"{names}  —  {excerpt}")
        self.preview = QtWidgets.QStackedWidget()
        self.art_label = QtWidgets.QLabel()
        self.art_label.setAlignment(QtCore.Qt.AlignCenter)
        self.art_label.setMinimumHeight(150)
        self.preview.addWidget(self.art_label)
        self.text_preview = QtWidgets.QPlainTextEdit()
        self.text_preview.setReadOnly(True)
        self.preview.addWidget(self.text_preview)
        layout.addWidget(self.preview, 1)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.choices.currentRowChanged.connect(self._show_option)
        self.choices.itemDoubleClicked.connect(lambda _: self.accept())
        self.choices.setCurrentRow(0)
        area = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.resize(min(610, int(area.width() * .8)), min(445, int(area.height() * .72)))

    def _show_option(self, row: int):
        if row < 0:
            return
        meta = self.options[row][0]
        if self.field == "artwork":
            self.preview.setCurrentWidget(self.art_label)
            art = meta["artwork"]
            pix = QtGui.QPixmap()
            if art and pix.loadFromData(art["data"]):
                self.art_label.setPixmap(pix.scaled(290, 210, QtCore.Qt.KeepAspectRatio,
                                                    QtCore.Qt.SmoothTransformation))
            else:
                self.art_label.setText("Unable to render this image; original bytes can still be copied")
        else:
            self.preview.setCurrentWidget(self.text_preview)
            self.text_preview.setPlainText(string_from_value(meta[self.field]))

    def accept(self):
        if self.choices.currentRow() >= 0:
            super().accept()

    def selected(self) -> Dict[str, Any]:
        return self.options[self.choices.currentRow()][0]


def native_tag_json(value: Any, depth: int = 0) -> Any:
    """Reversible supplemental metadata for otherwise incompatible source tags."""
    if depth >= 12:
        return {"repr": repr(value)}
    if value is None or isinstance(value, (int, float, bool, str)):
        return value
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [native_tag_json(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(k): native_tag_json(v, depth + 1) for k, v in value.items()}
    if isinstance(value, ID3):
        return {"id3_frames": [native_tag_json(v, depth + 1) for v in value.values()]}
    if hasattr(value, "__dict__"):
        return {"class": type(value).__name__,
                "properties": {str(k): native_tag_json(v, depth + 1)
                               for k, v in vars(value).items() if not str(k).startswith("_")}}
    return {"repr": repr(value)}


def _copy_source_native_tags(output: Any, meta_list: Sequence[Dict[str, Any]], target: str):
    """Retain the first source's native tags, merge nonconflicting tags from others."""
    if target == "mp3":
        if output.tags is None:
            output.add_tags()
        output.tags.clear()
        for meta in meta_list:
            if meta["kind"] == "id3" and meta["native"] is not None:
                for frame in meta["native"].values():
                    if frame.HashKey not in output.tags:
                        output.tags.add(copy.deepcopy(frame))
    else:
        if output.tags is None:
            output.add_tags()
        output.tags.clear()
        for meta in meta_list:
            if meta["kind"] == "mp4" and meta["native"]:
                for key, value in meta["native"].items():
                    if key not in output.tags:
                        output.tags[key] = copy.deepcopy(value)


def _apply_auxiliary_tags(output: Any, meta_list: Sequence[Dict[str, Any]], target: str):
    """Map common tags from source formats other than target container."""
    fields = ("album", "albumartist", "genre", "date", "composer", "comment",
              "copyright", "track", "disc", "bpm")
    id3_classes = {
        "album": TALB, "albumartist": TPE2, "genre": TCON, "date": TDRC,
        "composer": TCOM, "copyright": TCOP, "track": TRCK, "disc": TPOS, "bpm": TBPM,
    }
    mp4_keys = {
        "album": "\xa9alb", "albumartist": "aART", "genre": "\xa9gen",
        "date": "\xa9day", "composer": "\xa9wrt", "comment": "\xa9cmt",
        "copyright": "cprt", "track": "trkn", "disc": "disk", "bpm": "tmpo",
    }
    for field in fields:
        if target == "mp3":
            frame_name = "COMM" if field == "comment" else id3_classes[field].__name__
            if output.tags.getall(frame_name):
                continue
        else:
            if mp4_keys[field] in output.tags:
                continue
        source = next((m for m in meta_list if m[field]), None)
        if not source:
            continue
        value = source[field]
        if target == "mp3":
            if field == "comment":
                output.tags.add(COMM(encoding=3, lang="eng", desc="", text=[value]))
            else:
                output.tags.add(id3_classes[field](encoding=3, text=[value]))
        else:
            if field in ("track", "disc"):
                try:
                    nums = re.findall(r"\d+", value)
                    output.tags[mp4_keys[field]] = [(int(nums[0]), int(nums[1]) if len(nums) > 1 else 0)]
                except (ValueError, IndexError):
                    continue
            elif field == "bpm":
                try:
                    output.tags["tmpo"] = [int(float(value))]
                except ValueError:
                    continue
            else:
                output.tags[mp4_keys[field]] = [value]


def _replace_field(output: Any, target: str, field: str, selected: Optional[Dict[str, Any]],
                   first_source: Dict[str, Any]):
    """Copy selected native tag(s) when possible, including timestamped lyrics."""
    # If same native source supplied this field, keep exact frames/atoms intact.
    value = selected[field] if selected else None
    if selected and selected["path"] == first_source["path"] and selected["kind"] == target:
        return
    if target == "mp3":
        tags = output.tags
        if field == "title":
            tags.delall("TIT2")
            if value:
                tags.add(TIT2(encoding=3, text=[value]))
        elif field == "artist":
            tags.delall("TPE1")
            if value:
                tags.add(TPE1(encoding=3, text=[value]))
        elif field == "lyrics":
            tags.delall("USLT")
            tags.delall("SYLT")
            for frame in list(tags.getall("TXXX")):
                if "lyric" in str(frame.desc).lower():
                    tags.delall(frame.HashKey)
            if selected and selected["kind"] == "id3":
                native = selected["native"]
                for prefix in ("USLT", "SYLT"):
                    for frame in native.getall(prefix):
                        tags.add(copy.deepcopy(frame))
                for frame in native.getall("TXXX"):
                    if "lyric" in str(frame.desc).lower():
                        tags.add(copy.deepcopy(frame))
            elif value:
                tags.add(USLT(encoding=3, lang="eng", desc="", text=value))
        else:
            tags.delall("APIC")
            if selected and selected["kind"] == "id3":
                for frame in selected["native"].getall("APIC"):
                    tags.add(copy.deepcopy(frame))
            elif value:
                tags.add(APIC(encoding=3, mime=value["mime"], type=3,
                              desc="Cover", data=value["data"]))
    else:
        tags = output.tags
        mp4_fields = {"title": "\xa9nam", "artist": "\xa9ART", "lyrics": "\xa9lyr", "artwork": "covr"}
        key = mp4_fields[field]
        if field == "lyrics":
            for k in lyric_mp4_keys(tags):
                del tags[k]
            if selected and selected["kind"] == "mp4":
                for k in lyric_mp4_keys(selected["native"]):
                    tags[k] = copy.deepcopy(selected["native"][k])
            elif value:
                tags[key] = [value]
        elif field == "artwork":
            tags.pop("covr", None)
            if selected and selected["kind"] == "mp4":
                tags["covr"] = copy.deepcopy(selected["native"]["covr"])
            elif value:
                mime = value["mime"].lower()
                if "png" in mime:
                    image_fmt = MP4Cover.FORMAT_PNG
                elif "jpeg" in mime or "jpg" in mime:
                    image_fmt = MP4Cover.FORMAT_JPEG
                else:
                    raise ValueError("M4A artwork must be PNG or JPEG. Please choose a PNG/JPEG source cover.")
                tags["covr"] = [MP4Cover(value["data"], imageformat=image_fmt)]
        else:
            tags.pop(key, None)
            if selected and selected["kind"] == "mp4" and key in selected["native"]:
                tags[key] = copy.deepcopy(selected["native"][key])
            elif value:
                tags[key] = [value]


def apply_export_metadata(output_path: str, meta_list: Sequence[Dict[str, Any]],
                          selected: Dict[str, Optional[Dict[str, Any]]], target: str,
                          crops: Sequence[Crop]):
    if not meta_list:
        return
    output = MP3(output_path) if target == "mp3" else MP4(output_path)
    _copy_source_native_tags(output, meta_list, target)
    _apply_auxiliary_tags(output, meta_list, target)
    for field in META_FIELDS:
        _replace_field(output, target, field, selected.get(field), meta_list[0])

    # Preserve otherwise incompatible AND conflicting native data from other
    # sources as a supplemental tag in the SAME output file. Primary native
    # atoms/ID3 frames remain standard and readable by normal music players.
    # Do not expose absolute local file paths in exported metadata.
    archived = [m for i, m in enumerate(meta_list)
                if m["native"] is not None and (m["kind"] != target or i > 0)]
    if archived:
        archive = {"note": "Original source tags, including otherwise conflicting or incompatible attributes",
                   "sources": [{"name": os.path.basename(m["path"]), "kind": m["kind"],
                                "tags": native_tag_json(m["native"])} for m in archived]}
        payload = json.dumps(archive, ensure_ascii=False, separators=(",", ":"))
        if target == "mp3":
            output.tags.add(TXXX(encoding=3, desc="AudioCrop Original Source Metadata", text=[payload]))
        else:
            output.tags["----:com.audiocrop.editor:original-tags"] = [
                MP4FreeForm(payload.encode("utf-8"))]
    output.save()
    # Verify selected fields actually survive the mux/metadata save (a critical
    # point for M4A atoms, ID3 lyrics and embedded covers).
    verified = extract_metadata(output_path)
    for field in META_FIELDS:
        winner = selected.get(field)
        if winner is None:
            continue
        expected = winner[field]
        actual = verified[field]
        if field == "artwork":
            valid = artwork_digest(expected) == artwork_digest(actual)
        else:
            valid = string_from_value(expected) == string_from_value(actual)
        if not valid:
            raise RuntimeError("Could not verify preserved output " + LABELS[field])


class MP3Trimmer(QtWidgets.QMainWindow):
    FINE_STEP = 0.05

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Audio Trimmer — crop queue")
        screen = QtWidgets.QApplication.primaryScreen().availableGeometry()
        self.resize(max(850, int(screen.width() * .72)), max(430, int(screen.height() * .72)))
        self.setMinimumSize(720, 350)
        self.move(screen.center() - self.rect().center())
        self.original_file = ""
        self.audio_samples: Optional[np.ndarray] = None
        self.audio_rate = SAMPLE_RATE
        self.duration = 0.0
        self.preview_mode = False
        self.editing_crop_id: Optional[str] = None
        self.active_handle: Optional[str] = None
        self._editor_before_preview: Optional[Dict[str, Any]] = None
        self._joined_preview_path: Optional[str] = None
        self._preview_source_samples: Dict[str, np.ndarray] = {}
        self._preview_segments: List[PreviewSegment] = []
        self.preview_boundary_id: Optional[Tuple[str, int, str]] = None
        self._preview_edited_crop_ids: set = set()
        self._preview_playback_path: Optional[str] = None
        self._pending_play = False
        self._pending_seek = 0
        self._playback_map: Optional[Dict[str, Any]] = None
        self._busy = False
        self._worker: Optional[TaskThread] = None
        self.crops: List[Crop] = []
        self._draft_speed_enabled = False
        self._draft_speed_rate = 1.0
        self.next_index = 1
        self._restoring_list = False
        self._session_ready = False
        self._closing = False
        self.media_player = QtMultimedia.QMediaPlayer(self)
        self.media_player.setNotifyInterval(35)
        self.media_player.positionChanged.connect(self._player_position)
        self.media_player.mediaStatusChanged.connect(self._player_media_status)
        self.media_player.stateChanged.connect(self._player_state)
        self._init_ui()
        self._load_session()
        QtWidgets.QApplication.instance().installEventFilter(self)
        try:
            subprocess_ok(["ffmpeg", "-version"])
            subprocess_ok(["ffprobe", "-version"])
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "FFmpeg required",
                 "Install FFmpeg + FFprobe and add them to your PATH.\n\n" + str(exc))

    def _init_ui(self):
        self.setStyleSheet("""
            QMainWindow, QWidget#central {background:#0d233a;color:white;font-family:'Segoe UI';}
            QLabel {color:#dce9f5;}
            QPushButton {background:#1b415f;color:white;border:none;border-radius:5px;
                         padding:7px 12px;font-weight:600;}
            QPushButton:hover {background:#285b82;}
            QPushButton:disabled {background:#263a4a;color:#8398aa;}
            QLineEdit {background:#1a2a40;color:white;border:1px solid #3c5975;
                       border-radius:3px;padding:6px;}
            QDockWidget {color:white;}
            QListWidget {background:#102d46;color:white;border:1px solid #365b78;
                         border-radius:5px;outline:0;}
            QListWidget::item {padding:8px 5px;}
            QListWidget::item:selected {background:#26618a;}
            QStatusBar {background:#092036;color:#c4dceb;}
        """)
        central = QtWidgets.QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        main = QtWidgets.QVBoxLayout(central)
        main.setContentsMargins(12, 10, 12, 10)
        main.setSpacing(10)
        top = QtWidgets.QHBoxLayout()
        self.sidebar_button = self._button("Crops ◀", self._toggle_sidebar, top)
        self.sidebar_button.setCheckable(True)
        self.sidebar_button.setChecked(True)
        self.sidebar_button.setToolTip("Collapse or reopen the crop queue (Ctrl+B)")
        self.open_button = self._button("Open audio", self.open_file, top)
        self.btn_keep = self._button("Keep", lambda: self.set_mode(True), top)
        self.btn_remove = self._button("Remove", lambda: self.set_mode(False), top)
        self.btn_keep.setCheckable(True)
        self.btn_remove.setCheckable(True)
        self.btn_keep.setChecked(True)
        self.btn_keep.setStyleSheet("QPushButton{background:#1b415f;color:white;border:none;border-radius:5px;padding:7px 12px;font-weight:600;} QPushButton:checked{background:#2f7847;}")
        self.btn_remove.setStyleSheet("QPushButton{background:#1b415f;color:white;border:none;border-radius:5px;padding:7px 12px;font-weight:600;} QPushButton:checked{background:#974347;}")
        top.addStretch()
        main.addLayout(top)
        actions = QtWidgets.QHBoxLayout()
        self.reset_button = self._button("Reset", self.reset_trim, actions)
        self.save_button = self._button("Save current", self.save_file, actions)
        self.add_button = self._button("+ Add crop", self.add_crop, actions)
        self.update_button = self._button("Update crop", self.update_crop, actions)
        actions.addStretch()
        main.addLayout(actions)

        self.file_label = QtWidgets.QLabel("No file loaded")
        self.file_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.file_label.setStyleSheet("color:#a9c1d5;padding:2px;")
        main.addWidget(self.file_label)

        # Per-crop speed controls remain accessible with the right sidebar
        # collapsed. In joined preview they edit only the saved Crop rate.
        speed_row = QtWidgets.QHBoxLayout()
        self.speed_checkbox = QtWidgets.QCheckBox("Speed")
        self.speed_checkbox.setToolTip(
            "Enable custom tempo for the current crop. Pitch is preserved like YouTube playback.")
        self.speed_checkbox.setFocusPolicy(QtCore.Qt.NoFocus)
        speed_row.addWidget(self.speed_checkbox)
        self.speed_rate = QtWidgets.QDoubleSpinBox()
        self.speed_rate.setDecimals(3)
        self.speed_rate.setRange(MIN_SPEED_RATE, MAX_SPEED_RATE)
        self.speed_rate.setSingleStep(0.05)
        self.speed_rate.setSuffix("×")
        self.speed_rate.setValue(1.0)
        self.speed_rate.setKeyboardTracking(False)  # don't rebuild the timeline per keystroke
        self.speed_rate.setFixedWidth(106)
        self.speed_rate.setStyleSheet(
            "QDoubleSpinBox {background:#1a2a40;color:white;border:1px solid #3c5975;"
            "border-radius:3px;padding:5px;}")
        self.speed_rate.setToolTip("0.05× to 32×. Uses pitch-preserving FFmpeg atempo on playback/export.")
        self.speed_rate.hide()
        speed_row.addWidget(self.speed_rate)
        self.speed_target_label = QtWidgets.QLabel("Current crop · normal 1×")
        self.speed_target_label.setStyleSheet("color:#a9c1d5;")
        speed_row.addWidget(self.speed_target_label)
        speed_row.addStretch(1)
        main.addLayout(speed_row)
        self.speed_checkbox.toggled.connect(self._speed_toggled)
        self.speed_rate.valueChanged.connect(self._speed_rate_changed)

        self.waveform = WaveformWidget()
        self.waveform.leftHandleMoved.connect(lambda v: self.edit_left.setText(format_time(v)))
        self.waveform.rightHandleMoved.connect(lambda v: self.edit_right.setText(format_time(v)))
        self.waveform.cursorClicked.connect(self.on_waveform_clicked)
        self.waveform.handleClicked.connect(self.on_handle_focused)
        self.waveform.handleFocusLost.connect(self.on_handle_focus_lost)
        self.waveform.dragFinished.connect(self.on_handle_drag_finished)
        self.waveform.previewBoundaryClicked.connect(self._preview_boundary_selected)
        self.waveform.previewBoundaryDragged.connect(self._preview_boundary_dragged)
        self.waveform.previewDragFinished.connect(self._preview_drag_finished)
        self.wave_scroll = QtWidgets.QScrollArea()
        self.wave_scroll.setWidgetResizable(True)
        self.wave_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.wave_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.wave_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.wave_scroll.setMaximumHeight(WaveformWidget.MAX_WAVEFORM_HEIGHT)
        self.wave_scroll.setMinimumHeight(115)
        self.wave_scroll.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.wave_scroll.setAlignment(QtCore.Qt.AlignVCenter)
        self.wave_scroll.setWidget(self.waveform)
        margins = QtWidgets.QHBoxLayout()
        margins.setContentsMargins(30, 0, 30, 0)
        margins.addWidget(self.wave_scroll, 1, QtCore.Qt.AlignVCenter)
        main.addLayout(margins, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.btn_play = self._button("▶", self.toggle_play, bottom)
        self.btn_stop = self._button("■", self.stop, bottom)
        self.btn_play.setFixedSize(45, 43)
        self.btn_stop.setFixedSize(45, 43)
        bottom.addSpacing(8)
        bottom.addWidget(QtWidgets.QLabel("Start"))
        self.edit_left = QtWidgets.QLineEdit("00:00.000")
        self.edit_left.setFixedWidth(92)
        self.edit_left.setAlignment(QtCore.Qt.AlignCenter)
        self.edit_left.editingFinished.connect(self.on_left_time_edited)
        bottom.addWidget(self.edit_left)
        bottom.addWidget(QtWidgets.QLabel("End"))
        self.edit_right = QtWidgets.QLineEdit("00:00.000")
        self.edit_right.setFixedWidth(92)
        self.edit_right.setAlignment(QtCore.Qt.AlignCenter)
        self.edit_right.editingFinished.connect(self.on_right_time_edited)
        bottom.addWidget(self.edit_right)
        bottom.addStretch()
        self.mode_label = QtWidgets.QLabel("Select a section; Space plays or pauses")
        self.mode_label.setWordWrap(True)
        self.mode_label.setMinimumWidth(110)
        self.mode_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        bottom.addWidget(self.mode_label, 1)
        main.addLayout(bottom)
        self.statusBar().showMessage("Crops are virtual: no cropped files are written until export")
        self._init_sidebar()
        self.sidebar_shortcut = QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+B"), self)
        self.sidebar_shortcut.activated.connect(self.sidebar_button.click)
        self._refresh_edit_controls()

    def _button(self, name: str, callback, layout):
        btn = QtWidgets.QPushButton(name)
        btn.setFocusPolicy(QtCore.Qt.NoFocus)
        btn.clicked.connect(callback)
        layout.addWidget(btn)
        return btn

    def _init_sidebar(self):
        self.dock = QtWidgets.QDockWidget("Crop queue (drag to reorder)", self)
        self.dock.setObjectName("CropQueueDock")
        self.dock.setAllowedAreas(QtCore.Qt.RightDockWidgetArea)
        # Use ONE persistent, checkable button for collapse AND reopen.
        # Avoid a second close button that can hide the panel unexpectedly.
        self.dock.setFeatures(QtWidgets.QDockWidget.NoDockWidgetFeatures)
        self.dock.visibilityChanged.connect(self._dock_visibility_changed)
        panel = QtWidgets.QWidget()
        panel.setStyleSheet("background:#10283e;color:white;")
        v = QtWidgets.QVBoxLayout(panel)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(9)
        instruction = QtWidgets.QLabel("Drag entries to reorder. Double-click to edit a crop.")
        instruction.setWordWrap(True)
        v.addWidget(instruction)
        self.crop_list = QtWidgets.QListWidget()
        self.crop_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.crop_list.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.crop_list.setDefaultDropAction(QtCore.Qt.MoveAction)
        self.crop_list.setDropIndicatorShown(True)
        self.crop_list.setDragEnabled(True)
        self.crop_list.setAcceptDrops(True)
        self.crop_list.itemDoubleClicked.connect(self.load_selected_crop)
        self.crop_list.currentItemChanged.connect(self._on_crop_selection_changed)
        self.crop_list.model().rowsMoved.connect(self._list_reordered)
        v.addWidget(self.crop_list, 1)
        controls = QtWidgets.QHBoxLayout()
        self.btn_del_crop = self._button("Remove", self.delete_selected_crop, controls)
        self.btn_clear = self._button("Clear all", self.clear_crops, controls)
        v.addLayout(controls)
        self.btn_preview_queue = self._button("▶ Preview joined", self.preview_joined, v)
        self.btn_return = self._button("← Back to edit", self.exit_preview, v)
        self.btn_export = self._button("Export joined audio…", self.export_joined, v)
        tip = QtWidgets.QLabel("Queue saved automatically (paths + time ranges only).\n"
                               "Source files must remain at their original locations.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#9db5c9;font-size:11px;")
        v.addWidget(tip)
        self.dock.setWidget(panel)
        self.addDockWidget(QtCore.Qt.RightDockWidgetArea, self.dock)
        self.dock.setMinimumWidth(225)
        self.dock.resize(300, self.height())

    def _dock_visibility_changed(self, visible):
        # isHidden() tracks deliberate collapse, unlike isVisible() which is
        # false whenever the WHOLE app is minimized/closed or not shown yet.
        expanded = not self.dock.isHidden()
        previous_blocked = self.sidebar_button.blockSignals(True)
        self.sidebar_button.setChecked(expanded)
        self.sidebar_button.setText("Crops ◀" if expanded else "Crops ▶")
        self.sidebar_button.blockSignals(previous_blocked)
        if self.isVisible() and not self._closing:
            self._save_session()

    def _toggle_sidebar(self):
        expand = self.dock.isHidden()
        self.dock.setVisible(expand)
        if expand:
            self.dock.raise_()
        self._dock_visibility_changed(expand)

    def _session_path(self) -> str:
        base = QtCore.QStandardPaths.writableLocation(QtCore.QStandardPaths.AppDataLocation)
        if not base:
            base = os.path.join(str(Path.home()), ".audiocrop")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "crop_queue.json")

    def _save_session(self):
        if not getattr(self, "_session_ready", False) or not hasattr(self, "crop_list") or self._restoring_list:
            return
        try:
            path = self._session_path()
            saved = {"version": 3, "next_index": self.next_index,
                     "sidebar_visible": not self.dock.isHidden(),
                     "crops": [asdict(c) for c in self.crops]}
            tmp = path + ".new"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(saved, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            self.statusBar().showMessage("Queue could not be persisted: " + str(exc), 10000)

    def _load_session(self):
        self._restoring_list = True
        try:
            with open(self._session_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("version") not in (2, 3):
                return
            self.crops = [Crop.from_record(rec) for rec in data.get("crops", [])]
            self.next_index = max(1, int(data.get("next_index", 1)))
            for crop in self.crops:
                found = re.search(r"crop\((\d+)\)", crop.name)
                if found:
                    self.next_index = max(self.next_index, int(found.group(1)) + 1)
            self._rebuild_crop_list()
            self.dock.setVisible(bool(data.get("sidebar_visible", True)))
        except FileNotFoundError:
            self.dock.show()
        except Exception as exc:
            self.statusBar().showMessage("Could not restore queue: " + str(exc), 10000)
            self.dock.show()
        finally:
            self._restoring_list = False
            self._session_ready = True
        self._refresh_edit_controls()

    def _crop_description(self, crop: Crop) -> str:
        action = "KEEP" if crop.keep else "REMOVE"
        rate = (f" · {crop.playback_rate():g}× tempo" if crop.speed_enabled
                else "")
        return (f"{crop.name} · {format_time(crop.effective_duration())}{rate}\n"
                f"{os.path.basename(crop.source)}  "
                f"[{action} {format_time(crop.start)}–{format_time(crop.end)}]")

    def _rebuild_crop_list(self, selected_id: Optional[str] = None):
        self._restoring_list = True
        try:
            self.crop_list.clear()
            for crop in self.crops:
                item = QtWidgets.QListWidgetItem(self._crop_description(crop))
                item.setData(QtCore.Qt.UserRole, crop.uid)
                item.setToolTip(crop.source)
                if not os.path.isfile(crop.source):
                    item.setForeground(QtGui.QBrush(QtGui.QColor("#f3a395")))
                    item.setToolTip("Missing source: " + crop.source)
                if crop.uid == self.editing_crop_id:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.crop_list.addItem(item)
                if crop.uid == selected_id:
                    self.crop_list.setCurrentItem(item)
        finally:
            self._restoring_list = False
        self._refresh_edit_controls()

    def _refresh_crop_row(self, uid: str):
        """Update one queue entry after a preview nudge; preserve its scroll/order."""
        crop = self._find_crop(uid)
        if crop is None:
            return
        for row in range(self.crop_list.count()):
            item = self.crop_list.item(row)
            if item.data(QtCore.Qt.UserRole) != uid:
                continue
            item.setText(self._crop_description(crop))
            # Avoid scroll jumps from clearing/repopulating the list on every arrow.
            item.setToolTip(crop.source)
            self.crop_list.setCurrentItem(item, QtCore.QItemSelectionModel.NoUpdate)
            return

    def _list_reordered(self, *_):
        if self._restoring_list:
            return
        by_id = {c.uid: c for c in self.crops}
        keys = [self.crop_list.item(i).data(QtCore.Qt.UserRole) for i in range(self.crop_list.count())]
        if len(keys) != len(self.crops) or len(set(keys)) != len(keys):
            self._rebuild_crop_list()
            return
        self.crops = [by_id[k] for k in keys]
        # Qt's model is still finishing its move signal: never rebuild it here.
        QtCore.QTimer.singleShot(0, lambda: self._queue_changed(rebuild=False))

    def _queue_changed(self, rebuild: bool = True):
        if self.preview_mode:
            self.exit_preview()
        if rebuild:
            self._rebuild_crop_list()
        self._save_session()
        self._refresh_edit_controls()

    def _find_crop(self, uid: Optional[str]) -> Optional[Crop]:
        return next((c for c in self.crops if c.uid == uid), None)

    def _selected_crop(self) -> Optional[Crop]:
        item = self.crop_list.currentItem()
        return self._find_crop(item.data(QtCore.Qt.UserRole)) if item else None

    def _speed_target_crop(self) -> Optional[Crop]:
        if self.preview_mode:
            if self.preview_boundary_id:
                chosen = self._find_crop(self.preview_boundary_id[0])
                if chosen:
                    return chosen
            return self._selected_crop()
        return self._find_crop(self.editing_crop_id)

    def _sync_speed_controls(self):
        if not hasattr(self, "speed_checkbox"):
            return
        crop = self._speed_target_crop()
        if self.preview_mode:
            enabled = crop is not None and not self._busy
            checked = crop.speed_enabled if crop else False
            rate = crop.speed_rate if crop else 1.0
            label = f"Editing {crop.name} · saved tempo" if crop else "Select a crop to adjust its speed"
        else:
            enabled = self.audio_samples is not None and not self._busy
            checked, rate = self._draft_speed_enabled, self._draft_speed_rate
            label = (f"Editing {crop.name} · saved on Update crop" if crop else
                     "Current crop · saved when added/exported")
        old = self.speed_checkbox.blockSignals(True)
        self.speed_checkbox.setChecked(checked)
        self.speed_checkbox.blockSignals(old)
        old = self.speed_rate.blockSignals(True)
        self.speed_rate.setValue(rate)
        self.speed_rate.blockSignals(old)
        self.speed_checkbox.setEnabled(enabled)
        self.speed_rate.setEnabled(enabled)
        self.speed_rate.setVisible(checked)
        self.speed_target_label.setText(label)

    def _on_crop_selection_changed(self, current, previous):
        if self._restoring_list or not self.preview_mode:
            return
        uid = current.data(QtCore.Qt.UserRole) if current else None
        if self.preview_boundary_id and uid != self.preview_boundary_id[0]:
            self.preview_boundary_id = None
            self.waveform.preview_selected = None
            self.waveform.update()
            self._update_preview_marker_description()
        self._sync_speed_controls()

    def _update_crop_speed(self):
        """Speed edits modify only the Crop model and virtual joined timeline.

        No decoded audio gets modified, and no FFmpeg processing occurs here.
        The old playback file is discarded; Play builds a fresh one on demand.
        """
        crop = self._speed_target_crop()
        if crop is None:
            return
        crop.speed_enabled = self.speed_checkbox.isChecked()
        crop.speed_rate = validate_speed_rate(self.speed_rate.value())
        self._preview_edited_crop_ids.add(crop.uid)
        cue_key = self.preview_boundary_id
        # Cue either the selected marker or the beginning of the changed crop.
        old_seg = next((seg for seg in self._preview_segments if seg.crop_uid == crop.uid), None)
        cue = (self._preview_cue_time(cue_key) if cue_key and cue_key[0] == crop.uid
               else max(0.0, old_seg.preview_start - 3.0) if old_seg else 0.0)
        self._rebuild_preview_timeline(cursor=cue)
        self._refresh_crop_row(crop.uid)
        self._save_session()
        self.statusBar().showMessage(
            f"{crop.name} saved at {crop.playback_rate():g}×; press Space to audition", 4500)

    def _speed_toggled(self, checked: bool):
        if self._busy:
            return
        self.speed_rate.setVisible(checked)
        if self.preview_mode:
            self._update_crop_speed()
        else:
            self._draft_speed_enabled = bool(checked)
            self._stop_and_unload_media()

    def _speed_rate_changed(self, rate: float):
        if self._busy:
            return
        if self.preview_mode:
            self._update_crop_speed()
        else:
            self._draft_speed_rate = validate_speed_rate(rate)
            self._stop_and_unload_media()

    def _current_crop(self, uid: Optional[str] = None, name: str = "") -> Crop:
        if self.audio_samples is None or not self.original_file or self.preview_mode:
            raise ValueError("Open a source audio file and select a crop first")
        stat = os.stat(self.original_file)
        return Crop(uid or str(uuid.uuid4()), name,
                    self.original_file, self.waveform.left_frac * self.duration,
                    self.waveform.right_frac * self.duration,
                    self.btn_keep.isChecked(), self.duration, stat.st_size, stat.st_mtime_ns,
                    self._draft_speed_enabled, self._draft_speed_rate)

    def add_crop(self):
        if self._busy or self.preview_mode or self.audio_samples is None:
            return
        try:
            crop = self._current_crop(name=f"crop({self.next_index})")
            if not crop.parts():
                raise ValueError("Crop contains no audible region")
            self.next_index += 1
            self.crops.append(crop)
            self.editing_crop_id = None
            self._queue_changed()
            self.crop_list.setCurrentRow(self.crop_list.count() - 1)
            # Respect the user's collapsed sidebar state. The same top-row
            # button can reopen it whenever they want.
            self.statusBar().showMessage(crop.name + " added (metadata only)", 4000)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Cannot add crop", str(e))

    def update_crop(self):
        if not self.editing_crop_id or self._busy or self.preview_mode:
            return
        old = self._find_crop(self.editing_crop_id)
        if old:
            try:
                replacement = self._current_crop(old.uid, old.name)
                if not replacement.parts():
                    raise ValueError("Crop contains no audible region")
                self.crops[self.crops.index(old)] = replacement
                self._queue_changed()
                self.statusBar().showMessage(old.name + " updated", 4000)
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Cannot update crop", str(e))

    def delete_selected_crop(self):
        crop = self._selected_crop()
        if crop and not self._busy:
            self.crops.remove(crop)
            if self.editing_crop_id == crop.uid:
                self.editing_crop_id = None
            self._queue_changed()

    def clear_crops(self):
        if not self.crops or self._busy:
            return
        answer = QtWidgets.QMessageBox.question(
            self, "Clear crop queue?", "Remove all saved crop information? Original audio files are not affected.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if answer == QtWidgets.QMessageBox.Yes:
            self.crops.clear()
            self.editing_crop_id = None
            self._queue_changed()

    def _run_task(self, label: str, action, on_success):
        if self._busy:
            return
        self._busy = True
        self.centralWidget().setEnabled(False)
        self.dock.widget().setEnabled(False)
        self.statusBar().showMessage(label)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        thread = TaskThread(action, self)
        self._worker = thread

        def done(result=None, error=None):
            self._busy = False
            self.centralWidget().setEnabled(True)
            self.dock.widget().setEnabled(True)
            if QtWidgets.QApplication.overrideCursor():
                QtWidgets.QApplication.restoreOverrideCursor()
            self._worker = None
            if error:
                QtWidgets.QMessageBox.critical(self, "Operation failed", error)
                self.statusBar().showMessage("Operation failed", 8000)
            else:
                try:
                    on_success(result)
                except Exception as exc:
                    QtWidgets.QMessageBox.critical(self, "Operation failed", str(exc))

        # Only restore the UI after QThread itself has finished, not while it
        # is still emitting its completion signal (prevents close-time races).
        outcome = {"result": None, "error": None}
        thread.succeeded.connect(lambda result: outcome.__setitem__("result", result))
        thread.failed.connect(lambda message: outcome.__setitem__("error", message))
        thread.finished.connect(lambda: done(result=outcome["result"], error=outcome["error"]))
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _stop_and_unload_media(self):
        self.media_player.stop()
        self._pending_play = False
        self._playback_map = None
        self.media_player.setMedia(QtMultimedia.QMediaContent())
        self.btn_play.setText("▶")
        safe_unlink(self._preview_playback_path)
        self._preview_playback_path = None

    def _destroy_joined_preview(self):
        safe_unlink(self._joined_preview_path)
        self._joined_preview_path = None

    def open_file(self):
        if self._busy:
            return
        start = self._starting_directory()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open audio", start, OPEN_FILTER)
        if path:
            self._load_audio_path(path, crop=None)

    def _starting_directory(self):
        if self.original_file and os.path.exists(self.original_file):
            return os.path.dirname(self.original_file)
        if os.path.isdir(DEFAULT_DIR):
            return DEFAULT_DIR
        return str(Path.home() / "Downloads")

    def _load_audio_path(self, path: str, crop: Optional[Crop]):
        if not os.path.isfile(path):
            QtWidgets.QMessageBox.warning(self, "Missing source file", path)
            return
        def loaded(samples):
            if self.preview_mode:
                self._stop_and_unload_media()
                self._destroy_joined_preview()
                self.preview_mode = False
                self._editor_before_preview = None
            self._set_editor_audio(path, samples, crop)
            self.statusBar().showMessage("Loaded " + os.path.basename(path), 4000)
        self._run_task("Decoding source waveform…", lambda: decode_mono_samples(path), loaded)

    def _set_editor_audio(self, path: str, samples: np.ndarray, crop: Optional[Crop]):
        self._stop_and_unload_media()
        self.audio_samples = samples
        self.original_file = path
        self.duration = len(samples) / SAMPLE_RATE
        self.preview_mode = False
        self.editing_crop_id = crop.uid if crop else None
        self._draft_speed_enabled = crop.speed_enabled if crop else False
        self._draft_speed_rate = crop.speed_rate if crop else 1.0
        self.waveform.restore_editor_height()
        self.waveform.set_audio_data(samples, self.duration, editable=True)
        self.set_mode(crop.keep if crop else True)
        if crop:
            self.waveform.set_region(crop.start, crop.end)
            self.waveform.set_cursor_time(crop.start if crop.keep else 0)
        else:
            self.waveform.set_cursor_time(0)
        self.active_handle = None
        self.file_label.setText(os.path.basename(path) + ("  ·  editing " + crop.name if crop else ""))
        self.file_label.setToolTip(path)
        self._rebuild_crop_list(selected_id=crop.uid if crop else None)
        self._refresh_edit_controls()

    def load_selected_crop(self, item=None):
        if self._busy:
            return
        crop = self._find_crop(item.data(QtCore.Qt.UserRole)) if item else self._selected_crop()
        if crop:
            self._load_audio_path(crop.source, crop)

    def _refresh_edit_controls(self):
        if not hasattr(self, "add_button"):
            return
        enabled = self.audio_samples is not None and not self.preview_mode
        for btn in (self.btn_keep, self.btn_remove, self.reset_button, self.add_button):
            btn.setEnabled(enabled)
        # In joined preview these fields show the selected piece's original
        # source timestamps, rather than meaningless whole-join handles.
        for field in (self.edit_left, self.edit_right):
            field.setEnabled(enabled or self.preview_mode)
            field.setReadOnly(self.preview_mode)
        self.save_button.setEnabled(enabled)
        self.update_button.setEnabled(enabled and bool(self.editing_crop_id))
        self.btn_preview_queue.setEnabled(bool(self.crops) and not self.preview_mode)
        self.btn_return.setVisible(self.preview_mode)
        self.btn_export.setEnabled(bool(self.crops))
        self.btn_del_crop.setEnabled(bool(self.crops))
        self.btn_clear.setEnabled(bool(self.crops))
        if self.preview_mode:
            self.btn_keep.setChecked(True)
            self.btn_remove.setChecked(False)
            self._update_preview_marker_description()
        else:
            self.mode_label.setText("Space: play/pause · ←/→: move selected pink handle 0.05s")
        self._sync_speed_controls()

    def _capture_editor_state(self):
        return dict(path=self.original_file, samples=self.audio_samples,
                    crop_id=self.editing_crop_id, duration=self.duration,
                    left=self.waveform.left_frac * self.duration,
                    right=self.waveform.right_frac * self.duration,
                    cursor=self.waveform.cursor_frac * self.duration,
                    keep=self.btn_keep.isChecked(),
                    speed_enabled=self._draft_speed_enabled,
                    speed_rate=self._draft_speed_rate)

    def preview_joined(self):
        if self._busy or not self.crops or self.preview_mode:
            return
        missing = [c.source for c in self.crops if not os.path.isfile(c.source)]
        if missing:
            QtWidgets.QMessageBox.warning(self, "Missing source files", "\n".join(dict.fromkeys(missing)))
            return
        snapshot = self._capture_editor_state()
        clips = [copy.copy(c) for c in self.crops]
        originals = list(dict.fromkeys(c.source for c in clips))

        def job():
            # Decode each distinct source ONCE. The fine-tuning preview then
            # works with in-memory PCM, not FFmpeg processes on each key press.
            cache = {}
            for source in originals:
                cache[source] = (snapshot["samples"] if source == snapshot["path"]
                                 and snapshot["samples"] is not None
                                 else decode_mono_samples(source))
            describe_joined_preview(clips, cache)  # validate timestamp map only; no PCM join
            return cache

        def completed(cache):
            self._stop_and_unload_media()
            self._destroy_joined_preview()
            self._editor_before_preview = snapshot
            self._preview_source_samples = cache
            self._preview_segments = []
            self.preview_boundary_id = None
            self._preview_edited_crop_ids = set()
            self.preview_mode = True
            if not self._selected_crop() and self.crop_list.count():
                self.crop_list.setCurrentRow(0)
            self._rebuild_preview_timeline(cursor=0)
            self._refresh_edit_controls()
            self.statusBar().showMessage(
                "Joined preview: each crop has its own row. Drag a boundary or use ←/→; Space auditions the updated join.", 8000)

        self._run_task("Decoding original sources for adjustable joined preview…", job, completed)

    def _rebuild_preview_timeline(self, cursor: Optional[float] = None):
        """Only adjust Crop timestamps and draw virtual rows; no audio splicing."""
        if not self.preview_mode:
            return
        segments, count = describe_joined_preview(self.crops, self._preview_source_samples)
        # Edits invalidate playback, but waveform updates never allocate a
        # joined PCM buffer or run audio filters while a boundary is dragged.
        self._stop_and_unload_media()
        self._destroy_joined_preview()
        self.audio_samples = np.empty(0, dtype=np.int16)  # preview-mode sentinel
        self.audio_rate = SAMPLE_RATE
        self.duration = count / SAMPLE_RATE
        self._preview_segments = segments
        selected = self.preview_boundary_id
        if selected and not any((seg.crop_uid, seg.part_index) == selected[:2]
                                for seg in segments):
            selected = None
            self.preview_boundary_id = None
        self.waveform.set_audio_data(self.audio_samples, self.duration, editable=False)
        self.waveform.set_keep_mode(True)
        self.waveform.set_preview_segments(segments, selected,
                                           source_samples=self._preview_source_samples)
        if cursor is None and selected:
            seg = self.waveform.selected_preview_segment()
            cursor = seg.boundary_time(selected[2]) if seg else 0
        self.waveform.set_cursor_time(cursor if cursor is not None else 0)
        self.file_label.setText(
            f"Joined preview · {len(self.crops)} crops · {len(segments)} green pieces · {format_time(self.duration)}")
        self.file_label.setToolTip(
            "Crop boundaries are timestamps referencing original audio. "
            "The waveform is virtual, and combined audio is built only on Play.")
        self._update_preview_marker_description()
        self._sync_speed_controls()

    def _preview_boundary_selected(self, key):
        if not self.preview_mode:
            return
        self.preview_boundary_id = key
        self.active_handle = None
        self._update_preview_marker_description()
        if key:
            self._refresh_crop_row(key[0])
        self._sync_speed_controls()

    def _update_preview_marker_description(self):
        if not self.preview_mode:
            return
        key = self.preview_boundary_id
        seg = next((s for s in self._preview_segments
                    if key and (s.crop_uid, s.part_index) == key[:2]), None)
        if seg is None:
            self.mode_label.setText("Each green row is one clip · drag a bar, or ←/→ to fine tune · Space plays all")
            self.edit_left.setText("—")
            self.edit_right.setText("—")
            return
        side = key[2]
        crop = self._find_crop(seg.crop_uid)
        editable = seg.boundary_editable(side)
        self.edit_left.setText(format_time(seg.source_start))
        self.edit_right.setText(format_time(seg.source_end))
        part = (f" · part {seg.part_index + 1}" if crop and not crop.keep else "")
        suffix = "drag or ←/→ 50ms · Shift 5ms · Ctrl 1ms · Space audition" if editable else "locked source edge"
        self.mode_label.setText(f"{seg.crop_name}{part} {side.upper()} · {seg.speed_rate:g}× · {suffix}")
        self.statusBar().showMessage(
            f"{seg.crop_name}{part}: {side.upper()} at original {format_time(seg.original_time(side))} "
            f"of {os.path.basename(seg.source)} — {suffix}", 7500)

    def _preview_cue_time(self, key: Optional[Tuple[str, int, str]]) -> float:
        """Cue playback several seconds BEFORE the adjusted edit or join.

        For Start boundaries choose the previous piece's End (the seam). This
        allows Space to audition the preceding crop flowing into the new crop.
        For End boundaries choose several seconds before that End.
        """
        if not key:
            return 0.0
        for index, seg in enumerate(self._preview_segments):
            if (seg.crop_uid, seg.part_index) == key[:2]:
                if key[2] == "start":
                    # Audition the transition from INSIDE the previous piece,
                    # even when the previous piece itself lasts under 3 sec.
                    if index:
                        previous = self._preview_segments[index - 1]
                        return max(previous.preview_start, previous.preview_end - 3.0)
                    return 0.0
                # An End edit replays the tail of its own crop into the next.
                return max(seg.preview_start, seg.preview_end - 3.0)
        return 0.0

    def _edit_preview_boundary(self, key: Tuple[str, int, str],
                               *, delta: Optional[float] = None,
                               absolute_source: Optional[float] = None,
                               cue: bool = True) -> bool:
        if not self.preview_mode:
            return False
        uid, part, side = key
        crop = self._find_crop(uid)
        seg = next((s for s in self._preview_segments
                    if (s.crop_uid, s.part_index) == (uid, part)), None)
        if crop is None or seg is None or not seg.boundary_editable(side):
            return False
        step = (float(absolute_source) - seg.original_time(side)
                if absolute_source is not None else float(delta or 0))
        if not nudge_preview_boundary(crop, part, side, step):
            return False
        self.preview_boundary_id = key
        self._preview_edited_crop_ids.add(uid)
        self._rebuild_preview_timeline(cursor=0.0)  # invalidate old playback, redraw timestamps
        self._refresh_crop_row(uid)
        if cue:
            self.waveform.set_cursor_time(self._preview_cue_time(key))
        self._save_session()
        self.waveform.setFocus(QtCore.Qt.MouseFocusReason)
        return True

    def _preview_boundary_dragged(self, key, target_source: float):
        self._edit_preview_boundary(key, absolute_source=target_source, cue=False)

    def _preview_drag_finished(self, key):
        if self.preview_mode:
            # Stopped/unloaded during editing, so Space always plays the newest
            # timeline rather than resuming a stale/previously completed WAV.
            self.waveform.set_cursor_time(self._preview_cue_time(key))
            self.waveform.setFocus(QtCore.Qt.MouseFocusReason)

    def _nudge_preview_boundary(self, delta: float):
        if not self.preview_mode or not self.preview_boundary_id:
            return
        if not self._edit_preview_boundary(self.preview_boundary_id, delta=delta):
            self.statusBar().showMessage(
                "Selected boundary: source edge or adjacent boundary reached", 3000)

    def exit_preview(self):
        if not self.preview_mode or self._busy:
            return
        self._stop_and_unload_media()
        self._destroy_joined_preview()
        state = self._editor_before_preview
        edited = set(self._preview_edited_crop_ids)
        self._editor_before_preview = None
        self._preview_source_samples.clear()
        self._preview_segments.clear()
        self.preview_boundary_id = None
        self.waveform.restore_editor_height()
        self._preview_edited_crop_ids.clear()
        self.preview_mode = False
        if state and state["samples"] is not None:
            crop = self._find_crop(state["crop_id"])
            self._set_editor_audio(state["path"], state["samples"], crop)
            if state["crop_id"] not in edited:
                # Restore an unsaved editing draft unless that crop was just
                # fine-tuned in the joined preview (where edits save directly).
                self.set_mode(state["keep"])
                self.waveform.set_region(state["left"], state["right"])
                self.waveform.set_cursor_time(state["cursor"])
                self._draft_speed_enabled = state["speed_enabled"]
                self._draft_speed_rate = state["speed_rate"]
                self._sync_speed_controls()
        else:
            self.audio_samples = None
            self.duration = 0.0
            self.original_file = ""
            self.editing_crop_id = None
            self.waveform.restore_editor_height()
            self.waveform.set_audio_data(np.empty(0, dtype=np.int16), 0)
            self.file_label.setText("No file loaded")
            self._refresh_edit_controls()

    def set_mode(self, keep: bool):
        if self.preview_mode:
            return
        self.btn_keep.setChecked(keep)
        self.btn_remove.setChecked(not keep)
        self.waveform.set_keep_mode(keep)
        self._stop_and_unload_media()

    def reset_trim(self):
        if self.audio_samples is None or self.preview_mode:
            return
        self.waveform.set_region(0, self.duration)
        self.waveform.set_cursor_time(0)
        self.stop()

    def on_handle_focused(self, handle):
        self.active_handle = handle

    def on_handle_focus_lost(self):
        self.active_handle = None

    def on_handle_drag_finished(self, handle):
        if self.audio_samples is None or self.preview_mode:
            return
        left = self.waveform.left_frac * self.duration
        right = self.waveform.right_frac * self.duration
        keep = self.btn_keep.isChecked()
        if handle == "left":
            cursor = left if keep else (right if left <= .001 else max(0, left - 3))
        else:
            cursor = min(self.waveform.cursor_frac * self.duration, right) if keep else right
        self.waveform.set_cursor_time(cursor)
        if self.media_player.state() == QtMultimedia.QMediaPlayer.PlayingState:
            self.restart_playback()
        else:
            self._stop_and_unload_media()

    def eventFilter(self, obj, event):
        if event.type() == QtCore.QEvent.KeyPress and self.isActiveWindow() and not self._busy:
            if QtWidgets.QApplication.activeModalWidget():
                return False
            focus = self.focusWidget()
            speed_box = (focus if isinstance(focus, QtWidgets.QAbstractSpinBox)
                         else focus.parentWidget() if isinstance(focus, QtWidgets.QLineEdit)
                         and focus.parentWidget() is self.speed_rate else None)
            if speed_box is self.speed_rate:
                # Make Space a one-keystroke audition even immediately after
                # typing a new rate; commit it before generating preview audio.
                # Arrow keys still belong to the speed field while it is focused.
                if event.key() == QtCore.Qt.Key_Space and not event.isAutoRepeat():
                    self.speed_rate.interpretText()
                    self.toggle_play()
                    return True
                return False
            if isinstance(focus, (QtWidgets.QLineEdit, QtWidgets.QTextEdit,
                                  QtWidgets.QPlainTextEdit, QtWidgets.QAbstractSpinBox)):
                return False
            if event.key() == QtCore.Qt.Key_Space and not event.isAutoRepeat():
                self.toggle_play()
                return True
            if event.key() == QtCore.Qt.Key_Escape and self.preview_mode:
                self.exit_preview()
                return True
            if event.key() in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right) and self.waveform.hasFocus():
                delta = -self.FINE_STEP if event.key() == QtCore.Qt.Key_Left else self.FINE_STEP
                if self.preview_mode and self.preview_boundary_id:
                    # Preserve the original 50ms nudge while enabling genuine
                    # millisecond precision for joined-boundary fine tuning.
                    if event.modifiers() & QtCore.Qt.ControlModifier:
                        delta = (1 if delta > 0 else -1) * .001
                    elif event.modifiers() & QtCore.Qt.ShiftModifier:
                        delta = (1 if delta > 0 else -1) * .005
                    self._nudge_preview_boundary(delta)
                    return True
                if self.active_handle and not self.preview_mode:
                    self._nudge_handle(self.active_handle, delta)
                    return True
        return super().eventFilter(obj, event)

    def _nudge_handle(self, handle, delta):
        if self.audio_samples is None:
            return
        if handle == "left":
            self.waveform.set_left_time(self.waveform.left_frac * self.duration + delta)
        else:
            self.waveform.set_right_time(self.waveform.right_frac * self.duration + delta)
        self.on_handle_drag_finished(handle)

    def on_waveform_clicked(self, orig_time: float):
        if self.preview_mode:
            if self.media_player.state() in (QtMultimedia.QMediaPlayer.PlayingState,
                                              QtMultimedia.QMediaPlayer.PausedState):
                self.media_player.setPosition(round(orig_time * 1000))
        elif self.media_player.state() == QtMultimedia.QMediaPlayer.PlayingState:
            self.restart_playback()
        else:
            self._stop_and_unload_media()

    def _build_playback_segment(self, orig: float):
        if self.audio_samples is None:
            return None, None
        sr = self.audio_rate
        total = len(self.audio_samples)
        a = max(0, min(total, round(self.waveform.left_frac * self.duration * sr)))
        b = max(a, min(total, round(self.waveform.right_frac * self.duration * sr)))
        position = max(0, min(total, round(orig * sr)))
        if self.btn_keep.isChecked():
            start = max(a, position)
            return self.audio_samples[start:b], {"mode": "keep", "offset": start / sr}
        if position < a:
            first = self.audio_samples[position:a]
            second = self.audio_samples[b:]
            samples = np.concatenate((first, second)) if len(second) else first
            return samples, {"mode": "remove", "offset": position / sr,
                             "first": len(first) / sr, "after": b / sr}
        start = max(b, position)
        return self.audio_samples[start:], {"mode": "keep", "offset": start / sr}

    def restart_playback(self):
        if self.audio_samples is None or self._busy:
            return
        if self.preview_mode:
            if self.waveform.cursor_frac * self.duration >= self.duration - .01:
                self.waveform.set_cursor_time(self._preview_cue_time(self.preview_boundary_id))
            path = self._joined_preview_path
            if not path and any(abs(c.playback_rate() - 1.0) > 1e-8 for c in self.crops):
                # Pitch preservation is applied ONLY upon Play, not while
                # changing the numerical rate or dragging a source marker.
                # FFmpeg runs on a background thread so the UI never freezes.
                path = temporary_path(".wav")
                clips = [copy.copy(c) for c in self.crops]
                seek = round(self.waveform.cursor_frac * self.duration * 1000)
                def work():
                    try:
                        render_audio(clips, path, "preview")
                        return path
                    except Exception:
                        safe_unlink(path)
                        raise
                def ready(result):
                    if not self.preview_mode:
                        safe_unlink(result)
                        return
                    self._joined_preview_path = result
                    self._begin_media(result, seek, {"mode": "joined"})
                self._run_task("Rendering pitch-preserving joined preview…", work, ready)
                return
            if not path:
                path = temporary_path(".wav")
                try:
                    # Joined editor edits ONLY source timestamps. Assemble the
                    # temporary playback buffer when the user presses Space,
                    # with no fades, destructive source edits, or hidden audio.
                    joined, live_segments = assemble_joined_preview(
                        self.crops, self._preview_source_samples)
                    if live_segments != self._preview_segments:
                        raise RuntimeError("The joined playback timeline changed unexpectedly")
                    write_wav_fast(path, joined, SAMPLE_RATE)
                    self._joined_preview_path = path
                except Exception as exc:
                    safe_unlink(path)
                    QtWidgets.QMessageBox.critical(self, "Playback error", str(exc))
                    return
            self._begin_media(path, round(self.waveform.cursor_frac * self.duration * 1000),
                              {"mode": "joined"})
            return
        if self._draft_speed_enabled and abs(self._draft_speed_rate - 1) > 1e-8:
            self._play_speed_adjusted_source()
            return
        orig = self.waveform.cursor_frac * self.duration
        samples, mapping = self._build_playback_segment(orig)
        if samples is None or not len(samples):
            self.stop()
            return
        path = temporary_path(".wav")
        try:
            write_wav_fast(path, samples, self.audio_rate)
        except Exception as exc:
            safe_unlink(path)
            QtWidgets.QMessageBox.critical(self, "Playback error", str(exc))
            return
        self._begin_media(path, 0, mapping, generated_path=True)

    def _play_speed_adjusted_source(self):
        """Use the same pitch-preserving export pipeline for source audition."""
        if not self.original_file or self.audio_samples is None:
            return
        rate = validate_speed_rate(self._draft_speed_rate)
        cursor = self.waveform.cursor_frac * self.duration
        a = self.waveform.left_frac * self.duration
        b = self.waveform.right_frac * self.duration
        pieces = []
        if self.btn_keep.isChecked():
            start = max(a, cursor)
            if start < b:
                pieces.append((start, b))
            mapping = {"mode": "keep-speed", "offset": start, "rate": rate}
        elif cursor < a:
            if cursor < a:
                pieces.append((cursor, a))
            if b < self.duration:
                pieces.append((b, self.duration))
            mapping = {"mode": "remove-speed", "offset": cursor,
                       "first": (a - cursor) / rate, "after": b, "rate": rate}
        else:
            start = max(b, cursor)
            if start < self.duration:
                pieces.append((start, self.duration))
            mapping = {"mode": "keep-speed", "offset": start, "rate": rate}
        if not pieces:
            self.stop()
            return
        stat = os.stat(self.original_file)
        clips = [Crop(str(uuid.uuid4()), "source-playback", self.original_file, start, end,
                      True, self.duration, stat.st_size, stat.st_mtime_ns, True, rate)
                 for start, end in pieces]
        path = temporary_path(".wav")
        def work():
            try:
                render_audio(clips, path, "preview")
                return path
            except Exception:
                safe_unlink(path)
                raise
        self._run_task("Rendering pitch-preserving crop preview…", work,
                       lambda result: self._begin_media(result, 0, mapping, generated_path=True))

    def _begin_media(self, path: str, seek_ms: int, mapping: Dict[str, Any], generated_path=False):
        self._stop_and_unload_media()
        self._preview_playback_path = path if generated_path else None
        self._playback_map = mapping
        self._pending_seek = seek_ms
        self._pending_play = True
        self.media_player.setMedia(QtMultimedia.QMediaContent(QtCore.QUrl.fromLocalFile(path)))
        QtCore.QTimer.singleShot(0, self._start_pending_player)

    def _start_pending_player(self):
        if not self._pending_play:
            return
        status = self.media_player.mediaStatus()
        if status in (QtMultimedia.QMediaPlayer.LoadedMedia, QtMultimedia.QMediaPlayer.BufferedMedia,
                      QtMultimedia.QMediaPlayer.BufferingMedia):
            self._pending_play = False
            self.media_player.setPosition(self._pending_seek)
            self.media_player.play()

    def _player_media_status(self, status):
        if status in (QtMultimedia.QMediaPlayer.LoadedMedia, QtMultimedia.QMediaPlayer.BufferedMedia,
                      QtMultimedia.QMediaPlayer.BufferingMedia):
            self._start_pending_player()
        elif status == QtMultimedia.QMediaPlayer.EndOfMedia:
            self.btn_play.setText("▶")
            # After the final sample, pressing Space should replay rather than
            # seeking to the very end of the old file and stopping instantly.
            if self.preview_mode:
                self.waveform.set_cursor_time(self._preview_cue_time(self.preview_boundary_id))
            else:
                self.waveform.set_cursor_time(0)
        elif status == QtMultimedia.QMediaPlayer.InvalidMedia and self._pending_play:
            self._pending_play = False
            self.statusBar().showMessage("Media player could not load preview; check Windows codecs", 10000)

    def _player_state(self, state):
        self.btn_play.setText("⏸" if state == QtMultimedia.QMediaPlayer.PlayingState else "▶")

    def _player_position(self, millis: int):
        mapping = self._playback_map
        if mapping is None or self.duration <= 0:
            return
        t = millis / 1000
        if mapping["mode"] == "joined":
            sec = t
        elif mapping["mode"] == "keep-speed":
            sec = mapping["offset"] + t * mapping["rate"]
        elif mapping["mode"] == "remove-speed":
            sec = (mapping["offset"] + t * mapping["rate"]
                   if t <= mapping["first"] else
                   mapping["after"] + (t - mapping["first"]) * mapping["rate"])
        elif mapping["mode"] == "keep":
            sec = mapping["offset"] + t
        else:
            sec = (mapping["offset"] + t if t <= mapping["first"] else
                   mapping["after"] + (t - mapping["first"]))
        self.waveform.cursor_frac = max(0, min(sec / self.duration, 1))
        self.waveform.update()

    def toggle_play(self):
        if self.audio_samples is None:
            self.open_file()
            return
        state = self.media_player.state()
        if state == QtMultimedia.QMediaPlayer.PlayingState:
            self.media_player.pause()
        elif state == QtMultimedia.QMediaPlayer.PausedState:
            # Paused playback is valid only while its mapping/audio file still
            # matches the current timeline. Rebuild clears the mapping.
            if self._playback_map is not None:
                self.media_player.play()
            else:
                self.restart_playback()
        else:
            self.restart_playback()

    def stop(self):
        self._stop_and_unload_media()

    @staticmethod
    def _parse_time(value: str) -> Optional[float]:
        try:
            segments = value.strip().split(":")
            if len(segments) == 2:
                return max(0.0, int(segments[0]) * 60 + float(segments[1]))
            return max(0.0, float(value))
        except ValueError:
            return None

    def on_left_time_edited(self):
        if self.preview_mode:
            return
        result = self._parse_time(self.edit_left.text())
        if result is not None:
            self.waveform.set_left_time(result)
            self.on_handle_drag_finished("left")

    def on_right_time_edited(self):
        if self.preview_mode:
            return
        result = self._parse_time(self.edit_right.text())
        if result is not None:
            self.waveform.set_right_time(result)
            self.on_handle_drag_finished("right")

    def _default_save_name(self, joined: bool):
        if joined:
            first = os.path.splitext(os.path.basename(self.crops[0].source))[0] if self.crops else "audio"
            return first + " (joined).m4a" if self.crops and self.crops[0].source.lower().endswith((".m4a", ".m4u")) else first + " (joined).mp3"
        name = os.path.splitext(os.path.basename(self.original_file or "audio"))[0]
        match = re.search(r"\s*\((\d+)\)\s*$", name)
        name = name[:match.start()] + f"({int(match.group(1)) + 1})" if match else name + "(1)"
        extension = ".m4a" if self.original_file.lower().endswith((".m4a", ".m4u")) else ".mp3"
        return name + extension

    def save_file(self):
        if self.audio_samples is None or self.preview_mode:
            return
        try:
            current = self._current_crop(name="current")
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Cannot save", str(exc))
            return
        self._export_crops([current], joined=False)

    def export_joined(self):
        if self.crops:
            self._export_crops(list(self.crops), joined=True)

    def _export_crops(self, crops: List[Crop], joined: bool):
        if self._busy or not crops:
            return
        missing = list(dict.fromkeys(c.source for c in crops if not os.path.isfile(c.source)))
        if missing:
            QtWidgets.QMessageBox.warning(self, "Missing source audio", "Cannot export:\n" + "\n".join(missing))
            return
        if not any(c.parts() for c in crops):
            QtWidgets.QMessageBox.warning(self, "Empty output", "No audio remains in these crops")
            return
        changed = []
        for src, crop in {c.source: c for c in crops}.items():
            stat = os.stat(src)
            if crop.file_size and (stat.st_size != crop.file_size or stat.st_mtime_ns != crop.file_mtime_ns):
                changed.append(os.path.basename(src))
        if changed:
            res = QtWidgets.QMessageBox.question(self, "Sources changed since crop was saved",
                "The following audio file(s) changed. Saved timestamps might no longer match:\n\n" +
                "\n".join(changed) + "\n\nContinue exporting using the current files?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            if res != QtWidgets.QMessageBox.Yes:
                return
        initial = self._default_save_name(joined)
        base_dir = self._starting_directory()
        preferred = SAVE_FILTERS[1] if initial.endswith(".m4a") else SAVE_FILTERS[0]
        path, filter_name = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export joined audio" if joined else "Save trimmed audio",
            os.path.join(base_dir, initial), ";;".join(SAVE_FILTERS), preferred)
        if not path:
            return
        if filter_name == SAVE_FILTERS[0]:
            codec, target, extension = "mp3", "mp3", ".mp3"
        elif filter_name == SAVE_FILTERS[2]:
            codec, target, extension = "alac", "mp4", ".m4a"
        else:
            codec, target, extension = "aac", "mp4", ".m4a"
        if not path.lower().endswith(extension):
            path = os.path.splitext(path)[0] + extension
        path = os.path.abspath(path)
        if any(os.path.normcase(os.path.abspath(c.source)) == os.path.normcase(path) for c in crops):
            QtWidgets.QMessageBox.warning(self, "Cannot overwrite source", "Choose a different output filename to protect your source audio.")
            return
        # One dialog per CONFLICTING attribute. Identical/missing attributes are
        # resolved automatically; repeated crops from the same file count once.
        sources = list(dict.fromkeys(c.source for c in crops))
        try:
            metadata = [extract_metadata(src) for src in sources]
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Metadata error", str(exc))
            return
        selected: Dict[str, Optional[Dict[str, Any]]] = {}
        for field in META_FIELDS:
            available = choose_field_sources(metadata, field)
            if len(available) > 1:
                dialog = MetadataChoiceDialog(field, available, self)
                if dialog.exec_() != QtWidgets.QDialog.Accepted:
                    return
                selected[field] = dialog.selected()
            else:
                selected[field] = available[0][0] if available else None
        clips = [copy.copy(c) for c in crops]
        # Generate next to the destination so final os.replace is atomic.
        try:
            temp_output = temporary_path(extension, directory=os.path.dirname(path))
        except OSError as exc:
            QtWidgets.QMessageBox.critical(self, "Cannot create output", str(exc))
            return
        def job():
            try:
                render_audio(clips, temp_output, codec)
                apply_export_metadata(temp_output, metadata, selected, target, clips)
                os.replace(temp_output, path)
                return path
            finally:
                safe_unlink(temp_output)
        def saved(saved_path):
            self.statusBar().showMessage("Saved: " + saved_path, 12000)
            QtWidgets.QMessageBox.information(self, "Export complete", "Saved:\n" + saved_path)
        self._run_task("Joining and exporting original audio…", job, saved)

    def closeEvent(self, event):
        if self._busy:
            QtWidgets.QMessageBox.information(self, "Operation in progress",
                "An audio operation is still in progress. Please close after it completes.")
            event.ignore()
            return
        self._save_session()
        self._closing = True
        self._stop_and_unload_media()
        self._destroy_joined_preview()
        self._preview_source_samples.clear()
        self._preview_segments.clear()
        QtWidgets.QApplication.instance().removeEventFilter(self)
        super().closeEvent(event)


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("AudioCropTrimmer")
    app.setOrganizationName("LocalAudioTools")
    win = MP3Trimmer()
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
