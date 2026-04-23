#!/usr/bin/env python3
"""
p4Ons — terminal music player for Apple Music and local files.
macOS only. No external dependencies.
"""

import os, sys, subprocess, threading, time, termios, tty, signal, json, random
from pathlib import Path

BANNER = r"""
        _  _    ___            
 _ __ | || |  / _ \ _ __  ___ 
| '_ \| || |_| | | | '_ \/ __|
| |_) |__   _| |_| | | | \__ \
| .__/   |_|  \___/|_| |_|___/
|_|
"""

C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "green": "\033[92m", "cyan": "\033[96m", "yellow": "\033[93m",
    "red": "\033[91m", "magenta": "\033[95m", "blue": "\033[94m",
    "white": "\033[97m", "grey": "\033[90m",
}

MUSIC_EXTS = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".wma"}
SCAN_DIRS  = [Path.home() / "Downloads", Path.home() / "Music", Path.home() / "Desktop"]
PLAYLISTS_FILE = Path.home() / ".p4ons_playlists.json"

# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────

def term_width() -> int:
    try:    return os.get_terminal_size().columns
    except: return 80

def clr():
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()

def trunc(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"

def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"

def progress_bar(pos: float, dur: float, width: int = 32) -> str:
    pct  = min(pos / dur, 1.0) if dur > 0 else 0.0
    done = int(pct * width)
    rest = width - done - 1
    return (C["cyan"] + "━" * done + C["reset"] +
            (C["cyan"] + "●" + C["reset"] if dur > 0 else "") +
            C["grey"] + "─" * max(rest, 0) + C["reset"])

# ─────────────────────────────────────────────────────────────────────────────
# afinfo — read real duration from file metadata
# ─────────────────────────────────────────────────────────────────────────────

def get_duration(path: Path) -> float:
    try:
        r = subprocess.run(["afinfo", str(path)], capture_output=True, text=True, timeout=4)
        for line in r.stdout.splitlines():
            if "duration" in line.lower():
                for token in line.split():
                    try:
                        v = float(token)
                        if v > 0:
                            return v
                    except ValueError:
                        pass
    except Exception:
        pass
    return 0.0

# ─────────────────────────────────────────────────────────────────────────────
# Apple Music via AppleScript
# ─────────────────────────────────────────────────────────────────────────────

def _osa(script: str, timeout: int = 5) -> str:
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""

def am_check() -> bool:
    # Try to query Music.app — this will launch it if not running
    result = _osa('tell application "Music" to return name', timeout=10)
    return bool(result)

def am_state()   -> str:  return _osa('tell application "Music" to player state as string')
def am_play()    -> None: _osa('tell application "Music" to play')
def am_pause()   -> None: _osa('tell application "Music" to pause')
def am_next()    -> None: _osa('tell application "Music" to next track')
def am_prev()    -> None: _osa('tell application "Music" to previous track')

def am_set_vol(v: int) -> None: _osa(f'tell application "Music" to set sound volume to {v}')
def am_get_vol() -> int:
    v = _osa('tell application "Music" to sound volume')
    try:    return int(v)
    except: return 50

def am_position() -> float:
    v = _osa('tell application "Music" to player position')
    try:    return float(v)
    except: return 0.0

def am_set_position(sec: float) -> None:
    _osa(f'tell application "Music" to set player position to {max(0.0, sec)}')

def am_track_info() -> dict:
    raw = _osa('''
    tell application "Music"
        if player state is not stopped then
            set t to current track
            return (name of t) & "|||" & (artist of t) & "|||" & (album of t) & "|||" & ((duration of t) as string)
        end if
        return "|||||||0"
    end tell''')
    parts = (raw + "||||").split("|||")
    try:    dur = float(parts[3])
    except: dur = 0.0
    return {"name": parts[0] or "—", "artist": parts[1] or "—",
            "album": parts[2] or "—", "duration": dur}

def am_search(query: str) -> list:
    safe = query.replace('"', '\\"')
    raw = _osa(f'''
    tell application "Music"
        set results to search playlist "Library" for "{safe}"
        set out to ""
        repeat with t in results
            set out to out & (name of t) & "|||" & (artist of t) & "|||" & (album of t) & "\\n"
        end repeat
        return out
    end tell''')
    tracks = []
    for line in raw.strip().split("\n"):
        parts = line.split("|||")
        if len(parts) >= 2 and parts[0]:
            tracks.append({"name": parts[0], "artist": parts[1] if len(parts) > 1 else "",
                           "album": parts[2] if len(parts) > 2 else ""})
    return tracks[:24]

def am_play_track(name: str) -> None:
    safe = name.replace('"', '\\"')
    _osa(f'''
    tell application "Music"
        set results to search playlist "Library" for "{safe}"
        if results is not {{}} then play item 1 of results
    end tell''')

def am_playlists() -> list:
    raw = _osa('''
    tell application "Music"
        set out to ""
        repeat with pl in playlists
            set out to out & (name of pl) & "\\n"
        end repeat
        return out
    end tell''')
    return [l for l in raw.strip().split("\n") if l.strip()]

def am_play_playlist(name: str) -> None:
    safe = name.replace('"', '\\"')
    _osa(f'tell application "Music" to play playlist "{safe}"')

# ─────────────────────────────────────────────────────────────────────────────
# Local player — afplay with accurate position via monotonic clock
# ─────────────────────────────────────────────────────────────────────────────

class LocalPlayer:
    def __init__(self):
        self._proc    = None
        self._lock    = threading.Lock()
        self.current  : Path | None = None
        self.duration : float = 0.0
        self.paused   : bool  = False
        self._start_t : float = 0.0   # monotonic time when play/resume began
        self._accum   : float = 0.0   # seconds banked before last pause
        self._offset  : float = 0.0   # seek offset (passed to afplay -t)

    def play(self, path: Path, seek_to: float = 0.0):
        self.stop()
        with self._lock:
            self.current  = path
            self.duration = get_duration(path)
            self.paused   = False
            self._offset  = seek_to
            self._accum   = 0.0
            self._start_t = time.monotonic()
            cmd = ["afplay", str(path)]
            if seek_to > 0:
                cmd += ["-t", str(seek_to)]
            self._proc = subprocess.Popen(cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def toggle_pause(self):
        with self._lock:
            if not self._proc:
                return
            if self.paused:
                os.kill(self._proc.pid, signal.SIGCONT)
                self._start_t = time.monotonic()
                self.paused   = False
            else:
                os.kill(self._proc.pid, signal.SIGSTOP)
                self._accum  += time.monotonic() - self._start_t
                self.paused   = True

    def seek(self, delta: float):
        if not self.current:
            return
        new_pos = max(0.0, min(self.position() + delta, max(self.duration - 1, 0)))
        path    = self.current
        was_paused = self.paused
        self.play(path, seek_to=new_pos)
        if was_paused:
            time.sleep(0.05)
            self.toggle_pause()

    def stop(self):
        with self._lock:
            if self._proc:
                try:
                    if self.paused:
                        os.kill(self._proc.pid, signal.SIGCONT)
                    self._proc.terminate()
                    self._proc.wait(timeout=2)
                except Exception:
                    pass
                self._proc = None
            self.current  = None
            self.duration = 0.0
            self.paused   = False
            self._accum   = 0.0
            self._offset  = 0.0

    def position(self) -> float:
        if not self._proc:
            return 0.0
        elapsed = self._accum
        if not self.paused:
            elapsed += time.monotonic() - self._start_t
        return self._offset + elapsed

    def is_done(self) -> bool:
        with self._lock:
            return bool(self._proc and self._proc.poll() is not None)

    @property
    def playing(self) -> bool:
        return self._proc is not None

# ─────────────────────────────────────────────────────────────────────────────
# Queue
# ─────────────────────────────────────────────────────────────────────────────

class Queue:
    def __init__(self):
        self.tracks  : list[Path] = []
        self.index   : int        = 0
        self.shuffle : bool       = False
        self._order  : list[int]  = []

    def set(self, tracks: list[Path], start: int = 0):
        self.tracks = list(tracks)
        self.index  = start
        self._rebuild(start)

    def _rebuild(self, anchor: int):
        n = len(self.tracks)
        if self.shuffle and n:
            rest = list(range(n))
            if anchor in rest:
                rest.remove(anchor)
            random.shuffle(rest)
            self._order = [anchor] + rest
        else:
            self._order = list(range(n))

    def current(self) -> Path | None:
        if not self.tracks or self.index >= len(self.tracks):
            return None
        real = self._order[self.index] if self.shuffle and self._order else self.index
        return self.tracks[real]

    def _real_index(self) -> int:
        if self.shuffle and self._order:
            return self._order[self.index]
        return self.index

    def next(self) -> Path | None:
        if self.index < len(self.tracks) - 1:
            self.index += 1
            return self.current()
        return None

    def prev(self) -> Path | None:
        if self.index > 0:
            self.index -= 1
            return self.current()
        return None

    def toggle_shuffle(self):
        self.shuffle = not self.shuffle
        self._rebuild(self._real_index())

    def add(self, path: Path):
        self.tracks.append(path)
        self._order.append(len(self.tracks) - 1)

    def remove(self, track_idx: int):
        if 0 <= track_idx < len(self.tracks):
            self.tracks.pop(track_idx)
            self._order = list(range(len(self.tracks)))
            self.index  = min(self.index, max(0, len(self.tracks) - 1))

# ─────────────────────────────────────────────────────────────────────────────
# Playlist persistence
# ─────────────────────────────────────────────────────────────────────────────

def load_playlists() -> dict:
    try:
        if PLAYLISTS_FILE.exists():
            data = json.loads(PLAYLISTS_FILE.read_text())
            return {k: [Path(p) for p in v] for k, v in data.items()}
    except Exception:
        pass
    return {}

def save_playlists(pls: dict):
    try:
        PLAYLISTS_FILE.write_text(
            json.dumps({k: [str(p) for p in v] for k, v in pls.items()}, indent=2))
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Raw keyboard input
# ─────────────────────────────────────────────────────────────────────────────

def read_key() -> str:
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 in ("5", "6"):
                    sys.stdin.read(1)   # consume trailing ~
                    return "pgup" if ch3 == "5" else "pgdn"
                if ch3 == "3":
                    sys.stdin.read(1)
                    return "delete"
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(ch3, "")
            return "esc"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ─────────────────────────────────────────────────────────────────────────────
# Mode constants
# ─────────────────────────────────────────────────────────────────────────────

M_MAIN, M_LOCAL, M_QUEUE, M_PLAYLISTS, M_PL_NAME, M_AM, M_AM_SEARCH, M_AM_PL = range(8)

# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

class App:
    def __init__(self):
        self.mode       = M_MAIN
        self.prev_mode  = M_MAIN

        # local playback
        self.player     = LocalPlayer()
        self.all_files  : list[Path] = []
        self.file_cursor = 0
        self.file_offset = 0
        self.queue      = Queue()
        self.repeat     = False

        # local playlists
        self.playlists  : dict[str, list[Path]] = {}
        self.pl_cursor  = 0
        self.pl_input   = ""

        # AM
        self.am_ok      = False
        self.am_vol     = 50
        self.am_pls     : list[str] = []
        self.am_pl_cursor = 0

        # AM search
        self.sq         = ""   # search query
        self.sr         : list[dict] = []
        self.sc         = 0    # search cursor

        # UI
        self.running    = True
        self.message    = ""
        self.msg_t      = 0.0

    # ── Boot ──────────────────────────────────────────────────────────────

    def boot(self):
        clr()
        print(C["cyan"] + BANNER + C["reset"])
        print(f"  {C['grey']}Scanning music files…{C['reset']}", flush=True)
        self._scan()
        self.playlists = load_playlists()
        self.queue.set(self.all_files, 0)

        print(f"  {C['grey']}Connecting to Apple Music…{C['reset']}", flush=True)
        self.am_ok = am_check()
        if self.am_ok:
            self.am_vol = am_get_vol()

        threading.Thread(target=self._watcher, daemon=True).start()

    def _scan(self):
        seen, files = set(), []
        for d in SCAN_DIRS:
            if d.exists():
                for ext in MUSIC_EXTS:
                    for f in d.rglob(f"*{ext}"):
                        if f not in seen:
                            seen.add(f); files.append(f)
        self.all_files = sorted(files, key=lambda p: p.name.lower())

    def _watcher(self):
        while self.running:
            if self.mode in (M_LOCAL, M_QUEUE) and self.player.is_done():
                if self.repeat and self.queue.current():
                    self.player.play(self.queue.current())
                else:
                    nxt = self.queue.next()
                    if nxt:
                        self.file_cursor = self.queue.index
                        self.player.play(nxt)
            time.sleep(0.5)

    # ── Message ───────────────────────────────────────────────────────────

    def msg(self, text: str):
        self.message = text
        self.msg_t   = time.time()

    # ─────────────────────────────────────────────────────────────────────
    # Render
    # ─────────────────────────────────────────────────────────────────────

    def render(self):
        clr()
        w = term_width()
        print(C["cyan"] + C["bold"] + BANNER + C["reset"])

        {M_MAIN:      self._r_main,
         M_LOCAL:     self._r_local,
         M_QUEUE:     self._r_queue,
         M_PLAYLISTS: self._r_playlists,
         M_PL_NAME:   self._r_pl_name,
         M_AM:        self._r_am,
         M_AM_SEARCH: self._r_am_search,
         M_AM_PL:     self._r_am_pl,
         }.get(self.mode, self._r_main)(w)

        if self.message and time.time() - self.msg_t < 3:
            print(f"\n  {C['yellow']}→ {self.message}{C['reset']}")
        print(f"\n{C['grey']}{'─' * min(w, 72)}{C['reset']}")

    # ── Main ──────────────────────────────────────────────────────────────

    def _r_main(self, w):
        print(f"  {C['white']}{C['bold']}Choose a source{C['reset']}\n")
        print(f"  {C['green']}{C['bold']}[1]{C['reset']}  {C['green']}{C['bold']}Local Files{C['reset']}")
        print(f"       {C['grey']}Downloads · Music · Desktop  ({len(self.all_files)} tracks){C['reset']}\n")
        col = C["magenta"] if self.am_ok else C["grey"]
        status = "Connected" if self.am_ok else "not detected — is Music.app installed?"
        print(f"  {col}{C['bold']}[2]{C['reset']}  {col}{C['bold']}Apple Music{C['reset']}")
        print(f"       {C['grey']}{status}{C['reset']}\n")
        print(f"  {C['grey']}[q] quit{C['reset']}")

    # ── Local browser ──────────────────────────────────────────────────────

    def _r_local(self, w):
        self._r_np(w)
        self._r_filelist(w, self.all_files, self.file_cursor, visible=10)
        shuf = C["cyan"] if self.queue.shuffle else C["grey"]
        rep  = C["cyan"] if self.repeat        else C["grey"]
        print(f"\n  {shuf}[z] shuf{C['reset']}  {rep}[r] rep{C['reset']}  "
              f"{C['grey']}[q] queue  [p] playlists  "
              f"enter=play  spc=pause  ←→=seek±10s  ,/.=prev/next  b=back{C['reset']}")

    # ── Queue ─────────────────────────────────────────────────────────────

    def _r_queue(self, w):
        self._r_np(w)
        tracks = self.queue.tracks
        total  = len(tracks)
        print(f"  {C['white']}{C['bold']}Queue{C['reset']}  {C['grey']}({total} tracks){C['reset']}\n")
        visible = 12
        qi  = self.queue.index
        off = max(0, min(self.file_cursor - visible // 2, total - visible))
        for i in range(off, min(off + visible, total)):
            t = tracks[i]
            playing = (i == qi and self.player.playing)
            sel     = (i == self.file_cursor)
            if playing:
                m = f"{C['cyan']}{C['bold']}▶ "
                n = f"{C['white']}{C['bold']}{trunc(t.stem, w-12)}{C['reset']}"
            elif sel:
                m = f"{C['yellow']}› "
                n = f"{C['yellow']}{trunc(t.stem, w-12)}{C['reset']}"
            else:
                m = f"{C['grey']}  "
                n = f"{C['grey']}{trunc(t.stem, w-12)}{C['reset']}"
            print(f"  {m}{n}")
        print(f"\n  {C['grey']}enter=play  del=remove  ,/.=prev/next  b=back{C['reset']}")

    # ── Local playlists ────────────────────────────────────────────────────

    def _r_playlists(self, w):
        names = list(self.playlists.keys())
        print(f"  {C['white']}{C['bold']}Local Playlists{C['reset']}  {C['grey']}({len(names)}){C['reset']}\n")
        if not names:
            print(f"  {C['grey']}No playlists yet.{C['reset']}\n")
        else:
            visible = 14
            off = max(0, min(self.pl_cursor - visible // 2, len(names) - visible))
            for i in range(off, min(off + visible, len(names))):
                name = names[i]; count = len(self.playlists[name])
                sel  = (i == self.pl_cursor)
                m = f"{C['cyan']}{C['bold']}▶ " if sel else f"{C['grey']}  "
                n = (f"{C['white']}{C['bold']}{trunc(name, w-20)}{C['reset']}  {C['grey']}{count} tracks{C['reset']}"
                     if sel else f"{C['grey']}{trunc(name, w-20)}  {count} tracks{C['reset']}")
                print(f"  {m}{n}")
        print(f"\n  {C['grey']}enter=play  n=new  a=add current  del=delete  b=back{C['reset']}")

    def _r_pl_name(self, w):
        print(f"  {C['white']}{C['bold']}Playlist name:{C['reset']} "
              f"{C['cyan']}{self.pl_input}█{C['reset']}")
        print(f"\n  {C['grey']}enter=confirm  esc=cancel{C['reset']}")

    # ── Apple Music ────────────────────────────────────────────────────────

    def _r_am(self, w):
        state = am_state()
        info  = am_track_info()
        icon  = {"playing": "▶", "paused": "⏸"}.get(state, "■")
        col   = C["magenta"] if state == "playing" else C["grey"]
        print(f"  {col}{C['bold']}Apple Music  {icon} {state.upper()}{C['reset']}\n")
        if info["name"] != "—":
            print(f"  {C['white']}{C['bold']}{trunc(info['name'], w-6)}{C['reset']}")
            print(f"  {C['cyan']}{info['artist']}{C['reset']}  {C['grey']}{info['album']}{C['reset']}")
            pos, dur = am_position(), info["duration"]
            print(f"\n  {progress_bar(pos, dur, 38)}  "
                  f"{C['cyan']}{fmt_time(pos)}{C['reset']} / {C['grey']}{fmt_time(dur)}{C['reset']}")
        else:
            print(f"  {C['grey']}Nothing playing{C['reset']}")
        print(f"\n  {C['grey']}Vol: {self.am_vol}%  [-/+]{C['reset']}")
        print(f"\n  {C['grey']}spc=pause  ←→=seek±15s  ,/.=prev/next  s=search  p=playlists  b=back{C['reset']}")

    def _r_am_search(self, w):
        print(f"  {C['magenta']}{C['bold']}Search Apple Music Library{C['reset']}\n")
        print(f"  {C['grey']}›{C['reset']} {C['white']}{self.sq}{C['cyan']}█{C['reset']}\n")
        for i, t in enumerate(self.sr):
            sel = (i == self.sc)
            m   = f"{C['cyan']}{C['bold']}▶ " if sel else f"{C['grey']}  "
            n   = (f"{C['white']}{C['bold']}{trunc(t['name'], w-32)}{C['reset']}"
                   if sel else f"{C['grey']}{trunc(t['name'], w-32)}{C['reset']}")
            a   = f"{C['cyan']}{trunc(t['artist'], 22)}{C['reset']}" if sel else f"{C['grey']}{trunc(t['artist'], 22)}{C['reset']}"
            print(f"  {m}{n}  {a}")
        if not self.sr and self.sq:
            print(f"  {C['grey']}No results.{C['reset']}")
        print(f"\n  {C['grey']}type=search  ↑↓=select  enter=play  esc=back{C['reset']}")

    def _r_am_pl(self, w):
        total = len(self.am_pls)
        print(f"  {C['magenta']}{C['bold']}Apple Music Playlists{C['reset']}  {C['grey']}({total}){C['reset']}\n")
        visible = 18
        off = max(0, min(self.am_pl_cursor - visible // 2, total - visible))
        for i in range(off, min(off + visible, total)):
            sel = (i == self.am_pl_cursor)
            m   = f"{C['magenta']}{C['bold']}▶ " if sel else f"{C['grey']}  "
            n   = (f"{C['white']}{C['bold']}{trunc(self.am_pls[i], w-8)}{C['reset']}"
                   if sel else f"{C['grey']}{trunc(self.am_pls[i], w-8)}{C['reset']}")
            print(f"  {m}{n}")
        print(f"\n  {C['grey']}enter=play  ↑↓=navigate  b=back{C['reset']}")

    # ── Now-playing strip (shared) ─────────────────────────────────────────

    def _r_np(self, w):
        p = self.player
        if p.current:
            state = f"{C['cyan']}▶ PLAYING{C['reset']}" if not p.paused else f"{C['yellow']}⏸ PAUSED{C['reset']}"
            flags = (f"  {C['cyan']}⇄{C['reset']}" if self.queue.shuffle else "") + \
                    (f"  {C['cyan']}↻{C['reset']}" if self.repeat else "")
            print(f"  {state}{flags}")
            print(f"  {C['white']}{C['bold']}{trunc(p.current.stem, w-6)}{C['reset']}")
            print(f"  {C['grey']}{p.current.parent.name}{C['reset']}")
            pos, dur = p.position(), p.duration
            print(f"\n  {progress_bar(pos, dur, 38)}  "
                  f"{C['cyan']}{fmt_time(pos)}{C['reset']} / {C['grey']}{fmt_time(dur)}{C['reset']}\n")
        else:
            print(f"  {C['grey']}Nothing playing{C['reset']}\n")

    def _r_filelist(self, w, files, cursor, visible=10):
        total = len(files)
        if not total:
            print(f"  {C['red']}No music files found.{C['reset']}")
            return
        if cursor < self.file_offset:
            self.file_offset = cursor
        if cursor >= self.file_offset + visible:
            self.file_offset = cursor - visible + 1
        self.file_offset = max(0, self.file_offset)
        print(f"  {C['grey']}Library  ({total} tracks){C['reset']}")
        for i in range(self.file_offset, min(self.file_offset + visible, total)):
            f   = files[i]
            sel = (i == cursor)
            m   = f"{C['cyan']}{C['bold']}▶ " if sel else f"{C['grey']}  "
            n   = (f"{C['white']}{C['bold']}{trunc(f.stem, w-12)}{C['reset']}"
                   if sel else f"{C['grey']}{trunc(f.stem, w-12)}{C['reset']}")
            print(f"  {m}{n}")
        if self.file_offset + visible < total:
            print(f"  {C['grey']}  ↓ {total - self.file_offset - visible} more{C['reset']}")

    # ─────────────────────────────────────────────────────────────────────
    # Input dispatch
    # ─────────────────────────────────────────────────────────────────────

    def handle(self, key):
        if key in ("\x03", "\x04"):
            self.running = False
            return
        {M_MAIN:      self._h_main,
         M_LOCAL:     self._h_local,
         M_QUEUE:     self._h_queue,
         M_PLAYLISTS: self._h_playlists,
         M_PL_NAME:   self._h_pl_name,
         M_AM:        self._h_am,
         M_AM_SEARCH: self._h_am_search,
         M_AM_PL:     self._h_am_pl,
         }.get(self.mode, lambda k: None)(key)

    def _h_main(self, key):
        if key == "1":    self.mode = M_LOCAL
        elif key == "2":
            if self.am_ok: self.mode = M_AM
            else: self.msg("Apple Music not detected")
        elif key == "q":  self.running = False

    def _h_local(self, key):
        files = self.all_files; total = len(files)
        if key in ("b", "esc"):     self.mode = M_MAIN
        elif key == "q":            self.mode = M_QUEUE; self.file_cursor = self.queue.index
        elif key == "p":            self.mode = M_PLAYLISTS
        elif key == "up":           self.file_cursor = max(0, self.file_cursor - 1)
        elif key == "down":         self.file_cursor = min(total - 1, self.file_cursor + 1)
        elif key == "pgup":         self.file_cursor = max(0, self.file_cursor - 10)
        elif key == "pgdn":         self.file_cursor = min(total - 1, self.file_cursor + 10)
        elif key in ("\r", "\n"):
            if total:
                self.queue.set(files, self.file_cursor)
                self.player.play(files[self.file_cursor])
        elif key == " ":
            if self.player.playing:  self.player.toggle_pause()
            elif total:
                self.queue.set(files, self.file_cursor)
                self.player.play(files[self.file_cursor])
        elif key == "right":        self.player.seek(+10)
        elif key == "left":         self.player.seek(-10)
        elif key == ".":
            t = self.queue.next()
            if t: self.file_cursor = self.queue.index; self.player.play(t)
        elif key == ",":
            t = self.queue.prev()
            if t: self.file_cursor = self.queue.index; self.player.play(t)
        elif key == "z":
            self.queue.toggle_shuffle()
            self.msg(f"Shuffle {'on' if self.queue.shuffle else 'off'}")
        elif key == "r":
            self.repeat = not self.repeat
            self.msg(f"Repeat {'on' if self.repeat else 'off'}")

    def _h_queue(self, key):
        total = len(self.queue.tracks)
        if key in ("b", "esc", "q"): self.mode = M_LOCAL
        elif key == "up":    self.file_cursor = max(0, self.file_cursor - 1)
        elif key == "down":  self.file_cursor = min(total - 1, self.file_cursor + 1)
        elif key in ("\r", "\n"):
            if total:
                self.queue.index = self.file_cursor
                t = self.queue.current()
                if t: self.player.play(t)
        elif key == "delete":
            self.queue.remove(self.file_cursor)
            self.file_cursor = min(self.file_cursor, max(0, len(self.queue.tracks) - 1))
            self.msg("Removed from queue")
        elif key == ".":
            t = self.queue.next()
            if t: self.file_cursor = self.queue.index; self.player.play(t)
        elif key == ",":
            t = self.queue.prev()
            if t: self.file_cursor = self.queue.index; self.player.play(t)

    def _h_playlists(self, key):
        names = list(self.playlists.keys()); total = len(names)
        if key in ("b", "esc"):  self.mode = M_LOCAL
        elif key == "up":        self.pl_cursor = max(0, self.pl_cursor - 1)
        elif key == "down":      self.pl_cursor = min(total - 1, self.pl_cursor + 1)
        elif key in ("\r", "\n") and total:
            tracks = self.playlists[names[self.pl_cursor]]
            if tracks:
                self.queue.set(tracks, 0)
                self.player.play(tracks[0])
                self.file_cursor = 0
                self.mode = M_LOCAL
                self.msg(f"Playing: {names[self.pl_cursor]}")
        elif key == "n":
            self.pl_input  = ""
            self.prev_mode = M_PLAYLISTS
            self.mode      = M_PL_NAME
        elif key == "a":
            cur = self.player.current
            if cur and total:
                name = names[self.pl_cursor]
                if cur not in self.playlists[name]:
                    self.playlists[name].append(cur)
                    save_playlists(self.playlists)
                    self.msg(f"Added to '{name}'")
                else:
                    self.msg("Already in that playlist")
            else:
                self.msg("Nothing playing" if not cur else "No playlist selected")
        elif key == "delete" and total:
            name = names[self.pl_cursor]
            del self.playlists[name]
            save_playlists(self.playlists)
            self.pl_cursor = max(0, self.pl_cursor - 1)
            self.msg(f"Deleted \"{name}\"")

    def _h_pl_name(self, key):
        if key == "esc":         self.mode = M_PLAYLISTS
        elif key in ("\r", "\n"):
            name = self.pl_input.strip()
            if name and name not in self.playlists:
                self.playlists[name] = []
                save_playlists(self.playlists)
                self.msg(f"Created \"{name}\"")
            self.mode = M_PLAYLISTS
        elif key == "\x7f":      self.pl_input = self.pl_input[:-1]
        elif key.isprintable() and len(key) == 1: self.pl_input += key

    def _h_am(self, key):
        if key in ("b", "esc"):  self.mode = M_MAIN
        elif key == " ":
            if am_state() == "playing": am_pause()
            else:                       am_play()
        elif key == "right":     am_set_position(am_position() + 15)
        elif key == "left":      am_set_position(max(0, am_position() - 15))
        elif key == ".":         am_next()
        elif key == ",":         am_prev()
        elif key == "+":
            self.am_vol = min(100, self.am_vol + 5); am_set_vol(self.am_vol)
        elif key == "-":
            self.am_vol = max(0, self.am_vol - 5);   am_set_vol(self.am_vol)
        elif key == "s":
            self.sq = ""; self.sr = []; self.sc = 0; self.mode = M_AM_SEARCH
        elif key == "p":
            self.am_pls = am_playlists(); self.am_pl_cursor = 0; self.mode = M_AM_PL
        elif key == "q":         self.running = False

    def _h_am_search(self, key):
        if key == "esc":         self.mode = M_AM
        elif key in ("\r", "\n"):
            if self.sr:
                am_play_track(self.sr[self.sc]["name"])
                self.msg(f"Playing: {self.sr[self.sc]['name']}")
                self.mode = M_AM
            elif self.sq:
                self.sr = am_search(self.sq); self.sc = 0
        elif key == "up":        self.sc = max(0, self.sc - 1)
        elif key == "down":      self.sc = min(len(self.sr) - 1, self.sc + 1)
        elif key == "\x7f":      self.sq = self.sq[:-1]; self.sr = []
        elif key.isprintable() and len(key) == 1:
            self.sq += key
            if len(self.sq) >= 2:
                self.sr = am_search(self.sq); self.sc = 0

    def _h_am_pl(self, key):
        total = len(self.am_pls)
        if key in ("b", "esc"):  self.mode = M_AM
        elif key == "up":        self.am_pl_cursor = max(0, self.am_pl_cursor - 1)
        elif key == "down":      self.am_pl_cursor = min(total - 1, self.am_pl_cursor + 1)
        elif key in ("\r", "\n") and total:
            name = self.am_pls[self.am_pl_cursor]
            am_play_playlist(name)
            self.msg(f"Playing: {name}")
            self.mode = M_AM

    # ─────────────────────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────────────────────

    def run(self):
        self.boot()
        while self.running:
            self.render()
            key = read_key()
            self.handle(key)
        self.player.stop()
        clr()
        print(C["cyan"] + BANNER + C["reset"])
        print(f"  {C['grey']}Goodbye. ✌{C['reset']}\n")


if __name__ == "__main__":
    if sys.platform != "darwin":
        print("p4Ons requires macOS (afplay + Apple Music via AppleScript).")
        sys.exit(1)
    try:
        App().run()
    except KeyboardInterrupt:
        print("\n  Goodbye.\n")