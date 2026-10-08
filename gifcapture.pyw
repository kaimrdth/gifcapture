"""
GIF Capture - Win+Shift+D -> drag a region -> record (max 30 s) -> trim -> GIF on clipboard.

Runs in the system tray. Requirements: Python 3.9+ (with tkinter) and ffmpeg on PATH.
Run without a console window:  pythonw gifcapture.pyw
"""
import ctypes
import ctypes.wintypes as wt
import os
import queue
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

# ----------------------------------------------------------------- settings --
MAX_SECONDS = 30            # hard recording limit
CAPTURE_FPS = 30            # raw screen-capture frame rate
GIF_FPS = 15                # GIF frame rate (also the trim-editor frame step)
MAX_GIF_WIDTH = 1280        # GIFs wider than this are downscaled (lanczos)
PREVIEW_MAX = (960, 600)    # max size of the preview inside the trim editor
def _pictures_dir():
    # Ask Windows: Pictures is often redirected (e.g. into OneDrive).
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x27, None, 0, buf) == 0:  # CSIDL_MYPICTURES
        return Path(buf.value)
    return Path.home() / "Pictures"


OUTPUT_DIR = _pictures_dir() / "GifCapture"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "GifCapture"

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
HOTKEY_MODS = MOD_WIN | MOD_SHIFT
HOTKEY_VK = ord("D")
HOTKEY_LABEL = "Win+Shift+D"

# Colours
BG, PANEL, FG, MUTED = "#1b1b1f", "#26262c", "#f2f2f2", "#9a9aa3"
ACCENT, REC, HANDLE, PLAYHEAD = "#4c8bf5", "#ff3b30", "#ffffff", "#ffcc00"

# ---------------------------------------------------------------- win32 api --
# Physical pixels everywhere, so Tk coordinates match what gdigrab captures.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

LRESULT = wt.LPARAM
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]


def _sig(fn, argtypes, restype=wt.BOOL):
    fn.argtypes, fn.restype = argtypes, restype


_sig(user32.DefWindowProcW, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], LRESULT)
_sig(user32.RegisterClassW, [ctypes.POINTER(WNDCLASSW)], wt.ATOM)
_sig(user32.CreateWindowExW, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID], wt.HWND)
_sig(user32.RegisterHotKey, [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT])
_sig(user32.UnregisterHotKey, [wt.HWND, ctypes.c_int])
_sig(user32.GetMessageW, [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT], ctypes.c_int)
_sig(user32.TranslateMessage, [ctypes.POINTER(wt.MSG)])
_sig(user32.DispatchMessageW, [ctypes.POINTER(wt.MSG)], LRESULT)
_sig(user32.PostMessageW, [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM])
_sig(user32.PostQuitMessage, [ctypes.c_int], None)
_sig(user32.RegisterWindowMessageW, [wt.LPCWSTR], wt.UINT)
_sig(user32.LoadIconW, [wt.HINSTANCE, wt.LPVOID], wt.HICON)
_sig(user32.LoadImageW, [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT], wt.HANDLE)
_sig(user32.CreatePopupMenu, [], wt.HMENU)
_sig(user32.AppendMenuW, [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR])
_sig(user32.TrackPopupMenu, [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID], ctypes.c_int)
_sig(user32.DestroyMenu, [wt.HMENU])
_sig(user32.SetForegroundWindow, [wt.HWND])
_sig(user32.GetCursorPos, [ctypes.POINTER(wt.POINT)])
_sig(user32.SystemParametersInfoW, [wt.UINT, wt.UINT, wt.LPVOID, wt.UINT])
_sig(user32.OpenClipboard, [wt.HWND])
_sig(user32.EmptyClipboard, [])
_sig(user32.CloseClipboard, [])
_sig(user32.SetClipboardData, [wt.UINT, wt.HANDLE], wt.HANDLE)
_sig(user32.RegisterClipboardFormatW, [wt.LPCWSTR], wt.UINT)
_sig(kernel32.GetModuleHandleW, [wt.LPCWSTR], wt.HMODULE)
_sig(kernel32.GlobalAlloc, [wt.UINT, ctypes.c_size_t], wt.HGLOBAL)
_sig(kernel32.GlobalLock, [wt.HGLOBAL], wt.LPVOID)
_sig(kernel32.GlobalUnlock, [wt.HGLOBAL])
_sig(kernel32.GlobalFree, [wt.HGLOBAL], wt.HGLOBAL)
_sig(kernel32.CreateMutexW, [wt.LPVOID, wt.BOOL, wt.LPCWSTR], wt.HANDLE)
_sig(shell32.Shell_NotifyIconW, [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)])

WM_DESTROY, WM_CLOSE, WM_NULL, WM_HOTKEY = 0x0002, 0x0010, 0x0000, 0x0312
WM_LBUTTONUP, WM_RBUTTONUP = 0x0202, 0x0205
WM_TRAY = 0x8000 + 1
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4

CREATE_NO_WINDOW = 0x08000000


def virtual_screen():
    gsm = user32.GetSystemMetrics
    return gsm(76), gsm(77), gsm(78), gsm(79)  # x, y, w, h


def work_area():
    r = wt.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.left, r.top, r.right, r.bottom


# ---------------------------------------------------------------- clipboard --
def copy_gif_to_clipboard(path: Path):
    """Put the GIF on the clipboard as a file (CF_HDROP) plus raw 'GIF' bytes.

    The file form is what keeps the animation when pasting into Slack, Teams,
    Discord, browsers, Explorer, etc.; the raw 'GIF' format covers apps that read it."""
    path = str(path.resolve())
    dropfiles = struct.pack("<IiiII", 20, 0, 0, 0, 1)  # DROPFILES, fWide=TRUE
    hdrop = dropfiles + (path + "\0\0").encode("utf-16-le")
    payloads = [
        (15, hdrop),  # CF_HDROP
        (user32.RegisterClipboardFormatW("Preferred DropEffect"), struct.pack("<I", 1)),  # copy
        (user32.RegisterClipboardFormatW("GIF"), Path(path).read_bytes()),
        (13, (path + "\0").encode("utf-16-le")),  # CF_UNICODETEXT: the file path
    ]
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise OSError("Clipboard is busy")
    try:
        user32.EmptyClipboard()
        for fmt, data in payloads:
            h = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
            p = kernel32.GlobalLock(h)
            ctypes.memmove(p, data, len(data))
            kernel32.GlobalUnlock(h)
            if not user32.SetClipboardData(fmt, h):
                kernel32.GlobalFree(h)
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------------- icon --
def ensure_icon() -> Path:
    """Write a small 32x32 'record' icon (.ico) so the tray and windows aren't generic."""
    ico = APP_DIR / "icon.ico"
    if not ico.exists():
        write_icon(ico)
    return ico


def write_icon(ico: Path):
    ico.parent.mkdir(parents=True, exist_ok=True)
    n = 32
    rows = []
    for y in range(n - 1, -1, -1):  # bottom-up
        row = bytearray()
        for x in range(n):
            cx, cy = x + 0.5 - n / 2, y + 0.5 - n / 2
            # rounded square background
            qx, qy = max(abs(cx) - 10, 0), max(abs(cy) - 10, 0)
            sq = max(0.0, min(1.0, 6.5 - (qx * qx + qy * qy) ** 0.5))
            dot = max(0.0, min(1.0, 8.0 - (cx * cx + cy * cy) ** 0.5))
            r = int(0x2a * (1 - dot) + 0xff * dot)
            g = int(0x2a * (1 - dot) + 0x3b * dot)
            b = int(0x30 * (1 - dot) + 0x30 * dot)
            row += bytes((b, g, r, int(255 * sq)))
        rows.append(bytes(row))
    pixels = b"".join(rows)
    bih = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    image = bih + pixels + bytes(n * 4)  # + AND mask
    header = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", n, n, 0, 0, 1, 32, len(image), 22)
    ico.write_bytes(header + image)
    return ico


# --------------------------------------------------------- tray + hotkey ----
class TrayThread(threading.Thread):
    """Owns a hidden Win32 window: the global hotkey and the tray icon/menu live here.
    Events are posted to the Tk thread through a queue."""

    def __init__(self, events: queue.Queue, icon_path: Path):
        super().__init__(daemon=True)
        self.events, self.icon_path, self.hwnd = events, icon_path, None

    def run(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._proc)  # keep a reference
        wc = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinst, lpszClassName="GifCaptureTray")
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, "GifCaptureTray", "GIF Capture", 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        if not user32.RegisterHotKey(self.hwnd, 1, HOTKEY_MODS | MOD_NOREPEAT, HOTKEY_VK):
            self.events.put(("hotkey_failed",))
        self.hicon = user32.LoadImageW(None, str(self.icon_path), 1, 0, 0, 0x10 | 0x40) \
            or user32.LoadIconW(None, ctypes.c_void_p(32512))
        self._add_icon()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID = self.hwnd, 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage, nid.hIcon = WM_TRAY, self.hicon
        nid.szTip = f"GIF Capture  ({HOTKEY_LABEL})"
        return nid

    def _add_icon(self):
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid()))

    def _menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, 1, f"Capture region\t{HOTKEY_LABEL}")
        user32.AppendMenuW(menu, 0, 2, "Open GIF folder")
        user32.AppendMenuW(menu, 0x800, 0, None)
        user32.AppendMenuW(menu, 0, 3, "Quit")
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(menu, 0x100 | 0x2, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        return {1: "hotkey", 2: "open_folder", 3: "quit"}.get(cmd)

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY:
                self.events.put(("hotkey",))
                return 0
            if msg == WM_TRAY:
                ev = lparam & 0xFFFF
                if ev == WM_LBUTTONUP:
                    self.events.put(("hotkey",))
                elif ev == WM_RBUTTONUP:
                    cmd = self._menu()
                    if cmd:
                        self.events.put((cmd,))
                return 0
            if msg == self._taskbar_created:  # explorer restarted
                self._add_icon()
            if msg == WM_DESTROY:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
                user32.UnregisterHotKey(hwnd, 1)
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)


# ------------------------------------------------------------------- ffmpeg --
def find_ffmpeg():
    # Installed build ships ffmpeg.exe next to GifCapture.exe; dev runs fall back to PATH.
    here = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
    bundled = here / "ffmpeg.exe"
    return str(bundled) if bundled.exists() else shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()


class Job:
    """An ffmpeg subprocess polled from the Tk loop (no threads needed)."""

    def __init__(self, args, log_path: Path, stdin=subprocess.DEVNULL):
        self.log = open(log_path, "ab")
        self.proc = subprocess.Popen([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                                     stdin=stdin, stdout=subprocess.DEVNULL, stderr=self.log,
                                     creationflags=CREATE_NO_WINDOW)

    @property
    def done(self):
        return self.proc.poll() is not None

    @property
    def ok(self):
        return self.proc.poll() == 0

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.log.close()

    def close(self):
        self.log.close()


def gif_size(w, h):
    if w > MAX_GIF_WIDTH:
        return MAX_GIF_WIDTH, round(h * MAX_GIF_WIDTH / w / 2) * 2
    return w, h


def gif_args(src, dst, w, h, start=None, dur=None):
    gw, gh = gif_size(w, h)
    vf = f"fps={GIF_FPS}"
    if (gw, gh) != (w, h):
        vf += f",scale={gw}:{gh}:flags=lanczos"
    # Two-pass-in-one: build an optimal 256-colour palette, then map with error-diffusion
    # dithering; diff_mode=rectangle only re-dithers the changing area (smaller, no shimmer).
    vf += (",split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];"
           "[b][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle")
    args = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", src]
    if dur is not None:
        args += ["-t", f"{dur:.3f}"]
    return args + ["-vf", vf, "-loop", "0", dst]


def log_tail(path: Path, n=12):
    try:
        return "\n".join(path.read_text(errors="replace").strip().splitlines()[-n:])
    except OSError:
        return ""


def fmt_time(sec):
    return f"{int(sec // 60)}:{sec % 60:05.2f}"


def fmt_bytes(n):
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{n / 1024:.0f} KB"


# --------------------------------------------------------- region selector --
class RegionSelector:
    def __init__(self, app, on_done):
        self.app, self.on_done, self.start = app, on_done, None
        vx, vy, vw, vh = virtual_screen()
        self.vx, self.vy = vx, vy
        w = self.win = tk.Toplevel(app.root)
        w.overrideredirect(True)
        w.geometry(f"{vw}x{vh}+{vx}+{vy}")
        w.attributes("-topmost", True, "-alpha", 0.35)
        c = self.canvas = tk.Canvas(w, bg="black", highlightthickness=0, cursor="crosshair")
        c.pack(fill="both", expand=True)
        # hint centred on the primary monitor
        pw, ph = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        c.create_text(-vx + pw // 2, -vy + ph // 2, fill="white", font=("Segoe UI", 20, "bold"),
                      text=f"Drag to select an area to record  ·  Esc to cancel")
        self.rect = c.create_rectangle(0, 0, 0, 0, outline="white", width=2, state="hidden")
        self.label = c.create_text(0, 0, anchor="sw", fill="white", font=("Segoe UI", 12, "bold"))
        c.bind("<ButtonPress-1>", self._press)
        c.bind("<B1-Motion>", self._drag)
        c.bind("<ButtonRelease-1>", self._release)
        c.bind("<ButtonPress-3>", lambda e: self._finish(None))
        w.bind("<Escape>", lambda e: self._finish(None))
        w.after(30, lambda: (w.lift(), w.focus_force(), c.focus_set()))

    def _press(self, e):
        self.start = (e.x_root, e.y_root)
        self.canvas.itemconfigure(self.rect, state="normal")
        self._drag(e)

    def _drag(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        x1, y1 = e.x_root, e.y_root
        c, ox, oy = self.canvas, self.vx, self.vy
        c.coords(self.rect, min(x0, x1) - ox, min(y0, y1) - oy, max(x0, x1) - ox, max(y0, y1) - oy)
        c.coords(self.label, min(x0, x1) - ox, min(y0, y1) - oy - 4)
        c.itemconfigure(self.label, text=f"{abs(x1 - x0)} × {abs(y1 - y0)}")

    def _release(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        x, y = min(x0, e.x_root), min(y0, e.y_root)
        w, h = abs(e.x_root - x0), abs(e.y_root - y0)
        w, h = w - w % 2, h - h % 2
        self._finish((x, y, w, h) if w >= 16 and h >= 16 else None)

    def _finish(self, rect):
        self.win.destroy()
        # give the compositor a moment to remove the dim overlay before capture starts
        self.app.root.after(150, lambda: self.on_done(rect))


# ----------------------------------------------------------------- recorder --
class Recorder:
    BORDER, GAP = 3, 2

    def __init__(self, app, rect, on_done):
        self.app, self.rect, self.on_done = app, rect, on_done
        self.tmp = Path(tempfile.mkdtemp(prefix="gifcap_"))
        self.raw, self.log = self.tmp / "raw.mkv", self.tmp / "ffmpeg.log"
        x, y, w, h = rect
        # Lossless RGB capture so the GIF palette step gets pristine input.
        self.job = Job(["-f", "gdigrab", "-framerate", CAPTURE_FPS, "-draw_mouse", 1,
                        "-offset_x", x, "-offset_y", y, "-video_size", f"{w}x{h}", "-i", "desktop",
                        "-t", MAX_SECONDS, "-c:v", "libx264rgb", "-preset", "ultrafast", "-crf", 0,
                        self.raw], self.log, stdin=subprocess.PIPE)
        self.t0 = time.monotonic()
        self._make_border()
        self._make_controls()
        self._tick()

    def _make_border(self):
        x, y, w, h = self.rect
        o = self.BORDER + self.GAP  # border sits *outside* the captured area
        key = "#ff00fe"
        b = self.border = tk.Toplevel(self.app.root)
        b.overrideredirect(True)
        b.attributes("-topmost", True, "-transparentcolor", key)
        b.geometry(f"{w + 2 * o}x{h + 2 * o}+{x - o}+{y - o}")
        c = tk.Canvas(b, bg=key, highlightthickness=0)
        c.pack(fill="both", expand=True)
        half = self.BORDER / 2
        c.create_rectangle(half, half, w + 2 * o - half, h + 2 * o - half, outline=REC, width=self.BORDER)

    def _make_controls(self):
        bar = self.bar = tk.Toplevel(self.app.root, bg=PANEL)
        bar.overrideredirect(True)
        bar.attributes("-topmost", True)
        tk.Label(bar, text="●", fg=REC, bg=PANEL, font=("Segoe UI", 12)).pack(side="left", padx=(10, 4))
        self.time_lbl = tk.Label(bar, fg=FG, bg=PANEL, font=("Consolas", 11), width=13, anchor="w")
        self.time_lbl.pack(side="left")
        tk.Button(bar, text="■  Stop", command=self.stop, bg=REC, fg="white", activebackground="#d0302a",
                  activeforeground="white", relief="flat", font=("Segoe UI", 10, "bold"), padx=10,
                  cursor="hand2").pack(side="left", padx=8, pady=6)
        tk.Label(bar, text=HOTKEY_LABEL, fg=MUTED, bg=PANEL, font=("Segoe UI", 9)).pack(side="left", padx=(0, 10))
        bar.update_idletasks()
        bw, bh = bar.winfo_reqwidth(), bar.winfo_reqheight()
        x, y, w, h = self.rect
        vx, vy, vw, vh = virtual_screen()
        o = self.BORDER + self.GAP + 6
        bx = min(max(x, vx), vx + vw - bw)
        if y + h + o + bh <= vy + vh:
            by = y + h + o
        elif y - o - bh >= vy:
            by = y - o - bh
        else:  # region fills the screen; the bar will be visible in the recording
            by = y + h - bh - 10
        bar.geometry(f"+{bx}+{by}")

    def _tick(self):
        if self.job.done:
            self._finish()
            return
        el = time.monotonic() - self.t0
        self.time_lbl.configure(text=f"{fmt_time(min(el, MAX_SECONDS))[:-1]} / {MAX_SECONDS}s")
        if el > MAX_SECONDS + 5:
            self.job.proc.kill()
        self.app.root.after(100, self._tick)

    def stop(self):
        if not self.job.done:
            try:
                self.job.proc.stdin.write(b"q")
                self.job.proc.stdin.flush()
            except OSError:
                self.job.proc.kill()

    def abort(self):
        self.job.kill()
        for win in (self.border, self.bar):
            win.destroy()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _finish(self):
        self.job.close()
        self.border.destroy()
        self.bar.destroy()
        ok = self.raw.exists() and self.raw.stat().st_size > 1024
        self.on_done(self if ok else None, log_tail(self.log))


# ------------------------------------------------------------- trim editor --
class Editor:
    PAD = 14
    TL_H = 46

    def __init__(self, app, rec: Recorder):
        self.app, self.rec = app, rec
        self.tmp, self.raw, self.log = rec.tmp, rec.raw, rec.log
        _, _, self.w, self.h = rec.rect
        s = min(1.0, PREVIEW_MAX[0] / self.w, PREVIEW_MAX[1] / self.h)
        self.pw, self.ph = max(2, int(self.w * s) // 2 * 2), max(2, int(self.h * s) // 2 * 2)
        self.frames, self.cache = [], {}
        self.n = self.in_ = self.out = self.cur = 0
        self.playing, self.drag, self.exporting = False, None, False
        self.export_job = None

        # Background work starts immediately: preview frames + full-length GIF.
        fdir = self.tmp / "frames"
        fdir.mkdir()
        self.frame_job = Job(["-i", self.raw, "-vf", f"fps={GIF_FPS},scale={self.pw}:{self.ph}:flags=bilinear",
                              "-compression_level", 1, fdir / "%05d.png"], self.log)
        self.full_gif = self.tmp / "full.gif"
        self.full_job = Job(gif_args(self.raw, self.full_gif, self.w, self.h), self.log)

        self._build_ui()
        self._wait_for_frames()
        self._watch_full_gif()

    # ---- UI
    def _build_ui(self):
        win = self.win = tk.Toplevel(self.app.root, bg=BG)
        win.title("GIF Capture — Trim")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self.cancel)

        self.view = tk.Canvas(win, width=self.pw, height=self.ph, bg="black", highlightthickness=0)
        self.view.pack(padx=16, pady=(16, 10))
        self.img_id = self.view.create_image(0, 0, anchor="nw")
        self.loading_id = self.view.create_text(self.pw // 2, self.ph // 2, text="Preparing preview…",
                                                fill=MUTED, font=("Segoe UI", 11))

        self.tw = max(self.pw, 640)
        self.tl = tk.Canvas(win, width=self.tw, height=self.TL_H, bg=BG, highlightthickness=0, cursor="hand2")
        self.tl.pack(padx=16)
        self.tl.bind("<ButtonPress-1>", self._tl_press)
        self.tl.bind("<B1-Motion>", self._tl_drag)
        self.tl.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))

        info = tk.Frame(win, bg=BG)
        info.pack(fill="x", padx=16, pady=(4, 0))
        self.pos_lbl = tk.Label(info, bg=BG, fg=FG, font=("Consolas", 10))
        self.pos_lbl.pack(side="left")
        self.sel_lbl = tk.Label(info, bg=BG, fg=MUTED, font=("Segoe UI", 9))
        self.sel_lbl.pack(side="right")

        bar = tk.Frame(win, bg=BG)
        bar.pack(fill="x", padx=16, pady=12)

        def btn(text, cmd, primary=False, side="left"):
            b = tk.Button(bar, text=text, command=cmd, relief="flat", cursor="hand2", padx=12, pady=4,
                          font=("Segoe UI", 10, "bold" if primary else "normal"),
                          bg=ACCENT if primary else PANEL, fg="white",
                          activebackground="#3b74d6" if primary else "#33333a", activeforeground="white")
            b.pack(side=side, padx=(0, 6) if side == "left" else (6, 0))
            return b

        self.play_btn = btn("▶  Play", self.toggle_play)
        btn("Set start  [I]", self.set_in)
        btn("Set end  [O]", self.set_out)
        btn("Reset", self.reset)
        self.copy_btn = btn("Copy GIF  ⏎", self.export, primary=True, side="right")
        btn("Cancel", self.cancel, side="right")

        self.status = tk.Label(win, bg=BG, fg=MUTED, font=("Segoe UI", 9), anchor="w")
        self.status.pack(fill="x", padx=16, pady=(0, 12))

        keys = {"<space>": lambda e: self.toggle_play(), "<Left>": lambda e: self.step(-1),
                "<Right>": lambda e: self.step(1), "<Shift-Left>": lambda e: self.step(-GIF_FPS),
                "<Shift-Right>": lambda e: self.step(GIF_FPS), "<Home>": lambda e: self.seek(self.in_),
                "<End>": lambda e: self.seek(self.out), "i": lambda e: self.set_in(), "o": lambda e: self.set_out(),
                "r": lambda e: self.reset(), "<Return>": lambda e: self.export(), "<Escape>": lambda e: self.cancel()}
        for k, fn in keys.items():
            win.bind(k, fn)

        win.update_idletasks()
        l, t, r, b = work_area()
        ww, wh = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{l + (r - l - ww) // 2}+{t + max(0, (b - t - wh) // 2)}")
        win.attributes("-topmost", True)
        win.after(300, lambda: win.attributes("-topmost", False))
        win.focus_force()
        self._redraw()

    def lift(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    # ---- background jobs
    def _wait_for_frames(self):
        if not self.frame_job.done:
            self.status.configure(text="Preparing preview…")
            self.win.after(100, self._wait_for_frames)
            return
        self.frame_job.close()
        self.frames = sorted((self.tmp / "frames").glob("*.png"))
        if not self.frames:
            self.app.error("Couldn't read the recording.", log_tail(self.log))
            self.cancel()
            return
        self.n = len(self.frames)
        self.in_, self.out, self.cur = 0, self.n - 1, 0
        self.view.delete(self.loading_id)
        self._update()
        self.toggle_play()

    def _watch_full_gif(self):
        if self.exporting:
            return
        if not self.full_job.done:
            if self.frames:
                self.status.configure(text="Encoding GIF in the background…")
            self.win.after(200, self._watch_full_gif)
            return
        self.full_job.close()
        if self.full_job.ok:
            gw, gh = gif_size(self.w, self.h)
            self.status.configure(text=f"Full GIF ready · {fmt_bytes(self.full_gif.stat().st_size)} · "
                                       f"{gw}×{gh} @ {GIF_FPS} fps   —   drag the white handles to trim")
        else:
            self.status.configure(text="Background encode failed; it will be retried on Copy.")

    # ---- timeline
    def _x(self, i):
        return self.PAD + (i / max(1, self.n - 1)) * (self.tw - 2 * self.PAD)

    def _i(self, x):
        f = (x - self.PAD) / (self.tw - 2 * self.PAD)
        return min(max(round(f * max(1, self.n - 1)), 0), max(0, self.n - 1))

    def _redraw(self):
        c, P, H = self.tl, self.PAD, self.TL_H
        c.delete("all")
        c.create_rectangle(P, 16, self.tw - P, 30, fill="#3a3a42", width=0)
        if not self.n:
            return
        xi, xo, xc = self._x(self.in_), self._x(self.out), self._x(self.cur)
        c.create_rectangle(xi, 16, xo, 30, fill=ACCENT, width=0)
        for xx in (xi, xo):
            c.create_rectangle(xx - 4, 8, xx + 4, 38, fill=HANDLE, outline="#000000")
        c.create_line(xc, 4, xc, H - 4, fill=PLAYHEAD, width=2)
        c.create_polygon(xc - 5, 2, xc + 5, 2, xc, 9, fill=PLAYHEAD)

    def _tl_press(self, e):
        if not self.n:
            return
        self.pause()
        if abs(e.x - self._x(self.out)) <= 8 and (self.out != self.in_ or e.x > self._x(self.in_)):
            self.drag = "out"
        elif abs(e.x - self._x(self.in_)) <= 8:
            self.drag = "in"
        else:
            self.drag = "head"
        self._tl_drag(e)

    def _tl_drag(self, e):
        if not self.n or not self.drag:
            return
        i = self._i(e.x)
        if self.drag == "in":
            self.in_ = min(i, self.out)
            self.cur = self.in_
        elif self.drag == "out":
            self.out = max(i, self.in_)
            self.cur = self.out
        else:
            self.cur = i
        self._update()

    # ---- playback / transport
    def _show(self, i):
        img = self.cache.get(i)
        if img is None:
            img = self.cache[i] = tk.PhotoImage(file=str(self.frames[i]))
            if len(self.cache) > 120:
                self.cache.pop(next(iter(self.cache)))
        self.view.itemconfigure(self.img_id, image=img)

    def _update(self):
        if not self.n:
            return
        self._show(self.cur)
        self._redraw()
        total = self.n / GIF_FPS
        self.pos_lbl.configure(text=f"{fmt_time(self.cur / GIF_FPS)} / {fmt_time(total)}")
        a, b = self.in_ / GIF_FPS, (self.out + 1) / GIF_FPS
        self.sel_lbl.configure(text=f"Selection {fmt_time(a)} → {fmt_time(b)}  ·  {b - a:.2f}s  ·  "
                                    f"{self.out - self.in_ + 1} frames")

    def toggle_play(self):
        if not self.n:
            return
        if self.playing:
            self.pause()
            return
        self.playing = True
        self.play_btn.configure(text="❚❚  Pause")
        if not (self.in_ <= self.cur < self.out):
            self.cur = self.in_ - 1
        self._play_step()

    def _play_step(self):
        if not self.playing:
            return
        self.cur = self.cur + 1 if self.cur < self.out else self.in_
        self._update()
        self.win.after(int(1000 / GIF_FPS), self._play_step)

    def pause(self):
        self.playing = False
        self.play_btn.configure(text="▶  Play")

    def step(self, d):
        self.pause()
        self.seek(self.cur + d)

    def seek(self, i):
        if self.n:
            self.cur = min(max(i, 0), self.n - 1)
            self._update()

    def set_in(self):
        if self.n:
            self.in_ = min(self.cur, self.out)
            self._update()

    def set_out(self):
        if self.n:
            self.out = max(self.cur, self.in_)
            self._update()

    def reset(self):
        if self.n:
            self.in_, self.out = 0, self.n - 1
            self._update()

    # ---- export / close
    def export(self):
        if self.exporting or not self.n:
            return
        self.exporting = True
        self.pause()
        self.copy_btn.configure(state="disabled", text="Encoding…")
        full = self.in_ == 0 and self.out == self.n - 1
        if full and not (self.full_job.done and not self.full_job.ok):
            self.target, job = self.full_gif, self.full_job  # reuse the background encode
        else:
            self.full_job.kill()
            start = None if full else self.in_ / GIF_FPS
            dur = None if full else (self.out - self.in_ + 1) / GIF_FPS
            self.target = self.tmp / "trim.gif"
            job = self.export_job = Job(gif_args(self.raw, self.target, self.w, self.h, start, dur), self.log)
        self.status.configure(text="Encoding GIF…")
        self._wait_export(job)

    def _wait_export(self, job):
        if not job.done:
            self.win.after(100, lambda: self._wait_export(job))
            return
        job.close()
        if not job.ok or not self.target.exists():
            self.app.error("GIF encoding failed.", log_tail(self.log))
            self.exporting = False
            self.copy_btn.configure(state="normal", text="Copy GIF  ⏎")
            return
        self.app.deliver(self.target, gif_size(self.w, self.h))
        self.close()

    def cancel(self):
        self.close()

    def close(self):
        self.playing = False
        for j in (self.frame_job, self.full_job, self.export_job):
            if j:
                j.kill()
        self.cache.clear()
        self.win.destroy()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.app.editor_closed()


# ---------------------------------------------------------------------- app --
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        icon = ensure_icon()
        try:
            self.root.iconbitmap(default=str(icon))
        except tk.TclError:
            pass
        self.events = queue.Queue()
        self.state, self.recorder, self.editor = "idle", None, None
        self.tray = TrayThread(self.events, icon)
        self.tray.start()
        self.root.after(50, self._pump)
        self.toast(f"GIF Capture is running\nPress {HOTKEY_LABEL} to record a region")

    def _pump(self):
        try:
            while True:
                self._handle(self.events.get_nowait()[0])
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _handle(self, kind):
        if kind == "hotkey":
            if self.state == "idle":
                self.state = "selecting"
                RegionSelector(self, self._region_selected)
            elif self.state == "recording":
                self.recorder.stop()
            elif self.state == "editing":
                self.editor.lift()
        elif kind == "open_folder":
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(OUTPUT_DIR)
        elif kind == "quit":
            self.quit()
        elif kind == "hotkey_failed":
            self.error(f"Couldn't register {HOTKEY_LABEL} — another app may already use it.\n"
                       "You can still start a capture by clicking the tray icon.")

    def _region_selected(self, rect):
        if not rect:
            self.state = "idle"
            return
        self.state = "recording"
        self.recorder = Recorder(self, rect, self._recorded)

    def _recorded(self, rec, log):
        self.recorder = None
        if rec is None:
            self.state = "idle"
            self.error("Recording failed or was too short.", log)
            return
        self.state = "editing"
        self.editor = Editor(self, rec)

    def editor_closed(self):
        self.editor, self.state = None, "idle"

    def deliver(self, gif: Path, size):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        dest = OUTPUT_DIR / f"gif_{datetime.now():%Y%m%d_%H%M%S}.gif"
        shutil.copyfile(gif, dest)
        try:
            copy_gif_to_clipboard(dest)
            head = "GIF copied to clipboard"
        except OSError as e:
            head = f"Couldn't use clipboard ({e})"
        self.toast(f"{head}\n{fmt_bytes(dest.stat().st_size)} · {size[0]}×{size[1]} · {dest.name}")

    def toast(self, text, ms=3500):
        t = tk.Toplevel(self.root, bg=PANEL)
        t.overrideredirect(True)
        t.attributes("-topmost", True, "-alpha", 0.96)
        tk.Label(t, text=text, bg=PANEL, fg=FG, font=("Segoe UI", 10), justify="left",
                 padx=16, pady=12).pack()
        t.update_idletasks()
        _, _, r, b = work_area()
        t.geometry(f"+{r - t.winfo_reqwidth() - 20}+{b - t.winfo_reqheight() - 20}")
        t.bind("<Button-1>", lambda e: t.destroy())
        t.after(ms, t.destroy)

    def error(self, msg, detail=""):
        messagebox.showerror("GIF Capture", msg + (f"\n\nffmpeg said:\n{detail}" if detail else ""))

    def quit(self):
        if self.recorder:
            self.recorder.abort()
        if self.editor:
            self.editor.close()
        self.tray.stop()
        self.root.after(200, self.root.destroy)

    def run(self):
        self.root.mainloop()


def main():
    if not FFMPEG:
        ctypes.windll.user32.MessageBoxW(None, "ffmpeg was not found on PATH.\n\nInstall it with:\n"
                                         "winget install Gyan.FFmpeg", "GIF Capture", 0x10)
        sys.exit(1)
    mutex = kernel32.CreateMutexW(None, False, "Local\\GifCapture.SingleInstance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, f"GIF Capture is already running.\nPress {HOTKEY_LABEL}.",
                                         "GIF Capture", 0x40)
        sys.exit(0)
    App().run()
    del mutex


if __name__ == "__main__":
    main()
