"""Compact, dependency-free desktop launcher for Trinity's existing pipeline."""

from __future__ import annotations

from collections import deque
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
import threading
import traceback


# A pinned EXE may run from laptop/, laptop/dist/, or another location.
# Preferences must not depend on the executable's directory or PyInstaller's
# temporary one-file extraction location.
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def settings_path():
    """User-owned, writable, stable location across upgrades and taskbar pins."""
    if os.name == "nt":
        home = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(home) if home else Path.home() / "AppData" / "Local"
    else:
        home = os.environ.get("XDG_CONFIG_HOME")
        root = Path(home) if home else Path.home() / ".config"
    return root / "Trinity" / "ui_settings.json"


SETTINGS_FILE = settings_path()
DEFAULTS = {
    "should_upload": True,
    "skip_playlist_on_30": True,
    "lazy_playlist": False,
    "finished_only": False,
}


def _read_options(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        contents = json.load(handle)
    if not isinstance(contents, dict):
        raise ValueError("Preferences must be an object")
    options = DEFAULTS.copy()
    for key in options:
        if isinstance(contents.get(key), bool):
            options[key] = contents[key]
    if options["finished_only"]:
        options["should_upload"] = True
    return options


def load_options():
    """Read persistent settings; migrate the old next-to-EXE settings once."""
    if SETTINGS_FILE.is_file():
        try:
            return _read_options(SETTINGS_FILE)
        except (OSError, ValueError, TypeError):
            pass

    # Old Trinity builds saved relative to their executable, so look in both
    # laptop/ and laptop/dist/ when upgrading a pinned or freshly built EXE.
    for parent in (APP_DIR, *list(APP_DIR.parents)[:4]):
        candidate = parent / "_trinity_ui_settings.json"
        if candidate == SETTINGS_FILE or not candidate.is_file():
            continue
        try:
            previous = _read_options(candidate)
        except (OSError, ValueError, TypeError):
            continue
        try:
            save_options(previous)
        except OSError:
            # Keep the existing answers in this launch; a later toggle will
            # explicitly report any storage problem to the user.
            pass
        return previous
    return DEFAULTS.copy()


def save_options(options):
    """Atomically save all four choices in the user's persistent profile."""
    clean = {key: bool(options.get(key, default)) for key, default in DEFAULTS.items()}
    if clean["finished_only"]:
        clean["should_upload"] = True
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(clean, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, SETTINGS_FILE)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _project_dir():
    """Find backend alongside the source or above a one-folder UI build."""
    # PyInstaller onedir: laptop/dist/Trinity/Trinity.exe. The backend remains
    # in laptop/ and is not packaged into the UI (nor copied or imported).
    for directory in (APP_DIR, *list(APP_DIR.parents)[:4]):
        if (directory / "trinity.py").is_file():
            return directory
    raise FileNotFoundError(
        "Could not locate laptop/trinity.py near the Trinity launcher."
    )


def _python_command():
    """Resolve an installed Python without slowing down the window launch."""
    if not getattr(sys, "frozen", False):
        return [sys.executable]
    explicit = os.environ.get("TRINITY_PYTHON_EXE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"TRINITY_PYTHON_EXE not found: {path}")
        return [str(path)]
    if os.name == "nt":
        launcher = shutil.which("py")
        if launcher:
            return [launcher, "-3"]
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found and "WindowsApps" not in found:
            return [found]
    raise FileNotFoundError("Python is required for Trinity's companion scripts.")


def run_backend(*, on_output=None, **options):
    """Stream original Python output in real time without blocking the UI."""
    project = _project_dir()
    # The existing --headless entry uses the settings saved before START.
    command = [*_python_command(), "-u", str(project / "trinity.py"), "--headless"]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=project,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=flags,
    )
    try:
        if process.stdout:
            # TextIOWrapper normalizes carriage-return progress updates. This
            # also captures everything child processes print via trinity.py.
            for chunk in process.stdout:
                if on_output is not None:
                    on_output(chunk)
                elif sys.stdout is not None:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
    finally:
        if process.stdout:
            process.stdout.close()
    code = process.wait()
    if code:
        raise RuntimeError(f"Trinity backend stopped with exit code {code}")


_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MAX_VISIBLE = 150_000
_TRIM_AT = 200_000
_MAX_QUEUED = 240_000
_MAX_BATCH = 36_000
_TERMINAL_FILE = SETTINGS_FILE.with_name("terminal_output.log")


def _put_clipboard(root, text):
    """Copy Unicode faithfully, including very long logs on Windows."""
    if os.name != "nt":
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update_idletasks()
        return
    # Tcl/Tk's clipboard has had chunk-boundary Unicode issues with large
    # texts. Use Windows CF_UNICODETEXT directly instead of ANSI code pages.
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = ctypes.c_void_p
    kernel.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel.GlobalFree.argtypes = [ctypes.c_void_p]
    user.OpenClipboard.argtypes = [ctypes.c_void_p]
    user.OpenClipboard.restype = wintypes.BOOL
    user.EmptyClipboard.restype = wintypes.BOOL
    user.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user.SetClipboardData.restype = ctypes.c_void_p
    user.CloseClipboard.restype = wintypes.BOOL

    encoded = text.encode("utf-16-le") + b"\x00\x00"
    # GMEM_MOVEABLE, required by SetClipboardData.
    handle = kernel.GlobalAlloc(0x0002, len(encoded))
    if not handle:
        raise OSError("Could not allocate Windows clipboard memory")
    try:
        address = kernel.GlobalLock(handle)
        if not address:
            raise OSError("Could not lock Windows clipboard memory")
        try:
            ctypes.memmove(address, encoded, len(encoded))
        finally:
            kernel.GlobalUnlock(handle)

        for attempt in range(8):
            if user.OpenClipboard(ctypes.c_void_p(root.winfo_id())):
                break
            time.sleep(.025)
        else:
            raise OSError("Windows clipboard is in use")
        try:
            if not user.EmptyClipboard():
                raise OSError("Could not clear Windows clipboard")
            if not user.SetClipboardData(13, handle):  # CF_UNICODETEXT
                raise OSError("Could not set Windows clipboard contents")
            handle = None  # Windows owns the memory on success.
        finally:
            user.CloseClipboard()
    finally:
        if handle:
            kernel.GlobalFree(handle)


def _load_recent_terminal(path, max_bytes=70_000):
    """Loading an old, very large log must never slow down the window."""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            recent = handle.read().decode("utf-8", errors="replace")
            if size > max_bytes:
                recent = recent[recent.find("\n") + 1:] if "\n" in recent else recent
            return recent
    except OSError:
        return ""


def launch_ui(run_pipeline):
    """Fast, minimalist UI. The backend and stdout reader never touch Tk."""
    import tkinter as tk
    from tkinter import messagebox

    bg = "#f7f7f5"
    ink = "#111111"
    line = "#cececa"
    paper = "#ffffff"

    class TrinityWindow:
        def __init__(self):
            self.root = tk.Tk()
            self.root.title("Trinity")
            self.root.configure(bg=bg)
            self.root.geometry("690x675")
            self.root.minsize(390, 440)
            self.running = False
            self.closed = False
            self.values = {
                key: tk.BooleanVar(master=self.root, value=value)
                for key, value in load_options().items()
            }
            self.indicators = {}
            self._queue_lock = threading.Lock()
            self._queued = deque()
            self._queued_chars = 0
            self._dropped = False
            self._writer_lock = threading.Lock()
            self._writer = None
            self._writer_flush = 0.0
            self._worker_finished = False
            self._worker_failure = None
            self._display_chars = 0

            icon_image = tk.PhotoImage(width=32, height=32, master=self.root)
            icon_image.put(bg, to=(0, 0, 32, 32))
            for left, top in ((5, 11), (13, 5), (21, 9)):
                icon_image.put(ink, to=(left, top, left + 6, 27))
            self.root.iconphoto(True, icon_image)
            self.native_icon = icon_image

            wrapper = tk.Frame(self.root, bg=bg)
            wrapper.pack(fill="both", expand=True, padx=25, pady=(22, 20))

            header = tk.Frame(wrapper, bg=bg)
            header.pack(fill="x", pady=(0, 15))
            mark = tk.Canvas(header, width=33, height=36, bg=bg, bd=0,
                             highlightthickness=0)
            mark.pack(side="left", padx=(0, 12))
            for left, top in ((4, 12), (13, 5), (22, 9)):
                mark.create_rectangle(left, top, left + 6, 31, fill=ink, outline=ink)
            tk.Label(header, text="Trinity", bg=bg, fg=ink,
                     font=("Segoe UI", 26, "bold"), anchor="w").pack(side="left")

            choices = tk.Frame(wrapper, bg=bg)
            choices.pack(fill="x")
            for key, caption in (
                ("should_upload", "Phone + Drive"),
                ("skip_playlist_on_30", "Stop after 30 skips"),
                ("lazy_playlist", "Stream playlist"),
                ("finished_only", "Finished only"),
            ):
                self.make_option(choices, key, caption, bg, ink, line, tk)

            self.start_button = tk.Button(
                wrapper, text="START", bg=ink, fg=paper,
                activebackground="#343434", activeforeground=paper,
                disabledforeground="#dddddd", borderwidth=0, relief="flat",
                font=("Segoe UI", 11, "bold"), cursor="hand2",
                command=self.start, takefocus=True,
            )
            self.start_button.pack(fill="x", ipady=9, pady=(15, 16))

            console = tk.Frame(wrapper, bg=ink, highlightthickness=0, bd=0)
            console.pack(fill="both", expand=True)
            toolbar = tk.Frame(console, bg=ink, height=32)
            toolbar.pack(fill="x", padx=9, pady=(6, 0))
            toolbar.pack_propagate(False)

            # Two offset rectangles, not a decorative image. One click copies
            # the *complete* run log, including lines trimmed from the view.
            copy_icon = tk.Canvas(toolbar, width=28, height=26, bg=ink, bd=0,
                                  highlightthickness=0, cursor="hand2",
                                  takefocus=1)
            copy_icon.pack(side="right")
            copy_icon.create_rectangle(5, 7, 18, 22, outline=paper, width=1.5)
            copy_icon.create_rectangle(10, 3, 23, 18, outline=paper,
                                       fill=ink, width=1.5)
            copy_icon.bind("<Button-1>", self.copy_all)
            copy_icon.bind("<Return>", self.copy_all)
            copy_icon.bind("<space>", self.copy_all)
            self.copy_icon = copy_icon

            output_frame = tk.Frame(console, bg=ink)
            output_frame.pack(fill="both", expand=True, padx=(11, 6), pady=(1, 12))
            scrollbar = tk.Scrollbar(output_frame, orient="vertical", width=11,
                                     troughcolor=ink, bg="#626262",
                                     activebackground="#999999", relief="flat")
            scrollbar.pack(side="right", fill="y")
            self.terminal = tk.Text(
                output_frame, bg=ink, fg="#f1f1f0", insertbackground=paper,
                selectbackground="#555555", selectforeground=paper,
                borderwidth=0, highlightthickness=0, relief="flat",
                font=("Consolas", 10), wrap="char", padx=0, pady=0,
                yscrollcommand=scrollbar.set, state="disabled",
                undo=False, takefocus=True,
            )
            self.terminal.pack(side="left", fill="both", expand=True)
            scrollbar.config(command=self.terminal.yview)
            self.terminal.bind("<Control-c>", self.copy_selection)
            self.terminal.bind("<Control-a>", self.select_visible)

            self.append_visible(_load_recent_terminal(_TERMINAL_FILE))
            self.redraw_all()
            self.root.protocol("WM_DELETE_WINDOW", self.close)
            self.root.after(60, self.drain_output)

        def make_option(self, parent, key, caption, bg, ink, line, tk):
            row = tk.Frame(parent, bg=bg, height=39, cursor="hand2", takefocus=True)
            row.pack(fill="x")
            row.pack_propagate(False)
            box = tk.Canvas(row, width=21, height=21, bg=bg,
                            borderwidth=0, highlightthickness=0, cursor="hand2")
            box.pack(side="left", padx=(1, 13))
            tk.Label(row, text=caption, bg=bg, fg=ink,
                     font=("Segoe UI", 11), anchor="w", cursor="hand2").pack(
                side="left", fill="x", expand=True)
            self.indicators[key] = box
            for child in row.winfo_children() + [row]:
                child.bind("<Button-1>", lambda event, k=key: self.toggle(k))
            row.bind("<space>", lambda event, k=key: self.toggle(k))
            row.bind("<Return>", lambda event, k=key: self.toggle(k))
            tk.Frame(parent, bg=line, height=1).pack(fill="x")

        def toggle(self, key):
            if self.running:
                return
            active = not self.values[key].get()
            self.values[key].set(active)
            if key == "finished_only" and active:
                self.values["should_upload"].set(True)
            elif key == "should_upload" and not active:
                self.values["finished_only"].set(False)
            self.redraw_all()
            self.persist()

        def redraw_all(self):
            for key, canvas in self.indicators.items():
                canvas.delete("all")
                enabled = self.values[key].get()
                canvas.create_rectangle(2, 2, 19, 19, width=1.5,
                                        outline=ink, fill=ink if enabled else bg)
                if enabled:
                    canvas.create_line(6, 10, 9, 14, 16, 7, fill=paper,
                                       width=2, capstyle="round", joinstyle="round")

        def current_options(self):
            return {key: value.get() for key, value in self.values.items()}

        def persist(self):
            try:
                save_options(self.current_options())
                return True
            except OSError as error:
                messagebox.showerror("Trinity", f"Could not save preferences:\n{error}",
                                     parent=self.root)
                return False

        def start(self):
            if self.running or not self.persist():
                return
            try:
                _TERMINAL_FILE.parent.mkdir(parents=True, exist_ok=True)
                writer = _TERMINAL_FILE.open("w", encoding="utf-8",
                                             errors="replace", buffering=32768)
            except OSError as error:
                messagebox.showerror("Trinity", f"Could not open terminal log:\n{error}",
                                     parent=self.root)
                return
            with self._writer_lock:
                self._writer = writer
                self._writer_flush = time.monotonic()
            with self._queue_lock:
                self._queued.clear()
                self._queued_chars = 0
                self._dropped = False
            self.terminal.configure(state="normal")
            self.terminal.delete("1.0", "end")
            self.terminal.configure(state="disabled")
            self._display_chars = 0
            self._worker_finished = False
            self._worker_failure = None
            self.running = True
            self.start_button.configure(text="RUNNING", state="disabled")
            threading.Thread(target=self.worker, args=(self.current_options(),),
                             daemon=False, name="TrinityBackend").start()

        def push_output(self, chunk):
            """Called by the worker: never touches any Tk widget."""
            # Normalize terminal control codes for readable copy/paste and
            # prevent CR-based yt-dlp progress from overwriting earlier lines.
            chunk = _ANSI.sub("", str(chunk)).replace("\r\n", "\n").replace("\r", "\n")
            if not chunk:
                return
            with self._writer_lock:
                if self._writer is not None:
                    self._writer.write(chunk)
                    now = time.monotonic()
                    if now - self._writer_flush >= .2:
                        self._writer.flush()
                        self._writer_flush = now
            with self._queue_lock:
                self._queued.append(chunk)
                self._queued_chars += len(chunk)
                if self._queued_chars > _MAX_QUEUED:
                    self._dropped = True
                    while self._queued_chars > _MAX_QUEUED // 2 and self._queued:
                        removed = self._queued.popleft()
                        self._queued_chars -= len(removed)

        def worker(self, options):
            failure = None
            try:
                run_pipeline(on_output=self.push_output, **options)
            except BaseException as error:
                if not (isinstance(error, SystemExit) and error.code in (None, 0)):
                    failure = error
                    self.push_output("".join(traceback.format_exception(
                        type(error), error, error.__traceback__)))
            finally:
                with self._writer_lock:
                    if self._writer is not None:
                        try:
                            self._writer.flush()
                        finally:
                            self._writer.close()
                            self._writer = None
                with self._queue_lock:
                    self._worker_failure = failure
                    self._worker_finished = True

        def drain_output(self):
            if self.closed:
                return
            chunks = []
            size = 0
            with self._queue_lock:
                dropped = self._dropped
                self._dropped = False
                while self._queued and size < _MAX_BATCH:
                    chunk = self._queued.popleft()
                    self._queued_chars -= len(chunk)
                    chunks.append(chunk)
                    size += len(chunk)
                finished = self._worker_finished and not self._queued
                failure = self._worker_failure
            if dropped:
                self.append_visible("\n[Earlier output omitted from view; Copy All includes it.]\n")
            if chunks:
                self.append_visible("".join(chunks))
            if finished and self.running:
                self.running = False
                self.start_button.configure(text="DONE" if failure is None else "RETRY",
                                            state="normal")
            self.root.after(70, self.drain_output)

        def append_visible(self, value):
            if not value:
                return
            was_bottom = self.terminal.yview()[1] >= .98
            self.terminal.configure(state="normal")
            self.terminal.insert("end", value)
            self._display_chars += len(value)
            if self._display_chars > _TRIM_AT:
                cut = self._display_chars - _MAX_VISIBLE
                self.terminal.delete("1.0", f"1.0+{cut}c")
                self._display_chars = _MAX_VISIBLE
            self.terminal.configure(state="disabled")
            if was_bottom:
                self.terminal.see("end")

        def copy_all(self, event=None):
            try:
                with self._writer_lock:
                    if self._writer is not None:
                        self._writer.flush()
                # The full text lives on disk; never depend on trimmed Tk text.
                text = (_TERMINAL_FILE.read_text(encoding="utf-8", errors="replace")
                        if _TERMINAL_FILE.is_file() else "")
            except OSError as error:
                messagebox.showerror("Trinity", f"Could not copy terminal:\n{error}",
                                     parent=self.root)
                return "break"
            try:
                _put_clipboard(self.root, text)
            except OSError as error:
                messagebox.showerror("Trinity", f"Could not copy terminal:\n{error}",
                                     parent=self.root)
            return "break"

        def copy_selection(self, event=None):
            try:
                selection = self.terminal.get("sel.first", "sel.last")
            except tk.TclError:
                return self.copy_all()
            try:
                _put_clipboard(self.root, selection)
            except OSError as error:
                messagebox.showerror("Trinity", f"Could not copy selection:\n{error}",
                                     parent=self.root)
            return "break"

        def select_visible(self, event=None):
            self.terminal.tag_add("sel", "1.0", "end-1c")
            return "break"

        def close(self):
            if not self.running and not self.persist():
                return
            # Like the previous GUI, closing the window does not terminate an
            # upload in progress. The worker keeps draining the child pipe.
            self.closed = True
            self.root.destroy()

    TrinityWindow().root.mainloop()


if __name__ == "__main__":
    launch_ui(run_backend)
