import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

from mutagen import File as MutagenFile
from mutagen.id3 import ID3


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FINISHED_DIR = os.path.join(BASE_DIR, "finished")


def tag_key_looks_like_lyrics(value):
    value = str(value or "").casefold()
    return "lyric" in value or value in {"uslt", "sylt"}


def id3_frame_text(frame):
    text = getattr(frame, "text", "")

    if isinstance(text, list):
        pieces = []
        for item in text:
            if isinstance(item, tuple) and item:
                pieces.append(str(item[0]))
            else:
                pieces.append(str(item))
        return "\n".join(piece for piece in pieces if piece).strip()

    return str(text or "").strip()


def read_id3_lyrics(path):
    try:
        tags = ID3(path)
    except Exception:
        return []

    found = []

    for frame_id in ("USLT", "SYLT"):
        for frame in tags.getall(frame_id):
            text = id3_frame_text(frame)
            if text:
                found.append((frame_id, text))

    for frame in tags.getall("TXXX"):
        desc = str(getattr(frame, "desc", "") or "")
        if tag_key_looks_like_lyrics(desc):
            text = id3_frame_text(frame)
            if text:
                found.append((f"TXXX:{desc or 'lyrics'}", text))

    for frame in tags.getall("COMM"):
        desc = str(getattr(frame, "desc", "") or "")
        if tag_key_looks_like_lyrics(desc):
            text = id3_frame_text(frame)
            if text:
                found.append((f"COMM:{desc or 'lyrics'}", text))

    return found


def generic_tag_text(value):
    if value is None:
        return ""

    if isinstance(value, list):
        parts = [generic_tag_text(item) for item in value]
        return "\n".join(part for part in parts if part).strip()

    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()

    return str(value).strip()


def read_generic_lyrics(path):
    try:
        audio = MutagenFile(path)
    except Exception:
        return []

    if not audio or not audio.tags:
        return []

    found = []
    for key, value in audio.tags.items():
        if tag_key_looks_like_lyrics(key):
            text = generic_tag_text(value)
            if text:
                found.append((str(key), text))

    return found


def read_lyrics(path):
    ext = os.path.splitext(path)[1].casefold()
    if ext == ".mp3":
        found = read_id3_lyrics(path)
        if found:
            return found

    return read_generic_lyrics(path)


def build_display_text(path, lyrics_items):
    lines = [
        f"File: {path}",
        "",
    ]

    if not lyrics_items:
        lines.append("No lyrics attribute was found.")
        return "\n".join(lines)

    for index, (source, text) in enumerate(lyrics_items, start=1):
        if index > 1:
            lines.append("")
            lines.append("=" * 80)
            lines.append("")
        lines.append(f"Lyrics source: {source}")
        lines.append("")
        lines.append(text)

    return "\n".join(lines)


def show_lyrics_window(root, path, display_text):
    window = tk.Toplevel(root)
    window.title(f"Lyrics - {os.path.basename(path)}")
    window.geometry("900x700")

    text_box = scrolledtext.ScrolledText(window, wrap=tk.WORD, font=("Consolas", 11))
    text_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 6))
    text_box.insert("1.0", display_text)
    text_box.configure(state=tk.DISABLED)

    button_row = tk.Frame(window)
    button_row.pack(fill=tk.X, padx=10, pady=(0, 10))

    def copy_to_clipboard():
        window.clipboard_clear()
        window.clipboard_append(display_text)
        window.update()

    tk.Button(button_row, text="Copy", command=copy_to_clipboard).pack(side=tk.LEFT)
    tk.Button(button_row, text="Close", command=window.destroy).pack(side=tk.RIGHT)


def choose_file():
    initial_dir = FINISHED_DIR if os.path.isdir(FINISHED_DIR) else BASE_DIR
    return filedialog.askopenfilename(
        title="Choose an audio file from finished/",
        initialdir=initial_dir,
        filetypes=[
            ("Audio files", "*.mp3 *.m4a *.mp4 *.flac *.ogg *.opus *.wav *.aiff"),
            ("MP3 files", "*.mp3"),
            ("All files", "*.*"),
        ],
    )


def main():
    root = tk.Tk()
    root.withdraw()

    path = choose_file()
    if not path:
        return 0

    try:
        lyrics_items = read_lyrics(path)
    except Exception as error:
        messagebox.showerror("Lyrics Viewer", f"Could not read lyrics:\n{error}")
        return 1

    display_text = build_display_text(path, lyrics_items)
    print(display_text)
    show_lyrics_window(root, path, display_text)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
