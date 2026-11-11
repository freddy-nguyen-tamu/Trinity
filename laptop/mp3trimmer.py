#!/usr/bin/env python3
"""Audio Trimmer & Crop Queue — based on the supplied mp3trimmer.py.

Requirements: Python 3.9+, PyQt5, numpy, mutagen, FFmpeg and FFprobe in PATH.
Install Python packages: pip install PyQt5 numpy mutagen

Crops store ONLY source-file paths and edit ranges; no cropped audio is saved.
The queue is auto-restored between launches. Source audio files must remain in place.
The temporary WAV used for joined preview is deleted when it is no longer needed.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
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
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"


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

    @classmethod
    def from_record(cls, value: Dict[str, Any]) -> "Crop":
        return cls(uid=str(value["uid"]), name=str(value["name"]),
                   source=str(value["source"]), start=float(value["start"]),
                   end=float(value["end"]), keep=bool(value["keep"]),
                   duration=float(value["duration"]),
                   file_size=int(value.get("file_size", 0)),
                   file_mtime_ns=int(value.get("file_mtime_ns", 0)))

    def parts(self) -> List[Tuple[float, float]]:
        """'Remove' keeps BOTH sides of the removed interval as one logical crop."""
        end = max(0.0, self.duration)
        a = max(0.0, min(self.start, end))
        b = max(a, min(self.end, end))
        choices = [(a, b)] if self.keep else [(0.0, a), (b, end)]
        return [(a, b) for a, b in choices if b - a >= 0.001]

    def effective_duration(self) -> float:
        return sum(b - a for a, b in self.parts())


def source_audio_profile(path: str) -> Tuple[int, int]:
    """Probe original rate/channels so export need not inherit waveform settings."""
    result = subprocess_ok(["ffprobe", "-v", "error", "-select_streams", "a:0",
                           "-show_entries", "stream=sample_rate,channels", "-of", "json", path])
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise ValueError("No usable audio stream in " + path)
    return int(streams[0].get("sample_rate") or SAMPLE_RATE), int(streams[0].get("channels") or 2)


def render_audio(crops: Sequence[Crop], path: str, codec: str) -> None:
    """Trim from original audio, not the 44.1-kHz mono waveform preview.

    Consistent source rates pass through unchanged; mixed sources are unified at
    the highest compatible rate for the selected codec. ALAC uses 32-bit PCM
    internally rather than throwing away high-resolution sample precision.
    """
    segments: List[Tuple[str, float, float]] = []
    for crop in crops:
        for a, b in crop.parts():
            segments.append((crop.source, a, b))
    if not segments:
        raise ValueError("The selected crop(s) contain no audio")
    sources = list(dict.fromkeys(src for src, _, _ in segments))
    if not all(os.path.isfile(x) for x in sources):
        missing = [x for x in sources if not os.path.isfile(x)]
        raise FileNotFoundError("Source file missing:\n" + "\n".join(missing))
    index = {path: i for i, path in enumerate(sources)}
    if codec == "preview":
        output_rate, layout, sample_format = SAMPLE_RATE, "stereo", "fltp"
    else:
        profiles = [source_audio_profile(src) for src in sources]
        rates = [rate for rate, channels in profiles]
        output_rate = max(rates)
        if codec == "mp3":
            output_rate = min((32000, 44100, 48000), key=lambda rate: abs(rate - min(output_rate, 48000)))
        elif codec == "aac":
            output_rate = min(output_rate, 96000)
        else:
            output_rate = min(output_rate, 192000)
        layout = "mono" if all(channels == 1 for rate, channels in profiles) else "stereo"
        sample_format = "s32p" if codec == "alac" else "fltp"
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for src in sources:
        args.extend(["-i", src])
    chains: List[str] = []
    for n, (src, start, stop) in enumerate(segments):
        chains.append(
            f"[{index[src]}:a:0]atrim=start={start:.8f}:end={stop:.8f},"
            f"asetpts=PTS-STARTPTS,aresample={output_rate},"
            f"aformat=sample_fmts={sample_format}:channel_layouts={layout}[s{n}]"
        )
    if len(segments) == 1:
        chains.append("[s0]anull[aout]")
    else:
        chains.append("".join(f"[s{i}]" for i in range(len(segments))) +
                      f"concat=n={len(segments)}:v=0:a=1[aout]")
    args += ["-filter_complex", ";".join(chains), "-map", "[aout]", "-map_metadata", "-1", "-vn"]
    if codec == "preview":
        args += ["-ac", "1", "-c:a", "pcm_s16le", "-f", "wav"]
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
    leftHandleMoved = QtCore.pyqtSignal(float)
    rightHandleMoved = QtCore.pyqtSignal(float)
    cursorClicked = QtCore.pyqtSignal(float)
    handleClicked = QtCore.pyqtSignal(str)
    handleFocusLost = QtCore.pyqtSignal()
    dragFinished = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setMinimumSize(200, 115)
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
        self.handle_extend, self.handle_width = 16, 28
        self.cursor_color = QtGui.QColor(255, 200, 0)

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
        self._rebuild_visible_peaks()
        self._emit_time_signals()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._rebuild_visible_peaks()
        self.update()

    def _rebuild_visible_peaks(self):
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

    def mousePressEvent(self, event):
        if event.button() != QtCore.Qt.LeftButton or self.duration <= 0:
            return
        x = event.x()
        self.setFocus()
        lx = round(self.left_frac * (self.width() - 1))
        rx = round(self.right_frac * (self.width() - 1))
        if self.editable:
            # Prefer a selected handle when two handles overlap.
            choices = [(abs(x - lx), "left"), (abs(x - rx), "right")]
            closest = min(choices, key=lambda entry: entry[0])
            if closest[0] <= self.handle_width // 2:
                self.dragging = closest[1]
                self.setCursor(QtCore.Qt.SizeHorCursor)
                self.handleClicked.emit(self.dragging)
                return
        self.handleFocusLost.emit()
        self.set_cursor_time(x / max(1, self.width() - 1) * self.duration, notify=True)

    def mouseMoveEvent(self, event):
        if self.dragging is None or not self.editable:
            return
        seconds = max(0, min(event.x() / max(1, self.width() - 1), 1)) * self.duration
        if self.dragging == "left":
            self.set_left_time(seconds)
        else:
            self.set_right_time(seconds)

    def mouseReleaseEvent(self, event):
        if self.dragging is not None:
            previous = self.dragging
            self.dragging = None
            self.dragFinished.emit(previous)
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
        self._preview_playback_path: Optional[str] = None
        self._pending_play = False
        self._pending_seek = 0
        self._playback_map: Optional[Dict[str, Any]] = None
        self._busy = False
        self._worker: Optional[TaskThread] = None
        self.crops: List[Crop] = []
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
        self.open_button = self._button("Open audio", self.open_file, top)
        self.btn_keep = self._button("Keep", lambda: self.set_mode(True), top)
        self.btn_remove = self._button("Remove", lambda: self.set_mode(False), top)
        self.btn_keep.setCheckable(True)
        self.btn_remove.setCheckable(True)
        self.btn_keep.setChecked(True)
        self.btn_keep.setStyleSheet("QPushButton{background:#1b415f;color:white;border:none;border-radius:5px;padding:7px 12px;font-weight:600;} QPushButton:checked{background:#2f7847;}")
        self.btn_remove.setStyleSheet("QPushButton{background:#1b415f;color:white;border:none;border-radius:5px;padding:7px 12px;font-weight:600;} QPushButton:checked{background:#974347;}")
        self.reset_button = self._button("Reset", self.reset_trim, top)
        top.addStretch()
        self.save_button = self._button("Save current", self.save_file, top)
        self.add_button = self._button("+ Add crop", self.add_crop, top)
        self.update_button = self._button("Update crop", self.update_crop, top)
        self.sidebar_button = self._button("Hide crops ◀", self._toggle_sidebar, top)
        main.addLayout(top)

        self.file_label = QtWidgets.QLabel("No file loaded")
        self.file_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.file_label.setStyleSheet("color:#a9c1d5;padding:2px;")
        main.addWidget(self.file_label)

        self.waveform = WaveformWidget()
        self.waveform.leftHandleMoved.connect(lambda v: self.edit_left.setText(format_time(v)))
        self.waveform.rightHandleMoved.connect(lambda v: self.edit_right.setText(format_time(v)))
        self.waveform.cursorClicked.connect(self.on_waveform_clicked)
        self.waveform.handleClicked.connect(self.on_handle_focused)
        self.waveform.handleFocusLost.connect(self.on_handle_focus_lost)
        self.waveform.dragFinished.connect(self.on_handle_drag_finished)
        margins = QtWidgets.QHBoxLayout()
        margins.setContentsMargins(30, 0, 30, 0)
        margins.addWidget(self.waveform)
        main.addLayout(margins, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.btn_play = self._button("▶", self.toggle_play, bottom)
        self.btn_stop = self._button("■", self.stop, bottom)
        self.btn_play.setFixedSize(45, 43)
        self.btn_stop.setFixedSize(45, 43)
        bottom.addSpacing(8)
        bottom.addWidget(QtWidgets.QLabel("Start"))
        self.edit_left = QtWidgets.QLineEdit("00:00.00")
        self.edit_left.setFixedWidth(92)
        self.edit_left.setAlignment(QtCore.Qt.AlignCenter)
        self.edit_left.editingFinished.connect(self.on_left_time_edited)
        bottom.addWidget(self.edit_left)
        bottom.addWidget(QtWidgets.QLabel("End"))
        self.edit_right = QtWidgets.QLineEdit("00:00.00")
        self.edit_right.setFixedWidth(92)
        self.edit_right.setAlignment(QtCore.Qt.AlignCenter)
        self.edit_right.editingFinished.connect(self.on_right_time_edited)
        bottom.addWidget(self.edit_right)
        bottom.addStretch()
        self.mode_label = QtWidgets.QLabel("Select a section; Space plays or pauses")
        bottom.addWidget(self.mode_label)
        main.addLayout(bottom)
        self.statusBar().showMessage("Crops are virtual: no cropped files are written until export")
        self._init_sidebar()
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
        self.dock.setFeatures(QtWidgets.QDockWidget.DockWidgetClosable)
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
        self.sidebar_button.setText("Hide crops ◀" if visible else "Show crops ▶")
        if self.isVisible() and not self._closing:
            self._save_session()

    def _toggle_sidebar(self):
        self.dock.setVisible(not self.dock.isVisible())

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
            saved = {"version": 2, "next_index": self.next_index,
                     "sidebar_visible": self.dock.isVisible(),
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
            if data.get("version") != 2:
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

    def _rebuild_crop_list(self, selected_id: Optional[str] = None):
        self._restoring_list = True
        try:
            self.crop_list.clear()
            for crop in self.crops:
                action = "KEEP" if crop.keep else "REMOVE"
                description = f"{crop.name} · {format_time(crop.effective_duration())}\n"
                description += f"{os.path.basename(crop.source)}  [{action} {format_time(crop.start)}–{format_time(crop.end)}]"
                item = QtWidgets.QListWidgetItem(description)
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

    def _current_crop(self, uid: Optional[str] = None, name: str = "") -> Crop:
        if self.audio_samples is None or not self.original_file or self.preview_mode:
            raise ValueError("Open a source audio file and select a crop first")
        stat = os.stat(self.original_file)
        return Crop(uid or str(uuid.uuid4()), name,
                    self.original_file, self.waveform.left_frac * self.duration,
                    self.waveform.right_frac * self.duration,
                    self.btn_keep.isChecked(), self.duration, stat.st_size, stat.st_mtime_ns)

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
            self.dock.show()
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
        for btn in (self.btn_keep, self.btn_remove, self.reset_button, self.add_button,
                    self.edit_left, self.edit_right):
            btn.setEnabled(enabled)
        self.save_button.setEnabled(enabled)
        self.update_button.setEnabled(enabled and bool(self.editing_crop_id))
        self.btn_preview_queue.setEnabled(bool(self.crops) and not self.preview_mode)
        self.btn_return.setVisible(self.preview_mode)
        self.btn_export.setEnabled(bool(self.crops))
        self.btn_del_crop.setEnabled(bool(self.crops))
        self.btn_clear.setEnabled(bool(self.crops))
        if self.preview_mode:
            self.mode_label.setText("Joined preview · read-only · double-click a crop to edit")
        else:
            self.mode_label.setText("Space: play/pause · ←/→: move selected pink handle 0.05s")

    def _capture_editor_state(self):
        return dict(path=self.original_file, samples=self.audio_samples,
                    crop_id=self.editing_crop_id, duration=self.duration,
                    left=self.waveform.left_frac * self.duration,
                    right=self.waveform.right_frac * self.duration,
                    cursor=self.waveform.cursor_frac * self.duration,
                    keep=self.btn_keep.isChecked())

    def preview_joined(self):
        if self._busy or not self.crops or self.preview_mode:
            return
        missing = [c.source for c in self.crops if not os.path.isfile(c.source)]
        if missing:
            QtWidgets.QMessageBox.warning(self, "Missing source files", "\n".join(dict.fromkeys(missing)))
            return
        snapshot = self._capture_editor_state()
        clips = [copy.copy(c) for c in self.crops]
        outpath = temporary_path(".wav")
        def job():
            try:
                render_audio(clips, outpath, "preview")
                return decode_mono_samples(outpath)
            except Exception:
                safe_unlink(outpath)
                raise
        def completed(samples):
            self._stop_and_unload_media()
            self._destroy_joined_preview()
            self._editor_before_preview = snapshot
            self._joined_preview_path = outpath
            self.preview_mode = True
            self.audio_samples = samples
            self.duration = len(samples) / SAMPLE_RATE
            self.waveform.set_audio_data(samples, self.duration, editable=False)
            self.waveform.set_cursor_time(0)
            self.file_label.setText(f"Joined preview · {len(clips)} crops · {format_time(self.duration)}")
            self.file_label.setToolTip("Temporary PCM preview only; exports use the original source audio")
            self._refresh_edit_controls()
            self.statusBar().showMessage("Read-only joined preview ready (Space to play)", 5000)
        self._run_task("Preparing temporary joined preview…", job, completed)

    def exit_preview(self):
        if not self.preview_mode or self._busy:
            return
        self._stop_and_unload_media()
        self._destroy_joined_preview()
        state = self._editor_before_preview
        self._editor_before_preview = None
        self.preview_mode = False
        if state and state["samples"] is not None:
            crop = self._find_crop(state["crop_id"])
            self._set_editor_audio(state["path"], state["samples"], crop)
            self.set_mode(state["keep"])
            self.waveform.set_region(state["left"], state["right"])
            self.waveform.set_cursor_time(state["cursor"])
        else:
            self.audio_samples = None
            self.duration = 0.0
            self.original_file = ""
            self.editing_crop_id = None
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
            if isinstance(self.focusWidget(), (QtWidgets.QLineEdit, QtWidgets.QTextEdit,
                                               QtWidgets.QPlainTextEdit)):
                return False
            if event.key() == QtCore.Qt.Key_Space and not event.isAutoRepeat():
                self.toggle_play()
                return True
            if event.key() == QtCore.Qt.Key_Escape and self.preview_mode:
                self.exit_preview()
                return True
            if (event.key() in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right) and
                    self.active_handle and self.waveform.hasFocus() and not self.preview_mode):
                delta = -self.FINE_STEP if event.key() == QtCore.Qt.Key_Left else self.FINE_STEP
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
            path = self._joined_preview_path
            if path:
                self._begin_media(path, round(self.waveform.cursor_frac * self.duration * 1000),
                                  {"mode": "joined"})
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
            self.media_player.play()
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
