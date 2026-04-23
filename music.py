#!/usr/bin/env python3
"""
p4Ons — terminal music player for Apple Music and local files.
macOS only. No external dependencies except python-vlc.

Run from anywhere — the script finds itself via __file__.
"""

import shutil
import os, sys, subprocess, threading, time, termios, tty, signal, json, random
from pathlib import Path

def ensure_installed():
    """Copy script to home directory if running from .app bundle"""
    #Check if running from .app bundle
    if getattr(sys, 'frozen', False) or '.app' in sys.argv[0]:
        # Running from .app
        home_path = Path.home() / "p4ons.py"
        
        #current script path
        current_path = Path(sys.argv[0]).resolve()
        
        # Copy to home directory if needed
        if not home_path.exists() or current_path != home_path:
            try:
                shutil.copy2(current_path, home_path)
                home_path.chmod(0o755)  # Make executable
                print(f"✓ p4Ons installed to {home_path}")
                
                # relaunch from home directory
                os.execv('/usr/bin/python3', ['python3', str(home_path)] + sys.argv[1:])
            except Exception as e:
                print(f"Warning: Could not copy to home directory: {e}")
    
    #running from home directory or direct execution

# Run the installation check
ensure_installed()

try:
    import vlc
    VLC_AVAILABLE = True
except ImportError:
    VLC_AVAILABLE = False

# ── Self-location: always works whether run directly or via .app wrapper ──────
SCRIPT_DIR = Path(__file__).resolve().parent

BANNER = r"""
        _  _    ___            
 _ __ | || |  / _ \ _ __  ___ 
| '_ \| || |_| | | | '_ \/ __|
| |_) |__   _| |_| | | | \__ \
| .__/   |_|  \___/|_| |_|___/
|_|
"""

C = {
    "reset": "\033[0m", "bold": "\033[1m",
    "green": "\033[92m", "cyan": "\033[96m", "yellow": "\033[93m",
    "red": "\033[91m",   "magenta": "\033[95m", "blue": "\033[94m",
    "white": "\033[97m", "grey": "\033[90m",
}

MUSIC_EXTS     = {".mp3", ".m4a", ".aac", ".flac", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".wma"}
SCAN_DIRS      = [Path.home() / "Downloads", Path.home() / "Music", Path.home() / "Desktop"]
PLAYLISTS_FILE = Path.home() / ".p4ons_playlists.json"

# ─────────────────────────────────────────────────────────────────────────────
# Terminal helpers with explicit flushing
# ─────────────────────────────────────────────────────────────────────────────

def term_width() -> int:
    try:    return os.get_terminal_size().columns
    except: return 80

def clr():
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()  # Force clear to display immediately

def trunc(s: str, n: int) -> str:
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"

def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"

def progress_bar(pos: float, dur: float, width: int = 36) -> str:
    if dur <= 0:
        return C["grey"] + "─" * width + C["reset"]
    pct  = min(pos / dur, 1.0)
    done = int(pct * width)
    rest = width - done - 1
    return (C["cyan"] + "━" * done +
            C["cyan"] + "●" + C["reset"] +
            C["grey"] + "─" * max(rest, 0) + C["reset"])

# ─────────────────────────────────────────────────────────────────────────────
# afinfo — read real track duration from metadata (never estimate)
# ─────────────────────────────────────────────────────────────────────────────

def get_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["afinfo", str(path)],
            capture_output=True, text=True, timeout=5
        )
        for line in r.stdout.splitlines():
            # Matches: "  duration: 213.456 sec" or "Estimated Duration: 213.456 sec"
            low = line.lower()
            if "duration" in low:
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
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip()
    except Exception:
        return ""

def am_check() -> bool:
    return bool(_osa('tell application "Music" to return name', timeout=10))

def am_state()  -> str:  return _osa('tell application "Music" to player state as string')
def am_play()   -> None: _osa('tell application "Music" to play')
def am_pause()  -> None: _osa('tell application "Music" to pause')
def am_next()   -> None: _osa('tell application "Music" to next track')
def am_prev()   -> None: _osa('tell application "Music" to previous track')

def am_set_vol(v: int) -> None:
    _osa(f'tell application "Music" to set sound volume to {max(0, min(100, v))}')

def am_get_vol() -> int:
    try:    return int(_osa('tell application "Music" to sound volume'))
    except: return 50

def am_position() -> float:
    try:    return float(_osa('tell application "Music" to player position'))
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
    return {
        "name":   parts[0] or "—",
        "artist": parts[1] or "—",
        "album":  parts[2] or "—",
        "duration": dur,
    }

def am_search(query: str) -> list:
    safe = query.replace('"', '\\"')
    raw  = _osa(f'''
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
        if len(parts) >= 1 and parts[0].strip():
            tracks.append({
                "name":   parts[0],
                "artist": parts[1] if len(parts) > 1 else "",
                "album":  parts[2] if len(parts) > 2 else "",
            })
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
# plays with python-vlc
# ─────────────────────────────────────────────────────────────────────────────

class LocalPlayer:
    def __init__(self):
        if not VLC_AVAILABLE:
            print(C["red"] + "\n  ERROR: python-vlc not installed!" + C["reset"])
            print(C["yellow"] + "  Install it with: pip install python-vlc" + C["reset"])
            print(C["yellow"] + "  Also install VLC player from https://www.videolan.org/vlc/" + C["reset"])
            sys.exit(1)
        
        self.instance = vlc.Instance()
        self.player = self.instance.media_player_new()
        self.current: Path | None = None
        self.duration: float = 0.0
        self._is_playing = False
        self._monitor_thread = None
        self._monitor_running = False
        
    def play(self, path: Path, seek_to: float = 0.0):
        """Start playing path, optionally from seek_to seconds."""
        # Stop current playback if any
        self.player.stop()
        
        self.current = path
        self.duration = get_duration(path)
        
        # Create media
        media = self.instance.media_new(str(path))
        self.player.set_media(media)
        
        # Start playing
        self.player.play()
        self._is_playing = True
        
        # Seek to position 
        if seek_to > 0.0:
            # VLC uses milliseconds not seconds
            time.sleep(0.05)
            self.player.set_time(int(seek_to * 1000))
        
        # Start monitoring thread for track completion
        self._start_monitor()
    
    def _start_monitor(self):
        """Start background thread to detect when track ends."""
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_running = False
            self._monitor_thread.join(timeout=0.5)
        
        self._monitor_running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
    
    def _monitor_loop(self):
        """Monitor playback state to detect track completion."""
        while self._monitor_running and self._is_playing:
            time.sleep(0.3)
            state = self.player.get_state()
            # Check if playback ended
            if state in [vlc.State.Ended, vlc.State.Stopped, vlc.State.Error]:
                if self._is_playing:
                    self._is_playing = False
                break
    
    def toggle_pause(self):
        """Pause or resume playback."""
        if not self._is_playing and self.current:
            # If not playing resume from current position
            self.player.play()
            self._is_playing = True
            self._start_monitor()
        else:
            self.player.pause()
            # VLC pause toggles gotta track state
            state = self.player.get_state()
            self._is_playing = (state == vlc.State.Playing)
    
    def seek(self, delta: float):
        """Seek forward (delta>0) or backward (delta<0) by delta seconds."""
        if self.current is None:
            return
        
        current_ms = self.player.get_time()
        if current_ms == -1:  #Invalid time can't seek
            return
        
        new_pos_ms = max(0, current_ms + int(delta * 1000))
        # Don't seek beyond duration
        if self.duration > 0:
            new_pos_ms = min(new_pos_ms, int(self.duration * 1000) - 100)
        
        self.player.set_time(new_pos_ms)
    
    def stop(self):
        """Stop playback and clean up."""
        self._monitor_running = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=0.5)
        self.player.stop()
        self.current = None
        self.duration = 0.0
        self._is_playing = False
    
    def position(self) -> float:
        """Current playback position in seconds."""
        if self.current is None:
            return 0.0
        
        # VLC returns time in milliseconds, -1 if not playing/invalid
        time_ms = self.player.get_time()
        if time_ms == -1:
            return 0.0
        
        return time_ms / 1000.0
    
    def is_done(self) -> bool:
        """Check if current track has finished playing."""
        if self.current is None:
            return True
        
        state = self.player.get_state()
        #  paused isnt done
        if state == vlc.State.Paused:
            return False
            
        return state in [vlc.State.Ended, vlc.State.Stopped, vlc.State.Error]
    
    @property
    def playing(self) -> bool:
        """Check if currently playing (not paused/stopped)."""
        if self.current is None:
            return False
        
        state = self.player.get_state()
        return state == vlc.State.Playing
    
    @property
    def paused(self) -> bool:
        """Check if paused."""
        if self.current is None:
            return False
        
        state = self.player.get_state()
        return state == vlc.State.Paused
    
    def set_volume(self, volume: int):
        """Set volume (0-100)."""
        self.player.audio_set_volume(volume)
    
    def get_volume(self) -> int:
        """Get current volume."""
        return self.player.audio_get_volume()

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
        self.index  = max(0, min(start, len(tracks) - 1))
        self._rebuild()

    def _rebuild(self):
        n = len(self.tracks)
        if not n:
            self._order = []
            return
        if self.shuffle:
            anchor = self._order[self.index] if self._order else self.index
            rest   = [i for i in range(n) if i != anchor]
            random.shuffle(rest)
            self._order = [anchor] + rest
            self.index  = 0
        else:
            self._order = list(range(n))

    def current(self) -> Path | None:
        if not self.tracks or self.index >= len(self._order):
            return None
        return self.tracks[self._order[self.index]]

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
        self._rebuild()

    def remove(self, queue_pos: int):
        if not self._order or queue_pos >= len(self._order):
            return
        real = self._order[queue_pos]
        self.tracks.pop(real)
        self._order = list(range(len(self.tracks)))
        self.index  = min(self.index, max(0, len(self.tracks) - 1))

    def add(self, path: Path):
        self.tracks.append(path)
        self._order.append(len(self.tracks) - 1)

# ─────────────────────────────────────────────────────────────────────────────
# Playlist persistence  (~/.p4ons_playlists.json)
# ─────────────────────────────────────────────────────────────────────────────

def load_playlists() -> dict:
    try:
        if PLAYLISTS_FILE.exists():
            raw = json.loads(PLAYLISTS_FILE.read_text())
            return {k: [Path(p) for p in v] for k, v in raw.items()}
    except Exception:
        pass
    return {}

def save_playlists(pls: dict):
    try:
        PLAYLISTS_FILE.write_text(
            json.dumps({k: [str(p) for p in v] for k, v in pls.items()}, indent=2)
        )
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────────────────────
# Raw keyboard input with explicit flushing
# ─────────────────────────────────────────────────────────────────────────────

def read_key() -> str:
    """Read a single keypress from terminal in raw mode."""
    #Flush  output before entering raw mode
    sys.stdout.flush()
    sys.stderr.flush()
    
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                if ch3 in ("5", "6"):
                    sys.stdin.read(1)   # consume ~
                    return "pgup" if ch3 == "5" else "pgdn"
                if ch3 == "3":
                    sys.stdin.read(1)   # consume ~
                    return "delete"
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(ch3, "esc")
            return "esc"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)

# ─────────────────────────────────────────────────────────────────────────────
# Modes
# ─────────────────────────────────────────────────────────────────────────────

M_MAIN, M_LOCAL, M_QUEUE, M_PLAYLISTS, M_PL_NAME, M_AM, M_AM_SEARCH, M_AM_PL = range(8)

# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────

class App:
    def __init__(self):
        self.mode        = M_MAIN
        self.prev_mode   = M_MAIN

        # local
        self.player      = LocalPlayer()
        self.all_files   : list[Path] = []
        self.file_cursor = 0
        self.file_offset = 0
        self.queue       = Queue()
        self.repeat      = False

        # local playlists
        self.playlists   : dict[str, list[Path]] = {}
        self.pl_cursor   = 0
        self.pl_input    = ""

        # Apple Music
        self.am_ok       = False
        self.am_vol      = 50
        self.am_pls      : list[str] = []
        self.am_pl_cur   = 0

        # AM search
        self.sq          = ""
        self.sr          : list[dict] = []
        self.sc          = 0

        # UI
        self.running     = True
        self.message     = ""
        self.msg_t       = 0.0

    # ── Boot ─────────────────────────────────────────────────────────────────

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
                    for f in sorted(d.rglob(f"*{ext}")):
                        if f not in seen:
                            seen.add(f)
                            files.append(f)
        self.all_files = sorted(files, key=lambda p: p.name.lower())

    # ── Auto-advance watcher ──────────────────────────────────────────────────

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
            time.sleep(0.3)

    # ── Message flash ─────────────────────────────────────────────────────────

    def msg(self, text: str):
        self.message = text
        self.msg_t   = time.time()

    # ─────────────────────────────────────────────────────────────────────────
    # Render
    # ─────────────────────────────────────────────────────────────────────────

    def render(self):
        clr()  # already includes a flush
        w = term_width()
        print(C["cyan"] + C["bold"] + BANNER + C["reset"])

        {
            M_MAIN:      self._r_main,
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

        print(f"\n{C['grey']}{'─' * min(w, 70)}{C['reset']}")
        
        # Final flush to ensure everything is displayed
        sys.stdout.flush()

    def _r_main(self, w):
        print(f"  {C['white']}{C['bold']}Choose a source{C['reset']}\n")
        print(f"  {C['green']}{C['bold']}[1]{C['reset']}  {C['green']}{C['bold']}Local Files{C['reset']}")
        print(f"       {C['grey']}Downloads · Music · Desktop  ({len(self.all_files)} tracks found){C['reset']}\n")
        col    = C["magenta"] if self.am_ok else C["grey"]
        status = "Connected" if self.am_ok else "not detected — is Music.app installed?"
        print(f"  {col}{C['bold']}[2]{C['reset']}  {col}{C['bold']}Apple Music{C['reset']}")
        print(f"       {C['grey']}{status}{C['reset']}\n")
        print(f"  {C['grey']}[q] quit{C['reset']}")

    def _r_local(self, w):
        self._r_np(w)
        self._r_filelist(w, self.all_files, self.file_cursor, visible=10)
        shuf = C["cyan"] if self.queue.shuffle else C["grey"]
        rep  = C["cyan"] if self.repeat        else C["grey"]
        print(f"\n  {shuf}[z] shuffle{C['reset']}  {rep}[r] repeat{C['reset']}  "
              f"{C['grey']}[q] queue  [p] playlists  "
              f"enter=play  space=pause  ←→=seek±10s  ,/.=prev/next  b=back{C['reset']}")

    def _r_queue(self, w):
        self._r_np(w)
        tracks = self.queue.tracks
        total  = len(tracks)
        print(f"  {C['white']}{C['bold']}Queue{C['reset']}  {C['grey']}({total} tracks){C['reset']}\n")
        visible = 12
        qi  = self.queue.index
        off = max(0, min(self.file_cursor - visible // 2, max(0, total - visible)))
        for i in range(off, min(off + visible, total)):
            t       = tracks[self.queue._order[i]] if self.queue._order else tracks[i]
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
        print(f"\n  {C['grey']}enter=play selected  del=remove  ,/.=prev/next  b=back{C['reset']}")

    def _r_playlists(self, w):
        names = list(self.playlists.keys())
        print(f"  {C['white']}{C['bold']}Local Playlists{C['reset']}  {C['grey']}({len(names)}){C['reset']}\n")
        if not names:
            print(f"  {C['grey']}No playlists yet. Press [n] to create one.{C['reset']}\n")
        else:
            visible = 14
            off = max(0, min(self.pl_cursor - visible // 2, max(0, len(names) - visible)))
            for i in range(off, min(off + visible, len(names))):
                name  = names[i]
                count = len(self.playlists[name])
                sel   = (i == self.pl_cursor)
                m = f"{C['cyan']}{C['bold']}▶ " if sel else f"{C['grey']}  "
                n = (f"{C['white']}{C['bold']}{trunc(name, w-22)}{C['reset']}  {C['grey']}{count} tracks{C['reset']}"
                     if sel else f"{C['grey']}{trunc(name, w-22)}  {count} tracks{C['reset']}")
                print(f"  {m}{n}")
        print(f"\n  {C['grey']}enter=play  n=new  a=add current track  del=delete playlist  b=back{C['reset']}")

    def _r_pl_name(self, w):
        print(f"  {C['white']}{C['bold']}Playlist name:{C['reset']} {C['cyan']}{self.pl_input}█{C['reset']}")
        print(f"\n  {C['grey']}enter=confirm  esc=cancel{C['reset']}")

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
            print(f"\n  {progress_bar(pos, dur, 36)}  "
                  f"{C['cyan']}{fmt_time(pos)}{C['reset']} / {C['grey']}{fmt_time(dur)}{C['reset']}")
        else:
            print(f"  {C['grey']}Nothing playing{C['reset']}")
        print(f"\n  {C['grey']}Vol: {self.am_vol}%  [-/+]{C['reset']}")
        print(f"\n  {C['grey']}space=pause  ←→=seek±15s  ,/.=prev/next  s=search  p=playlists  b=back{C['reset']}")

    def _r_am_search(self, w):
        print(f"  {C['magenta']}{C['bold']}Search Apple Music Library{C['reset']}\n")
        print(f"  {C['grey']}›{C['reset']} {C['white']}{self.sq}{C['cyan']}█{C['reset']}\n")
        for i, t in enumerate(self.sr):
            sel = (i == self.sc)
            m   = f"{C['cyan']}{C['bold']}▶ " if sel else f"{C['grey']}  "
            n   = (f"{C['white']}{C['bold']}{trunc(t['name'], w-34)}{C['reset']}"
                   if sel else f"{C['grey']}{trunc(t['name'], w-34)}{C['reset']}")
            a   = (f"{C['cyan']}{trunc(t['artist'], 24)}{C['reset']}"
                   if sel else f"{C['grey']}{trunc(t['artist'], 24)}{C['reset']}")
            print(f"  {m}{n}  {a}")
        if not self.sr and self.sq:
            print(f"  {C['grey']}No results.{C['reset']}")
        print(f"\n  {C['grey']}type=search  ↑↓=select  enter=play  esc=back{C['reset']}")

    def _r_am_pl(self, w):
        total = len(self.am_pls)
        print(f"  {C['magenta']}{C['bold']}Apple Music Playlists{C['reset']}  {C['grey']}({total}){C['reset']}\n")
        visible = 18
        off = max(0, min(self.am_pl_cur - visible // 2, max(0, total - visible)))
        for i in range(off, min(off + visible, total)):
            sel = (i == self.am_pl_cur)
            m   = f"{C['magenta']}{C['bold']}▶ " if sel else f"{C['grey']}  "
            n   = (f"{C['white']}{C['bold']}{trunc(self.am_pls[i], w-8)}{C['reset']}"
                   if sel else f"{C['grey']}{trunc(self.am_pls[i], w-8)}{C['reset']}")
            print(f"  {m}{n}")
        print(f"\n  {C['grey']}enter=play  ↑↓=navigate  b=back{C['reset']}")

    # ── Now-playing strip ─────────────────────────────────────────────────────

    def _r_np(self, w):
        p = self.player
        if p.current:
            state = (f"{C['cyan']}▶  PLAYING{C['reset']}" if p.playing
                     else f"{C['yellow']}⏸  PAUSED{C['reset']}" if p.paused
                     else f"{C['grey']}■  STOPPED{C['reset']}")
            flags = ("  " + C["cyan"] + "⇄" + C["reset"] if self.queue.shuffle else "") + \
                    ("  " + C["cyan"] + "↻" + C["reset"] if self.repeat        else "")
            print(f"  {state}{flags}")
            print(f"  {C['white']}{C['bold']}{trunc(p.current.stem, w-6)}{C['reset']}")
            print(f"  {C['grey']}{p.current.parent.name}{C['reset']}")
            pos, dur = p.position(), p.duration
            bar = progress_bar(pos, dur, 36)
            print(f"\n  {bar}  {C['cyan']}{fmt_time(pos)}{C['reset']} / {C['grey']}{fmt_time(dur)}{C['reset']}\n")
        else:
            print(f"  {C['grey']}Nothing playing{C['reset']}\n")

    def _r_filelist(self, w, files, cursor, visible=10):
        total = len(files)
        if not total:
            print(f"  {C['red']}No music files found in Downloads, Music, or Desktop.{C['reset']}")
            return
        # clamp scroll window
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

    # ─────────────────────────────────────────────────────────────────────────
    # Input
    # ─────────────────────────────────────────────────────────────────────────

    def handle(self, key):
        if key in ("\x03", "\x04"):
            self.running = False
            return
        {
            M_MAIN:      self._h_main,
            M_LOCAL:     self._h_local,
            M_QUEUE:     self._h_queue,
            M_PLAYLISTS: self._h_playlists,
            M_PL_NAME:   self._h_pl_name,
            M_AM:        self._h_am,
            M_AM_SEARCH: self._h_am_search,
            M_AM_PL:     self._h_am_pl,
        }.get(self.mode, lambda k: None)(key)

    def _h_main(self, key):
        if key == "1":
            self.mode = M_LOCAL
        elif key == "2":
            if self.am_ok: self.mode = M_AM
            else:          self.msg("Apple Music not detected — is Music.app installed?")
        elif key == "q":
            self.running = False

    def _h_local(self, key):
        files = self.all_files
        total = len(files)
        if key in ("b", "esc"):
            self.mode = M_MAIN
        elif key == "q":
            self.mode = M_QUEUE
            self.file_cursor = self.queue.index
        elif key == "p":
            self.mode = M_PLAYLISTS
        elif key == "up":
            self.file_cursor = max(0, self.file_cursor - 1)
        elif key == "down":
            self.file_cursor = min(total - 1, self.file_cursor + 1)
        elif key == "pgup":
            self.file_cursor = max(0, self.file_cursor - 10)
        elif key == "pgdn":
            self.file_cursor = min(total - 1, self.file_cursor + 10)
        elif key in ("\r", "\n"):
            if total:
                self.queue.set(files, self.file_cursor)
                self.player.play(files[self.file_cursor])
        elif key == " ":
            if self.player.playing or self.player.paused:
                self.player.toggle_pause()
            elif total:
                self.queue.set(files, self.file_cursor)
                self.player.play(files[self.file_cursor])
        elif key == "right":
            self.player.seek(+10)
        elif key == "left":
            self.player.seek(-10)
        elif key == ".":
            t = self.queue.next()
            if t:
                self.file_cursor = self.queue.index
                self.player.play(t)
        elif key == ",":
            t = self.queue.prev()
            if t:
                self.file_cursor = self.queue.index
                self.player.play(t)
        elif key == "z":
            self.queue.toggle_shuffle()
            self.msg(f"Shuffle {'on' if self.queue.shuffle else 'off'}")
        elif key == "r":
            self.repeat = not self.repeat
            self.msg(f"Repeat {'on' if self.repeat else 'off'}")

    def _h_queue(self, key):
        total = len(self.queue.tracks)
        if key in ("b", "esc", "q"):
            self.mode = M_LOCAL
        elif key == "up":
            self.file_cursor = max(0, self.file_cursor - 1)
        elif key == "down":
            self.file_cursor = min(total - 1, self.file_cursor + 1)
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
        names = list(self.playlists.keys())
        total = len(names)
        if key in ("b", "esc"):
            self.mode = M_LOCAL
        elif key == "up":
            self.pl_cursor = max(0, self.pl_cursor - 1)
        elif key == "down":
            self.pl_cursor = min(total - 1, self.pl_cursor + 1)
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
                    self.msg(f"Added to \"{name}\"")
                else:
                    self.msg("Already in that playlist")
            else:
                self.msg("Nothing playing" if not cur else "Select a playlist first")
        elif key == "delete" and total:
            name = names[self.pl_cursor]
            del self.playlists[name]
            save_playlists(self.playlists)
            self.pl_cursor = max(0, self.pl_cursor - 1)
            self.msg(f"Deleted \"{name}\"")

    def _h_pl_name(self, key):
        if key == "esc":
            self.mode = M_PLAYLISTS
        elif key in ("\r", "\n"):
            name = self.pl_input.strip()
            if name and name not in self.playlists:
                self.playlists[name] = []
                save_playlists(self.playlists)
                self.msg(f"Created \"{name}\"")
            self.mode = M_PLAYLISTS
        elif key == "\x7f":
            self.pl_input = self.pl_input[:-1]
        elif key.isprintable() and len(key) == 1:
            self.pl_input += key

    def _h_am(self, key):
        if key in ("b", "esc"):
            self.mode = M_MAIN
        elif key == " ":
            if am_state() == "playing": am_pause()
            else:                       am_play()
        elif key == "right":
            am_set_position(am_position() + 15)
        elif key == "left":
            am_set_position(max(0, am_position() - 15))
        elif key == ".":  am_next()
        elif key == ",":  am_prev()
        elif key == "+":
            self.am_vol = min(100, self.am_vol + 5); am_set_vol(self.am_vol)
        elif key == "-":
            self.am_vol = max(0,   self.am_vol - 5); am_set_vol(self.am_vol)
        elif key == "s":
            self.sq = ""; self.sr = []; self.sc = 0
            self.mode = M_AM_SEARCH
        elif key == "p":
            self.am_pls  = am_playlists()
            self.am_pl_cur = 0
            self.mode = M_AM_PL
        elif key == "q":
            self.running = False

    def _h_am_search(self, key):
        if key == "esc":
            self.mode = M_AM
        elif key in ("\r", "\n"):
            if self.sr:
                am_play_track(self.sr[self.sc]["name"])
                self.msg(f"Playing: {self.sr[self.sc]['name']}")
                self.mode = M_AM
            elif self.sq:
                self.sr = am_search(self.sq); self.sc = 0
        elif key == "up":
            self.sc = max(0, self.sc - 1)
        elif key == "down":
            self.sc = min(len(self.sr) - 1, self.sc + 1)
        elif key == "\x7f":
            self.sq = self.sq[:-1]; self.sr = []
        elif key.isprintable() and len(key) == 1:
            self.sq += key
            if len(self.sq) >= 2:
                self.sr = am_search(self.sq); self.sc = 0

    def _h_am_pl(self, key):
        total = len(self.am_pls)
        if key in ("b", "esc"):
            self.mode = M_AM
        elif key == "up":
            self.am_pl_cur = max(0, self.am_pl_cur - 1)
        elif key == "down":
            self.am_pl_cur = min(total - 1, self.am_pl_cur + 1)
        elif key in ("\r", "\n") and total:
            name = self.am_pls[self.am_pl_cur]
            am_play_playlist(name)
            self.msg(f"Playing: {name}")
            self.mode = M_AM

    # ─────────────────────────────────────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────────────────────────────────────

    def run(self):
        self.boot()
        while self.running:
            self.render()
            key = read_key()  # read_key includes its own flush
            self.handle(key)
        self.player.stop()
        clr()
        print(C["cyan"] + BANNER + C["reset"])
        print(f"  {C['grey']}Goodbye. ✌{C['reset']}\n")
        sys.stdout.flush()  # Final flush


if __name__ == "__main__":
    if sys.platform != "darwin":
        print("p4Ons requires macOS (afplay + Apple Music via AppleScript).")
        sys.exit(1)
    
    if not VLC_AVAILABLE:
        print(C["red"] + "\n  ERROR: python-vlc not installed!" + C["reset"])
        print(C["yellow"] + "  Install it with: pip install python-vlc" + C["reset"])
        print(C["yellow"] + "  Also install VLC player from https://www.videolan.org/vlc/" + C["reset"])
        sys.exit(1)
    
    try:
        App().run()
    except KeyboardInterrupt:
        print("\n  Goodbye.\n")
