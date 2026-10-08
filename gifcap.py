"""
gifcap - GIF Capture for agents and scripts.

Every command prints JSON to stdout (errors: {"error": ...} with a non-zero exit code).

  gifcap windows                      visible windows: title, process, rect, monitor
  gifcap monitors                     monitors and work areas
  gifcap record OUT.mkv --window T    record a window / --region x,y,w,h / --monitor N
  gifcap stop                         stop background recordings
  gifcap run SCENARIO.json            drive apps and record clips from a scenario file
  gifcap focus|click|drag|type|keys   one-off input actions
  gifcap wait-idle --window T         wait until a window stops changing (e.g. an AI reply)
  gifcap idle IN.mkv                  find still stretches
  gifcap gif IN.mkv OUT.gif           encode a GIF (trim, speed-up, redact, crop)
  gifcap mp4 IN.mkv OUT.mp4           same options, as video
  gifcap sheet IN OUT.png             contact sheet with timestamps, for reviewing a take
  gifcap still IN --at T OUT.png      one frame
  gifcap reel REEL.json OUT.mp4       title cards + clips + stills -> one captioned video
  gifcap copy FILE.gif                put a GIF on the clipboard (pastes animated)

Run `gifcap <command> --help` for options.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import gifcapture as core  # shared Win32/ffmpeg helpers; also makes this process DPI aware

VERSION = "1.2.0"
user32, kernel32 = core.user32, core.kernel32
dwmapi = ctypes.WinDLL("dwmapi")
kernel32.OpenProcess.argtypes, kernel32.OpenProcess.restype = [wt.DWORD, wt.BOOL, wt.DWORD], wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
STATE_DIR = core.APP_DIR / "recordings"
ARIAL = "C\\:/Windows/Fonts/arial.ttf"


class Fail(Exception):
    pass


def out(obj):
    print(json.dumps(obj, indent=2))


# ------------------------------------------------------------------ windows --
def _window_title(h):
    n = user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, buf, n + 1)
    return buf.value


def _process_name(h):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    hp = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not hp:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(520), wt.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(hp)


def _frame(h):
    """Visible window bounds (excludes the invisible resize border)."""
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(wt.HWND(h), 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(wt.HWND(h), ctypes.byref(r))
    return [r.left, r.top, r.right - r.left, r.bottom - r.top]


def _cloaked(h):
    v = wt.DWORD()
    dwmapi.DwmGetWindowAttribute(wt.HWND(h), 14, ctypes.byref(v), ctypes.sizeof(v))  # DWMWA_CLOAKED
    return v.value != 0


def list_monitors():
    mons = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def cb(hmon, _hdc, _rect, _):
        mi = core.MONITORINFO(cbSize=ctypes.sizeof(core.MONITORINFO))
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        m, w = mi.rcMonitor, mi.rcWork
        mons.append({"rect": [m.left, m.top, m.right - m.left, m.bottom - m.top],
                     "work": [w.left, w.top, w.right - w.left, w.bottom - w.top],
                     "primary": bool(mi.dwFlags & 1)})
        return True

    user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    mons.sort(key=lambda m: (not m["primary"], m["rect"][0], m["rect"][1]))  # primary first
    for i, m in enumerate(mons, 1):
        m["index"] = i
    return mons


def _monitor_of(rect, mons):
    cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
    for m in mons:
        x, y, w, h = m["rect"]
        if x <= cx < x + w and y <= cy < y + h:
            return m["index"]
    return None


def list_windows(include_all=False):
    mons, wins = list_monitors(), []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(h, _):
        if not user32.IsWindowVisible(h) or _cloaked(h):
            return True
        title = _window_title(h)
        if not title and not include_all:
            return True
        rect = _frame(h)
        if (rect[2] < 50 or rect[3] < 50) and not include_all:
            return True
        wins.append({"hwnd": h, "title": title, "process": _process_name(h), "rect": rect,
                     "minimized": bool(user32.IsIconic(h)), "monitor": _monitor_of(rect, mons)})
        return True

    user32.EnumWindows(proc(cb), 0)  # top of z-order first
    return wins


def find_window(query):
    """First (topmost) window whose title or process name contains `query`, case-insensitively."""
    q = query.lower()
    wins = [w for w in list_windows() if q in w["title"].lower() or q in w["process"].lower()]
    if not wins:
        raise Fail(f"no visible window matches {query!r}; run `gifcap windows` to see what's open")
    return wins[0]


user32.GetForegroundWindow.restype = wt.HWND


def focus_window(query):
    w = find_window(query)
    h = wt.HWND(w["hwnd"])
    if w["minimized"]:
        user32.ShowWindow(h, 9)  # SW_RESTORE
    _key_tap(0x12)  # an Alt tap lets SetForegroundWindow succeed from a background process
    user32.SetForegroundWindow(h)
    time.sleep(0.3)
    return find_window(query)


def ensure_front(query):
    """Refuse to send keystrokes unless `query`'s window is in front: never type into the wrong app."""
    for attempt in range(2):
        w = find_window(query)
        if user32.GetForegroundWindow() == w["hwnd"]:
            return w
        if attempt == 0:
            focus_window(query)
    front = user32.GetForegroundWindow()
    raise Fail(f"stopped before typing: {_window_title(front)!r} is in front, not {query!r}")


# ------------------------------------------------------------- input --------
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


VK = {"ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "enter": 0x0D,
      "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
      "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "insert": 0x2D}
VK.update({f"f{i}": 0x6F + i for i in range(1, 13)})
EXTENDED = {0x2E, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x2D, 0x5B}


def _vk(name):
    n = name.strip().lower()
    if n in VK:
        return VK[n]
    if len(n) == 1 and n.isalnum():
        return ord(n.upper())
    raise Fail(f"unknown key {name!r}")


def _send_key(vk, up=False):
    flags = (0x2 if up else 0) | (0x1 if vk in EXTENDED else 0)
    user32.keybd_event(vk, 0, flags, 0)


def _key_tap(vk):
    _send_key(vk)
    _send_key(vk, up=True)


def _guard(hwnd, done, total, what):
    """Stop the moment another window takes focus, so keystrokes never land in the wrong app."""
    if hwnd and user32.GetForegroundWindow() != hwnd:
        front = _window_title(user32.GetForegroundWindow())
        raise Fail(f"stopped {what} after {done} of {total}: {front!r} took focus")


def press_keys(combo, guard=None):
    """'ctrl+v', 'win+shift+d', 'enter'. Several combos: 'ctrl+a delete'."""
    chords = combo.split()
    for n, chord in enumerate(chords):
        _guard(guard, n, len(chords), "pressing keys")
        vks = [_vk(k) for k in chord.split("+")]
        for vk in vks:
            _send_key(vk)
            time.sleep(0.02)
        for vk in reversed(vks):
            _send_key(vk, up=True)
        time.sleep(0.05)


def type_text(text, cps=12.0, guard=None):
    """Type like a person (cps = characters per second). Newlines press Enter.
    With `guard` (a window handle), checks before every character that it is still in front."""
    delay = 1.0 / cps if cps > 0 else 0
    for n, ch in enumerate(text):
        _guard(guard, n, len(text), "typing")
        if ch == "\n":
            _key_tap(0x0D)
        else:
            for flags in (0x4, 0x6):  # KEYEVENTF_UNICODE down, up
                i = INPUT(type=1)
                i.ki = KEYBDINPUT(0, ord(ch), flags, 0, 0)
                user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
        time.sleep(delay)


def _cursor():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def move_mouse(x, y, dur=0.4):
    x0, y0 = _cursor()
    steps = max(1, int(dur / 0.012))
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)  # ease in/out, so it reads as a human hand on video
        user32.SetCursorPos(int(x0 + (x - x0) * t), int(y0 + (y - y0) * t))
        time.sleep(dur / steps)


def resolve_point(spec):
    """{"x":..,"y":..} screen pixels, or {"window": T, "frac": [fx, fy]} / {"window": T, "px": [dx, dy]}."""
    if "window" in spec:
        x, y, w, h = find_window(spec["window"])["rect"]
        if "frac" in spec:
            fx, fy = spec["frac"]
            return int(x + fx * w), int(y + fy * h)
        dx, dy = spec.get("px", [w // 2, h // 2])
        return int(x + dx), int(y + dy)
    return int(spec["x"]), int(spec["y"])


def click(pt, button="left", double=False, dur=0.4):
    move_mouse(*pt, dur=dur)
    time.sleep(0.08)
    down, up = (0x2, 0x4) if button == "left" else (0x8, 0x10)
    for _ in range(2 if double else 1):
        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.04)
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(0.06)


def drag(a, b, dur=0.8):
    move_mouse(*a, dur=0.3)
    time.sleep(0.1)
    user32.mouse_event(0x2, 0, 0, 0, 0)
    move_mouse(*b, dur=dur)
    time.sleep(0.1)
    user32.mouse_event(0x4, 0, 0, 0, 0)


# ---------------------------------------------------------------- capture ---
def _even(rect):
    x, y, w, h = rect
    return [x, y, w - w % 2, h - h % 2]


def target_rect(window=None, region=None, monitor=None, pad=0):
    if window:
        rect = find_window(window)["rect"]
    elif region:
        rect = list(region)
    else:
        mons = list_monitors()
        idx = monitor or 1
        if not 1 <= idx <= len(mons):
            raise Fail(f"monitor {idx} doesn't exist; there are {len(mons)}")
        rect = mons[idx - 1]["rect"]
    if pad:
        rect = [rect[0] - pad, rect[1] - pad, rect[2] + 2 * pad, rect[3] + 2 * pad]
    vx, vy, vw, vh = core.virtual_screen()  # clamp to the desktop
    x0, y0 = max(rect[0], vx), max(rect[1], vy)
    x1, y1 = min(rect[0] + rect[2], vx + vw), min(rect[1] + rect[3], vy + vh)
    if x1 - x0 < 16 or y1 - y0 < 16:
        raise Fail(f"capture area {rect} is off-screen or too small")
    return _even([x0, y0, x1 - x0, y1 - y0])


class Recording:
    def __init__(self, out_path, rect, fps=30, cursor=True, max_seconds=600):
        self.out, self.rect = Path(out_path), rect
        self.out.parent.mkdir(parents=True, exist_ok=True)
        x, y, w, h = rect
        self.log = open(self.out.with_suffix(".ffmpeg.log"), "wb")
        self.proc = subprocess.Popen(
            [core.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "gdigrab", "-framerate", str(fps),
             "-draw_mouse", "1" if cursor else "0", "-offset_x", str(x), "-offset_y", str(y),
             "-video_size", f"{w}x{h}", "-i", "desktop", "-t", str(max_seconds),
             "-c:v", "libx264rgb", "-preset", "ultrafast", "-crf", "0", str(self.out)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self.log, creationflags=core.CREATE_NO_WINDOW)
        self.t0 = time.monotonic()
        time.sleep(0.4)  # let gdigrab start before anything happens on screen

    def now(self):
        return round(time.monotonic() - self.t0, 2)

    def stop(self):
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log.close()
        if not self.out.exists() or self.out.stat().st_size < 1024:
            raise Fail(f"recording failed: {core.log_tail(self.out.with_suffix('.ffmpeg.log'))}")
        log = self.out.with_suffix(".ffmpeg.log")
        if log.stat().st_size == 0:
            log.unlink()
        return {"file": str(self.out.resolve()), "rect": self.rect, "duration": probe_duration(self.out)}


def probe_duration(path):
    r = subprocess.run([core.FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True, text=True,
                       creationflags=core.CREATE_NO_WINDOW)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    return round(int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]), 2) if m else None


def grab_small(rect, width=192):
    """One downscaled RGB frame of an area, as bytes (for change detection)."""
    x, y, w, h = rect
    hh = max(2, round(h * width / w / 2) * 2)
    r = subprocess.run([core.FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "gdigrab", "-offset_x", str(x),
                        "-offset_y", str(y), "-video_size", f"{w}x{h}", "-i", "desktop", "-frames:v", "1",
                        "-vf", f"scale={width}:{hh}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True, creationflags=core.CREATE_NO_WINDOW)
    return r.stdout


def wait_idle(rect, quiet=2.0, timeout=120.0, threshold=0.002):
    """Block until the area hasn't visibly changed for `quiet` seconds (e.g. an AI reply finished)."""
    start = time.monotonic()
    prev, still_since = grab_small(rect), None
    while time.monotonic() - start < timeout:
        time.sleep(0.25)
        cur = grab_small(rect)
        changed = sum(1 for a, b in zip(prev, cur) if abs(a - b) > 24) / max(1, len(cur))
        prev = cur
        if changed <= threshold:
            still_since = still_since or time.monotonic()
            if time.monotonic() - still_since >= quiet:
                return {"idle": True, "waited": round(time.monotonic() - start, 2)}
        else:
            still_since = None
    return {"idle": False, "waited": round(time.monotonic() - start, 2)}


# ------------------------------------------------------------------ encode --
def parse_range(s):
    a, b = s.split("-", 1) if "-" in s[1:] else (s, "")
    return float(a) if a else 0.0, float(b) if b else None


def parse_speed(s):
    """'3.5-20=4' -> (3.5, 20.0, 4.0)"""
    rng, k = s.split("=")
    a, b = parse_range(rng)
    return a, b, float(k)


def parse_rect(s):
    v = [int(float(p)) for p in s.split(",")]
    if len(v) != 4:
        raise Fail(f"expected x,y,w,h but got {s!r}")
    return v


def find_still(path, min_len=1.5, noise=0.003):
    r = subprocess.run([core.FFMPEG, "-hide_banner", "-i", str(path), "-vf", f"freezedetect=n={noise}:d={min_len}",
                        "-map", "0:v", "-f", "null", "-"], capture_output=True, text=True,
                       creationflags=core.CREATE_NO_WINDOW)
    starts = [float(x) for x in re.findall(r"freeze_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"freeze_end: ([\d.]+)", r.stderr)]
    dur = probe_duration(path)
    return [[round(s, 2), round(ends[i] if i < len(ends) else dur, 2)] for i, s in enumerate(starts)]


def build_filter(src, opts, for_gif):
    dur = probe_duration(src)
    a, b = opts.trim if opts.trim else (0.0, None)
    b = min(b, dur) if b is not None else dur
    speeds = [parse_speed(s) for s in (opts.speed or [])]
    if opts.auto_speed:
        speeds += [(s + 0.3, e - 0.3, opts.auto_speed) for s, e in find_still(src, opts.still_min) if e - s > 0.8]
    # Split [a, b] into normal and sped-up segments.
    segs, t = [], a
    for s, e, k in sorted((max(s, a), min(e if e is not None else b, b), k) for s, e, k in speeds):
        if e - s < 0.2 or s < t:
            continue
        if s > t:
            segs.append((t, s, 1.0))
        segs.append((s, e, k))
        t = e
    if t < b:
        segs.append((t, b, 1.0))
    parts, labels, ot = [], [], 0.0
    for i, (s, e, k) in enumerate(segs):
        parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=(PTS-STARTPTS)/{k}[s{i}]")
        if k != 1.0 and opts.speed_label:
            labels.append((ot, ot + (e - s) / k, k))
        ot += (e - s) / k
    chain = "".join(f"[s{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0"
    for r in opts.redact or []:  # solid boxes: unrecoverable, unlike blur
        x, y, w, h = parse_rect(r)
        chain += f",drawbox=x={x}:y={y}:w={w}:h={h}:color=0x9a9aa3@1:t=fill"
    for r in opts.blur or []:
        x, y, w, h = parse_rect(r)
        rad = max(1, min(10, min(w, h) // 4 - 1))
        chain += (f",split[m{x}_{y}][c{x}_{y}];[c{x}_{y}]crop={w}:{h}:{x}:{y},boxblur={rad}:4[b{x}_{y}];"
                  f"[m{x}_{y}][b{x}_{y}]overlay={x}:{y}")
    width = None
    if opts.crop:
        x, y, w, h = parse_rect(opts.crop)
        chain += f",crop={w}:{h}:{x}:{y}"
    fps = opts.fps or (core.GIF_FPS if for_gif else 30)
    chain += f",fps={fps}"
    maxw = opts.max_width or (core.MAX_GIF_WIDTH if for_gif else 1920)
    chain += f",scale='min({maxw},iw)':-2:flags=lanczos"
    for s, e, k in labels:
        chain += (f",drawtext=fontfile='{ARIAL}':text='{k:g}x  >>':fontsize=h/18:fontcolor=white:"
                  f"box=1:boxcolor=0x000000@0.55:boxborderw=10:x=w-tw-24:y=24:enable='between(t,{s:.2f},{e:.2f})'")
    if for_gif:
        chain += (",split[p1][p2];[p1]palettegen=max_colors=256:stats_mode=full[pal];"
                  "[p2][pal]paletteuse=dither=sierra2_4a:diff_mode=rectangle")
    else:
        chain += ",scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
    return ";".join(parts + [chain]), round(ot, 2), segs


def encode(src, dst, opts, for_gif):
    src, dst = Path(src), Path(dst)
    if not src.exists():
        raise Fail(f"{src} not found")
    dst.parent.mkdir(parents=True, exist_ok=True)
    graph, out_dur, segs = build_filter(src, opts, for_gif)
    tail = ["-loop", "0"] if for_gif else ["-c:v", "libx264", "-crf", "18", "-preset", "slow",
                                           "-movflags", "+faststart"]
    r = subprocess.run([core.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                        "-filter_complex", graph, *tail, str(dst)], capture_output=True, text=True,
                       creationflags=core.CREATE_NO_WINDOW)
    if r.returncode or not dst.exists():
        raise Fail(f"encode failed: {r.stderr.strip()[-800:]}")
    info = subprocess.run([core.FFMPEG, "-hide_banner", "-i", str(dst)], capture_output=True, text=True,
                          creationflags=core.CREATE_NO_WINDOW).stderr
    m = re.search(r", (\d+)x(\d+)", info)
    return {"file": str(dst.resolve()), "bytes": dst.stat().st_size, "size": core.fmt_bytes(dst.stat().st_size),
            "width": int(m[1]) if m else None, "height": int(m[2]) if m else None, "duration": out_dur,
            "segments": [{"from": s, "to": e, "speed": k} for s, e, k in segs]}


def contact_sheet(src, dst, every=None, cols=4, width=480):
    dur = probe_duration(src) or 1
    every = every or max(0.5, round(dur / 16, 2))
    rows = max(1, -(-int(dur / every + 1) // cols))
    label = (f"drawtext=fontfile='{ARIAL}':text='%{{pts\\:hms}}':x=w-tw-8:y=h-th-8:fontsize=h/16:fontcolor=white:"
             f"box=1:boxcolor=0x000000@0.6:boxborderw=6")
    base = f"fps=1/{every},scale={width}:-2"
    for vf in (f"{base},{label},tile={cols}x{rows}:padding=4:color=white",
               f"fps=1/{every},scale={width}:-2,tile={cols}x{rows}:padding=4:color=white"):
        r = subprocess.run([core.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vf", vf,
                            "-frames:v", "1", str(dst)], capture_output=True, text=True,
                           creationflags=core.CREATE_NO_WINDOW)
        if r.returncode == 0:
            return {"file": str(Path(dst).resolve()), "every": every, "frames": int(dur / every) + 1,
                    "labels": "pts" in vf and "drawtext" in vf}
    raise Fail(f"contact sheet failed: {r.stderr.strip()[-500:]}")


def still(src, at, dst):
    r = subprocess.run([core.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(at), "-i", str(src),
                        "-frames:v", "1", str(dst)], capture_output=True, text=True,
                       creationflags=core.CREATE_NO_WINDOW)
    if r.returncode or not Path(dst).exists():
        raise Fail(f"could not extract a frame at {at}s: {r.stderr.strip()[-300:]}")
    return {"file": str(Path(dst).resolve()), "at": at}


# -------------------------------------------------------------------- reel --
FONT = "C\\:/Windows/Fonts/segoeui.ttf"
FONT_SEMIBOLD = "C\\:/Windows/Fonts/seguisb.ttf"


def _ffmpeg(args, what):
    r = subprocess.run([core.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
                       capture_output=True, text=True, creationflags=core.CREATE_NO_WINDOW)
    if r.returncode:
        raise Fail(f"{what} failed: {r.stderr.strip()[-600:]}")


def _text_filter(work, text, size, y, color="white", font=FONT, box=None):
    """drawtext via a text file, so quotes, colons and accents need no escaping."""
    f = Path(work) / f"t{uuid.uuid4().hex[:8]}.txt"
    f.write_text(text, encoding="utf-8")
    path = str(f).replace("\\", "/").replace(":", "\\:")
    boxopt = f":box=1:boxcolor={box}:boxborderw={int(size * 0.5)}" if box else ""
    return (f"drawtext=fontfile='{font}':textfile='{path}':fontsize={size}:fontcolor={color}"
            f":x=(w-tw)/2:y={y}:line_spacing={int(size * 0.25)}{boxopt}")


def reel(spec_path, out_path):
    """Join title cards, clips and stills into one MP4 with captions and crossfades. See SCENARIOS.md."""
    spec_path = Path(spec_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    base = spec_path.parent
    W, H = spec.get("size", [1920, 1080])
    fps = spec.get("fps", 30)
    fade = float(spec.get("transition", 0.4))
    bg, accent = spec.get("background", "0x1b1b1f"), spec.get("accent", "0x4c8bf5")
    work = Path(tempfile.mkdtemp(prefix="gifcap-reel-"))
    fit = (f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,"
           f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={spec.get('letterbox', '0xf3f4f7')},setsar=1")
    caption = lambda text: "," + _text_filter(work, text, int(H / 24), f"h-th-{int(H / 13)}", box="black@0.62")
    segs = []
    try:
        for i, item in enumerate(spec["items"]):
            seg = work / f"seg{i:02d}.mp4"
            enc = ["-r", fps, "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p", "-an", seg]
            if "title" in item:
                secs = float(item.get("seconds", 3))
                vf = (f"drawbox=x=0:y=0:w=iw:h={max(6, H // 120)}:color={accent}:t=fill,"
                      + _text_filter(work, item["title"], int(H / 11), f"(h/2)-th-{int(H / 40)}", font=FONT_SEMIBOLD))
                if item.get("subtitle"):
                    vf += "," + _text_filter(work, item["subtitle"], int(H / 26), f"(h/2)+{int(H / 30)}",
                                             color="0xc9cbd3")
                _ffmpeg(["-f", "lavfi", "-i", f"color=c={bg}:s={W}x{H}:d={secs}:r={fps}", "-vf", vf, *enc],
                        f"title card {i + 1}")
            elif "clip" in item:
                src = base / item["clip"]
                opts = argparse.Namespace(
                    trim=parse_range(item["trim"]) if item.get("trim") else None, speed=item.get("speed"),
                    auto_speed=item.get("auto_speed"), still_min=1.5, speed_label=item.get("speed_label", True),
                    redact=item.get("redact"), blur=item.get("blur"), crop=item.get("crop"), fps=fps,
                    max_width=10000)
                mid = work / f"clip{i:02d}.mp4"
                encode(src, mid, opts, for_gif=False)
                vf = fit + (caption(item["caption"]) if item.get("caption") else "")
                _ffmpeg(["-i", mid, "-vf", vf, *enc], f"clip {i + 1}")
            elif "image" in item:
                secs = float(item.get("seconds", 3))
                vf = fit + (caption(item["caption"]) if item.get("caption") else "")
                _ffmpeg(["-loop", "1", "-t", secs, "-i", base / item["image"], "-vf", vf, *enc], f"image {i + 1}")
            else:
                raise Fail(f"reel item {i + 1} needs one of: title, clip, image")
            segs.append((seg, probe_duration(seg)))
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        inputs = [x for s, _ in segs for x in ("-i", s)]
        if len(segs) == 1 or fade <= 0:
            graph = "".join(f"[{i}:v]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[v]"
        else:
            parts, prev, t = [], "[0:v]", 0.0
            for i in range(1, len(segs)):
                t += segs[i - 1][1] - fade
                label = "[v]" if i == len(segs) - 1 else f"[x{i}]"
                parts.append(f"{prev}[{i}:v]xfade=transition=fade:duration={fade}:offset={t:.3f}{label}")
                prev = label
            graph = ";".join(parts)
        _ffmpeg([*inputs, "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-crf", "18", "-preset", "slow",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", out], "joining the reel")
        return {"file": str(out.resolve()), "duration": probe_duration(out), "size": core.fmt_bytes(out.stat().st_size),
                "items": len(segs), "width": W, "height": H}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- scenario --
ACTIONS = ("focus", "record_start", "record_stop", "click", "double_click", "right_click", "drag", "move",
           "type", "keys", "wait", "wait_idle", "mark", "screenshot")


def run_scenario(path, out_dir=None):
    """Run a scenario file. See SCENARIOS.md for the format."""
    path = Path(path)
    sc = json.loads(path.read_text(encoding="utf-8"))
    name = sc.get("name") or path.stem
    out_dir = Path(out_dir or path.parent / "takes")
    out_dir.mkdir(parents=True, exist_ok=True)
    rec, clip, clips, log = None, None, [], []
    target = None  # window that keystrokes are allowed to go to (set by `focus`)
    t_start = time.monotonic()
    try:
        for i, step in enumerate(sc["steps"]):
            if len(step) != 1 or next(iter(step)) not in ACTIONS:
                raise Fail(f"step {i + 1}: expected one of {', '.join(ACTIONS)}; got {json.dumps(step)}")
            action, arg = next(iter(step.items()))
            entry = {"step": i + 1, "action": action, "at": round(time.monotonic() - t_start, 2)}
            if rec:
                entry["t"] = rec.now()  # seconds into the current clip
            if action == "focus":
                focus_window(arg)
                target = arg
            elif action == "record_start":
                if rec:
                    raise Fail(f"step {i + 1}: already recording; add record_stop first")
                arg = arg or {}
                clip_name = arg.get("name") or (name if not clips else f"{name}-{len(clips) + 1:02d}")
                clip = {"name": clip_name, "marks": {}}
                rect = target_rect(arg.get("window"), arg.get("region"), arg.get("monitor"), arg.get("pad", 0))
                rec = Recording(out_dir / f"{clip['name']}.mkv", rect, arg.get("fps", 30), arg.get("cursor", True))
                entry["t"] = 0.0
            elif action == "record_stop":
                if not rec:
                    raise Fail(f"step {i + 1}: not recording")
                time.sleep(float((arg or {}).get("after", 0.6)))
                clip.update(rec.stop())
                clips.append(clip)
                rec = clip = None
            elif action in ("click", "double_click", "right_click"):
                click(resolve_point(arg), button="right" if action == "right_click" else "left",
                      double=action == "double_click", dur=float(arg.get("move", 0.4)))
                if "window" in arg:
                    target = arg["window"]  # clicking into a window is choosing where to type next
            elif action == "drag":
                drag(resolve_point(arg["from"]), resolve_point(arg["to"]), float(arg.get("duration", 0.8)))
            elif action == "move":
                move_mouse(*resolve_point(arg), dur=float(arg.get("duration", 0.5)))
            elif action == "type":
                text, cps = (arg, 12.0) if isinstance(arg, str) else (arg["text"], float(arg.get("cps", 12)))
                window = None if isinstance(arg, str) else arg.get("window")
                if not (window or target):
                    raise Fail(f"step {i + 1}: add a `focus` step (or click into a window) before typing, "
                               "so keystrokes can't land in the wrong app")
                type_text(text, cps, ensure_front(window or target)["hwnd"])
            elif action == "keys":
                combo, window = (arg, None) if isinstance(arg, str) else (arg["keys"], arg.get("window"))
                guard = None
                if not combo.lower().startswith("win+"):  # global hotkeys may go anywhere
                    if not (window or target):
                        raise Fail(f"step {i + 1}: add a `focus` step (or click into a window) before keys")
                    guard = ensure_front(window or target)["hwnd"]
                press_keys(combo, guard)
            elif action == "wait":
                time.sleep(float(arg))
            elif action == "wait_idle":
                arg = arg or {}
                rect = target_rect(arg.get("window"), arg.get("region"), arg.get("monitor")) \
                    if any(k in arg for k in ("window", "region", "monitor")) else (rec.rect if rec else target_rect())
                entry.update(wait_idle(rect, float(arg.get("quiet", 2.0)), float(arg.get("timeout", 120))))
            elif action == "mark":
                if not rec:
                    raise Fail(f"step {i + 1}: marks only make sense while recording")
                clip["marks"][str(arg)] = rec.now()
            elif action == "screenshot":
                arg = arg if isinstance(arg, dict) else {"name": arg}
                rect = target_rect(arg.get("window"), arg.get("region"), arg.get("monitor"))
                shot = out_dir / f"{arg.get('name', f'{name}-step{i + 1}')}.png"
                x, y, w, h = rect
                subprocess.run([core.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "gdigrab",
                                "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}", "-i", "desktop",
                                "-frames:v", "1", str(shot)], creationflags=core.CREATE_NO_WINDOW)
                entry["file"] = str(shot.resolve())
            log.append(entry)
        if rec:  # forgot record_stop: finish the clip anyway
            clip.update(rec.stop())
            clips.append(clip)
            rec = None
    finally:
        if rec:
            rec.proc.kill()
            rec.log.close()
    result = {"scenario": name, "clips": clips, "steps": log}
    (out_dir / f"{name}.timeline.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    result["timeline"] = str((out_dir / f"{name}.timeline.json").resolve())
    return result


# -------------------------------------------------------- background record --
def record_cmd(a):
    rect = target_rect(a.window, parse_rect(a.region) if a.region else None, a.monitor, a.pad)
    if a.background:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        rid = uuid.uuid4().hex[:8]
        stopfile = STATE_DIR / f"{rid}.stop"
        args = [a.out, "--region", ",".join(map(str, rect)), "--fps", str(a.fps), "--max", str(a.max),
                "--_stopfile", str(stopfile)] + ([] if a.cursor else ["--no-cursor"])
        exe = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]
        proc = subprocess.Popen(exe + ["record"] + args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=core.CREATE_NO_WINDOW | 0x00000200)  # + new process group
        (STATE_DIR / f"{rid}.json").write_text(json.dumps({"id": rid, "pid": proc.pid, "out": str(Path(a.out).resolve()),
                                                           "rect": rect, "started": time.time()}))
        return {"id": rid, "recording": True, "out": str(Path(a.out).resolve()), "rect": rect,
                "stop_with": "gifcap stop"}
    rec = Recording(a.out, rect, a.fps, a.cursor, a.max)
    try:
        deadline = rec.t0 + a.duration if a.duration else None
        stopfile = Path(a._stopfile) if a._stopfile else None
        while rec.proc.poll() is None:
            if deadline and time.monotonic() >= deadline:
                break
            if stopfile and stopfile.exists():
                break
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    result = rec.stop()
    if a._stopfile:
        sf = Path(a._stopfile)
        (sf.with_suffix(".done")).write_text(json.dumps(result))
        sf.unlink(missing_ok=True)
    return result


def stop_cmd(a):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    active = [json.loads(p.read_text()) for p in STATE_DIR.glob("*.json")]
    if a.id:
        active = [r for r in active if r["id"] == a.id]
    for r in active:
        (STATE_DIR / f"{r['id']}.stop").write_text("stop")
    results = []
    for r in active:
        done = STATE_DIR / f"{r['id']}.done"
        for _ in range(200):
            if done.exists():
                break
            time.sleep(0.1)
        results.append(json.loads(done.read_text()) if done.exists() else {"id": r["id"], "error": "did not stop"})
        for suffix in (".json", ".done", ".stop"):
            (STATE_DIR / f"{r['id']}{suffix}").unlink(missing_ok=True)
    return {"stopped": results}


# --------------------------------------------------------------------- cli --
def main(argv=None):
    p = argparse.ArgumentParser(prog="gifcap", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"gifcap {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("windows", help="list visible windows")
    s.add_argument("--all", action="store_true", help="include untitled and tiny windows")
    sub.add_parser("monitors", help="list monitors")

    s = sub.add_parser("record", help="record a window, region or monitor to lossless .mkv")
    s.add_argument("out")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--window", help="title or process name (substring)")
    g.add_argument("--region", help="x,y,w,h in screen pixels")
    g.add_argument("--monitor", type=int, help="1 = primary (see `gifcap monitors`)")
    s.add_argument("--pad", type=int, default=0, help="grow the area by N pixels each side")
    s.add_argument("--duration", type=float, help="stop after N seconds")
    s.add_argument("--background", action="store_true", help="return immediately; end with `gifcap stop`")
    s.add_argument("--fps", type=int, default=30)
    s.add_argument("--max", type=float, default=600, help="hard limit in seconds (default 600)")
    s.add_argument("--no-cursor", dest="cursor", action="store_false")
    s.add_argument("--_stopfile", help=argparse.SUPPRESS)

    s = sub.add_parser("stop", help="stop background recordings")
    s.add_argument("--id")

    s = sub.add_parser("run", help="run a scenario file (see SCENARIOS.md)")
    s.add_argument("scenario")
    s.add_argument("--out-dir", help="where clips go (default: <scenario folder>/takes)")

    s = sub.add_parser("focus", help="bring a window to the front")
    s.add_argument("window")
    for name in ("click", "double-click", "right-click", "move"):
        s = sub.add_parser(name, help=f"{name} at a point")
        s.add_argument("--window")
        s.add_argument("--frac", help="fx,fy as a fraction of the window (0.5,0.5 = centre)")
        s.add_argument("--px", help="dx,dy pixels from the window's top-left")
        s.add_argument("--at", help="x,y screen pixels")
    s = sub.add_parser("drag", help="drag between two screen points")
    s.add_argument("start", help="x,y")
    s.add_argument("end", help="x,y")
    s.add_argument("--duration", type=float, default=0.8)
    s = sub.add_parser("type", help="type text like a person")
    s.add_argument("text")
    s.add_argument("--cps", type=float, default=12.0, help="characters per second")
    s.add_argument("--window", help="type only while this window is in front")
    s.add_argument("--anywhere", action="store_true", help="type into whatever is in front (no check)")
    s = sub.add_parser("keys", help="press keys, e.g. 'ctrl+v' or 'ctrl+a delete'")
    s.add_argument("combo")
    s.add_argument("--window", help="press only while this window is in front")
    s.add_argument("--anywhere", action="store_true", help="press into whatever is in front (no check)")

    s = sub.add_parser("wait-idle", help="wait until an area stops changing")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--window")
    g.add_argument("--region")
    g.add_argument("--monitor", type=int)
    s.add_argument("--quiet", type=float, default=2.0, help="seconds without change (default 2)")
    s.add_argument("--timeout", type=float, default=120.0)

    s = sub.add_parser("idle", help="find still stretches in a recording")
    s.add_argument("input")
    s.add_argument("--min", type=float, default=1.5, help="minimum length in seconds")

    for fmt in ("gif", "mp4"):
        s = sub.add_parser(fmt, help=f"encode a recording as {fmt.upper()}")
        s.add_argument("input")
        s.add_argument("output")
        s.add_argument("--trim", type=parse_range, help="START-END seconds, e.g. 2.1-9.5 (or 2.1-)")
        s.add_argument("--speed", action="append", help="speed up a range: START-END=FACTOR, e.g. 4-19=4 (repeatable)")
        s.add_argument("--auto-speed", type=float, metavar="FACTOR", help="speed up still stretches by FACTOR")
        s.add_argument("--still-min", type=float, default=1.5, help="shortest still stretch for --auto-speed")
        s.add_argument("--no-speed-label", dest="speed_label", action="store_false",
                       help="don't show '4x >>' while sped up")
        s.add_argument("--redact", action="append", help="x,y,w,h solid box, in recording pixels (repeatable)")
        s.add_argument("--blur", action="append", help="x,y,w,h blur, in recording pixels (repeatable)")
        s.add_argument("--crop", help="x,y,w,h in recording pixels")
        s.add_argument("--fps", type=int)
        s.add_argument("--max-width", type=int)

    s = sub.add_parser("sheet", help="contact sheet of a recording or GIF, with timestamps")
    s.add_argument("input")
    s.add_argument("output")
    s.add_argument("--every", type=float, help="seconds between frames (default: ~16 frames)")
    s.add_argument("--cols", type=int, default=4)
    s.add_argument("--width", type=int, default=480)

    s = sub.add_parser("still", help="save one frame")
    s.add_argument("input")
    s.add_argument("output")
    s.add_argument("--at", type=float, default=0.0)

    s = sub.add_parser("reel", help="join title cards, clips and stills into one MP4 (see SCENARIOS.md)")
    s.add_argument("spec")
    s.add_argument("output")

    s = sub.add_parser("copy", help="copy a GIF to the clipboard")
    s.add_argument("file")

    a = p.parse_args(argv)
    try:
        if not core.FFMPEG and a.cmd not in ("windows", "monitors", "focus", "click", "double-click",
                                             "right-click", "move", "drag", "type", "keys"):
            raise Fail("ffmpeg not found; install GIF Capture or `winget install Gyan.FFmpeg`")
        if a.cmd == "windows":
            out(list_windows(a.all))
        elif a.cmd == "monitors":
            out(list_monitors())
        elif a.cmd == "record":
            out(record_cmd(a))
        elif a.cmd == "stop":
            out(stop_cmd(a))
        elif a.cmd == "run":
            out(run_scenario(a.scenario, a.out_dir))
        elif a.cmd == "focus":
            out(focus_window(a.window))
        elif a.cmd in ("click", "double-click", "right-click", "move"):
            spec = {"window": a.window} if a.window else {}
            if a.frac:
                spec["frac"] = [float(v) for v in a.frac.split(",")]
            elif a.px:
                spec["px"] = [float(v) for v in a.px.split(",")]
            elif a.at:
                x, y = a.at.split(",")
                spec = {"x": x, "y": y}
            if not spec:
                raise Fail("give --window (with --frac/--px) or --at")
            pt = resolve_point(spec)
            if a.cmd == "move":
                move_mouse(*pt)
            else:
                click(pt, "right" if a.cmd == "right-click" else "left", a.cmd == "double-click")
            out({"done": a.cmd, "at": pt})
        elif a.cmd == "drag":
            s0 = [int(v) for v in a.start.split(",")]
            s1 = [int(v) for v in a.end.split(",")]
            drag(s0, s1, a.duration)
            out({"done": "drag", "from": s0, "to": s1})
        elif a.cmd in ("type", "keys") and not (
                a.window or a.anywhere or (a.cmd == "keys" and a.combo.lower().startswith("win+"))):
            raise Fail(f"`gifcap {a.cmd}` needs --window TITLE (or --anywhere) so keystrokes can't land in the wrong app")
        elif a.cmd == "type":
            type_text(a.text, a.cps, ensure_front(a.window)["hwnd"] if a.window else None)
            out({"done": "type", "chars": len(a.text)})
        elif a.cmd == "keys":
            press_keys(a.combo, ensure_front(a.window)["hwnd"] if a.window else None)
            out({"done": "keys", "keys": a.combo})
        elif a.cmd == "wait-idle":
            out(wait_idle(target_rect(a.window, parse_rect(a.region) if a.region else None, a.monitor),
                          a.quiet, a.timeout))
        elif a.cmd == "idle":
            out({"input": a.input, "duration": probe_duration(a.input), "still": find_still(a.input, a.min)})
        elif a.cmd in ("gif", "mp4"):
            out(encode(a.input, a.output, a, for_gif=a.cmd == "gif"))
        elif a.cmd == "sheet":
            out(contact_sheet(a.input, a.output, a.every, a.cols, a.width))
        elif a.cmd == "still":
            out(still(a.input, a.at, a.output))
        elif a.cmd == "reel":
            out(reel(a.spec, a.output))
        elif a.cmd == "copy":
            core.copy_gif_to_clipboard(Path(a.file))
            out({"copied": str(Path(a.file).resolve())})
    except Fail as e:
        out({"error": str(e)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
