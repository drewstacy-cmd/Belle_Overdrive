"""
belle_integrations.py  --  Adobe / free-tool / asset-library layer for Belle Overdrive.

Place this file beside Belle_Overdrive_Director.py.  The main file calls
`install(globals())` once, after all of its own classes are defined.  Nothing here
is required for the base application to run: if this module fails to import or
install, the workstation starts exactly as before.

What it adds
------------
* Tool discovery (Photoshop, Illustrator, After Effects, Premiere Pro, Audition,
  Character Animator, Krita, GIMP, Blender, Inkscape, Synfig, Pencil2D, FFmpeg).
* Photoshop / Krita / GIMP round-trip: export the actor, edit, save, and the preview
  reloads automatically.
* Universal sprite import: PNG/WebP/TIFF, PSD, SVG, Illustrator (.ai), PDF, Krita (.kra), OpenRaster (.ora).
* Asset library folder (`belle_assets/`): animated sprite clips, background plates,
  SFX overrides, a music bed, and font overrides - all picked up automatically.
* Headless renders from Blender (incl. Mixamo FBX), Synfig, Pencil2D, and video/GIF
  (e.g. Character Animator exports) into sprite clips.
* FFmpeg H.264/AAC export with a single deterministic audio mixdown.
* After Effects package (PNG sequence + mixdown WAV + .jsx comp builder),
  Premiere Pro package (FCP7 XML + SRT + marker CSV).
* Openverse search/download of CC0 / CC-BY images and audio, with a CREDITS.json ledger.
"""
from __future__ import annotations

import csv
import glob
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import wave
import webbrowser
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

try:
    import fitz  # PyMuPDF: SVG / AI / PDF rasterization
except ImportError:  # pragma: no cover
    fitz = None

try:
    from PyQt5.QtCore import QThread, QTimer, pyqtSignal
except Exception:  # pragma: no cover
    QThread = QTimer = pyqtSignal = None

LOGGER = logging.getLogger("belle_overdrive.integrations")

SR = 44100
RASTER_EXT = {".png", ".webp", ".tif", ".tiff", ".jpg", ".jpeg", ".bmp", ".gif"}
IMPORT_EXT = RASTER_EXT | {".psd", ".psb", ".svg", ".ai", ".pdf", ".kra", ".ora"}
AUDIO_EXT = (".wav", ".ogg", ".mp3", ".flac", ".m4a", ".aif", ".aiff")
SFX_NAMES = ("LASER_BOLT", "PLASMA_BLAST", "GUIDE_POPUP", "EVA_PUFFER", "BEBOP_FREEZE", "HARUKI_JAZZ_CHORD")
VFX_TO_SFX = {"LASER_BOLT": "LASER_BOLT", "PLASMA_BLAST": "PLASMA_BLAST", "GUIDE_POPUP": "GUIDE_POPUP",
              "EVA_PUFFER": "EVA_PUFFER", "BEBOP_FREEZE": "BEBOP_FREEZE"}
SFX_DURATION = {"HARUKI_JAZZ_CHORD": 2.2}

FREE_SOURCES = [
    ("Mixamo (free with your Adobe ID: rigged characters + animations, FBX)", "https://www.mixamo.com"),
    ("Kenney (CC0 sprites, UI, audio)", "https://kenney.nl/assets"),
    ("Quaternius (CC0 3D characters, Blender-ready)", "https://quaternius.com"),
    ("OpenGameArt (mixed licences - check each)", "https://opengameart.org"),
    ("Freesound (SFX; filter by CC0)", "https://freesound.org"),
    ("Blender Studio (open-movie rigs and assets)", "https://studio.blender.org"),
    ("Openverse (CC search engine)", "https://openverse.org"),
    ("Internet Archive (public-domain footage/audio)", "https://archive.org"),
    ("itch.io CC0 asset packs", "https://itch.io/game-assets/assets-cc0"),
    ("Adobe Fonts (included in Creative Cloud)", "https://fonts.adobe.com"),
]


# ============================================================================
# Settings + tool discovery
# ============================================================================

def settings_path() -> Path:
    return Path(os.environ.get("BELLE_SETTINGS", Path.home() / ".belle_overdrive" / "integrations.json"))


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _pf() -> List[str]:
    return [p for p in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) if p]


# key -> (label, windows globs relative to Program Files, mac globs, PATH names)
TOOL_SPECS = {
    "photoshop": ("Adobe Photoshop", ["Adobe/Adobe Photoshop*/Photoshop.exe"],
                  ["/Applications/Adobe Photoshop*/Adobe Photoshop*.app"], []),
    "illustrator": ("Adobe Illustrator", ["Adobe/Adobe Illustrator*/Support Files/Contents/Windows/Illustrator.exe"],
                    ["/Applications/Adobe Illustrator*/Adobe Illustrator*.app"], []),
    "after_effects": ("Adobe After Effects", ["Adobe/Adobe After Effects*/Support Files/AfterFX.exe"],
                      ["/Applications/Adobe After Effects*/Adobe After Effects*.app"], []),
    "aerender": ("After Effects renderer (aerender)", ["Adobe/Adobe After Effects*/Support Files/aerender.exe"],
                 ["/Applications/Adobe After Effects*/aerender"], []),
    "premiere": ("Adobe Premiere Pro", ["Adobe/Adobe Premiere Pro*/Adobe Premiere Pro.exe"],
                 ["/Applications/Adobe Premiere Pro*/Adobe Premiere Pro*.app"], []),
    "audition": ("Adobe Audition", ["Adobe/Adobe Audition*/Adobe Audition.exe"],
                 ["/Applications/Adobe Audition*/Adobe Audition*.app"], []),
    "character_animator": ("Adobe Character Animator",
                           ["Adobe/Adobe Character Animator*/Support Files/Character Animator.exe"],
                           ["/Applications/Adobe Character Animator*/Adobe Character Animator*.app"], []),
    "krita": ("Krita", ["Krita*/bin/krita.exe"], ["/Applications/krita.app"], ["krita"]),
    "gimp": ("GIMP", ["GIMP*/bin/gimp-*.exe"], ["/Applications/GIMP*.app"], ["gimp"]),
    "blender": ("Blender", ["Blender Foundation/Blender*/blender.exe"],
                ["/Applications/Blender.app/Contents/MacOS/Blender"], ["blender"]),
    "inkscape": ("Inkscape", ["Inkscape/bin/inkscape.exe"],
                 ["/Applications/Inkscape.app/Contents/MacOS/inkscape"], ["inkscape"]),
    "synfig": ("Synfig Studio (CLI)", ["SynfigStudio*/bin/synfig.exe", "Synfig*/bin/synfig.exe"],
               ["/Applications/Synfig*.app/Contents/MacOS/synfig"], ["synfig"]),
    "pencil2d": ("Pencil2D", ["Pencil2D/pencil2d.exe"], ["/Applications/Pencil2D.app/Contents/MacOS/Pencil2D"],
                 ["pencil2d"]),
    "ffmpeg": ("FFmpeg", [], [], ["ffmpeg"]),
}
RASTER_EDITORS = ("photoshop", "krita", "gimp")


class ToolRegistry:
    """Finds installed tools. Override any path in settings -> {"paths": {"blender": "..."}}
    or with an environment variable such as BELLE_BLENDER."""

    def __init__(self, settings: dict):
        self.settings = settings
        self._cache: Dict[str, Optional[str]] = {}

    def locate(self, key: str) -> Optional[str]:
        if key in self._cache:
            return self._cache[key]
        found = None
        override = os.environ.get(f"BELLE_{key.upper()}") or self.settings.get("paths", {}).get(key)
        if override and os.path.exists(override):
            found = override
        else:
            _, win, mac, names = TOOL_SPECS[key]
            for name in names:
                found = shutil.which(name)
                if found:
                    break
            if not found and sys.platform.startswith("win"):
                for root in _pf():
                    hits = []
                    for pattern in win:
                        hits += sorted(glob.glob(os.path.join(root, pattern)), reverse=True)
                    if hits:
                        found = hits[0]
                        break
            elif not found and sys.platform == "darwin":
                for pattern in mac:
                    hits = sorted(glob.glob(pattern), reverse=True)
                    if hits:
                        found = hits[0]
                        break
        self._cache[key] = found
        return found

    def executable(self, key: str) -> Optional[str]:
        """Command-line executable (resolves a macOS .app bundle to its inner binary)."""
        path = self.locate(key)
        if path and path.endswith(".app"):
            macos = Path(path) / "Contents" / "MacOS"
            candidates = sorted(macos.glob("*")) if macos.is_dir() else []
            return str(candidates[0]) if candidates else None
        return path

    def launch(self, key: str, files: List[str], extra_args: Optional[List[str]] = None) -> bool:
        path = self.locate(key)
        if not path:
            return False
        try:
            if path.endswith(".app"):
                subprocess.Popen(["open", "-a", path] + [str(f) for f in files])
            else:
                subprocess.Popen([path] + list(extra_args or []) + [str(f) for f in files])
            return True
        except OSError:
            LOGGER.exception("Could not launch %s", key)
            return False

    def status(self) -> List[Tuple[str, str, Optional[str]]]:
        return [(key, spec[0], self.locate(key)) for key, spec in TOOL_SPECS.items()]


# ============================================================================
# Image import: PNG/WebP/TIFF, PSD, SVG, AI, PDF, KRA, ORA
# ============================================================================

def _from_zip_composite(path: Path) -> Image.Image:
    with zipfile.ZipFile(path) as z:
        names = {n.lower(): n for n in z.namelist()}
        for wanted in ("mergedimage.png", "thumbnail/thumbnail.png"):
            if wanted in names:
                with z.open(names[wanted]) as handle:
                    return Image.open(handle).convert("RGBA").copy()
    raise ValueError(f"{path.name} has no embedded merged image. Save it with a flattened preview or export a PNG.")


def _from_fitz(path: Path, target_px: int) -> Image.Image:
    if fitz is None:
        raise RuntimeError("PyMuPDF is required to import SVG / AI / PDF files (pip install PyMuPDF).")
    doc = fitz.open(str(path))
    try:
        page = doc[0]
        rect = page.rect
        scale = target_px / max(rect.width, rect.height, 1.0)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
        return Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def load_any_image(path, target_px: int = 2048, tools: Optional[ToolRegistry] = None) -> Image.Image:
    """Load any supported art file as RGBA. Vector formats are rasterized with transparency."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in RASTER_EXT:
        with Image.open(p) as im:
            im.seek(0)
            return im.convert("RGBA")
    if ext in (".psd", ".psb"):
        try:
            from psd_tools import PSDImage
        except ImportError as exc:
            raise RuntimeError("Reading PSD files needs psd-tools (pip install psd-tools).") from exc
        return PSDImage.open(str(p)).composite().convert("RGBA")
    if ext in (".kra", ".ora"):
        return _from_zip_composite(p)
    if ext in (".svg", ".ai", ".pdf"):
        try:
            return _from_fitz(p, target_px)
        except Exception as first:
            ink = tools.executable("inkscape") if tools else None
            if ink and ext in (".svg", ".ai", ".pdf"):
                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "out.png"
                    subprocess.run([ink, str(p), "--export-type=png", f"--export-filename={out}",
                                    f"--export-height={target_px}", "--export-background-opacity=0"],
                                   check=True, capture_output=True, timeout=120)
                    return Image.open(out).convert("RGBA").copy()
            if ext == ".ai":
                raise RuntimeError("Could not read this .ai file. In Illustrator choose Save As > Illustrator (.ai) with "
                                   "'Create PDF Compatible File' ticked, or export SVG/PNG instead.") from first
            raise
    raise ValueError(f"Unsupported file type: {ext}")


def fit_sprite(img: Image.Image, trim: bool = True, max_h: Optional[int] = 720, max_w: int = 1400) -> Image.Image:
    """Optionally trim transparent margins and downscale so imports fit the 480x720 rig scale."""
    img = img.convert("RGBA")
    if trim:
        box = img.getchannel("A").getbbox()
        if box:
            img = img.crop(box)
    limit_h = max_h or 4096
    scale = min(1.0, limit_h / img.height, max_w / img.width)
    if scale < 1.0:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)
    return img


def _natural_key(path: Path):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", path.name)]


# ============================================================================
# Asset library: sprite clips, backgrounds, sfx, music, fonts
# ============================================================================

class _LRU:
    def __init__(self, size=96):
        self.size, self.data, self.lock = size, OrderedDict(), threading.Lock()

    def get(self, key):
        with self.lock:
            if key in self.data:
                self.data.move_to_end(key)
                return self.data[key]
        return None

    def put(self, key, value):
        with self.lock:
            self.data[key] = value
            self.data.move_to_end(key)
            while len(self.data) > self.size:
                self.data.popitem(last=False)


class SpriteLibrary:
    """belle_assets/sprites/<CHAR>/<POSE>/frame_0001.png  (animated clip per pose)
       belle_assets/sprites/<CHAR>/*.png                  (used for every pose)
       belle_assets/sprites/<CHAR>.png                    (single still)
       Optional <POSE>_TALK folders (or TALK) are used while the character is speaking.
       Optional belle_assets/sprites/<CHAR>/meta.json  {"fps": 12}"""

    def __init__(self, root: Path):
        self.root = Path(root) / "sprites"
        self.clips: Dict[Tuple[str, str], List[Path]] = {}
        self.fps: Dict[str, float] = {}
        self._cache = _LRU(96)
        self._bad = set()
        self._sig = None
        self.scan()

    @staticmethod
    def _frames(folder: Path) -> List[Path]:
        return sorted((f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in (".png", ".webp")),
                      key=_natural_key)

    def scan(self):
        self.clips, self.fps = {}, {}
        if self.root.is_dir():
            for entry in sorted(self.root.iterdir()):
                if entry.is_file() and entry.suffix.lower() in (".png", ".webp"):
                    self.clips[(entry.stem.upper(), "*")] = [entry]
                elif entry.is_dir():
                    char = entry.name.upper()
                    direct = self._frames(entry)
                    if direct:
                        self.clips[(char, "*")] = direct
                    for sub in sorted(entry.iterdir()):
                        if sub.is_dir():
                            frames = self._frames(sub)
                            if frames:
                                self.clips[(char, sub.name.upper())] = frames
                    try:
                        self.fps[char] = float(json.loads((entry / "meta.json").read_text()).get("fps", 12))
                    except (OSError, ValueError, TypeError):
                        pass
        self._sig = self.signature()

    def signature(self):
        if not self.root.is_dir():
            return ()
        items = []
        for dirpath, _, files in os.walk(self.root):
            for name in files:
                try:
                    st = os.stat(os.path.join(dirpath, name))
                    items.append((dirpath, name, st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
        return tuple(sorted(items))

    def refresh_if_changed(self) -> bool:
        sig = self.signature()
        if sig != self._sig:
            self.scan()
            return True
        return False

    def resolve(self, char: str, pose: str, talking: bool) -> Optional[List[Path]]:
        char, pose = char.upper(), pose.upper()
        keys = ([f"{pose}_TALK", "TALK"] if talking else []) + [pose, "IDLE", "*"]
        for key in keys:
            if (char, key) in self.clips:
                return self.clips[(char, key)]
        return None

    def _load(self, path: Path) -> Optional[Image.Image]:
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            return None
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        if key in self._bad:
            return None
        try:
            img = fit_sprite(Image.open(path).convert("RGBA"), trim=False, max_h=1440)
        except Exception as exc:
            LOGGER.warning("Unreadable sprite frame %s: %s", path, exc)
            self._bad.add(key)
            return None
        self._cache.put(key, img)
        return img

    def frame(self, char: str, pose: str, talking: bool, seconds: float) -> Optional[Image.Image]:
        frames = self.resolve(char, pose, talking)
        if not frames:
            return None
        fps = self.fps.get(char.upper(), 12.0)
        index = int(max(0.0, seconds) * fps) % len(frames) if len(frames) > 1 else 0
        img = self._load(frames[index])
        return img.copy() if img is not None else None


class BackgroundLibrary:
    """belle_assets/backgrounds/<BG_STYLE>.(png|jpg|webp|psd|svg|ai|pdf|kra|ora), e.g. DESERT.png
    Used instead of the procedural set, with a slow push-in matched to the shot grammar."""
    OVERSIZE = 1.15
    ZOOM = {"LONELY_EXT": (1.15, 1.07), "CONFINED_INT": (1.08, 1.0), "DYNAMIC_ACTION": (1.15, 1.0)}

    def __init__(self, root: Path, tools: ToolRegistry):
        self.dir = Path(root) / "backgrounds"
        self.tools = tools
        self.index: Dict[str, Path] = {}
        self._cache = _LRU(8)
        self._sig = None
        self.scan()

    def scan(self):
        self.index = {}
        if self.dir.is_dir():
            for f in sorted(self.dir.iterdir()):
                if f.is_file() and f.suffix.lower() in IMPORT_EXT:
                    self.index.setdefault(f.stem.upper(), f)
        self._sig = self.signature()

    def signature(self):
        out = []
        for p in self.index.values():
            try:
                out.append((str(p), p.stat().st_mtime_ns))
            except OSError:
                pass
        n = len(list(self.dir.iterdir())) if self.dir.is_dir() else 0
        return (tuple(sorted(out)), n)

    def refresh_if_changed(self) -> bool:
        if self.signature() != self._sig:
            self.scan()
            return True
        return False

    def _base(self, path: Path) -> Optional[Image.Image]:
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            return None
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        try:
            img = load_any_image(path, 2600, self.tools).convert("RGBA")
        except Exception as exc:
            LOGGER.warning("Background %s unreadable: %s", path, exc)
            return None
        tw, th = round(1920 * self.OVERSIZE), round(1080 * self.OVERSIZE)
        scale = max(tw / img.width, th / img.height)
        img = img.resize((max(tw, round(img.width * scale)), max(th, round(img.height * scale))), Image.Resampling.LANCZOS)
        left, top = (img.width - tw) // 2, (img.height - th) // 2
        img = img.crop((left, top, left + tw, top + th))
        self._cache.put(key, img)
        return img

    def render(self, style: str, grammar: str, t_norm: float) -> Optional[Image.Image]:
        path = self.index.get(str(style).upper())
        if path is None:
            return None
        base = self._base(path)
        if base is None:
            return None
        z0, z1 = self.ZOOM.get(grammar, (1.1, 1.0))
        s = z0 + (z1 - z0) * float(np.clip(t_norm, 0.0, 1.0))
        cw, ch = 1920 * s, 1080 * s
        left, top = (base.width - cw) / 2, (base.height - ch) / 2
        view = base.crop((round(left), round(top), round(left + cw), round(top + ch)))
        return view.resize((1920, 1080), Image.Resampling.BILINEAR)


# ============================================================================
# Audio: library overrides, decode, deterministic mixdown
# ============================================================================

def read_wav_mono(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        channels, width, rate, frames = wf.getnchannels(), wf.getsampwidth(), wf.getframerate(), wf.getnframes()
        raw = wf.readframes(frames)
    if width != 2:
        raise ValueError(f"{path}: only 16-bit PCM is supported here (got {8 * width}-bit).")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != SR and data.size:
        n = int(round(data.size * SR / rate))
        data = np.interp(np.linspace(0, data.size - 1, n), np.arange(data.size), data).astype(np.float32)
    return data


def write_wav_mono(path: str, data: np.ndarray) -> str:
    pcm = (np.clip(np.asarray(data, dtype=np.float32), -1.0, 1.0) * 32767.0).astype("<i2")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(pcm.tobytes())
    return path


class AudioLibrary:
    """belle_assets/sfx/<SFX_NAME>.(wav|ogg|mp3|flac)   overrides the procedural sound
       belle_assets/music/score.(wav|ogg|mp3|flac)       optional looping music bed"""

    def __init__(self, root: Path, tools: ToolRegistry):
        self.root, self.tools = Path(root), tools
        self.cache_dir = self.root / ".cache"

    def _find(self, folder: str, stem: str) -> Optional[Path]:
        base = self.root / folder
        if base.is_dir():
            for f in base.iterdir():
                if f.stem.upper() == stem.upper() and f.suffix.lower() in AUDIO_EXT:
                    return f
        return None

    def decode(self, path: Path) -> Optional[str]:
        """Return a 44.1 kHz mono 16-bit WAV for any audio file (needs FFmpeg for non-WAV)."""
        st = path.stat()
        tag = hashlib.sha256(f"{path}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:16]
        out = self.cache_dir / f"{path.stem}_{tag}.wav"
        if out.exists():
            return str(out)
        ffmpeg = self.tools.executable("ffmpeg")
        if ffmpeg:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(".part.wav")
            res = subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", str(SR),
                                  "-c:a", "pcm_s16le", str(tmp)], capture_output=True, timeout=300)
            if res.returncode == 0:
                os.replace(tmp, out)
                return str(out)
            LOGGER.warning("FFmpeg could not decode %s: %s", path, res.stderr[-300:])
        if path.suffix.lower() == ".wav":
            try:
                read_wav_mono(str(path))
                return str(path)
            except Exception:
                return None
        return None

    def sfx(self, name: str, engine, duration: float) -> str:
        override = self._find("sfx", name)
        if override:
            decoded = self.decode(override)
            if decoded:
                return decoded
        return engine.generate_sfx(name, duration)

    def music(self) -> Optional[str]:
        found = self._find("music", "score")
        return self.decode(found) if found else None


def frames_of(beat, fps: int) -> int:
    return max(1, int(round(float(beat.duration) * fps)))


def collect_audio_cues(beats, audio_lib: AudioLibrary, engine, fps: int) -> List[Tuple[str, float]]:
    """(wav path, start seconds). Times are frame-snapped so audio stays locked to the picture."""
    cues, elapsed = [], 0.0
    for i, beat in enumerate(beats):
        if i == 0 or beat.scene_heading != beats[i - 1].scene_heading:
            cues.append((audio_lib.sfx("HARUKI_JAZZ_CHORD", engine, 2.2), elapsed))
        for ev in beat.vfx:
            name = VFX_TO_SFX.get(ev.vfx_type)
            if name:
                cues.append((audio_lib.sfx(name, engine, SFX_DURATION.get(name, 0.8)),
                             elapsed + float(ev.time_start) * float(beat.duration)))
        elapsed += frames_of(beat, fps) / float(fps)
    return [c for c in cues if c[0]]


def total_seconds(beats, fps: int) -> float:
    return sum(frames_of(b, fps) for b in beats) / float(fps)


def mixdown(beats, audio_lib: AudioLibrary, engine, fps: int, out_wav: str) -> Optional[str]:
    seconds = total_seconds(beats, fps)
    cues = collect_audio_cues(beats, audio_lib, engine, fps)
    bed = audio_lib.music()
    if not cues and not bed:
        return None
    n = int((seconds + 1.0) * SR)
    mix = np.zeros(n, dtype=np.float32)
    if bed:
        loop = read_wav_mono(bed)
        if loop.size:
            reps = int(math.ceil(int(seconds * SR) / loop.size)) + 1
            track = np.tile(loop, reps)[: int(seconds * SR)]
            fade = min(track.size, SR)
            if fade:
                track[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
            mix[: track.size] += 0.28 * track
    cache: Dict[str, np.ndarray] = {}
    for path, start in cues:
        if path not in cache:
            cache[path] = read_wav_mono(path)
        data, pos = cache[path], int(round(start * SR))
        end = min(n, pos + data.size)
        if pos < n and end > pos:
            mix[pos:end] += data[: end - pos]
    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak > 0.95:
        mix *= 0.95 / peak
    return write_wav_mono(out_wav, mix)


# ============================================================================
# Export: FFmpeg H.264, PNG sequence, After Effects, Premiere Pro
# ============================================================================

def _ffmpeg_has_libx264(ffmpeg: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=30).stdout
        return "libx264" in out
    except (OSError, subprocess.SubprocessError):
        return False


def ffmpeg_export(ctx, beats, compositor, audio_eng, out_path, fps=24, progress_cb=None, cancel_event=None,
                  validate=None, mix_dir=None):
    ffmpeg = ctx.tools.executable("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg not found.")
    if not beats:
        raise ValueError("There are no beats to export.")
    if fps < 1 or fps > 120:
        raise ValueError("Frame rate must be between 1 and 120 FPS.")
    if validate:
        errors, _ = validate(beats)
        if errors:
            raise ValueError("Project validation failed:\n" + "\n".join(errors[:12]))
    output = Path(out_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.stem + "_rendering" + output.suffix)
    work = Path(mix_dir) if mix_dir else Path(tempfile.mkdtemp(prefix="belle_mix_"))
    work.mkdir(parents=True, exist_ok=True)
    stderr_file = tempfile.TemporaryFile()
    proc = None
    try:
        if progress_cb:
            progress_cb(1, "Mixing audio")
        mix = mixdown(beats, ctx.audio, audio_eng, fps, str(work / "mixdown.wav"))
        cfg = ctx.settings
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", "1920x1080", "-r", str(fps), "-i", "-"]
        if mix:
            cmd += ["-i", mix]
        cmd += ["-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-c:v", "libx264", "-preset", str(cfg.get("ffmpeg_preset", "medium")),
                "-crf", str(cfg.get("ffmpeg_crf", 16)), "-movflags", "+faststart"]
        if mix:
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ac", "2", "-shortest"]
        cmd += [str(partial)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=stderr_file)
        total = sum(frames_of(b, fps) for b in beats)
        done = 0
        for beat in beats:
            frames = frames_of(beat, fps)
            for i in range(frames):
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Export cancelled.")
                rgb = compositor.render_frame(beat, i / max(1, frames - 1)).convert("RGB")
                try:
                    proc.stdin.write(rgb.tobytes())
                except (BrokenPipeError, OSError):
                    break
                done += 1
                if progress_cb and (done % max(1, fps // 2) == 0 or done == total):
                    progress_cb(int(2 + done * 96 / max(1, total)), f"Rendering {done:,} / {total:,} frames")
        try:
            proc.stdin.close()
        except OSError:
            pass
        code = proc.wait()
        if code != 0:
            stderr_file.seek(0)
            raise RuntimeError("FFmpeg failed:\n" + stderr_file.read().decode("utf-8", "replace")[-1200:])
        os.replace(partial, output)
        if progress_cb:
            progress_cb(100, f"Export complete: {output}")
        return str(output)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        stderr_file.close()
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass
        if not mix_dir:
            shutil.rmtree(work, ignore_errors=True)


def _timecode(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def beat_timeline(beats, fps: int):
    """[(beat, first_frame, end_frame)] using the exact frame counts the exporter renders."""
    out, cursor = [], 0
    for beat in beats:
        n = frames_of(beat, fps)
        out.append((beat, cursor, cursor + n))
        cursor += n
    return out


def write_srt(beats, fps: int, path: Path) -> None:
    lines, index = [], 1
    for beat, a, b in beat_timeline(beats, fps):
        if str(beat.beat_type).upper() == "DIALOGUE" and beat.text.strip():
            lines += [str(index), f"{_timecode(a / fps)} --> {_timecode(b / fps)}", f"{beat.speaker}: {beat.text}", ""]
            index += 1
    path.write_text("\n".join(lines), encoding="utf-8")


def write_markers_csv(beats, fps: int, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as stream:
        w = csv.writer(stream)
        w.writerow(["Marker Name", "Description", "In", "Out", "Duration", "Marker Type"])
        for beat, a, b in beat_timeline(beats, fps):
            w.writerow([f"#{beat.beat_id:03d} {beat.scene_heading}"[:80], f"{beat.speaker}: {beat.text}"[:400],
                        f"{_timecode(a / fps)}", f"{_timecode(b / fps)}", f"{(b - a) / fps:.3f}", "Comment"])


def _js(value) -> str:
    return json.dumps(value)


def write_ae_jsx(beats, fps: int, package: Path) -> Path:
    seconds = total_seconds(beats, fps)
    markers = [(a / fps, f"#{beat.beat_id:03d} {beat.speaker}: {beat.text}"[:120]) for beat, a, _ in beat_timeline(beats, fps)]
    first = package / "frames" / "frame_000000.png"
    audio = package / "audio" / "mixdown.wav"
    script = f"""// Generated by Belle Overdrive. Run via File > Scripts > Run Script File...
(function () {{
  app.beginUndoGroup("Belle Overdrive import");
  var FPS = {fps}, DURATION = {seconds:.6f};
  var seq = new ImportOptions(new File({_js(first.as_posix())}));
  seq.sequence = true;
  seq.forceAlphabetical = true;
  var footage = app.project.importFile(seq);
  footage.mainSource.conformFrameRate = FPS;
  var comp = app.project.items.addComp("Belle Overdrive", 1920, 1080, 1.0, DURATION, FPS);
  comp.layers.add(footage);
  var wav = new File({_js(audio.as_posix())});
  if (wav.exists) {{ var a = app.project.importFile(new ImportOptions(wav)); comp.layers.add(a); }}
  var markers = {json.dumps(markers)};
  try {{
    for (var i = 0; i < markers.length; i++) {{
      comp.markerProperty.setValueAtTime(markers[i][0], new MarkerValue(markers[i][1]));
    }}
  }} catch (e) {{}}
  comp.openInViewer();
  app.endUndoGroup();
}})();
"""
    path = package / "belle_import.jsx"
    path.write_text(script, encoding="utf-8")
    return path


def export_png_sequence(beats, compositor, fps, folder: Path, progress_cb=None, cancel_event=None) -> int:
    folder.mkdir(parents=True, exist_ok=True)
    total = sum(frames_of(b, fps) for b in beats)
    n = 0
    for beat in beats:
        frames = frames_of(beat, fps)
        for i in range(frames):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Export cancelled.")
            compositor.render_frame(beat, i / max(1, frames - 1)).convert("RGB").save(
                folder / f"frame_{n:06d}.png", compress_level=3)
            n += 1
            if progress_cb and (n % max(1, fps // 2) == 0 or n == total):
                progress_cb(int(5 + n * 85 / max(1, total)), f"Writing frame {n:,} / {total:,}")
    return n


def export_after_effects_package(ctx, beats, compositor, audio_eng, folder, fps=24, progress_cb=None,
                                 cancel_event=None, validate=None) -> str:
    if validate:
        errors, _ = validate(beats)
        if errors:
            raise ValueError("Project validation failed:\n" + "\n".join(errors[:12]))
    package = Path(folder).expanduser().resolve()
    package.mkdir(parents=True, exist_ok=True)
    if progress_cb:
        progress_cb(2, "Mixing audio")
    mixdown(beats, ctx.audio, audio_eng, fps, str(package / "audio" / "mixdown.wav"))
    export_png_sequence(beats, compositor, fps, package / "frames", progress_cb, cancel_event)
    write_srt(beats, fps, package / "captions.srt")
    write_markers_csv(beats, fps, package / "markers.csv")
    jsx = write_ae_jsx(beats, fps, package)
    ctx.write_credits(package / "CREDITS.txt")
    (package / "manifest.json").write_text(json.dumps({
        "fps": fps, "frames": sum(frames_of(b, fps) for b in beats), "seconds": total_seconds(beats, fps),
        "beats": len(beats)}, indent=2), encoding="utf-8")
    if progress_cb:
        progress_cb(100, f"After Effects package ready: {package}")
    return str(jsx)


def write_premiere_xml(beats, fps: int, movie: Path, path: Path) -> None:
    from xml.sax.saxutils import escape
    total = sum(frames_of(b, fps) for b in beats)
    url = "file://localhost/" + urllib.parse.quote(movie.resolve().as_posix().lstrip("/"))
    rate = f"<rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>"
    file_def = (f'<file id="file-1"><name>{escape(movie.name)}</name><pathurl>{url}</pathurl>{rate}'
                f"<duration>{total}</duration><media><video><samplecharacteristics>{rate}<width>1920</width>"
                f"<height>1080</height></samplecharacteristics></video><audio><samplecharacteristics><depth>16</depth>"
                f"<samplerate>{SR}</samplerate></samplecharacteristics><channelcount>2</channelcount></audio></media></file>")
    video, a1, a2 = [], [], []
    for n, (beat, a, b) in enumerate(beat_timeline(beats, fps), 1):
        name = escape(f"#{beat.beat_id:03d} {beat.speaker}: {beat.text}"[:60])
        common = f"<name>{name}</name><duration>{total}</duration>{rate}<start>{a}</start><end>{b}</end><in>{a}</in><out>{b}</out>"
        ref = file_def if n == 1 else '<file id="file-1"/>'
        video.append(f'<clipitem id="v{n}">{common}{ref}<link><linkclipref>v{n}</linkclipref></link>'
                     f'<link><linkclipref>a{n}</linkclipref></link><link><linkclipref>b{n}</linkclipref></link></clipitem>')
        for tag, lst, ti in (("a", a1, 1), ("b", a2, 2)):
            lst.append(f'<clipitem id="{tag}{n}">{common}<file id="file-1"/><sourcetrack><mediatype>audio</mediatype>'
                       f"<trackindex>{ti}</trackindex></sourcetrack></clipitem>")
    xml = (f'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4"><sequence id="belle">'
           f"<name>Belle Overdrive</name><duration>{total}</duration>{rate}"
           f"<timecode>{rate}<string>00:00:00:00</string><frame>0</frame><displayformat>NDF</displayformat></timecode>"
           f"<media><video><format><samplecharacteristics>{rate}<width>1920</width><height>1080</height>"
           f"<anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio><fielddominance>none</fielddominance>"
           f"</samplecharacteristics></format><track>{''.join(video)}</track></video>"
           f"<audio><track>{''.join(a1)}</track><track>{''.join(a2)}</track></audio></media></sequence></xmeml>")
    path.write_text(xml, encoding="utf-8")


def export_premiere_package(ctx, beats, compositor, audio_eng, exporter_cls, folder, fps=24, progress_cb=None,
                            cancel_event=None) -> str:
    package = Path(folder).expanduser().resolve()
    package.mkdir(parents=True, exist_ok=True)
    movie = package / "Belle_Overdrive_Animatic.mp4"
    exporter_cls.export_mp4(beats, compositor, audio_eng, str(movie), fps=fps, progress_cb=progress_cb,
                            cancel_event=cancel_event)
    mixdown(beats, ctx.audio, audio_eng, fps, str(package / "audio" / "mixdown.wav"))
    write_premiere_xml(beats, fps, movie, package / "belle_sequence.xml")
    write_srt(beats, fps, package / "captions.srt")
    write_markers_csv(beats, fps, package / "markers.csv")
    ctx.write_credits(package / "CREDITS.txt")
    return str(package / "belle_sequence.xml")


# ============================================================================
# Headless renders from free tools -> sprite clips
# ============================================================================

BLENDER_SCRIPT = r'''
import bpy, sys, math
from mathutils import Vector
argv = sys.argv[sys.argv.index("--") + 1:]
out_dir, start, end, w, h, fps = argv[0], int(argv[1]), int(argv[2]), int(argv[3]), int(argv[4]), int(argv[5])
src = argv[7] if len(argv) > 7 and argv[6] == "--import" else None
if src:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ext = src.lower().rsplit(".", 1)[-1]
    if ext == "fbx":
        bpy.ops.import_scene.fbx(filepath=src)
    elif ext in ("glb", "gltf"):
        bpy.ops.import_scene.gltf(filepath=src)
    else:
        raise SystemExit("Unsupported model format: " + ext)
    scene = bpy.context.scene
    meshes = [o for o in scene.objects if o.type == 'MESH']
    if not meshes:
        raise SystemExit("No mesh objects found in " + src)
    pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    center, height = (mn + mx) / 2.0, max(mx.z - mn.z, 1e-3)
    cam_data = bpy.data.cameras.new("BelleCam")
    cam_data.type = 'ORTHO'
    cam_data.ortho_scale = height * 1.15
    dist = max(height * 4.0, (mx.y - mn.y) * 2.0, 1.0)
    cam_data.clip_start, cam_data.clip_end = dist * 0.01, dist * 10.0
    cam = bpy.data.objects.new("BelleCam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = (center.x, center.y - dist, center.z)
    cam.rotation_euler = (math.radians(90), 0, 0)
    scene.camera = cam
    sun_data = bpy.data.lights.new("BelleSun", 'SUN')
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("BelleSun", sun_data)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (math.radians(50), 0, math.radians(20))
    scene.world = bpy.data.worlds.new("BelleWorld")
    scene.world.color = (0.8, 0.8, 0.8)
    if end <= 0 and bpy.data.actions:
        start, end = int(bpy.data.actions[0].frame_range[0]), int(bpy.data.actions[0].frame_range[1])
scene = bpy.context.scene
r = scene.render
r.film_transparent = True
r.image_settings.file_format = 'PNG'
r.image_settings.color_mode = 'RGBA'
r.image_settings.color_depth = '8'
r.resolution_x, r.resolution_y, r.resolution_percentage = w, h, 100
r.fps = fps
if end > 0:
    scene.frame_start, scene.frame_end = start, max(start, end)
r.filepath = out_dir.rstrip("/\\") + "/frame_"
bpy.ops.render.render(animation=True)
'''


def _clip_dir(ctx, char: str, pose: str) -> Path:
    char = re.sub(r"[^A-Za-z0-9_]+", "_", char.upper()) or "ACTOR"
    pose = re.sub(r"[^A-Za-z0-9_]+", "_", pose.upper())
    folder = ctx.root / "sprites" / char
    if pose and pose != "ANY":
        folder = folder / pose
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("frame_*.png"):
        try:
            old.unlink()
        except OSError:
            pass
    return folder


def _run(cmd, timeout=3600):
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({res.returncode}):\n{(res.stderr or res.stdout)[-1200:]}")
    return res


def render_clip(ctx, source: str, char: str, pose: str, fps: int = 12, start: int = 0, end: int = 0,
                height: int = 1080) -> str:
    """Render a Blender/FBX/GLB, Synfig, Pencil2D, or video/GIF file into sprites/<CHAR>/<POSE>/frame_*.png"""
    src = Path(source)
    ext = src.suffix.lower()
    out = _clip_dir(ctx, char, pose)
    width = int(height * 2 / 3)
    if ext in (".blend", ".fbx", ".glb", ".gltf"):
        blender = ctx.tools.executable("blender")
        if not blender:
            raise RuntimeError("Blender was not found. Install it or set its path in Integration Status.")
        script = Path(tempfile.mkdtemp(prefix="belle_bl_")) / "render.py"
        script.write_text(BLENDER_SCRIPT, encoding="utf-8")
        args = ["--", str(out), str(start), str(end), str(width), str(height), str(fps)]
        if ext == ".blend":
            _run([blender, "-b", str(src), "-P", str(script)] + args)
        else:
            _run([blender, "-b", "-P", str(script)] + args + ["--import", str(src)])
    elif ext in (".sif", ".sifz", ".sfg"):
        synfig = ctx.tools.executable("synfig")
        if not synfig:
            raise RuntimeError("Synfig CLI was not found.")
        _run([synfig, str(src), "-t", "png", "-o", str(out / "frame_.png"), "-w", str(width), "-h", str(height),
              "--fps", str(fps)])
        for f in out.glob("frame_*.png"):  # normalise "frame_.0001.png"
            m = re.search(r"(\d+)", f.stem)
            if m:
                f.rename(out / f"frame_{int(m.group(1)):04d}.png")
    elif ext in (".pclx", ".pcl"):
        pencil = ctx.tools.executable("pencil2d")
        if not pencil:
            raise RuntimeError("Pencil2D was not found.")
        _run([pencil, str(src), "-o", str(out / "frame_.png"), "--width", str(width), "--height", str(height),
              "--transparency"])
    elif ext in (".mov", ".webm", ".gif", ".mp4", ".mkv", ".apng"):
        ffmpeg = ctx.tools.executable("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("FFmpeg was not found.")
        pre = ["-c:v", "libvpx-vp9"] if ext == ".webm" else []  # keeps WebM alpha
        _run([ffmpeg, "-y", "-loglevel", "error"] + pre + ["-i", str(src), "-vf", f"fps={fps}", "-pix_fmt", "rgba",
                                                           str(out / "frame_%04d.png")])
    else:
        raise ValueError(f"Unsupported source for clip rendering: {ext}")
    count = len(list(out.glob("frame_*.png")))
    if not count:
        raise RuntimeError("The tool finished but produced no frames.")
    try:
        (ctx.root / "sprites" / out.parts[-1 if pose == "ANY" else -2] / "meta.json").write_text(
            json.dumps({"fps": fps}), encoding="utf-8")
    except OSError:
        pass
    return f"{count} frames -> {out}"


def import_frame_folder(ctx, folder: str, char: str, pose: str, fps: int = 12) -> str:
    src = Path(folder)
    files = sorted((f for f in src.iterdir() if f.suffix.lower() in (".png", ".webp")), key=_natural_key)
    if not files:
        raise ValueError("That folder has no PNG/WebP frames.")
    out = _clip_dir(ctx, char, pose)
    for i, f in enumerate(files, 1):
        shutil.copy2(f, out / f"frame_{i:04d}{f.suffix.lower()}")
    char_dir = ctx.root / "sprites" / re.sub(r"[^A-Za-z0-9_]+", "_", char.upper())
    (char_dir / "meta.json").write_text(json.dumps({"fps": fps}), encoding="utf-8")
    return f"{len(files)} frames -> {out}"


# ============================================================================
# Openverse (CC0 / CC-BY search, anonymous API)
# ============================================================================

def openverse_search(query: str, kind: str = "images", licenses: str = "cc0,pdm,by", page_size: int = 12) -> List[dict]:
    kind = "audio" if kind == "audio" else "images"
    params = {"q": query, "license": licenses, "page_size": str(page_size), "mature": "false"}
    if kind == "images":
        params["extension"] = "png,svg,webp"
    url = f"https://api.openverse.org/v1/{kind}/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "BelleOverdriveDirector/2.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8")).get("results", [])


def download_asset(url: str, dest: Path, max_bytes: int = 60 * 1024 * 1024) -> Path:
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("Only http(s) downloads are allowed.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "BelleOverdriveDirector/2.0"})
    size = 0
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                out.close()
                tmp.unlink(missing_ok=True)
                raise ValueError("Download exceeds the size limit.")
            out.write(chunk)
    os.replace(tmp, dest)
    return dest


# ============================================================================
# Adobe Fonts (synced locally by Creative Cloud) + library fonts
# ============================================================================

def adobe_font_dirs() -> List[Path]:
    dirs = []
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        if base:
            dirs.append(Path(base) / "Adobe" / "CoreSync" / "plugins" / "livetype")
    elif sys.platform == "darwin":
        dirs.append(Path.home() / "Library" / "Application Support" / "Adobe" / "CoreSync" / "plugins" / "livetype")
    return [d for d in dirs if d.is_dir()]


def scan_fonts(extra_dirs: List[Path], limit: int = 4000) -> Dict[str, str]:
    """{ 'Family Style': path } for fonts in Adobe's synced folder and belle_assets/fonts."""
    from PIL import ImageFont
    found: Dict[str, str] = {}
    seen = 0
    for base in adobe_font_dirs() + list(extra_dirs):
        for dirpath, _, files in os.walk(base):
            for name in files:
                if seen >= limit:
                    return found
                p = os.path.join(dirpath, name)
                if Path(name).suffix.lower() not in ("", ".ttf", ".otf"):
                    continue
                seen += 1
                try:
                    fam, style = ImageFont.truetype(p, 20).getname()
                except Exception:
                    continue
                found.setdefault(f"{fam} {style}".strip(), p)
    return dict(sorted(found.items()))


# ============================================================================
# Context + installation into the main module
# ============================================================================

class IntegrationContext:
    def __init__(self, root: Path):
        self.root = Path(root)
        for sub in ("sprites", "backgrounds", "sfx", "music", "fonts", "roundtrip"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.settings = load_settings()
        self.tools = ToolRegistry(self.settings)
        self.sprites = SpriteLibrary(self.root)
        self.backgrounds = BackgroundLibrary(self.root, self.tools)
        self.audio = AudioLibrary(self.root, self.tools)
        self.watch: Dict[str, Tuple[int, str]] = {}

    # Credits ledger so CC-BY assets stay attributable.
    @property
    def credits_path(self) -> Path:
        return self.root / "CREDITS.json"

    def add_credit(self, entry: dict) -> None:
        try:
            data = json.loads(self.credits_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = []
        data.append(entry)
        self.credits_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def write_credits(self, dest: Path) -> None:
        try:
            data = json.loads(self.credits_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        lines = [f'{e.get("file", "?")}: "{e.get("title", "")}" by {e.get("creator", "unknown")} '
                 f'({e.get("license", "")}) {e.get("source_url", "")}' for e in data]
        dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def install(ns: dict, root: Optional[str] = None) -> IntegrationContext:
    """Patch the main module's classes. `ns` is the main module's globals()."""
    here = Path(ns.get("__file__") or __file__).resolve().parent
    ctx = IntegrationContext(Path(root or os.environ.get("BELLE_ASSET_ROOT") or here / "belle_assets"))
    Art, Comp, Exp, WS = (ns["AnimeCelArtEngine"], ns["CinematicCompositor"], ns["StudioExporter"],
                          ns["BelleDirectorWorkstation"])
    tls = threading.local()

    orig_rig, orig_bg, orig_font, orig_frame = (Art.render_character_rig, Art.render_background, Art.get_font,
                                                Comp.render_frame)

    def render_character_rig(self, char_type, pose, expression, is_talking, t_norm):
        if char_type not in self.custom_sprites:
            seconds = getattr(tls, "seconds", 0.0) * float(t_norm)
            img = ctx.sprites.frame(char_type, pose, bool(is_talking), seconds)
            if img is not None:
                return img
        return orig_rig(self, char_type, pose, expression, is_talking, t_norm)

    def render_background(self, style, grammar, t_norm):
        img = ctx.backgrounds.render(style, grammar, t_norm)
        return img if img is not None else orig_bg(self, style, grammar, t_norm)

    def get_font(self, size=28, bold=False, serif=False):
        role = "serif" if serif else ("title" if bold else "body")
        path = ctx.settings.get("fonts", {}).get(role)
        if not path:
            for ext in (".ttf", ".otf"):
                cand = ctx.root / "fonts" / f"{role}{ext}"
                if cand.exists():
                    path = str(cand)
                    break
        if path and os.path.exists(path):
            try:
                from PIL import ImageFont
                return ImageFont.truetype(path, size)
            except Exception:
                LOGGER.warning("Could not load font override %s", path)
        return orig_font(self, size, bold, serif)

    def render_frame(self, beat, t_norm):
        previous = getattr(tls, "seconds", 0.0)
        tls.seconds = float(beat.duration)  # thread-local: preview and export threads never interfere
        try:
            return orig_frame(self, beat, t_norm)
        finally:
            tls.seconds = previous

    Art.render_character_rig = render_character_rig
    Art.render_background = render_background
    Art.get_font = get_font
    Art._procedural_rig = orig_rig
    Art._procedural_background = orig_bg
    Comp.render_frame = render_frame

    orig_export = Exp.export_mp4.__func__

    def export_mp4(cls, beats, compositor, audio_eng, out_path, fps=24, progress_cb=None, cancel_event=None):
        if ctx.settings.get("use_ffmpeg", True) and ctx.tools.executable("ffmpeg") and \
                _ffmpeg_has_libx264(ctx.tools.executable("ffmpeg")):
            try:
                return ffmpeg_export(ctx, beats, compositor, audio_eng, out_path, fps, progress_cb, cancel_event,
                                     validate=ns.get("validate_project"))
            except (ValueError, InterruptedError):
                raise
            except Exception:
                LOGGER.exception("FFmpeg export failed; falling back to the built-in encoder")
        return orig_export(cls, beats, compositor, audio_eng, out_path, fps=fps, progress_cb=progress_cb,
                           cancel_event=cancel_event)

    Exp.export_mp4 = classmethod(export_mp4)

    orig_init_gui = WS._init_gui

    def _init_gui(self):
        orig_init_gui(self)
        try:
            from belle_integrations_gui import build_menus
            build_menus(self, ns, ctx)
        except Exception:
            LOGGER.exception("Integration menus could not be built; the workstation will run without them")

    WS._init_gui = _init_gui

    def _import_sprite(self):
        from belle_integrations_gui import import_sprite_any  # one importer for PNG/PSD/SVG/AI/PDF/KRA/ORA
        import_sprite_any(self, ctx)

    WS._import_sprite = _import_sprite
    LOGGER.info("Integrations active. Asset library: %s", ctx.root)
    return ctx
