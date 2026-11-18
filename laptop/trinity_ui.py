
"""Compact, dependency-free desktop launcher for Trinity's existing pipeline."""

from __future__ import annotations

import json
import codecs
import signal
import uuid
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


def _app_icon_path():
    """Locate the real Trinity icon in source and packaged UI builds.

    PyInstaller 6 stores one-folder bundled data under _internal (sys._MEIPASS).
    Prefer it, but also support unbundled Python and older build layouts.
    """
    candidates = [Path(getattr(sys, "_MEIPASS", APP_DIR)), APP_DIR]
    candidates.extend(list(APP_DIR.parents)[:4])
    for folder in candidates:
        icon = folder / "trinity.ico"
        if icon.is_file():
            return icon
    return None


def _set_windows_window_icon(window, icon_path):
    """Use the original multi-size Windows icon for both large and small UI.

    Tk's generated 32x32 PhotoImage used to override the real EXE icon and
    looked blurry in the title bar/taskbar. Load native HICONs at the monitor's
    system icon sizes instead, preserving high-DPI ICO frames when available.
    The original Trinity executable's resource is the fallback if the asset
    is not separately present. No website favicon is used.
    """
    if os.name != "nt":
        return ()
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                      wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT]
        user32.LoadImageW.restype = wintypes.HANDLE

        window.update_idletasks()
        tk_handle = window.winfo_id()
        # Tk has a native wrapper HWND around its client HWND.
        hwnd = user32.GetParent(tk_handle) or tk_handle
        dpi = 96
        if hasattr(user32, "GetDpiForWindow"):
            user32.GetDpiForWindow.argtypes = [wintypes.HWND]
            user32.GetDpiForWindow.restype = wintypes.UINT
            dpi = user32.GetDpiForWindow(hwnd) or dpi

        def metrics(x, y):
            if hasattr(user32, "GetSystemMetricsForDpi"):
                user32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, wintypes.UINT]
                user32.GetSystemMetricsForDpi.restype = ctypes.c_int
                return (max(1, user32.GetSystemMetricsForDpi(x, dpi)),
                        max(1, user32.GetSystemMetricsForDpi(y, dpi)))
            user32.GetSystemMetrics.argtypes = [ctypes.c_int]
            user32.GetSystemMetrics.restype = ctypes.c_int
            return (max(1, user32.GetSystemMetrics(x)),
                    max(1, user32.GetSystemMetrics(y)))

        icons = []
        if icon_path is not None:
            # Give any Tk child dialogs the same real icon, never the
            # 32x32, hand-rendered three-bar placeholder.
            try:
                window.iconbitmap(default=str(icon_path))
            except Exception:
                pass
            for dimensions in (metrics(11, 12), metrics(49, 50)):
                handle = user32.LoadImageW(
                    None, str(icon_path), 1, *dimensions, 0x0010  # LR_LOADFROMFILE
                )
                icons.append(int(handle) if handle else 0)
        else:
            icons = [0, 0]

        if not all(icons) and getattr(sys, "frozen", False):
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                               ctypes.POINTER(wintypes.HANDLE),
                                               ctypes.POINTER(wintypes.HANDLE),
                                               wintypes.UINT]
            shell32.ExtractIconExW.restype = wintypes.UINT
            user32.DestroyIcon.argtypes = [wintypes.HANDLE]
            user32.DestroyIcon.restype = wintypes.BOOL
            large, small = wintypes.HANDLE(), wintypes.HANDLE()
            if shell32.ExtractIconExW(sys.executable, 0,
                                      ctypes.byref(large), ctypes.byref(small), 1):
                for index, extracted in enumerate((large.value, small.value)):
                    if not icons[index] and extracted:
                        icons[index] = int(extracted)
                    elif extracted:
                        user32.DestroyIcon(extracted)

        # ICON_BIG=1, ICON_SMALL=0. Win32 uses the two source resolutions
        # directly rather than stretching a Python-generated 32px bitmap.
        for index, icon_type in ((0, 1), (1, 0)):
            if icons[index]:
                user32.SendMessageW(hwnd, 0x0080, icon_type, icons[index])
        return tuple(icon for icon in icons if icon)
    except Exception:
        # Icon setup must never prevent a functional Trinity UI from opening.
        return ()


def _release_windows_icons(handles):
    if os.name != "nt" or not handles:
        return
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.DestroyIcon.argtypes = [ctypes.c_void_p]
        for handle in set(handles):
            user32.DestroyIcon(handle)
    except Exception:
        pass


def _trimmer_python_command():
    """Use the interpreter behind `python mp3trimmer.py` for the Qt editor.

    Windows' `py -3` launcher may select a *different* Python installation
    without PyQt5. The main pipeline may still use its own interpreter; the
    editor must match the already-working `python mp3trimmer.py` command.
    Resolve only when clicked so startup remains fast.
    """
    explicit = os.environ.get("TRINITY_TRIMMER_PYTHON", "").strip()
    if explicit:
        interpreter = Path(explicit).expanduser()
        if not interpreter.is_file():
            raise FileNotFoundError(
                f"TRINITY_TRIMMER_PYTHON does not exist: {interpreter}"
            )
        return [str(interpreter)]
    if not getattr(sys, "frozen", False):
        return [sys.executable]
    # `python` is deliberately first here, NOT `py -3`. The latter caused
    # ModuleNotFoundError: PyQt5 when launching from bundled Trinity.exe.
    installed = shutil.which("python")
    if installed:
        return [installed]
    return _python_command()


def _trimmer_command():
    """Run the standalone Qt editor from laptop/, independent of the pipeline."""
    project = _project_dir()
    script = project / "mp3trimmer.py"
    if not script.is_file():
        raise FileNotFoundError(f"MP3 Trimmer was not found: {script}")
    return [*_trimmer_python_command(), str(script)], project


def run_backend(*, output_path=None, on_output=None, on_process=None, stop_file=None, **options):
    """Run the unchanged backend without routing its output through Tk.

    The desktop UI directs stdout/stderr to a file descriptor. The operating
    system writes to that file directly, so neither Python nor Tk processes
    thousands of per-video progress lines on the critical download path.
    The optional callback is kept for callers that need the old API.
    """
    if output_path is not None and on_output is not None:
        raise ValueError("Choose output_path or on_output, not both")

    project = _project_dir()
    command = [*_python_command(), "-u", str(project / "trinity.py"), "--headless"]
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if stop_file is not None:
        env["TRINITY_STOP_FILE"] = str(stop_file)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    kwargs = dict(
        cwd=project,
        env=env,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        **({"start_new_session": True} if os.name != "nt" else {}),
    )

    if output_path is not None:
        # The parent launcher closes its handle immediately after spawning.
        # The child inherits the output handle and continues writing on its own.
        with Path(output_path).open("ab", buffering=0) as sink:
            process = subprocess.Popen(command, stdout=sink, **kwargs)
        if on_process is not None:
            on_process(process)
        code = process.wait()
    else:
        # Backward-compatible path for callers that request a callback or a
        # normal console, without imposing a pipe on the graphical launcher.
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", bufsize=1, **kwargs,
        )
        if on_process is not None:
            on_process(process)
        try:
            if process.stdout:
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
# Keep a small visible terminal. Copy All always reads the full file on disk.
# Reading and rendering are deliberately decoupled from the running backend.
_MAX_VISIBLE = 24_000
_TRIM_AT = 34_000
_MAX_BATCH = 12_000
_MAX_BACKLOG = 96_000
_TERMINAL_POLL_MS = 450
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


def another_backend_running():
    """Non-destructive check; the backend owns the authoritative run lock."""
    try:
        lock_path = _project_dir() / "_trinity_active.lock"
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return True
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except (OSError, FileNotFoundError):
        return False  # The backend itself will reject any collision.




class _WindowsJob:
    """Windows Job Object: closing it stops the backend and ALL descendants.

    Prevents ffmpeg/yt-dlp/grandchildren from surviving a UI close even if a
    script ignores cancellation or spawns a separate process. Uses the WinAPI
    already present on Windows; no third-party dependency or polling loop.
    """
    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                        ("WriteOperationCount", ctypes.c_uint64),
                        ("OtherOperationCount", ctypes.c_uint64),
                        ("ReadTransferCount", ctypes.c_uint64),
                        ("WriteTransferCount", ctypes.c_uint64),
                        ("OtherTransferCount", ctypes.c_uint64)]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                        ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                    ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = EXTENDED_LIMIT()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(handle, 9,
                    ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not kernel.AssignProcessToJobObject(handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        self._kernel = kernel
        self._handle = handle

    def close(self):
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _force_stop_tree(process, job=None):
    """Emergency cleanup; always scope to this UI's child process tree."""
    if job is not None:
        job.close()  # KILL_ON_JOB_CLOSE also kills any surviving descendants.
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        # Used only if Job assignment failed / child survived closing the job.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            process.kill()

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
            self.closing = False
            self._close_started = None
            self._backend_process = None
            self._process_job = None
            self._trimmer_process = None
            self._process_lock = threading.Lock()
            self._stop_file = None
            self.values = {
                key: tk.BooleanVar(master=self.root, value=value)
                for key, value in load_options().items()
            }
            self.indicators = {}
            self._reader = None
            self._reader_offset = 0
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
            self._worker_finished = False
            self._worker_failure = None
            self._display_chars = 0

            # Use the original multi-resolution Windows app icon from the
            # compiled executable or laptop/trinity.ico. Do not replace it
            # with a 32x32 PhotoImage or a website favicon.
            self.native_icons = _set_windows_window_icon(
                self.root, _app_icon_path()
            )
            if self.native_icons:
                self.root.bind(
                    "<Destroy>",
                    lambda event: _release_windows_icons(self.native_icons)
                    if event.widget is self.root else None,
                    add="+",
                )

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

            # Keep the pipeline's primary action prominent and offer the
            # independent editor alongside it, without changing the layout
            # of the existing options or terminal. Both fit on narrow windows.
            actions = tk.Frame(wrapper, bg=bg)
            actions.pack(fill="x", pady=(15, 16))
            self.start_button = tk.Button(
                actions, text="START", bg=ink, fg=paper,
                activebackground="#343434", activeforeground=paper,
                disabledforeground="#dddddd", borderwidth=0, relief="flat",
                font=("Segoe UI", 11, "bold"), cursor="hand2",
                command=self.start, takefocus=True,
            )
            self.start_button.pack(side="left", fill="x", expand=True, ipady=9)
            self.trimmer_button = tk.Button(
                actions, text="MP3 TRIMMER", bg=paper, fg=ink,
                activebackground="#e9e9e6", activeforeground=ink,
                borderwidth=1, relief="solid", highlightthickness=0,
                font=("Segoe UI", 10, "bold"), cursor="hand2",
                command=self.launch_trimmer, takefocus=True,
            )
            self.trimmer_button.pack(side="left", padx=(10, 0), ipady=8)

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
            self.root.after(_TERMINAL_POLL_MS, self.drain_output)

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

        def launch_trimmer(self):
            """Open the Qt editor independently of the running pipeline.

            Do not register it in the pipeline's shutdown Job Object: closing
            Trinity must not kill an editor where unsaved crops may remain.
            """
            if self.closing or self.closed:
                return
            if self._trimmer_process is not None and self._trimmer_process.poll() is None:
                messagebox.showinfo(
                    "Trinity", "MP3 Trimmer is already running.", parent=self.root
                )
                return
            try:
                command, project = _trimmer_command()
                # PyInstaller's windowed launcher has no visible stderr. Keep
                # editor errors separate from the pipeline's live terminal.
                error_log = SETTINGS_FILE.with_name("mp3trimmer_launch.log")
                error_log.parent.mkdir(parents=True, exist_ok=True)
                with error_log.open("wb") as output:
                    process = subprocess.Popen(
                        command, cwd=project, stdin=subprocess.DEVNULL,
                        stdout=output, stderr=subprocess.STDOUT,
                        **({"creationflags": subprocess.CREATE_NO_WINDOW |
                            subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                           else {"start_new_session": True}),
                    )
            except (OSError, ValueError) as error:
                messagebox.showerror(
                    "MP3 Trimmer", f"Could not launch MP3 Trimmer:\n{error}",
                    parent=self.root,
                )
                return
            self._trimmer_process = process
            self.root.after(1000, self._monitor_trimmer, process, error_log)

        def _monitor_trimmer(self, process, error_log):
            if self.closed or self.closing:
                return
            code = process.poll()
            if code is None:
                self.root.after(1000, self._monitor_trimmer, process, error_log)
                return
            if self._trimmer_process is process:
                self._trimmer_process = None
            if code != 0:
                try:
                    details = error_log.read_text(encoding="utf-8", errors="replace")[-2500:].strip()
                except OSError:
                    details = ""
                messagebox.showerror(
                    "MP3 Trimmer",
                    f"MP3 Trimmer exited with code {code}." +
                    (f"\n\n{details}" if details else
                     "\n\nCheck that PyQt5, NumPy, Mutagen and FFmpeg are installed."),
                    parent=self.root,
                )

        def start(self):
            if self.running or self.closing or not self.persist():
                return
            if another_backend_running():
                messagebox.showwarning(
                    "Trinity", "A Trinity run is already active. "
                    "Wait for it to finish before starting another.", parent=self.root
                )
                return
            try:
                _TERMINAL_FILE.parent.mkdir(parents=True, exist_ok=True)
                # Reset the full log BEFORE opening a reader or launching the
                # subprocess, eliminating append/truncate races on Windows.
                with _TERMINAL_FILE.open("wb"):
                    pass
                if self._reader is not None:
                    self._reader.close()
                self._reader = _TERMINAL_FILE.open("rb", buffering=0)
            except OSError as error:
                messagebox.showerror("Trinity", f"Could not open terminal log:\n{error}",
                                     parent=self.root)
                return
            self._reader_offset = 0
            self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
            self.terminal.configure(state="normal")
            self.terminal.delete("1.0", "end")
            self.terminal.configure(state="disabled")
            self._display_chars = 0
            self._worker_finished = False
            self._worker_failure = None
            self._backend_process = None
            self._process_job = None
            self._stop_file = SETTINGS_FILE.parent / ("stop-" + uuid.uuid4().hex + ".flag")
            self.running = True
            self.start_button.configure(text="RUNNING", state="disabled")
            threading.Thread(target=self.worker, args=(self.current_options(),),
                             daemon=True, name="TrinityBackend").start()

        def register_process(self, process):
            with self._process_lock:
                self._backend_process = process
                if os.name == "nt":
                    try:
                        self._process_job = _WindowsJob(process)
                    except OSError as error:
                        # Built-in taskkill /T /F remains a last-resort backup.
                        self._append_shutdown_log(
                            f"[Windows process containment unavailable: {error}]\n")
                if self.closing:
                    self._request_stop()

        def _append_shutdown_log(self, value):
            try:
                with _TERMINAL_FILE.open("ab", buffering=0) as output:
                    output.write(value.encode("utf-8", "replace"))
            except OSError:
                pass

        def _request_stop(self):
            if self._stop_file is not None:
                try:
                    self._stop_file.write_text("stop\n", encoding="ascii")
                except OSError as error:
                    self._append_shutdown_log(f"[Could not signal cancellation: {error}]\n")

        def _cleanup_stop_file(self):
            if self._stop_file is not None:
                try:
                    self._stop_file.unlink(missing_ok=True)
                except OSError:
                    pass
                self._stop_file = None

        def worker(self, options):
            failure = None
            try:
                # A direct file descriptor, not stdout=PIPE. The Python
                # backend runs at its normal speed regardless of UI rendering.
                run_pipeline(output_path=_TERMINAL_FILE,
                             on_process=self.register_process,
                             stop_file=self._stop_file, **options)
            except BaseException as error:
                if not (isinstance(error, SystemExit) and error.code in (None, 0)):
                    failure = error
                    details = "".join(traceback.format_exception(
                        type(error), error, error.__traceback__))
                    try:
                        with _TERMINAL_FILE.open("ab", buffering=0) as sink:
                            sink.write(details.encode("utf-8", errors="replace"))
                    except OSError:
                        pass
            finally:
                # Single writer (the child) is complete before this flag is
                # updated. Tk's polling always does bounded work.
                # Once the backend exits, close the job to stop any orphaned
                # grandchildren before allowing another launch or UI exit.
                with self._process_lock:
                    job = self._process_job
                    self._process_job = None
                    if job is not None:
                        job.close()
                    self._backend_process = None
                self._worker_failure = failure
                self._worker_finished = True

        def drain_output(self):
            if self.closed:
                return
            if self._reader is not None:
                try:
                    # Never read more than a small batch per redraw. When a
                    # downloader floods output, jump forward rather than
                    # making the UI burn CPU trying to catch up. The full log
                    # is still recorded byte-for-byte for the Copy All icon.
                    self._reader.seek(0, os.SEEK_END)
                    end = self._reader.tell()
                    remaining = end - self._reader_offset
                    if remaining > _MAX_BACKLOG:
                        self._reader_offset = max(0, end - _MAX_BATCH)
                        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
                        self.append_visible(
                            "\n[Older output omitted from view; Copy All includes everything.]\n"
                        )
                    self._reader.seek(self._reader_offset)
                    data = self._reader.read(_MAX_BATCH)
                    self._reader_offset += len(data)
                    if data:
                        visible = self._decoder.decode(data)
                        visible = _ANSI.sub("", visible).replace("\r\n", "\n").replace("\r", "\n")
                        self.append_visible(visible)
                    if self._worker_finished and self._reader_offset >= end:
                        remainder = self._decoder.decode(b"", final=True)
                        self.append_visible(remainder)
                        self._reader.close()
                        self._reader = None
                except OSError as error:
                    self.append_visible(f"\n[Terminal view unavailable: {error}]\n")
                    try:
                        self._reader.close()
                    except OSError:
                        pass
                    self._reader = None
            if self._worker_finished and self.running and self._reader is None:
                self.running = False
                if not self.closing:
                    self._cleanup_stop_file()
                self.start_button.configure(
                    text="DONE" if self._worker_failure is None else "RETRY",
                    state="normal",
                )
            if not self.closing:
                self.root.after(_TERMINAL_POLL_MS, self.drain_output)

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
                # The complete output is written straight from the child.
                # Nothing is omitted from Copy All when the view is throttled.
                text = (_TERMINAL_FILE.read_text(encoding="utf-8", errors="replace")
                        if _TERMINAL_FILE.is_file() else "")
                text = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
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

        def _finish_closing(self):
            if not self.closing or self.closed:
                return
            process = self._backend_process
            if self._worker_finished or (process is None and not self.running):
                with self._process_lock:
                    job = self._process_job
                    self._process_job = None
                    if job is not None:
                        job.close()
                self._cleanup_stop_file()
                self.closed = True
                if self._reader is not None:
                    self._reader.close()
                    self._reader = None
                self.root.destroy()
                return
            # The backend gets time to release file handles and persist the
            # independent phone/Drive histories. After the deadline, make a
            # best-effort tree termination to prevent any lingering workers.
            if self._close_started is not None and time.monotonic() - self._close_started > 10:
                self._append_shutdown_log("\n[Shutdown deadline reached: stopping remaining subprocesses.]\n")
                with self._process_lock:
                    process = self._backend_process
                    job = self._process_job
                    self._process_job = None
                try:
                    _force_stop_tree(process, job)
                except (OSError, subprocess.TimeoutExpired) as error:
                    self._append_shutdown_log(f"[Process cleanup warning: {error}]\n")
                # Wait for the process waiter to observe termination before
                # closing the UI; never assume cleanup has succeeded.
                self._close_started = None
            self.root.after(150, self._finish_closing)

        def close(self):
            if self.closing or self.closed:
                return
            # Failure to save a checkbox must never strand active processes
            # or block cancellation behind a modal dialog.
            try:
                save_options(self.current_options())
            except OSError as error:
                self._append_shutdown_log(f"[Could not save preferences: {error}]\n")
            if not self.running:
                self._cleanup_stop_file()
                self.closed = True
                if self._reader is not None:
                    self._reader.close()
                    self._reader = None
                self.root.destroy()
                return
            self.closing = True
            self._close_started = time.monotonic()
            self.start_button.configure(text="STOPPING", state="disabled")
            self._append_shutdown_log("\n[Closing Trinity: requesting graceful stop...]\n")
            self._request_stop()
            self._finish_closing()

    TrinityWindow().root.mainloop()


if __name__ == "__main__":
    launch_ui(run_backend)
