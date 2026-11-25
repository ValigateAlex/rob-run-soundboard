"""Rob Run Soundboard.

Mixes your microphone with sound clips and sends the result into the
VB-Audio virtual cable ("CABLE Input"). Pick "CABLE Output" as the
microphone in Rob Run (or make it the Windows default recording device)
and everyone in voice chat hears your voice plus the sounds.

Boards are plain folders of audio files under Documents/RobRun Soundboard/Boards.
Each board folder keeps a board.json with button names, colors, hotkeys and volume.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
from PySide6.QtCore import QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget,
)

try:
    import keyboard  # global hotkeys (work while the game has focus)
except Exception:  # pragma: no cover
    keyboard = None

APP_NAME = "Rob Run Soundboard"
SR = 48000
BLOCK = 480  # 10 ms
AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".flac", ".aiff", ".aif"}
BOARDS_ROOT = Path(os.path.expanduser("~")) / "Documents" / "RobRun Soundboard" / "Boards"
SETTINGS_PATH = Path(os.environ.get("APPDATA", Path.home())) / "RobRunSoundboard" / "settings.json"
PALETTE = ["#e5484d", "#f76b15", "#ffc53d", "#46a758", "#12a594", "#0090ff", "#6e56cf", "#d6409f", "#8d8d8d"]


# --------------------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    "mic_device": "",  # first run: your real mic (Windows default, skipping the cable)
    "game_device": "CABLE Input (VB-Audio Virtual Cable)",
    "monitor_device": "__windows_default__",  # your normal speakers/headphones
    "mic_gain": 1.0,
    "game_gain": 0.8,
    "monitor_gain": 1.0,
    "monitor_on": True,
    "mic_muted": False,
    "hotkeys_enabled": True,
    "auto_fkeys": True,
    "trim_silence": True,
    "stop_hotkey": "ctrl+shift+s",
    "mute_hotkey": "ctrl+shift+m",
    "hud_hotkey": "ctrl+shift+h",
    "hud_unlock_hotkey": "ctrl+shift+j",
    "next_board_hotkey": "ctrl+shift+page down",
    "prev_board_hotkey": "ctrl+shift+page up",
    "show_hud": True,
    "hud_opacity": 0.9,
    "hud_pos": None,
    "last_board": "",
    "extra_boards": [],
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    if not s.get("_monitor_v2"):  # older versions defaulted to a quiet 50% in your headphones
        s["monitor_gain"] = max(float(s.get("monitor_gain", 1.0)), 1.0)
        s["monitor_on"] = True
        s["_monitor_v2"] = True
    if not s.get("mic_device"):
        s["mic_device"] = guess_mic()
    return s


def guess_mic() -> str:
    """Your real microphone: the Windows default input, unless that is already the cable."""
    try:
        hostapis = sd.query_hostapis()
        devs = sd.query_devices()
        ins = [(i, d["name"]) for i, d in enumerate(devs)
               if "WASAPI" in hostapis[d["hostapi"]]["name"] and d["max_input_channels"] > 0]
        default = next((h["default_input_device"] for h in hostapis if "WASAPI" in h["name"]), -1)
        ordered = [n for i, n in ins if i == default] + [n for i, n in ins if i != default]
        return next((n for n in ordered if "CABLE" not in n.upper()), "")
    except Exception:
        return ""


def save_settings(s: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception as e:
        print("could not save settings:", e)


# --------------------------------------------------------------------------- devices

def wasapi_devices(kind: str) -> list[tuple[int, str]]:
    """(index, name) of WASAPI devices with inputs (kind='in') or outputs (kind='out')."""
    out = []
    hostapis = sd.query_hostapis()
    for i, d in enumerate(sd.query_devices()):
        if "WASAPI" not in hostapis[d["hostapi"]]["name"]:
            continue
        ch = d["max_input_channels"] if kind == "in" else d["max_output_channels"]
        if ch > 0:
            out.append((i, d["name"]))
    return out


DEFAULT_OUT = "__windows_default__"


def default_output() -> int | None:
    for h in sd.query_hostapis():
        if "WASAPI" in h["name"] and h["default_output_device"] >= 0:
            return h["default_output_device"]
    return None


def find_device(name: str, kind: str) -> int | None:
    if not name:
        return None
    devs = wasapi_devices(kind)
    for i, n in devs:
        if n == name:
            return i
    for i, n in devs:  # loose match (names sometimes change slightly)
        if name.lower() in n.lower() or n.lower() in name.lower():
            return i
    return None


# --------------------------------------------------------------------------- audio

def trim_silence(data: np.ndarray, threshold_db: float = -45.0, pad_ms: float = 2.0, fade_ms: float = 4.0) -> np.ndarray:
    """Cut the quiet lead-in and tail off a clip so it starts instantly and loops without a gap.

    "Quiet" is relative to the clip's own peak, so quiet recordings are trimmed correctly too.
    A few-ms fade at each edge prevents a click where the loop wraps around.
    """
    if len(data) == 0:
        return data
    level = np.abs(data).max(axis=1)
    peak = float(level.max())
    if peak <= 1e-5:
        return data  # all silence: leave it alone
    thr = max(peak * 10 ** (threshold_db / 20), 1e-4)
    loud = np.flatnonzero(level > thr)
    pad = int(SR * pad_ms / 1000)
    start = max(0, int(loud[0]) - pad)
    end = min(len(data), int(loud[-1]) + 1 + pad)
    out = data[start:end].copy()
    f = min(int(SR * fade_ms / 1000), len(out) // 4)
    if f > 1:
        ramp = np.linspace(0.0, 1.0, f, dtype=np.float32)[:, None]
        out[:f] *= ramp
        out[-f:] *= ramp[::-1]
    return out


def load_clip(path: Path, trim: bool = True) -> np.ndarray:
    data = _read_clip(path)
    return np.ascontiguousarray(trim_silence(data) if trim else data)


def _read_clip(path: Path) -> np.ndarray:
    data, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    if rate != SR and len(data):
        n = int(round(len(data) * SR / rate))
        x_old = np.linspace(0.0, 1.0, len(data), endpoint=False)
        x_new = np.linspace(0.0, 1.0, n, endpoint=False)
        data = np.stack([np.interp(x_new, x_old, data[:, c]) for c in range(2)], axis=1).astype(np.float32)
    return np.ascontiguousarray(data)


class Ring:
    """Mono float ring buffer between the mic stream and the cable stream."""

    def __init__(self, size: int = SR):
        self.buf = np.zeros(size, dtype=np.float32)
        self.size = size
        self.r = 0
        self.n = 0
        self.lock = threading.Lock()

    def write(self, x: np.ndarray) -> None:
        with self.lock:
            k = len(x)
            if k >= self.size:
                x = x[-self.size:]
                k = self.size
            w = (self.r + self.n) % self.size
            first = min(k, self.size - w)
            self.buf[w:w + first] = x[:first]
            self.buf[:k - first] = x[first:]
            self.n += k
            if self.n > self.size:
                self.r = (self.r + self.n - self.size) % self.size
                self.n = self.size
            # keep latency low: never let more than ~80 ms pile up
            limit = int(SR * 0.08)
            if self.n > limit:
                drop = self.n - limit
                self.r = (self.r + drop) % self.size
                self.n -= drop

    def read(self, k: int) -> np.ndarray:
        out = np.zeros(k, dtype=np.float32)
        with self.lock:
            m = min(k, self.n)
            first = min(m, self.size - self.r)
            out[:first] = self.buf[self.r:self.r + first]
            out[first:m] = self.buf[:m - first]
            self.r = (self.r + m) % self.size
            self.n -= m
        return out

    def clear(self) -> None:
        with self.lock:
            self.r = self.n = 0


@dataclass
class Voice:
    data: np.ndarray
    gain: float
    key: str
    pos: int = 0
    loop: bool = False


class Engine:
    def __init__(self, settings: dict):
        self.s = settings
        self.lock = threading.Lock()
        self.game_voices: list[Voice] = []
        self.mon_voices: list[Voice] = []
        self.preview_voices: list[Voice] = []
        self.mon_level = 0.0
        self.monitor_open = False
        self.ring = Ring()
        self.streams: list[sd.Stream] = []
        self.cache: dict[str, np.ndarray] = {}
        self.game_level = 0.0
        self.mic_level = 0.0
        self.errors: list[str] = []

    # ---- streams
    def start(self) -> list[str]:
        self.stop()
        self.errors = []
        ws = sd.WasapiSettings(auto_convert=True)
        game = find_device(self.s["game_device"], "out")
        mic = find_device(self.s["mic_device"], "in")
        if self.s["monitor_device"] == DEFAULT_OUT:
            mon = default_output()
        else:
            mon = find_device(self.s["monitor_device"], "out")
        self.monitor_open = False
        if mon is not None and mon == game:
            self.errors.append("You can't hear sounds: “Let me hear sounds on” is the cable. Pick your headphones in Settings.")
        elif mon is None and self.s["monitor_device"]:
            self.errors.append(f"Headphones not found: {self.s['monitor_device']!r}. Pick them in Settings to hear sounds.")

        if game is None:
            self.errors.append(f"Game output device not found: {self.s['game_device']!r}. Is VB-CABLE installed?")
        else:
            try:
                st = sd.OutputStream(device=game, samplerate=SR, channels=2, dtype="float32",
                                     blocksize=BLOCK, latency="low", extra_settings=ws, callback=self._game_cb)
                st.start()
                self.streams.append(st)
            except Exception as e:
                self.errors.append(f"Could not open game output: {e}")

        if mic is not None:
            try:
                ch = min(2, sd.query_devices(mic)["max_input_channels"])
                st = sd.InputStream(device=mic, samplerate=SR, channels=ch, dtype="float32",
                                    blocksize=BLOCK, latency="low", extra_settings=ws, callback=self._mic_cb)
                st.start()
                self.streams.append(st)
            except Exception as e:
                self.errors.append(f"Could not open microphone: {e}")
        elif self.s["mic_device"]:
            self.errors.append(f"Microphone not found: {self.s['mic_device']!r}")

        if mon is not None and mon != game:
            try:
                st = sd.OutputStream(device=mon, samplerate=SR, channels=2, dtype="float32",
                                     blocksize=BLOCK, latency="low", extra_settings=ws, callback=self._mon_cb)
                st.start()
                self.streams.append(st)
                self.monitor_open = True
            except Exception as e:
                self.errors.append(f"Could not open your headphones (another app may have exclusive control): {e}")
        return self.errors

    def stop(self) -> None:
        for st in self.streams:
            try:
                st.stop()
                st.close()
            except Exception:
                pass
        self.streams = []
        self.ring.clear()

    # ---- callbacks (audio threads)
    def _mic_cb(self, indata, frames, t, status):
        mono = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
        self.mic_level = max(self.mic_level * 0.85, float(np.abs(mono).max(initial=0.0)))
        self.ring.write(mono.astype(np.float32, copy=True))

    def _mix(self, voices: list[Voice], frames: int) -> np.ndarray:
        buf = np.zeros((frames, 2), dtype=np.float32)
        with self.lock:
            alive = []
            for v in voices:
                n = len(v.data)
                filled = 0
                while filled < frames and n:
                    chunk = v.data[v.pos:v.pos + frames - filled]
                    buf[filled:filled + len(chunk)] += chunk * v.gain
                    filled += len(chunk)
                    v.pos += len(chunk)
                    if v.pos >= n:
                        if not v.loop:
                            break
                        v.pos = 0  # loop forever until stopped
                if n and (v.loop or v.pos < n):
                    alive.append(v)
            voices[:] = alive
        return buf

    def _game_cb(self, outdata, frames, t, status):
        mic = self.ring.read(frames)
        out = self._mix(self.game_voices, frames) * float(self.s["game_gain"])
        if not self.s["mic_muted"]:
            out += (mic * float(self.s["mic_gain"]))[:, None]
        np.clip(out, -1.0, 1.0, out=out)
        self.game_level = max(self.game_level * 0.85, float(np.abs(out).max(initial=0.0)))
        outdata[:] = out

    def _mon_cb(self, outdata, frames, t, status):
        gain = float(self.s["monitor_gain"]) if self.s.get("monitor_on", True) else 0.0
        out = self._mix(self.mon_voices, frames) * gain
        out += self._mix(self.preview_voices, frames) * max(gain, 0.8)  # previews always audible
        self.mon_level = max(self.mon_level * 0.85, float(np.abs(out).max(initial=0.0)))
        np.clip(out, -1.0, 1.0, out=out)
        outdata[:] = out

    # ---- control
    def clip(self, path: Path) -> np.ndarray:
        key = str(path)
        mtime = path.stat().st_mtime
        trim = bool(self.s.get("trim_silence", True))
        ck = f"{key}|{mtime}|{trim}"
        if ck not in self.cache:
            self.cache[ck] = load_clip(path, trim)
        return self.cache[ck]

    def play(self, path: Path, gain: float = 1.0, loop: bool = False) -> bool:
        """Start a sound. A looping sound that's already playing is stopped instead (toggle).
        Returns True if it's now playing."""
        data = self.clip(path)
        key = str(path)
        with self.lock:
            was_playing = any(v.key == key for v in self.game_voices)
            # clicking a playing sound again restarts it instead of stacking
            self.game_voices[:] = [v for v in self.game_voices if v.key != key]
            self.mon_voices[:] = [v for v in self.mon_voices if v.key != key]
            if loop and was_playing:
                return False
            self.game_voices.append(Voice(data, gain, key, loop=loop))
            # your own ears never get the boost: headphones play at most 100%
            self.mon_voices.append(Voice(data, min(gain, 1.0), key, loop=loop))
            return True

    def set_loop(self, path: Path, loop: bool) -> None:
        key = str(path)
        with self.lock:
            for v in self.game_voices + self.mon_voices:
                if v.key == key:
                    v.loop = loop

    def preview(self, path: Path, gain: float = 1.0) -> None:
        """Play only in your headphones - the game doesn't hear it."""
        data = self.clip(path)
        with self.lock:
            self.preview_voices[:] = [Voice(data, min(gain, 1.0), str(path))]

    def stop_all(self) -> None:
        with self.lock:
            self.game_voices.clear()
            self.mon_voices.clear()
            self.preview_voices.clear()

    def progress(self) -> dict[str, float]:
        """key -> 0..1 for every sound currently playing."""
        with self.lock:
            return {v.key: min(1.0, v.pos / max(1, len(v.data))) for v in self.game_voices}


# --------------------------------------------------------------------------- boards

@dataclass
class SoundEntry:
    file: str
    name: str
    color: str = ""
    hotkey: str = ""
    volume: float = 1.0
    loop: bool = False


@dataclass
class Board:
    path: Path
    sounds: list[SoundEntry] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def meta(self) -> Path:
        return self.path / "board.json"

    @classmethod
    def open(cls, path: Path) -> "Board":
        b = cls(path)
        b.reload()
        return b

    def reload(self) -> None:
        entries: list[SoundEntry] = []
        try:
            raw = json.loads(self.meta.read_text(encoding="utf-8"))
            for e in raw.get("sounds", []):
                entries.append(SoundEntry(**{k: e[k] for k in ("file", "name", "color", "hotkey", "volume", "loop") if k in e}))
        except Exception:
            pass
        present = {p.name for p in self.path.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS} if self.path.exists() else set()
        entries = [e for e in entries if e.file in present]
        known = {e.file for e in entries}
        for f in sorted(present - known, key=str.lower):
            entries.append(SoundEntry(file=f, name=Path(f).stem))
        self.sounds = entries
        self.save()

    def save(self) -> None:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            self.meta.write_text(json.dumps({"sounds": [e.__dict__ for e in self.sounds]}, indent=2), encoding="utf-8")
        except Exception as e:
            print("could not save board:", e)

    def add_files(self, files: list[str]) -> int:
        n = 0
        for f in files:
            src = Path(f)
            if src.suffix.lower() not in AUDIO_EXTS or not src.is_file():
                continue
            dst = self.path / src.name
            i = 2
            while dst.exists() and dst.resolve() != src.resolve():
                dst = self.path / f"{src.stem} ({i}){src.suffix}"
                i += 1
            if dst.resolve() != src.resolve():
                shutil.copy2(src, dst)
            n += 1
        self.reload()
        return n


CONTROL_KEYS = ["stop_hotkey", "mute_hotkey", "hud_hotkey", "hud_unlock_hotkey", "next_board_hotkey", "prev_board_hotkey"]


def hotkey_map(s: dict, sounds: list[SoundEntry]) -> list[str]:
    """Key for each sound: its custom key if set, otherwise the next free F1-F12 (if enabled)."""
    used = {e.hotkey.lower() for e in sounds if e.hotkey}
    used |= {str(s.get(k, "")).lower() for k in CONTROL_KEYS if s.get(k)}  # never collide with Stop/Mute/etc.
    free = [f"f{i}" for i in range(1, 13) if f"f{i}" not in used] if s.get("auto_fkeys", True) else []
    out = []
    for e in sounds:
        if e.hotkey:
            out.append(e.hotkey)
        elif free:
            out.append(free.pop(0))
        else:
            out.append("")
    return out


def effective_hotkey(s: dict, entry: SoundEntry, idx: int, sounds: list[SoundEntry] | None = None) -> str:
    if sounds is None:
        return entry.hotkey
    return hotkey_map(s, sounds)[idx]


def make_starter_board() -> None:
    """First run: a small board of generated sounds so there's something to test with."""
    board = BOARDS_ROOT / "Starter"
    if board.exists():
        return
    board.mkdir(parents=True, exist_ok=True)
    t = lambda d: np.arange(int(SR * d)) / SR

    def env(x, a=0.01, r=0.15):
        n = len(x)
        e = np.ones(n)
        ai, ri = int(SR * a), int(SR * r)
        e[:ai] = np.linspace(0, 1, ai)
        e[-ri:] = np.linspace(1, 0, ri)
        return x * e

    sounds = {}
    x = t(1.2)
    horn = sum(np.sign(np.sin(2 * np.pi * f * x)) for f in (440, 554, 659)) / 3
    horn *= 1 + 0.15 * np.sin(2 * np.pi * 6 * x)
    sounds["Air Horn"] = env(horn * 0.35, 0.02, 0.2)
    x = t(0.25)
    sounds["Beep"] = env(np.sin(2 * np.pi * 1000 * x) * 0.5, 0.005, 0.05)
    x = t(0.9)
    sounds["Sad Trombone"] = np.concatenate([env(np.sign(np.sin(2 * np.pi * f * t(0.3 if i < 3 else 0.9))) * 0.25, 0.02, 0.08)
                                             for i, f in enumerate((392, 370, 349, 330))])
    rng = np.random.default_rng(7)
    hits = []
    for i in range(24):
        d = 0.09 - i * 0.0025
        hits.append(env(rng.standard_normal(int(SR * d)) * np.exp(-t(d) * 40) * 0.5, 0.001, 0.01))
    hits.append(env(rng.standard_normal(int(SR * 0.8)) * np.exp(-t(0.8) * 4) * 0.6, 0.001, 0.2))
    sounds["Drum Roll"] = np.concatenate(hits)
    x = t(0.6)
    f = 300 + 900 * x / 0.6
    sounds["Ding Up"] = env(np.sin(2 * np.pi * np.cumsum(f) / SR) * 0.45, 0.005, 0.2)

    entries = []
    for i, (name, data) in enumerate(sounds.items()):
        fn = f"{name}.wav"
        sf.write(str(board / fn), np.clip(data, -1, 1).astype(np.float32), SR)
        entries.append({"file": fn, "name": name, "color": PALETTE[i % len(PALETTE)], "hotkey": "", "volume": 1.0})
    (board / "board.json").write_text(json.dumps({"sounds": entries}, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- UI

STYLE = """
QWidget { background: #17181b; color: #e8e8ea; font-family: 'Segoe UI'; font-size: 10pt; }
QListWidget { background: #1e1f23; border: 1px solid #2c2d33; border-radius: 8px; padding: 4px; }
QListWidget::item { padding: 8px 10px; border-radius: 6px; }
QListWidget::item:selected { background: #2f6fed; color: white; }
QListWidget::item:hover:!selected { background: #26272c; }
QPushButton { background: #26272c; border: 1px solid #34353c; border-radius: 6px; padding: 6px 12px; }
QPushButton:hover { background: #2e2f35; }
QPushButton:pressed { background: #1f2024; }
QPushButton#danger { background: #a3262b; border-color: #c2343a; color: white; font-weight: 600; }
QPushButton#danger:hover { background: #bb2f35; }
QPushButton#toggle:checked { background: #a3262b; border-color: #c2343a; }
QLineEdit, QComboBox { background: #1e1f23; border: 1px solid #34353c; border-radius: 6px; padding: 6px 8px; }
QScrollArea { border: none; }
QSlider::groove:horizontal { height: 4px; background: #34353c; border-radius: 2px; }
QSlider::handle:horizontal { background: #2f6fed; width: 14px; margin: -6px 0; border-radius: 7px; }
QSlider::sub-page:horizontal { background: #2f6fed; border-radius: 2px; }
QProgressBar { background: #1e1f23; border: 1px solid #2c2d33; border-radius: 3px; max-height: 6px; }
QProgressBar::chunk { background: #46a758; border-radius: 3px; }
QLabel#muted { color: #9a9aa3; }
QLabel#title { font-size: 15pt; font-weight: 600; }
QMenu { background: #1e1f23; border: 1px solid #34353c; }
QMenu::item:selected { background: #2f6fed; }
"""


class Bridge(QObject):
    """Moves global-hotkey callbacks from the keyboard thread onto the Qt thread."""
    play = Signal(str)
    stop = Signal()
    action = Signal(str)
    captured = Signal(str)


class SoundButton(QPushButton):
    def __init__(self, entry: SoundEntry, idx: int, hotkey: str = ""):
        super().__init__()
        self.entry = entry
        self.idx = idx
        self.hotkey = hotkey
        self.setMinimumSize(QSize(150, 84))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.set_playing(False)

    def set_playing(self, playing: bool) -> None:
        color = self.entry.color or PALETTE[self.idx % len(PALETTE)]
        c = QColor(color)
        bg = c.darker(115).name() if playing else c.darker(260).name()
        border = c.lighter(130).name() if playing else c.name()
        hk = f"\n⌨ {pretty_key(self.hotkey)}" if self.hotkey else ""
        loop = "🔁 " if self.entry.loop else ""
        self.setText(f"{'▶ ' if playing else ''}{loop}{self.entry.name}{hk}")
        self.setStyleSheet(
            f"QPushButton {{ background: {bg}; border: 2px solid {border}; border-radius: 10px;"
            f" font-size: 11pt; font-weight: 600; padding: 8px; }}"
            f"QPushButton:hover {{ background: {c.darker(170).name()}; }}"
        )


HUD_STYLE = """
QFrame#hud { background: rgba(14, 15, 19, 215); border: 1px solid rgba(255, 255, 255, 35); border-radius: 12px; }
QFrame#hud[unlocked="true"] { border: 2px solid #2f6fed; }
QLabel { background: transparent; color: #e8e8ea; font-family: 'Segoe UI'; font-size: 9pt; }
QLabel#hudTitle { font-size: 10pt; font-weight: 700; }
QLabel#hudDim { color: #8f909a; font-size: 8pt; }
QLabel#key { background: rgba(255, 255, 255, 22); border-radius: 4px; padding: 1px 6px; color: #c9cad1;
             font-family: 'Consolas'; font-size: 8pt; }
QLabel#micOn { color: #46a758; font-weight: 700; }
QLabel#micOff { color: #e5484d; font-weight: 700; }
QFrame#row { background: transparent; border-radius: 6px; }
QFrame#row[playing="true"] { background: rgba(47, 111, 237, 70); }
QProgressBar { background: rgba(255, 255, 255, 18); border: none; border-radius: 2px; max-height: 3px; }
QProgressBar::chunk { background: #2f6fed; border-radius: 2px; }
QProgressBar#mic::chunk { background: #46a758; }
"""


class HudRow(QFrame):
    clicked = Signal()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.clicked.emit()
            ev.accept()


class HudWindow(QWidget):
    """Always-on-top overlay for borderless/windowed games.

    Locked (default): click-through and never takes focus, so the game keeps
    the mouse and keyboard. Unlocked: drag it around and click sounds.
    It's a normal separate window - nothing is injected into the game.
    """

    MAX_ROWS = 12
    BASE_FLAGS = (Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus)

    def __init__(self, main: "MainWindow"):
        super().__init__(None, self.BASE_FLAGS)
        self.main = main
        self.unlocked = False
        self._drag = None
        self.rows: list[tuple[SoundEntry, HudRow, QProgressBar]] = []
        self.add_row: HudRow | None = None
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet(HUD_STYLE)
        self.setWindowTitle("Soundboard HUD")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.panel = QFrame()
        self.panel.setObjectName("hud")
        self.panel.setFixedWidth(250)
        outer.addWidget(self.panel)
        v = QVBoxLayout(self.panel)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(3)

        head = QHBoxLayout()
        self.title = QLabel("Soundboard")
        self.title.setObjectName("hudTitle")
        head.addWidget(self.title, 1)
        self.mic = QLabel("")
        head.addWidget(self.mic)
        v.addLayout(head)
        self.mic_bar = QProgressBar()
        self.mic_bar.setObjectName("mic")
        self.mic_bar.setTextVisible(False)
        v.addWidget(self.mic_bar)
        v.addSpacing(4)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(1)
        v.addLayout(self.rows_box)
        self.more = QLabel("")
        self.more.setObjectName("hudDim")
        v.addWidget(self.more)
        self.hint = QLabel("")
        self.hint.setObjectName("hudDim")
        self.hint.setWordWrap(True)
        v.addSpacing(4)
        v.addWidget(self.hint)

        self.setWindowOpacity(float(main.s.get("hud_opacity", 0.9)))
        self._apply_flags()
        self._place()

        # Some games re-raise themselves; keep re-asserting topmost without stealing focus.
        self._top = QTimer(self)
        self._top.timeout.connect(self._raise_topmost)
        self._top.start(2000)

    # ---- window behaviour
    def _apply_flags(self):
        vis = self.isVisible()
        flags = self.BASE_FLAGS if self.unlocked else (self.BASE_FLAGS | Qt.WindowTransparentForInput)
        self.setWindowFlags(flags)
        self.panel.setProperty("unlocked", self.unlocked)
        self.panel.style().unpolish(self.panel)
        self.panel.style().polish(self.panel)
        if self.add_row is not None:
            self.add_row.setVisible(self.unlocked)
            self.adjustSize()
        s = self.main.s
        if self.unlocked:
            self.hint.setText(f"Unlocked: drag to move, click to play · {s['hud_unlock_hotkey'] or 'unlock hotkey'} to lock")
        else:
            self.hint.setText(f"{s['hud_hotkey']}: hide · {s['hud_unlock_hotkey']}: unlock · {s['stop_hotkey']}: stop")
        if vis:
            self.show()

    def _place(self):
        pos = self.main.s.get("hud_pos")
        self.adjustSize()
        if pos:
            self.move(int(pos[0]), int(pos[1]))
        else:
            geo = QApplication.primaryScreen().availableGeometry()
            self.move(geo.right() - self.width() - 24, geo.top() + 120)

    def _raise_topmost(self):
        if not self.isVisible():
            return
        try:  # HWND_TOPMOST, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE
            ctypes.windll.user32.SetWindowPos(int(self.winId()), -1, 0, 0, 0, 0, 0x0002 | 0x0001 | 0x0010)
        except Exception:
            pass

    def toggle_unlocked(self):
        self.unlocked = not self.unlocked
        self._apply_flags()
        self._raise_topmost()

    # ---- content
    def rebuild(self, board: "Board | None"):
        for _, row, _ in self.rows:
            row.deleteLater()
        self.rows = []
        if self.add_row is not None:
            self.add_row.deleteLater()
            self.add_row = None
        if not board:
            return
        self.title.setText(board.name)
        for i, e in enumerate(board.sounds[: self.MAX_ROWS]):
            hk = effective_hotkey(self.main.s, e, i, board.sounds)
            row = HudRow()
            row.setObjectName("row")
            rv = QVBoxLayout(row)
            rv.setContentsMargins(6, 3, 6, 3)
            rv.setSpacing(2)
            line = QHBoxLayout()
            if hk:
                key = QLabel(pretty_key(hk))
                key.setObjectName("key")
                key.setMinimumWidth(34)
                key.setAlignment(Qt.AlignCenter)
                line.addWidget(key)
            name = QLabel(("🔁 " if e.loop else "") + e.name)
            line.addWidget(name, 1)
            rv.addLayout(line)
            bar = QProgressBar()
            bar.setTextVisible(False)
            bar.setRange(0, 1000)
            bar.hide()
            rv.addWidget(bar)
            row.clicked.connect(lambda e=e: self.unlocked and self.main.play(e))
            self.rows_box.addWidget(row)
            self.rows.append((e, row, bar))
        add = HudRow()
        add.setObjectName("row")
        al = QHBoxLayout(add)
        al.setContentsMargins(6, 3, 6, 3)
        lbl = QLabel("＋ Add sounds / change keys…")
        lbl.setObjectName("hudDim")
        al.addWidget(lbl)
        add.clicked.connect(lambda: self.unlocked and self.main.open_keybinds())
        add.setVisible(self.unlocked)
        self.rows_box.addWidget(add)
        self.add_row = add
        extra = len(board.sounds) - self.MAX_ROWS
        self.more.setText(f"+{extra} more on this board" if extra > 0 else "")
        self.more.setVisible(extra > 0)
        self.adjustSize()

    def update_state(self, progress: dict[str, float], board_path: Path | None, muted: bool, mic_level: float):
        if not self.isVisible():
            return
        self.mic.setText("● MIC OFF" if muted else "● MIC")
        self.mic.setObjectName("micOff" if muted else "micOn")
        self.mic.style().unpolish(self.mic)
        self.mic.style().polish(self.mic)
        self.mic_bar.setValue(0 if muted else int(min(1.0, mic_level) * 100))
        for e, row, bar in self.rows:
            p = progress.get(str(board_path / e.file)) if board_path else None
            playing = p is not None
            if row.property("playing") != playing:
                row.setProperty("playing", playing)
                row.style().unpolish(row)
                row.style().polish(row)
                bar.setVisible(playing)
                self.adjustSize()
            if playing:
                bar.setValue(int(p * 1000))

    # ---- dragging (only while unlocked)
    def mousePressEvent(self, ev):
        if self.unlocked and ev.button() == Qt.LeftButton:
            self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, ev):
        if self._drag is not None:
            self.move(ev.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            self._drag = None
            self.main.s["hud_pos"] = [self.x(), self.y()]
            save_settings(self.main.s)


HOTKEY_FIELDS = [
    ("stop_hotkey", "Stop all sounds"),
    ("mute_hotkey", "Mute / unmute mic"),
    ("hud_hotkey", "Show / hide HUD"),
    ("hud_unlock_hotkey", "Unlock HUD (move/click)"),
    ("next_board_hotkey", "Next board"),
    ("prev_board_hotkey", "Previous board"),
]


class SettingsDialog(QDialog):
    def __init__(self, parent, s: dict, bridge: Bridge):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self.s = s
        self.bridge = bridge
        form = QFormLayout(self)

        def combo(kind, current, allow_none, allow_default=False):
            cb = QComboBox()
            if allow_none:
                cb.addItem("(off)", "")
            if allow_default:
                cb.addItem("Windows default output", DEFAULT_OUT)
            for _, n in wasapi_devices(kind):
                cb.addItem(n, n)
            i = cb.findData(current)
            cb.setCurrentIndex(max(i, 0))
            return cb

        self.mic = combo("in", s["mic_device"], True)
        self.game = combo("out", s["game_device"], False)
        self.mon = combo("out", s["monitor_device"], True, allow_default=True)
        form.addRow("Your microphone", self.mic)
        form.addRow("Send to game (cable)", self.game)
        form.addRow("Let me hear sounds on", self.mon)
        hint = QLabel("“Send to game” should be CABLE Input. In Rob Run, pick CABLE Output as your microphone.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        form.addRow(hint)

        self.hk = QCheckBox("Global hotkeys (work while Rob Run is focused)")
        self.hk.setChecked(s["hotkeys_enabled"])
        self.hk.setEnabled(keyboard is not None)
        form.addRow(self.hk)
        self.fkeys = QCheckBox("Give the first 12 sounds F1–F12 automatically (a custom hotkey overrides)")
        self.fkeys.setChecked(s.get("auto_fkeys", True))
        form.addRow(self.fkeys)
        self.trim = QCheckBox("Cut silence off the start and end of sounds (starts instantly, loops without gaps)")
        self.trim.setChecked(s.get("trim_silence", True))
        form.addRow(self.trim)
        self.hk_edits: dict[str, QLineEdit] = {}
        for key, label in HOTKEY_FIELDS:
            row = QHBoxLayout()
            ed = QLineEdit(s.get(key, ""))
            ed.setReadOnly(True)
            ed.setPlaceholderText("(none)")
            rec = QPushButton("Record…")
            rec.clicked.connect(lambda _=False, ed=ed: self._record(ed))
            clr = QPushButton("Clear")
            clr.clicked.connect(lambda _=False, ed=ed: ed.setText(""))
            row.addWidget(ed, 1)
            row.addWidget(rec)
            row.addWidget(clr)
            form.addRow(label, row)
            self.hk_edits[key] = ed

        self.show_hud = QCheckBox("Show in-game HUD overlay")
        self.show_hud.setChecked(s.get("show_hud", True))
        form.addRow(self.show_hud)
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(30, 100)
        self.opacity.setValue(int(s.get("hud_opacity", 0.9) * 100))
        form.addRow("HUD opacity", self.opacity)
        reset = QPushButton("Reset HUD position")
        reset.clicked.connect(lambda: s.update(hud_pos=None))
        form.addRow(reset)
        hint2 = QLabel("The HUD shows over Rob Run in Borderless/Windowed mode (not exclusive fullscreen).")
        hint2.setObjectName("muted")
        hint2.setWordWrap(True)
        form.addRow(hint2)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _record(self, ed: QLineEdit):
        ed.setText("press keys…")
        record_hotkey(self.bridge, lambda hk: ed.setText(hk))

    def apply(self) -> None:
        self.s["mic_device"] = self.mic.currentData()
        self.s["game_device"] = self.game.currentData()
        self.s["monitor_device"] = self.mon.currentData()
        self.s["hotkeys_enabled"] = self.hk.isChecked()
        self.s["auto_fkeys"] = self.fkeys.isChecked()
        self.s["trim_silence"] = self.trim.isChecked()
        for key, ed in self.hk_edits.items():
            t = ed.text()
            self.s[key] = "" if t == "press keys…" else t
        self.s["show_hud"] = self.show_hud.isChecked()
        self.s["hud_opacity"] = self.opacity.value() / 100


_capture_target = None


def record_hotkey(bridge: Bridge, on_done) -> None:
    """Capture the next key combo OR mouse button (middle / 4 / 5, with optional ctrl/shift/alt)."""
    global _capture_target
    _capture_target = on_done
    # don't fire sounds while the user is pressing the key they want to bind
    KEYS.set_bindings([])
    MOUSE.set_bindings({})

    state = {"done": False, "kb": None}
    pressed: list[str] = []
    lock = threading.Lock()

    def finish(hk: str):
        with lock:
            if state["done"]:
                return
            state["done"] = True
        MOUSE.capture = None
        if state["kb"] is not None and keyboard is not None:
            try:
                keyboard.unhook(state["kb"])
            except Exception:
                pass
        bridge.captured.emit(hk)

    def on_key(ev):
        if ev.event_type == "down":
            if ev.name and ev.name not in pressed:
                pressed.append(ev.name)
        elif pressed:  # first key released -> the combo is complete
            try:
                name = keyboard.get_hotkey_name(pressed)
            except Exception:
                name = "+".join(pressed)
            finish(name)

    MOUSE.capture = finish
    if keyboard is not None:
        try:
            state["kb"] = keyboard.hook(on_key)
        except Exception:
            pass


# --------------------------------------------------------------------------- mouse hotkeys

MOUSE_BUTTONS = {"mouse3": "Middle mouse", "mouse4": "Mouse 4", "mouse5": "Mouse 5"}


def parse_mouse_hotkey(hk: str) -> tuple[frozenset, str] | None:
    parts = [p.strip() for p in hk.lower().split("+") if p.strip()]
    if not parts or parts[-1] not in MOUSE_BUTTONS:
        return None
    return frozenset(parts[:-1]), parts[-1]


def pretty_key(hk: str) -> str:
    if not hk:
        return ""
    parts = hk.split("+")
    return "+".join(MOUSE_BUTTONS.get(p.strip().lower(), p.strip().upper()) for p in parts)


class MouseHook:
    """Low-level Windows mouse hook for middle / side buttons (Mouse 4 = back, Mouse 5 = forward).

    Only listens; clicks still reach the game. Runs its own thread with a message loop.
    """

    WM_MBUTTONDOWN = 0x0207
    WM_XBUTTONDOWN = 0x020B

    def __init__(self):
        self.bindings: dict[tuple[frozenset, str], object] = {}
        self.capture = None  # callable(hk) while recording a hotkey
        self._thread = None
        self._proc = None

    def set_bindings(self, b: dict) -> None:
        self.bindings = dict(b)

    @staticmethod
    def _mods() -> frozenset:
        gk = ctypes.windll.user32.GetAsyncKeyState
        mods = set()
        if gk(0x11) & 0x8000:
            mods.add("ctrl")
        if gk(0x10) & 0x8000:
            mods.add("shift")
        if gk(0x12) & 0x8000:
            mods.add("alt")
        return frozenset(mods)

    def _on_button(self, button: str) -> None:
        mods = self._mods()
        cap = self.capture
        if cap is not None:
            order = [m for m in ("ctrl", "shift", "alt") if m in mods]
            threading.Thread(target=cap, args=("+".join(order + [button]),), daemon=True).start()
            return
        # Extra held keys (W, Shift-sprint, ...) never block a bind: the bind only needs ITS modifiers.
        # If both "mouse4" and "ctrl+mouse4" exist, the most specific one that matches wins.
        best = None
        for (need, b), fn in self.bindings.items():
            if b == button and need <= mods and (best is None or len(need) > len(best[0])):
                best = (need, fn)
        cb = best[1] if best else None
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def start(self) -> None:
        if self._thread or sys.platform != "win32":
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = LRESULT
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        def proc(n_code, w_param, l_param):
            if n_code == 0 and w_param in (self.WM_XBUTTONDOWN, self.WM_MBUTTONDOWN):
                if w_param == self.WM_MBUTTONDOWN:
                    button = "mouse3"
                else:
                    info = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    button = "mouse4" if (info.mouseData >> 16) & 0xFFFF == 1 else "mouse5"
                self._on_button(button)
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        self._proc = HOOKPROC(proc)  # keep a reference so it isn't garbage-collected
        hook = user32.SetWindowsHookExW(14, self._proc, kernel32.GetModuleHandleW(None), 0)  # WH_MOUSE_LL
        if not hook:
            print("mouse hook failed:", ctypes.get_last_error())
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


MOUSE = MouseHook()


class KeyHook:
    """Keyboard hotkeys that fire on the key going down, no matter what else is held.

    The `keyboard` module's add_hotkey only fires when exactly that combo is pressed, so holding
    W to move (or Shift to sprint) blocked every bind. Here a bind only checks its own trigger key
    and its own modifiers; any other held keys are ignored.
    """

    def __init__(self):
        self.bindings: list[tuple[list[tuple[int, ...]], tuple[int, ...], object]] = []
        self.down: set[int] = set()
        self.hook = None

    @staticmethod
    def parse(hk: str):
        """'ctrl+shift+s' -> ([ctrl scancodes, shift scancodes], s scancodes)."""
        if keyboard is None or not hk:
            return None
        try:
            steps = keyboard.parse_hotkey(hk)
        except Exception:
            return None
        if len(steps) != 1 or not steps[0]:
            return None
        combo = steps[0]
        return [tuple(k) for k in combo[:-1]], tuple(combo[-1])

    def set_bindings(self, b: list) -> None:
        self.bindings = list(b)

    def start(self) -> None:
        if keyboard is None or self.hook is not None:
            return
        try:
            self.hook = keyboard.hook(self._on)
        except Exception as e:
            print("keyboard hook failed:", e)

    def _on(self, ev) -> None:
        sc = ev.scan_code
        if ev.event_type == "up":
            self.down.discard(sc)
            return
        if sc in self.down:  # held key auto-repeating: fire once per press
            return
        self.down.add(sc)
        best = None
        for mods, trig, cb in self.bindings:
            if sc in trig and all(any(m in self.down for m in mod) for mod in mods):
                if best is None or len(mods) > len(best[0]):
                    best = (mods, cb)
        if best:
            try:
                best[1]()
            except Exception:
                pass


KEYS = KeyHook()


class KeybindsDialog(QDialog):
    """Clickable list of every sound on the board: hear it, change/clear its key, add or remove sounds."""

    def __init__(self, main: "MainWindow"):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Sounds & Keybinds")
        self.setMinimumSize(880, 520)
        self.recording: SoundEntry | None = None
        v = QVBoxLayout(self)

        top = QHBoxLayout()
        self.title = QLabel("")
        self.title.setObjectName("title")
        top.addWidget(self.title, 1)
        add = QPushButton("＋ Add sounds…")
        add.clicked.connect(self._add)
        top.addWidget(add)
        v.addLayout(top)

        self.info = QLabel("Click “Change key”, then press the key (or combo) you want. Esc cancels.")
        self.info.setObjectName("muted")
        self.info.setWordWrap(True)
        v.addWidget(self.info)

        self.fkeys = QCheckBox("Sounds without a custom key get the next free F1–F12 automatically")
        self.fkeys.setChecked(main.s.get("auto_fkeys", True))
        self.fkeys.toggled.connect(self._fkeys)
        v.addWidget(self.fkeys)

        self.list_host = QWidget()
        self.rows = QVBoxLayout(self.list_host)
        self.rows.setAlignment(Qt.AlignTop)
        self.rows.setSpacing(4)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.list_host)
        v.addWidget(scroll, 1)

        bottom = QHBoxLayout()
        stop = QPushButton("■ Stop")
        stop.clicked.connect(main.stop_all)
        bottom.addWidget(stop)
        bottom.addStretch(1)
        close = QPushButton("Done")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        v.addLayout(bottom)
        self.refresh()

    def refresh(self):
        while self.rows.count():
            it = self.rows.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        b = self.main.board
        if not b:
            return
        self.title.setText(f"Board: {b.name}")
        s = self.main.s

        def header(text):
            lbl = QLabel(text)
            lbl.setStyleSheet("QLabel { font-weight: 700; color: #9a9aa3; padding: 8px 2px 2px 2px; }")
            self.rows.addWidget(lbl)

        def key_badge(target, hk, auto):
            rec = self.recording is target if not isinstance(target, str) else self.recording == target
            key = QLabel("press key / mouse…" if rec else (pretty_key(hk) if hk else "—"))
            key.setFixedWidth(150)
            key.setAlignment(Qt.AlignCenter)
            colors = ("#7a5b00", "#ffe08a") if rec else (("#26272c", "#c9cad1") if auto else ("#1d3f8a", "#ffffff"))
            key.setStyleSheet("QLabel { background: %s; border-radius: 6px; padding: 5px; font-family: Consolas;"
                              " font-weight: 700; color: %s; }" % colors)
            key.setToolTip("Automatic F-key" if auto else ("Custom key" if hk else "No key"))
            return key

        def make_row(badge, label, buttons):
            row = QFrame()
            row.setStyleSheet("QFrame { background: #1e1f23; border-radius: 8px; }")
            h = QHBoxLayout(row)
            h.setContentsMargins(10, 6, 10, 6)
            h.addWidget(badge)
            name = QLabel(label)
            name.setMinimumWidth(80)
            name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            name.setToolTip(label)
            name.setStyleSheet("QLabel { background: transparent; font-weight: 600; }")
            h.addWidget(name, 1)
            for text, tip, fn, enabled, checked in buttons:
                btn = QPushButton(text)
                btn.setToolTip(tip)
                if text in ("↑", "↓", "🗑"):
                    btn.setFixedWidth(36)
                if checked is not None:
                    btn.setCheckable(True)
                    btn.setChecked(checked)
                    btn.setStyleSheet("QPushButton:checked { background: #1d6b3a; border-color: #46a758; }")
                btn.setEnabled(enabled)
                btn.clicked.connect(fn)
                h.addWidget(btn)
            self.rows.addWidget(row)

        # ---- controls (Stop all, mute, HUD...)
        header("CONTROLS")
        for ck, label in HOTKEY_FIELDS:
            hk = s.get(ck, "")
            make_row(key_badge(ck, hk, False), label, [
                ("Change key", "Click, then press a key or mouse button", lambda _=False, ck=ck: self._record(ck), True, None),
                ("Clear", "Remove this key", lambda _=False, ck=ck: self._set_control(ck, ""), bool(hk), None),
            ])

        # ---- sounds
        header("SOUNDS")
        keys = hotkey_map(s, b.sounds)
        if not b.sounds:
            empty = QLabel("No sounds yet. Click “＋ Add sounds…” to pick your own files.")
            empty.setObjectName("muted")
            self.rows.addWidget(empty)
        for e, hk in zip(b.sounds, keys):
            make_row(key_badge(e, hk, bool(hk and not e.hotkey)), e.name, [
                ("🎧 Hear", "Play only in your headphones (the game doesn't hear it)",
                 lambda _=False, e=e: self.main.preview(e), True, None),
                ("🔁 Loop", "Repeat forever until you press its key again or Stop",
                 lambda on, e=e: self.main._update(e, loop=on), True, e.loop),
                (f"🔊 {int(round(e.volume * 100))}%", "Volume for this sound in the game (up to 1000%)",
                 lambda _=False, e=e: (self.main._volume(e), self.refresh()), True, None),
                ("Change key", "Click, then press a key or mouse button", lambda _=False, e=e: self._record(e), True, None),
                ("Clear", "Remove the custom key", lambda _=False, e=e: self._set_key(e, ""), bool(e.hotkey), None),
                ("↑", "Move up", lambda _=False, e=e: self._move(e, -1), True, None),
                ("↓", "Move down", lambda _=False, e=e: self._move(e, 1), True, None),
                ("🗑", "Delete this sound", lambda _=False, e=e: self._delete(e), True, None),
            ])

    def _record(self, target):
        """target is a SoundEntry or a control setting name like 'stop_hotkey'."""
        self.recording = target
        what = dict(HOTKEY_FIELDS).get(target, "") if isinstance(target, str) else target.name
        self.info.setText(f"Press the key or mouse button (middle / 4 / 5) for “{what}” now… Esc cancels.")
        self.info.setStyleSheet("color: #ffc53d;")
        self.refresh()
        record_hotkey(self.main.bridge, lambda hk, t=target: self._captured(t, hk))

    def _free_key(self, hk: str, keep) -> list[str]:
        """A key belongs to one thing only: take it away from any other sound/control."""
        taken = []
        for o in self.main.board.sounds:
            if o is not keep and o.hotkey.lower() == hk.lower():
                o.hotkey = ""
                taken.append(o.name)
        for ck, label in HOTKEY_FIELDS:
            if ck != keep and str(self.main.s.get(ck, "")).lower() == hk.lower():
                self.main.s[ck] = ""
                taken.append(label)
        self.main.board.save()
        save_settings(self.main.s)
        return taken

    def _captured(self, target, hk: str):
        self.recording = None
        self.info.setStyleSheet("")
        if hk and hk != "esc":
            taken = self._free_key(hk, target)
            what = dict(HOTKEY_FIELDS).get(target, "") if isinstance(target, str) else target.name
            self.info.setText(f"“{what}” is now on {pretty_key(hk)}" +
                              (f" (removed it from {', '.join(taken)})" if taken else ""))
            if isinstance(target, str):
                self._set_control(target, hk)
            else:
                self._set_key(target, hk)
        else:
            self.info.setText("Cancelled.")
            self.main.rebuild_grid()
            self.refresh()

    def _set_control(self, ck: str, hk: str):
        self.main.s[ck] = hk
        save_settings(self.main.s)
        self.main.rebuild_grid()
        self.main.hud._apply_flags()
        self.refresh()

    def _set_key(self, e, hk):
        self.main._update(e, hotkey=hk)
        self.refresh()

    def _move(self, e, d):
        self.main._move(e, d)
        self.refresh()

    def _delete(self, e):
        self.main._delete_sound(e)
        self.refresh()

    def _add(self):
        self.main.add_sounds()
        self.refresh()

    def _fkeys(self, on):
        self.main.s["auto_fkeys"] = on
        save_settings(self.main.s)
        self.main.rebuild_grid()
        self.refresh()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 700)
        self.setAcceptDrops(True)
        self.s = load_settings()
        self.engine = Engine(self.s)
        self.bridge = Bridge()
        self.bridge.play.connect(self._play_by_file)
        self.bridge.stop.connect(self.stop_all)
        self.bridge.action.connect(self._on_action)
        self.bridge.captured.connect(self._on_captured)
        self.board: Board | None = None
        self.buttons: list[SoundButton] = []

        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(14)

        # sidebar
        side = QVBoxLayout()
        lbl = QLabel("Boards")
        lbl.setObjectName("title")
        side.addWidget(lbl)
        self.board_list = QListWidget()
        self.board_list.setMinimumWidth(220)
        self.board_list.setMaximumWidth(260)
        self.board_list.currentItemChanged.connect(self._board_selected)
        self.board_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.board_list.customContextMenuRequested.connect(self._board_menu)
        side.addWidget(self.board_list, 1)
        for text, fn in (("＋ New board", self.new_board), ("Open folder as board…", self.open_folder),
                         ("Show boards folder", lambda: os.startfile(BOARDS_ROOT))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            side.addWidget(b)
        outer.addLayout(side)

        # main
        main = QVBoxLayout()
        top = QHBoxLayout()
        self.board_title = QLabel("")
        self.board_title.setObjectName("title")
        top.addWidget(self.board_title)
        top.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search sounds…")
        self.search.setFixedWidth(220)
        self.search.textChanged.connect(self._filter)
        top.addWidget(self.search)
        add = QPushButton("＋ Add sounds…")
        add.clicked.connect(self.add_sounds)
        top.addWidget(add)
        keys = QPushButton("⌨ Sounds && Keybinds")
        keys.setToolTip("Add sounds, hear them, and change their keys")
        keys.clicked.connect(self.open_keybinds)
        top.addWidget(keys)
        gear = QPushButton("⚙ Settings")
        gear.clicked.connect(self.open_settings)
        top.addWidget(gear)
        main.addLayout(top)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(10)
        self.grid.setAlignment(Qt.AlignTop)
        self.add_tile = QPushButton("＋\nAdd your own sound")
        self.add_tile.setMinimumSize(QSize(150, 84))
        self.add_tile.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.add_tile.setToolTip("Pick .mp3/.wav/.ogg/.flac files - or drag them onto this window")
        self.add_tile.setStyleSheet("QPushButton { background: transparent; border: 2px dashed #4a4b53;"
                                    " border-radius: 10px; color: #9a9aa3; font-size: 11pt; font-weight: 600; }"
                                    "QPushButton:hover { border-color: #2f6fed; color: #e8e8ea; }")
        self.add_tile.clicked.connect(self.add_sounds)
        self.add_tile.hide()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.grid_host)
        main.addWidget(scroll, 1)
        self.empty = QLabel("This board is empty.\nDrag audio files here or click “Add sounds…”.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setObjectName("muted")
        main.addWidget(self.empty)

        # bottom bar
        bar = QHBoxLayout()
        stop = QPushButton("■ Stop all")
        stop.setObjectName("danger")
        stop.clicked.connect(self.stop_all)
        bar.addWidget(stop)
        self.mute = QPushButton("🎤 Mic on")
        self.mute.setObjectName("toggle")
        self.mute.setCheckable(True)
        self.mute.setChecked(self.s["mic_muted"])
        self.mute.toggled.connect(self._mute)
        self._mute(self.s["mic_muted"])
        bar.addWidget(self.mute)
        self.hud_btn = QPushButton("🖥 HUD")
        self.hud_btn.setCheckable(True)
        self.hud_btn.setChecked(self.s.get("show_hud", True))
        self.hud_btn.setToolTip("Show the overlay on top of the game")
        self.hud_btn.toggled.connect(self._set_hud)
        bar.addWidget(self.hud_btn)
        self.hear_btn = QPushButton()
        self.hear_btn.setCheckable(True)
        self.hear_btn.setChecked(self.s.get("monitor_on", True))
        self.hear_btn.setToolTip("Also play sounds in your own headphones")
        self.hear_btn.toggled.connect(self._set_hear)
        self._set_hear(self.hear_btn.isChecked())
        bar.addWidget(self.hear_btn)
        test = QPushButton("▶ Test 🎧")
        test.setToolTip("Play a beep only in your headphones")
        test.clicked.connect(self.test_headphones)
        bar.addWidget(test)
        bar.addSpacing(10)
        max_pct = {"mic_gain": 300, "game_gain": 1000, "monitor_gain": 100}
        for label, key in (("Mic", "mic_gain"), ("Sounds → game", "game_gain"), ("Sounds → me", "monitor_gain")):
            col = QVBoxLayout()
            l = QLabel()
            l.setObjectName("muted")
            col.addWidget(l)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, max_pct[key])
            sl.setValue(int(round(self.s[key] * 100)))
            sl.setFixedWidth(160 if key == "game_gain" else 140)

            def changed(v, k=key, l=l, name=label):
                self._gain(k, v)
                l.setText(f"{name}: {v}%" + ("  🔥" if v > 100 else ""))

            sl.valueChanged.connect(changed)
            changed(sl.value())
            col.addWidget(sl)
            bar.addLayout(col)
        bar.addSpacing(10)
        meters = QVBoxLayout()
        self.mic_meter = QProgressBar()
        self.mic_meter.setTextVisible(False)
        self.game_meter = QProgressBar()
        self.game_meter.setTextVisible(False)
        for txt, m in (("Mic in", self.mic_meter), ("To game", self.game_meter)):
            r = QHBoxLayout()
            l = QLabel(txt)
            l.setObjectName("muted")
            l.setFixedWidth(55)
            r.addWidget(l)
            r.addWidget(m)
            meters.addLayout(r)
        bar.addLayout(meters, 1)
        main.addLayout(bar)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        main.addWidget(self.status)
        outer.addLayout(main, 1)

        QShortcut(QKeySequence("Escape"), self, activated=self.stop_all)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

        self.hud = HudWindow(self)
        BOARDS_ROOT.mkdir(parents=True, exist_ok=True)
        make_starter_board()
        self.refresh_boards(select=self.s.get("last_board") or "")
        self.restart_audio()
        if self.s.get("show_hud", True):
            self.hud.show()

    # ---------------- HUD
    def _set_hud(self, on: bool):
        self.s["show_hud"] = on
        save_settings(self.s)
        if on:
            self.hud.show()
            self.hud._raise_topmost()
        else:
            self.hud.hide()

    def _on_action(self, name: str):
        if name == "hud":
            self.hud_btn.toggle()
        elif name == "unlock":
            if not self.hud.isVisible():
                self.hud_btn.setChecked(True)
            self.hud.toggle_unlocked()
        elif name == "mute":
            self.mute.toggle()
        elif name in ("next", "prev") and self.board_list.count():
            step = 1 if name == "next" else -1
            self.board_list.setCurrentRow((self.board_list.currentRow() + step) % self.board_list.count())

    # ---------------- audio
    def restart_audio(self):
        errs = self.engine.start()
        if errs:
            self.status.setText("⚠ " + "  ".join(errs))
            self.status.setStyleSheet("color: #ff8b8b;")
        else:
            mon = self.s["monitor_device"] or "off"
            if mon == DEFAULT_OUT:
                mon = "Windows default output"
            self.status.setText(f"Live → {self.s['game_device']}   ·   Mic: {self.s['mic_device'] or 'off'}   ·   You hear sounds on: {mon}")
            self.status.setStyleSheet("")
        self.register_hotkeys()

    def _gain(self, key, v):
        self.s[key] = v / 100
        save_settings(self.s)

    def _mute(self, on):
        self.s["mic_muted"] = on
        self.mute.setText("🎤 Mic muted" if on else "🎤 Mic on")
        save_settings(self.s)

    def stop_all(self):
        self.engine.stop_all()

    def _tick(self):
        self.mic_meter.setValue(int(min(1.0, self.engine.mic_level) * 100))
        self.game_meter.setValue(int(min(1.0, self.engine.game_level) * 100))
        progress = self.engine.progress()
        for b in self.buttons:
            p = str(self.board.path / b.entry.file) in progress if self.board else False
            if getattr(b, "_was", None) != p:
                b._was = p
                b.set_playing(p)
        self.hud.update_state(progress, self.board.path if self.board else None,
                              self.s["mic_muted"], self.engine.mic_level)
        self.engine.mic_level *= 0.9
        self.engine.game_level *= 0.9

    # ---------------- boards
    def all_board_paths(self) -> list[Path]:
        paths = sorted([p for p in BOARDS_ROOT.iterdir() if p.is_dir()], key=lambda p: p.name.lower())
        for extra in self.s.get("extra_boards", []):
            p = Path(extra)
            if p.is_dir() and p not in paths:
                paths.append(p)
        return paths

    def refresh_boards(self, select: str = ""):
        self.board_list.blockSignals(True)
        self.board_list.clear()
        target = None
        for p in self.all_board_paths():
            it = QListWidgetItem(("📁 " if p.parent != BOARDS_ROOT else "") + p.name)
            it.setData(Qt.UserRole, str(p))
            it.setToolTip(str(p))
            self.board_list.addItem(it)
            if str(p) == select:
                target = it
        self.board_list.blockSignals(False)
        if target is None and self.board_list.count():
            target = self.board_list.item(0)
        if target:
            self.board_list.setCurrentItem(target)

    def _board_selected(self, item, _prev=None):
        if not item:
            return
        self.board = Board.open(Path(item.data(Qt.UserRole)))
        self.s["last_board"] = str(self.board.path)
        save_settings(self.s)
        self.board_title.setText(self.board.name)
        self.rebuild_grid()
        self.register_hotkeys()

    def _board_menu(self, pos):
        item = self.board_list.itemAt(pos)
        if not item:
            return
        path = Path(item.data(Qt.UserRole))
        m = QMenu(self)
        m.addAction("Open in Explorer", lambda: os.startfile(path))
        if path.parent == BOARDS_ROOT:
            m.addAction("Rename…", lambda: self.rename_board(path))
            m.addAction("Delete board…", lambda: self.delete_board(path))
        else:
            m.addAction("Remove from list", lambda: self.forget_board(path))
        m.exec(self.board_list.mapToGlobal(pos))

    def new_board(self):
        name, ok = QInputDialog.getText(self, "New board", "Board name:")
        name = "".join(c for c in name if c not in '\\/:*?"<>|').strip()
        if not ok or not name:
            return
        p = BOARDS_ROOT / name
        p.mkdir(parents=True, exist_ok=True)
        self.refresh_boards(select=str(p))

    def open_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Open a folder of sounds as a board", str(Path.home()))
        if not d:
            return
        p = Path(d)
        if p.parent != BOARDS_ROOT and str(p) not in self.s["extra_boards"]:
            self.s["extra_boards"].append(str(p))
            save_settings(self.s)
        self.refresh_boards(select=str(p))

    def rename_board(self, path: Path):
        name, ok = QInputDialog.getText(self, "Rename board", "New name:", text=path.name)
        name = "".join(c for c in name if c not in '\\/:*?"<>|').strip()
        if ok and name and name != path.name:
            new = path.with_name(name)
            path.rename(new)
            self.refresh_boards(select=str(new))

    def delete_board(self, path: Path):
        n = len([p for p in path.iterdir() if p.suffix.lower() in AUDIO_EXTS])
        if QMessageBox.question(self, "Delete board",
                                f"Move board “{path.name}” and its {n} sound file(s) to the Recycle Bin?") != QMessageBox.Yes:
            return
        try:
            from send2trash import send2trash  # type: ignore
            send2trash(str(path))
        except Exception:
            trash = BOARDS_ROOT.parent / "Deleted boards"
            trash.mkdir(exist_ok=True)
            shutil.move(str(path), str(trash / path.name))
        self.refresh_boards()

    def forget_board(self, path: Path):
        self.s["extra_boards"] = [b for b in self.s["extra_boards"] if b != str(path)]
        save_settings(self.s)
        self.refresh_boards()

    # ---------------- sounds grid
    def rebuild_grid(self):
        for b in self.buttons:
            b.deleteLater()
        self.buttons = []
        if not self.board:
            return
        for i, e in enumerate(self.board.sounds):
            b = SoundButton(e, i, effective_hotkey(self.s, e, i, self.board.sounds))
            b.clicked.connect(lambda _=False, e=e: self.play(e))
            b.customContextMenuRequested.connect(lambda pos, b=b: self._sound_menu(b, pos))
            self.buttons.append(b)
        self._filter(self.search.text())
        self.hud.rebuild(self.board)
        self.register_hotkeys()

    def _filter(self, text: str):
        text = text.lower().strip()
        for b in self.buttons:
            self.grid.removeWidget(b)
            b.hide()
        shown = [b for b in self.buttons if not text or text in b.entry.name.lower()]
        if self.board and not text:
            shown.append(self.add_tile)
        else:
            self.grid.removeWidget(self.add_tile)
            self.add_tile.hide()
        cols = max(1, (self.grid_host.width() or 800) // 170)
        for i, b in enumerate(shown):
            self.grid.addWidget(b, i // cols, i % cols)
            b.show()
        self.empty.setVisible(not self.buttons)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._filter(self.search.text())

    def _set_hear(self, on: bool):
        self.s["monitor_on"] = on
        self.hear_btn.setText("🎧 I hear sounds" if on else "🎧 Not hearing sounds")
        self.hear_btn.setStyleSheet("" if on else "QPushButton { color: #ff8b8b; }")
        save_settings(self.s)

    def preview(self, e: SoundEntry):
        """Play only in your headphones."""
        if not self.board:
            return
        if not self.engine.monitor_open:
            self.status.setText("⚠ Your headphones aren't connected to the app. Pick them in ⚙ Settings → “Let me hear sounds on”.")
            self.status.setStyleSheet("color: #ff8b8b;")
            return
        try:
            self.engine.preview(self.board.path / e.file, e.volume)
        except Exception as ex:
            self.status.setText(f"⚠ Could not play {e.file}: {ex}")
            self.status.setStyleSheet("color: #ff8b8b;")

    def test_headphones(self):
        beep = BOARDS_ROOT / "Starter" / "Beep.wav"
        if not beep.exists():
            make_starter_board()
        if not self.engine.monitor_open:
            self.status.setText("⚠ Your headphones aren't connected to the app. Pick them in ⚙ Settings → “Let me hear sounds on”.")
            self.status.setStyleSheet("color: #ff8b8b;")
            return
        if beep.exists():
            self.engine.preview(beep)
            dev = self.s["monitor_device"] if self.s["monitor_device"] != DEFAULT_OUT else "Windows default output"
            self.status.setStyleSheet("")
            self.status.setText(f"🎧 Beep sent to {dev}. Can't hear it? Check that device in ⚙ Settings and the Windows volume mixer.")

    def open_keybinds(self):
        dlg = KeybindsDialog(self)
        dlg.exec()

    def play(self, e: SoundEntry):
        if not self.board:
            return
        try:
            self.engine.play(self.board.path / e.file, e.volume, loop=e.loop)
        except Exception as ex:
            self.status.setText(f"⚠ Could not play {e.file}: {ex}")
            self.status.setStyleSheet("color: #ff8b8b;")

    def _play_by_file(self, file: str):
        if self.board:
            for e in self.board.sounds:
                if e.file == file:
                    self.play(e)
                    return

    def _sound_menu(self, b: SoundButton, pos):
        e = b.entry
        m = QMenu(self)
        m.addAction("🎧 Hear it (only me)", lambda: self.preview(e))
        m.addAction("⌨ Sounds & keybinds…", self.open_keybinds)
        m.addSeparator()
        m.addAction("🔁 Stop looping" if e.loop else "🔁 Loop forever", lambda: self._update(e, loop=not e.loop))
        m.addSeparator()
        m.addAction("Rename…", lambda: self._rename_sound(e))
        m.addAction("Set hotkey…", lambda: self._set_hotkey(e))
        if e.hotkey:
            m.addAction(f"Clear hotkey ({e.hotkey})", lambda: self._update(e, hotkey=""))
        m.addAction("Color…", lambda: self._color(e))
        m.addAction(f"Volume… ({int(e.volume * 100)}%)", lambda: self._volume(e))
        m.addSeparator()
        m.addAction("Move left", lambda: self._move(e, -1))
        m.addAction("Move right", lambda: self._move(e, +1))
        m.addSeparator()
        m.addAction("Show file", lambda: os.system(f'explorer /select,"{self.board.path / e.file}"'))
        m.addAction("Delete sound…", lambda: self._delete_sound(e))
        m.exec(b.mapToGlobal(pos))

    def _update(self, e: SoundEntry, **kw):
        for k, v in kw.items():
            setattr(e, k, v)
        if "loop" in kw:  # turning loop off lets a playing copy finish naturally
            self.engine.set_loop(self.board.path / e.file, bool(kw["loop"]))
        self.board.save()
        self.rebuild_grid()
        self.register_hotkeys()

    def _rename_sound(self, e):
        name, ok = QInputDialog.getText(self, "Rename", "Button name:", text=e.name)
        if ok and name.strip():
            self._update(e, name=name.strip())

    def _color(self, e):
        c = QColorDialog.getColor(QColor(e.color or "#0090ff"), self, "Button color")
        if c.isValid():
            self._update(e, color=c.name())

    def _volume(self, e):
        v, ok = QInputDialog.getInt(self, "Volume", "Volume for this sound (%, up to 1000):",
                                    int(round(e.volume * 100)), 0, 1000, 10)
        if ok:
            self._update(e, volume=v / 100)

    def _move(self, e, d):
        s = self.board.sounds
        i = s.index(e)
        j = i + d
        if 0 <= j < len(s):
            s[i], s[j] = s[j], s[i]
            self.board.save()
            self.rebuild_grid()

    def _delete_sound(self, e):
        if QMessageBox.question(self, "Delete sound", f"Delete “{e.name}” ({e.file}) from this board?\n"
                                "The file is moved to the Recycle Bin if possible.") != QMessageBox.Yes:
            return
        path = self.board.path / e.file
        try:
            from send2trash import send2trash  # type: ignore
            send2trash(str(path))
        except Exception:
            trash = BOARDS_ROOT.parent / "Deleted sounds"
            trash.mkdir(exist_ok=True)
            shutil.move(str(path), str(trash / path.name))
        self.board.reload()
        self.rebuild_grid()
        self.register_hotkeys()

    def _set_hotkey(self, e):
        if keyboard is None:
            QMessageBox.information(self, "Hotkeys", "Global hotkeys are unavailable (the 'keyboard' module is missing).")
            return
        self.status.setText(f"Press the key combo for “{e.name}”… (e.g. a numpad key or F-key)")
        self.status.setStyleSheet("color: #ffc53d;")
        self._pending_hotkey = e
        record_hotkey(self.bridge, None)

    def _on_captured(self, hk: str):
        global _capture_target
        if _capture_target:
            cb, _capture_target = _capture_target, None
            cb(hk)
            self.register_hotkeys()
            return
        e = getattr(self, "_pending_hotkey", None)
        self._pending_hotkey = None
        if e and hk and hk != "esc":
            for o in self.board.sounds:
                if o is not e and o.hotkey.lower() == hk.lower():
                    o.hotkey = ""
            self._update(e, hotkey=hk)
        self.register_hotkeys()
        self.restart_status()

    def restart_status(self):
        self.status.setStyleSheet("")
        self.status.setText(f"Live → {self.s['game_device']}")

    def add_sounds(self):
        if not self.board:
            return
        self.showNormal()
        self.raise_()
        self.activateWindow()
        start = self.s.get("last_add_dir") or str(Path.home() / "Downloads")
        files, _ = QFileDialog.getOpenFileNames(self, f"Add sounds to “{self.board.name}”", start,
                                                "Audio (*.wav *.mp3 *.ogg *.flac *.aiff *.aif)")
        if files:
            self.s["last_add_dir"] = str(Path(files[0]).parent)
            save_settings(self.s)
            n = self.board.add_files(files)
            self.rebuild_grid()
            self.status.setStyleSheet("")
            self.status.setText(f"Added {n} sound(s) to {self.board.name}")

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        if not self.board:
            return
        files = [u.toLocalFile() for u in ev.mimeData().urls()]
        n = self.board.add_files(files)
        self.rebuild_grid()
        self.status.setText(f"Added {n} sound(s) to {self.board.name}")

    # ---------------- hotkeys
    def register_hotkeys(self):
        mouse_binds: dict[tuple[frozenset, str], object] = {}
        key_binds: list = []
        if not self.s["hotkeys_enabled"]:
            MOUSE.set_bindings(mouse_binds)
            KEYS.set_bindings(key_binds)
            return

        def bind(hk: str, cb):
            if not hk:
                return
            parsed = parse_mouse_hotkey(hk)
            if parsed:
                mouse_binds[parsed] = cb
                return
            kp = KEYS.parse(hk)
            if kp:
                key_binds.append((kp[0], kp[1], cb))

        bind(self.s["stop_hotkey"], self.bridge.stop.emit)
        for key, action in (("mute_hotkey", "mute"), ("hud_hotkey", "hud"), ("hud_unlock_hotkey", "unlock"),
                            ("next_board_hotkey", "next"), ("prev_board_hotkey", "prev")):
            bind(self.s.get(key, ""), lambda a=action: self.bridge.action.emit(a))
        if self.board:
            for hk, e in zip(hotkey_map(self.s, self.board.sounds), self.board.sounds):
                bind(hk, lambda f=e.file: self.bridge.play.emit(f))
        MOUSE.set_bindings(mouse_binds)
        KEYS.set_bindings(key_binds)

    # ---------------- settings
    def open_settings(self):
        dlg = SettingsDialog(self, self.s, self.bridge)
        if dlg.exec() == QDialog.Accepted:
            dlg.apply()
            save_settings(self.s)
            self.restart_audio()
            self.hud.setWindowOpacity(self.s["hud_opacity"])
            self.hud._apply_flags()
            self.hud._place()
            self.hud_btn.setChecked(self.s["show_hud"])

    def closeEvent(self, ev):
        self.hud.close()
        self.engine.stop()
        if keyboard is not None:
            try:
                keyboard.unhook_all()
            except Exception:
                pass
        save_settings(self.s)
        super().closeEvent(ev)


def app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#2f6fed"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(4, 4, 56, 56, 14, 14)
    p.setBrush(QColor("white"))
    for i, h in enumerate((18, 34, 26, 40, 22)):
        p.drawRoundedRect(13 + i * 8, 32 - h // 2, 5, h, 2, 2)
    p.end()
    return QIcon(pm)


def main():
    if "--selftest" in sys.argv:  # headless check: route a sound into the cable and record it back
        return selftest()
    MOUSE.start()
    KEYS.start()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLE)
    app.setWindowIcon(app_icon())
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


def selftest():
    import time
    BOARDS_ROOT.mkdir(parents=True, exist_ok=True)
    make_starter_board()
    s = load_settings()
    s["monitor_device"] = ""  # don't blast the user's headphones during the test
    s["mic_muted"] = True
    eng = Engine(s)
    errs = eng.start()
    print("start errors:", errs)
    cable_out = find_device("CABLE Output (VB-Audio Virtual Cable)", "in")
    rec = []
    st = sd.InputStream(device=cable_out, samplerate=SR, channels=2, dtype="float32",
                        extra_settings=sd.WasapiSettings(auto_convert=True),
                        callback=lambda d, f, t, x: rec.append(d.copy()))
    st.start()
    time.sleep(0.3)
    eng.play(BOARDS_ROOT / "Starter" / "Beep.wav")
    time.sleep(0.8)
    st.stop(); st.close(); eng.stop()
    a = np.concatenate(rec) if rec else np.zeros((1, 2))
    peak = float(np.abs(a).max())
    print(f"recorded {len(a) / SR:.2f}s from CABLE Output, peak={peak:.3f}")
    print("PASS" if peak > 0.1 else "FAIL")


if __name__ == "__main__":
    main()
