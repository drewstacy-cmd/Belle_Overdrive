import sys
import os
import re
import json
import math
import wave
import hashlib
import copy
import logging
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Tuple, Optional

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# Optional PDF Parsing
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# Optional Offline TTS
try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

# Optional Video/Audio Muxing
try:
    from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip
except ImportError:
    try:
        from moviepy import VideoFileClip, AudioFileClip, CompositeAudioClip
    except ImportError:
        VideoFileClip = AudioFileClip = CompositeAudioClip = None

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QListWidget, QListWidgetItem, QLabel, QPushButton, QSlider,
    QComboBox, QSpinBox, QDoubleSpinBox, QSplitter, QFileDialog,
    QProgressBar, QMessageBox, QGroupBox, QFormLayout, QCheckBox
)
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QImage, QPixmap


logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
LOGGER = logging.getLogger("belle_overdrive")

# ============================================================================
# 1. DATA MODEL
# ============================================================================

@dataclass
class Keyframe:
    time_norm: float = 0.0
    x: float = 960.0
    y: float = 640.0
    scale: float = 1.0
    rotation: float = 0.0
    alpha: float = 1.0
    pose: str = "IDLE"          # IDLE, AIM, ACTION, SHRUG, DANCE, FLEX, DRINK_MILK, KNIFE_THROW, FAYE_LEAN
    expression: str = "DEFAULT" # DEFAULT, UNBOTHERED, SULTRY, ANGRY, MANIC, HIMBO_GRIN
    flip_x: bool = False


@dataclass
class ActorTrack:
    actor_id: str
    char_type: str              # CORA, JULES, PETE_1, PETE_2, SHERIFF, CAPTAIN, DEPUTY, NORTHRIDGE, LEO, GUARD, BELLE, SKIFF, CAT
    z_index: int = 1
    keyframes: List[Keyframe] = field(default_factory=list)


@dataclass
class VFXEvent:
    vfx_type: str               # LASER_BOLT, PLASMA_BLAST, EVA_PUFFER, BEBOP_FREEZE, GUIDE_POPUP, MURAKAMI_MOONS, SPEED_LINES
    time_start: float = 0.05
    time_end: float = 0.95
    start_x: float = 400.0
    start_y: float = 500.0
    end_x: float = 1450.0
    end_y: float = 480.0
    label_text: str = ""


@dataclass
class Beat:
    beat_id: int
    scene_heading: str
    beat_type: str              # ACTION or DIALOGUE
    speaker: str
    text: str
    duration: float
    bg_style: str               # DESERT, KITCHEN, CANYON, COCKPIT, SPACE_GVR, ROOF, BRUCILLAS, PANTRY, SKIFF, TRASH, BALLROOM, FOYER, CARGO
    camera_grammar: str         # LONELY_EXT, CONFINED_INT, DYNAMIC_ACTION
    panel_mode: str = "FULL"    # FULL, SPLIT_DUEL, TRIPLE_BEBOP, GUIDE_HUD, HARUKI_DREAM
    tracks: List[ActorTrack] = field(default_factory=list)
    vfx: List[VFXEvent] = field(default_factory=list)


# ============================================================================
# 2. PROCEDURAL AUDIO ENGINE
# ============================================================================

class ProceduralAudioEngine:
    """Deterministic procedural sound design with content-addressed WAV caching."""

    def __init__(self, out_dir="belle_audio_cache"):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _write_wav(self, filename: str, samples: np.ndarray, sample_rate: int = 44100) -> str:
        data = np.asarray(samples, dtype=np.float32).reshape(-1)
        if sample_rate <= 0 or data.size == 0:
            raise ValueError("Audio requires a positive sample rate and non-empty samples.")
        digest = hashlib.sha256()
        digest.update(filename.encode("utf-8"))
        digest.update(str(sample_rate).encode("ascii"))
        digest.update(data.tobytes())
        stem = Path(filename).stem
        target = self.out_dir / f"{stem}_{digest.hexdigest()[:20]}.wav"
        if target.exists():
            return str(target)
        pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype("<i2")
        fd, temp_name = tempfile.mkstemp(prefix=".audio_", suffix=".wav", dir=self.out_dir)
        os.close(fd)
        try:
            with wave.open(temp_name, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(int(sample_rate))
                wf.writeframes(pcm.tobytes())
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return str(target)

    @staticmethod
    def _envelope(t: np.ndarray, attack: float = 0.008, decay: float = 2.5) -> np.ndarray:
        attack_env = np.clip(t / max(attack, 1e-5), 0.0, 1.0)
        return attack_env * np.exp(-decay * t)

    def generate_sfx(self, sfx_type: str, duration: float = 0.8, sr: int = 44100) -> str:
        duration = float(np.clip(duration, 0.05, 30.0))
        sr = int(np.clip(sr, 8000, 192000))
        count = max(1, int(round(sr * duration)))
        t = np.arange(count, dtype=np.float64) / sr
        rng_seed = int(hashlib.sha256(f"{sfx_type}:{duration:.6f}:{sr}".encode()).hexdigest()[:8], 16)
        rng = np.random.default_rng(rng_seed)
        kind = sfx_type.upper()

        if kind == "LASER_BOLT":
            f0, f1 = 1450.0, 95.0
            frequencies = f0 * np.power(f1 / f0, t / max(duration, 1e-6))
            phase = 2.0 * np.pi * np.cumsum(frequencies) / sr
            signal = (0.72 * np.sin(phase) + 0.12 * np.sin(phase * 2.01)) * np.exp(-4.2 * t)
        elif kind == "PLASMA_BLAST":
            sweep = 118.0 - 38.0 * (t / max(duration, 1e-6))
            phase = 2.0 * np.pi * np.cumsum(sweep) / sr
            noise = rng.standard_normal(count)
            # A short moving-average filter keeps the transient broadband without harsh digital hiss.
            kernel = np.ones(max(3, int(sr * 0.002)), dtype=np.float64)
            noise = np.convolve(noise, kernel / kernel.sum(), mode="same")
            signal = (0.72 * np.sin(phase) + 0.5 * noise) * np.exp(-2.8 * t)
        elif kind == "HARUKI_JAZZ_CHORD":
            frequencies = (110.0, 164.81, 220.0, 261.63, 329.63, 392.0, 493.88)
            signal = np.zeros(count, dtype=np.float64)
            for index, frequency in enumerate(frequencies):
                phase = rng.uniform(0.0, 2.0 * np.pi)
                partial = np.sin(2.0 * np.pi * frequency * t + phase)
                signal += partial * (0.17 / (1.0 + index * 0.22))
            signal *= (0.86 + 0.14 * np.sin(2.0 * np.pi * 4.2 * t)) * np.exp(-0.85 * t)
        elif kind == "GUIDE_POPUP":
            notes = (587.33, 783.99, 987.77, 1174.66)
            signal = np.zeros(count, dtype=np.float64)
            segment = duration / len(notes)
            for index, frequency in enumerate(notes):
                mask = (t >= index * segment) & (t < (index + 1) * segment)
                local_t = t[mask] - index * segment
                env = np.sin(np.pi * np.clip(local_t / segment, 0.0, 1.0)) ** 1.5
                signal[mask] = 0.34 * np.sin(2.0 * np.pi * frequency * local_t) * env
        elif kind == "EVA_PUFFER":
            carrier = 165.0 + 28.0 * np.sin(2.0 * np.pi * 7.0 * t)
            phase = 2.0 * np.pi * np.cumsum(carrier) / sr
            signal = (np.sin(phase) + 0.18 * rng.standard_normal(count)) * np.exp(-5.0 * t)
        elif kind == "BEBOP_FREEZE":
            signal = np.sin(2.0 * np.pi * 880.0 * t) * np.exp(-7.0 * t)
        else:
            return ""

        peak = float(np.max(np.abs(signal))) if signal.size else 0.0
        if peak > 0.98:
            signal *= 0.98 / peak
        return self._write_wav(f"sfx_{kind.lower()}.wav", signal.astype(np.float32), sr)


# ============================================================================
# 3. ART AND BACKGROUND RENDERING
# ============================================================================

class AnimeCelArtEngine:
    INK_BLACK = (10, 10, 14, 255)
    GRIT_DARK = (22, 24, 30, 255)
    GRIT_SLATE = (44, 48, 58, 255)
    GRIT_GREY = (78, 84, 96, 255)
    # Restrained production palette. Saturated accents remain available to the
    # screenplay, but the default grade is built around slate, oxblood, brass,
    # petrol blue and desaturated cyan rather than full-frame neon.
    PRIMARY_RED = (184, 54, 63, 255)
    PRIMARY_YELLOW = (218, 177, 104, 255)
    PRIMARY_BLUE = (73, 115, 158, 255)
    NEON_MAGENTA = (160, 89, 133, 255)
    NEON_CYAN = (102, 158, 164, 255)
    NEON_LIME = (142, 165, 106, 255)

    def __init__(self):
        self.custom_sprites: Dict[str, Image.Image] = {}
        self.custom_sprite_paths: Dict[str, str] = {}

    def get_font(self, size=28, bold=False, serif=False):
        if serif:
            candidates = [
                "C:/Windows/Fonts/georgia.ttf",
                "C:/Windows/Fonts/times.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
                "/System/Library/Fonts/Supplemental/Georgia.ttf"
            ]
        else:
            candidates = [
                "C:/Windows/Fonts/impact.ttf" if bold else "C:/Windows/Fonts/arialbd.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/System/Library/Fonts/Supplemental/Impact.ttf"
            ]
        for c in candidates:
            if os.path.exists(c):
                try:
                    return ImageFont.truetype(c, size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def draw_haruki_twin_moons(self, d: ImageDraw.ImageDraw, x1: int, y1: int, r1: int):
        d.ellipse([x1 - r1, y1 - r1, x1 + r1, y1 + r1], fill=(255, 238, 165, 230), outline=self.INK_BLACK, width=4)
        d.chord([x1 - r1, y1 - r1, x1 + r1, y1 + r1], 120, 260, fill=(215, 195, 120, 180))
        r2 = int(r1 * 0.44)
        x2, y2 = x1 + int(r1 * 1.45), y1 + int(r1 * 0.45)
        d.ellipse([x2 - r2, y2 - r2, x2 + r2, y2 + r2], fill=(145, 235, 160, 235), outline=self.INK_BLACK, width=4)

    def draw_takashi_flower_neon(self, d: ImageDraw.ImageDraw, cx: int, cy: int, radius: int, rot_deg=0.0):
        petals = [
            (176, 66, 72, 255), (211, 176, 104, 255), (154, 105, 137, 255), (104, 151, 157, 255),
            (146, 155, 111, 255), (91, 118, 157, 255), (174, 119, 84, 255), (130, 104, 151, 255)
        ]
        pr = int(radius * 0.34)
        for i in range(12):
            ang = math.radians(i * 30 + rot_deg)
            px = cx + int(math.cos(ang) * radius * 0.76)
            py = cy + int(math.sin(ang) * radius * 0.76)
            d.ellipse([px - pr, py - pr, px + pr, py + pr], fill=petals[i % len(petals)], outline=self.INK_BLACK, width=4)
        cr = int(radius * 0.48)
        d.ellipse([cx - cr, cy - cr, cx + cr, cy + cr],
                  fill=(31, 34, 40, 255), outline=(205, 181, 137, 255), width=max(2, radius // 30))
        inner = int(cr * 0.62)
        d.ellipse([cx - inner, cy - inner, cx + inner, cy + inner],
                  outline=(205, 181, 137, 220), width=max(2, radius // 42))
        d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=(224, 207, 167, 255))

    def render_background(self, style: str, grammar: str, t_norm: float) -> Image.Image:
        img = Image.new("RGBA", (1920, 1080), self.GRIT_DARK)
        d = ImageDraw.Draw(img)

        if grammar == "DYNAMIC_ACTION":
            d.rectangle([0, 0, 1920, 1080], fill=self.GRIT_DARK)
            d.polygon([(0, 220), (1920, 0), (1920, 660), (0, 880)], fill=self.PRIMARY_RED, outline=self.INK_BLACK, width=10)
            d.polygon([(0, 620), (1920, 420), (1920, 540), (0, 740)], fill=self.PRIMARY_YELLOW, outline=self.INK_BLACK, width=6)
            for i in range(0, 1920, 65):
                offset = int((t_norm * 400) % 65)
                d.line([(i + offset, 0), (i - 350 + offset, 1080)], fill=(255, 255, 255, 95), width=4)
                d.line([(i + 25 + offset, 0), (i - 325 + offset, 1080)], fill=self.NEON_CYAN, width=2)
            return img

        if style == "DESERT":
            for y in range(740):
                ratio = y / 740.0
                r = int(28 + ratio * 215)
                g = int(32 + ratio * 155)
                b = int(48 + ratio * 35)
                d.line([(0, y), (1920, y)], fill=(r, g, b, 255))
            d.rectangle([0, 728, 1920, 742], fill=self.NEON_CYAN)
            self.draw_haruki_twin_moons(d, 380, 220, 115)
            d.rectangle([0, 740, 1920, 1080], fill=(42, 38, 44, 255))
            d.polygon([(0, 740), (1920, 740), (1920, 840), (0, 920)], fill=self.PRIMARY_YELLOW)
            d.polygon([(120, 740), (240, 560), (460, 560), (550, 740)], fill=self.INK_BLACK)
            d.line([(1620, 745), (1620, 420), (1490, 380)], fill=self.INK_BLACK, width=18)
            d.line([(1515, 388), (1515, 480)], fill=self.PRIMARY_RED, width=4)

        elif style in ("SPACE_GVR", "ROOF", "BRUCILLAS", "TRASH"):
            d.rectangle([0, 0, 1920, 1080], fill=(14, 15, 22, 255))
            star_rng = np.random.default_rng(1984)
            for _ in range(260):
                sx, sy = int(star_rng.integers(0, 1920)), int(star_rng.integers(0, 1080))
                col = self.NEON_CYAN if sx % 7 == 0 else (200, 205, 215, 190)
                d.ellipse([sx, sy, sx + 3, sy + 3], fill=col)
            self.draw_haruki_twin_moons(d, 260, 210, 85)

            if style == "SPACE_GVR":
                d.polygon([(960, 620), (540, 130), (1380, 130)], fill=self.GRIT_SLATE, outline=self.INK_BLACK, width=8)
                d.polygon([(960, 620), (540, 130), (960, 130)], fill=self.INK_BLACK)
                d.line([(500, 190), (1420, 190)], fill=self.NEON_MAGENTA, width=10)
                d.line([(580, 250), (1340, 250)], fill=self.PRIMARY_YELLOW, width=8)
                self.draw_takashi_flower_neon(d, 1540, 240, 52, rot_deg=t_norm * 18)
            elif style == "BRUCILLAS":
                d.rectangle([0, 740, 1920, 1080], fill=self.GRIT_SLATE, outline=self.INK_BLACK, width=8)
                self.draw_takashi_flower_neon(d, 960, 290, 92, rot_deg=t_norm * 12)
                font = self.get_font(42, bold=True)
                d.rectangle([620, 440, 1300, 515], fill=self.INK_BLACK, outline=self.NEON_CYAN, width=5)
                d.text((650, 452), "BRUCILLA'S #9 // MIN 50 CREDITS", fill=self.PRIMARY_YELLOW, font=font)
            elif style == "TRASH":
                d.ellipse([1280, 110, 1780, 610], fill=self.INK_BLACK, outline=self.PRIMARY_RED, width=22)
                d.ellipse([1300, 130, 1760, 590], outline=self.NEON_MAGENTA, width=6)
                d.rectangle([0, 760, 1920, 1080], fill=self.GRIT_DARK, outline=self.INK_BLACK, width=8)
            else:
                d.polygon([(0, 1080), (260, 580), (1660, 580), (1920, 1080)], fill=self.GRIT_SLATE, outline=self.INK_BLACK, width=8)
                for gx in range(340, 1600, 180):
                    d.polygon([(gx, 600), (gx + 110, 600), (gx + 140, 1040), (gx - 30, 1040)],
                              fill=self.PRIMARY_YELLOW if (gx // 180) % 2 == 0 else self.NEON_MAGENTA)
        else:
            d.rectangle([0, 0, 1920, 1080], fill=self.GRIT_SLATE)
            d.polygon([(0, 0), (1920, 0), (1680, 210), (240, 210)], fill=self.INK_BLACK)
            for rx in range(180, 1800, 320):
                d.rectangle([rx, 180, rx + 55, 860], fill=self.GRIT_DARK, outline=self.INK_BLACK, width=6)
            for sx in range(0, 1920, 120):
                col = self.PRIMARY_YELLOW if (sx // 120) % 2 == 0 else self.INK_BLACK
                d.polygon([(sx, 760), (sx + 60, 760), (sx + 30, 800), (sx - 30, 800)], fill=col)
            neon_col = self.NEON_MAGENTA if style in ("KITCHEN", "BALLROOM", "FOYER") else self.NEON_CYAN
            d.rectangle([260, 235, 1660, 250], fill=neon_col)
            d.rectangle([0, 800, 1920, 1080], fill=self.GRIT_DARK, outline=self.INK_BLACK, width=8)

            if style == "KITCHEN":
                d.ellipse([1420, 320, 1580, 480], fill=self.INK_BLACK, outline=self.NEON_CYAN, width=4)
                d.ellipse([1475, 375, 1525, 425], fill=self.PRIMARY_RED)
                self.draw_takashi_flower_neon(d, 410, 390, 38, rot_deg=8)

        return img

    def render_foreground_occlusion(self, d: ImageDraw.ImageDraw, grammar: str, style: str, t_norm: float):
        if grammar == "CONFINED_INT":
            d.polygon([(0, 0), (245, 0), (185, 1080), (0, 1080)], fill=self.INK_BLACK)
            d.polygon([(1675, 0), (1920, 0), (1920, 1080), (1735, 1080)], fill=self.INK_BLACK)
            d.rectangle([0, 0, 1920, 115], fill=self.INK_BLACK)
            d.rectangle([180, 115, 1740, 148], fill=(18, 20, 26, 255), outline=self.INK_BLACK, width=6)
            d.ellipse([260, 85, 370, 195], outline=self.PRIMARY_RED, width=14)
            d.polygon([(0, 910), (540, 935), (620, 1080), (0, 1080)], fill=self.INK_BLACK)
            d.rectangle([340, 845, 415, 930], fill=self.INK_BLACK, outline=self.NEON_CYAN, width=2)
            steam_x = 375 + int(12 * math.sin(t_norm * math.pi * 4))
            d.arc([steam_x - 20, 740, steam_x + 25, 840], 90, 270, fill=(220, 240, 255, 150), width=4)
        elif grammar == "LONELY_EXT":
            d.rectangle([0, 0, 1920, 110], fill=self.INK_BLACK)
            d.rectangle([0, 960, 1920, 1080], fill=self.INK_BLACK)
            d.polygon([(0, 960), (290, 960), (195, 790), (0, 730)], fill=self.INK_BLACK)

    def render_character_rig(
        self, char_type: str, pose: str, expression: str, is_talking: bool, t_norm: float
    ) -> Image.Image:
        if char_type in self.custom_sprites:
            return self.custom_sprites[char_type].copy()
        if char_type == "BELLE":
            return self._draw_rig_belle(t_norm)
        if char_type == "SKIFF":
            return self._draw_rig_skiff()
        if char_type == "CAT":
            return self._draw_rig_sabrina_cat(t_norm)

        img = Image.new("RGBA", (480, 720), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        mouth_open = is_talking and (int(t_norm * 18) % 2 == 0)

        if char_type == "CORA":
            self._draw_cora_faye_zendaya_zoe(d, pose, expression, mouth_open, t_norm)
        elif char_type == "JULES":
            self._draw_jules_faye_mai_marceline(d, pose, expression, mouth_open, t_norm)
        elif char_type in ("PETE_1", "PETE_2"):
            self._draw_pete_barry_syx_anime(d, char_type, pose, expression, mouth_open, t_norm)
        elif char_type == "NORTHRIDGE":
            self._draw_penelope_faye_holo(d, mouth_open, t_norm)
        else:
            self._draw_supporting_anime(d, char_type, mouth_open)

        return img

    def _draw_anime_eyes_and_lips(
        self, d: ImageDraw.ImageDraw, cx: int, cy: int, eye_col: Tuple, lip_col: Tuple,
        sultry: bool, mouth_open: bool, angry=False
    ):
        ink = self.INK_BLACK
        for side in [-1, 1]:
            ex = cx + side * 26
            d.polygon([(ex - 15, cy), (ex, cy - 9), (ex + 15, cy), (ex, cy + 8)], fill=(250, 250, 255, 255), outline=ink, width=3)
            d.ellipse([ex - 7, cy - 7, ex + 7, cy + 7], fill=eye_col, outline=ink, width=2)
            d.ellipse([ex - 3, cy - 3, ex + 3, cy + 3], fill=ink)
            d.ellipse([ex - 6, cy - 6, ex - 1, cy - 1], fill=(255, 255, 255, 255))
            if sultry:
                d.polygon([(ex - 17, cy - 10), (ex + 17, cy - 10), (ex + 16, cy - 1), (ex - 16, cy - 1)],
                          fill=(140, 85, 65, 255), outline=ink, width=2)
            wing_x = ex + side * 21
            d.line([(ex - 16, cy - 2), (ex, cy - 9), (wing_x, cy - 6)], fill=ink, width=5)
            brow_y = cy - 18 if not angry else cy - 13
            d.line([(ex - 14, brow_y), (ex + 14, brow_y + (4 * side if angry else -2))], fill=ink, width=4)

        d.polygon([(cx, cy + 8), (cx - 5, cy + 24), (cx + 2, cy + 24)], fill=(120, 70, 50, 220))
        my = cy + 38
        if mouth_open:
            d.polygon([(cx - 14, my), (cx + 14, my), (cx + 10, my + 16), (cx - 10, my + 16)],
                      fill=(195, 25, 45, 255), outline=ink, width=3)
            d.line([(cx - 9, my + 3), (cx + 9, my + 3)], fill=(255, 255, 255, 255), width=3)
        else:
            d.polygon([(cx - 14, my), (cx, my - 4), (cx + 14, my), (cx, my + 7)], fill=lip_col, outline=ink, width=3)
            d.line([(cx - 5, my + 2), (cx + 5, my + 2)], fill=(255, 220, 230, 240), width=2)

    def _draw_cora_faye_zendaya_zoe(self, d: ImageDraw.ImageDraw, pose: str, expr: str, mouth_open: bool, t: float):
        ink = self.INK_BLACK
        skin = (186, 124, 88, 255)
        skin_shadow = (128, 76, 48, 255)
        hair_dark = (18, 15, 22, 255)

        for cx, cy, r in [(190, 120, 52), (290, 120, 52), (165, 175, 48), (315, 175, 48), (240, 88, 56)]:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=hair_dark, outline=ink, width=5)

        d.polygon([(205, 345), (275, 345), (292, 415), (188, 415)], fill=skin, outline=ink, width=5)
        d.polygon([(205, 345), (235, 345), (225, 415), (188, 415)], fill=skin_shadow)
        d.polygon([(185, 405), (295, 405), (302, 468), (240, 455), (178, 468)], fill=self.GRIT_DARK, outline=ink, width=6)
        d.polygon([(182, 468), (228, 468), (222, 535), (176, 535)], fill=skin, outline=ink, width=5)
        d.polygon([(252, 468), (298, 468), (304, 535), (258, 535)], fill=skin_shadow, outline=ink, width=5)
        d.rectangle([252, 485, 304, 502], fill=self.PRIMARY_RED, outline=ink, width=4)
        d.rectangle([288, 470, 322, 545], fill=(60, 35, 25, 255), outline=ink, width=5)
        d.polygon([(174, 530), (224, 530), (218, 690), (168, 690)], fill=self.INK_BLACK, outline=self.NEON_CYAN, width=3)
        d.polygon([(256, 530), (306, 530), (312, 690), (262, 690)], fill=self.INK_BLACK, outline=self.NEON_CYAN, width=3)

        d.polygon([(190, 230), (290, 230), (275, 345), (205, 345)], fill=skin, outline=ink, width=5)
        d.polygon([(186, 242), (228, 295), (240, 295), (252, 295), (294, 242), (282, 348), (198, 348)],
                  fill=self.PRIMARY_YELLOW, outline=ink, width=6)
        d.line([(240, 265), (240, 302)], fill=skin_shadow, width=4)

        d.polygon([(135, 255), (192, 242), (188, 435), (122, 420)], fill=(210, 175, 120, 255), outline=ink, width=6)
        d.polygon([(288, 242), (345, 255), (358, 420), (292, 435)], fill=(210, 175, 120, 255), outline=ink, width=6)
        d.polygon([(172, 245), (192, 242), (188, 390), (165, 380)], fill=self.PRIMARY_RED, outline=ink, width=4)
        d.polygon([(288, 242), (308, 245), (315, 380), (292, 390)], fill=self.PRIMARY_RED, outline=ink, width=4)

        if pose == "AIM":
            d.polygon([(315, 255), (445, 225), (445, 260), (315, 290)], fill=(210, 175, 120, 255), outline=ink, width=6)
            d.polygon([(425, 195), (475, 195), (475, 232), (435, 252)], fill=self.NEON_CYAN, outline=ink, width=5)
        else:
            d.polygon([(140, 265), (105, 345), (175, 410), (188, 390), (140, 340), (168, 275)], fill=(210, 175, 120, 255), outline=ink, width=6)
            d.polygon([(340, 265), (375, 345), (305, 410), (292, 390), (340, 340), (312, 275)], fill=(210, 175, 120, 255), outline=ink, width=6)

        d.polygon([(192, 110), (288, 110), (280, 185), (240, 222), (200, 185)], fill=skin, outline=ink, width=6)
        d.ellipse([172, 165, 196, 195], outline=self.PRIMARY_YELLOW, width=5)
        d.ellipse([284, 165, 308, 195], outline=self.PRIMARY_YELLOW, width=5)
        self._draw_anime_eyes_and_lips(
            d, 240, 156, eye_col=(195, 125, 45, 255), lip_col=self.PRIMARY_RED,
            sultry=(expr != "ANGRY"), mouth_open=mouth_open, angry=(expr == "ANGRY")
        )

    def _draw_jules_faye_mai_marceline(self, d: ImageDraw.ImageDraw, pose: str, expr: str, mouth_open: bool, t: float):
        ink = self.INK_BLACK
        skin = (246, 228, 220, 255)
        hair_black = (14, 12, 22, 255)
        sweater_pink = (255, 145, 185, 255)
        sweater_shadow = (195, 85, 130, 255)

        sway = int(14 * math.sin(t * math.pi * 2))
        d.polygon([
            (175, 105), (305, 105), (350 + sway, 490),
            (290 + sway, 540), (180 + sway, 540), (130 + sway, 480)
        ], fill=hair_black, outline=ink, width=6)

        d.polygon([(195, 450), (232, 450), (226, 525), (190, 525)], fill=skin, outline=ink, width=5)
        d.polygon([(248, 450), (285, 450), (290, 525), (254, 525)], fill=skin, outline=ink, width=5)
        d.rectangle([248, 482, 290, 496], fill=self.PRIMARY_RED, outline=ink, width=4)
        d.polygon([(190, 520), (226, 520), (220, 685), (184, 685)], fill=(24, 22, 32, 240), outline=ink, width=5)
        d.polygon([(254, 520), (290, 520), (296, 685), (260, 685)], fill=(24, 22, 32, 240), outline=ink, width=5)

        d.polygon([(185, 215), (295, 215), (285, 275), (195, 275)], fill=skin, outline=ink, width=5)
        d.rectangle([222, 218, 258, 228], fill=ink)
        d.polygon([
            (160, 250), (320, 238), (290, 355), (318, 462), (162, 462), (190, 355)
        ], fill=sweater_pink, outline=ink, width=6)
        d.polygon([(160, 250), (215, 246), (205, 462), (162, 462)], fill=sweater_shadow)
        d.ellipse([245, 385, 295, 425], fill=(255, 255, 250, 230))

        if pose in ("KNIFE_THROW", "ACTION"):
            d.polygon([(305, 240), (435, 180), (450, 245), (315, 290)], fill=sweater_pink, outline=ink, width=6)
            for ky in [190, 212, 234]:
                d.polygon([(425, ky), (478, ky - 6), (425, ky + 6)], fill=self.NEON_CYAN, outline=ink, width=3)
        else:
            bounce = int(14 * math.cos(t * math.pi * 4))
            d.polygon([(160, 250), (105, 360 + bounce), (148, 390 + bounce), (188, 285)], fill=sweater_pink, outline=ink, width=6)
            d.polygon([(320, 240), (375, 360 - bounce), (332, 390 - bounce), (292, 285)], fill=sweater_pink, outline=ink, width=6)

        d.polygon([(194, 112), (286, 112), (278, 188), (240, 220), (202, 188)], fill=skin, outline=ink, width=6)
        d.polygon([(186, 98), (294, 98), (294, 145), (265, 138), (240, 150), (215, 138), (186, 145)], fill=hair_black, outline=ink, width=5)

        d.polygon([(172, 102), (150, 48), (205, 78)], fill=self.NEON_MAGENTA, outline=ink, width=5)
        d.polygon([(308, 102), (330, 48), (275, 78)], fill=self.NEON_MAGENTA, outline=ink, width=5)
        d.ellipse([162, 132, 192, 180], fill=self.NEON_CYAN, outline=ink, width=5)
        d.ellipse([288, 132, 318, 180], fill=self.NEON_CYAN, outline=ink, width=5)

        eye_col = self.PRIMARY_RED if expr in ("MANIC", "ANGRY") else (215, 45, 125, 255)
        self._draw_anime_eyes_and_lips(
            d, 240, 156, eye_col=eye_col, lip_col=self.NEON_MAGENTA,
            sultry=True, mouth_open=mouth_open, angry=(expr in ("MANIC", "ANGRY"))
        )

    def _draw_penelope_faye_holo(self, d: ImageDraw.ImageDraw, mouth_open: bool, t: float):
        ink = (10, 45, 65, 255)
        holo_skin = (155, 248, 255, 215)
        holo_suit = (20, 65, 95, 225)
        d.polygon([(165, 235), (315, 235), (290, 375), (315, 580), (165, 580), (190, 375)], fill=holo_suit, outline=self.NEON_CYAN, width=5)
        d.polygon([(205, 235), (275, 235), (240, 335)], fill=holo_skin, outline=self.NEON_MAGENTA, width=4)
        d.polygon([(195, 110), (285, 110), (278, 188), (240, 220), (202, 188)], fill=holo_skin, outline=self.NEON_CYAN, width=5)
        self._draw_anime_eyes_and_lips(d, 240, 155, self.NEON_MAGENTA, self.NEON_MAGENTA, True, mouth_open)
        for y in range(100, 580, 18):
            d.line([(155, y), (325, y)], fill=(24, 245, 255, 110), width=2)

    def _draw_pete_barry_syx_anime(self, d: ImageDraw.ImageDraw, char_type: str, pose: str, expr: str, mouth_open: bool, t: float):
        ink = self.INK_BLACK
        skin = (235, 168, 122, 255)
        skin_shadow = (165, 102, 64, 255)

        d.polygon([(155, 75), (325, 75), (365, 245), (115, 245)], fill=self.PRIMARY_YELLOW, outline=ink, width=6)
        d.rectangle([150, 460, 225, 670], fill=skin, outline=ink, width=6)
        d.rectangle([255, 460, 330, 670], fill=skin_shadow, outline=ink, width=6)
        d.polygon([(140, 415), (340, 415), (335, 488), (240, 470), (145, 488)], fill=self.PRIMARY_BLUE, outline=ink, width=6)
        d.rectangle([145, 595, 230, 685], fill=self.INK_BLACK, outline=ink, width=6)
        d.rectangle([250, 595, 335, 685], fill=self.INK_BLACK, outline=ink, width=6)

        d.polygon([(75, 195), (405, 195), (335, 420), (145, 420)], fill=skin, outline=ink, width=7)
        d.polygon([(240, 195), (405, 195), (335, 420), (240, 420)], fill=skin_shadow)
        d.polygon([(110, 195), (205, 195), (195, 395), (130, 395)], fill=self.GRIT_SLATE, outline=ink, width=6)
        d.polygon([(275, 195), (370, 195), (350, 395), (285, 395)], fill=self.GRIT_SLATE, outline=ink, width=6)
        
        font = self.get_font(20, bold=True)
        tag = "BARRY-PETE 01" if "1" in char_type else "BARRY-PETE 02"
        d.rectangle([135, 235, 345, 270], fill=self.INK_BLACK, outline=self.NEON_LIME, width=3)
        d.text((168, 242), tag, fill=self.NEON_LIME, font=font)

        d.ellipse([30, 200, 130, 360], fill=skin, outline=ink, width=6)
        d.ellipse([350, 200, 450, 360], fill=skin_shadow, outline=ink, width=6)
        if pose == "DRINK_MILK":
            d.rectangle([370, 115, 445, 210], fill=(250, 250, 255, 255), outline=ink, width=5)
            d.rectangle([370, 145, 445, 178], fill=self.PRIMARY_RED)
            d.text((380, 150), "MILK", fill=(255, 255, 255, 255), font=font)

        d.rectangle([188, 88, 292, 198], fill=skin, outline=ink, width=6)
        d.polygon([(175, 95), (205, 45), (240, 72), (275, 45), (305, 95)], fill=self.PRIMARY_YELLOW, outline=ink, width=5)
        shade_col = self.NEON_MAGENTA if "1" in char_type else self.NEON_CYAN
        d.polygon([(180, 115), (300, 115), (288, 150), (240, 140), (192, 150)], fill=shade_col, outline=ink, width=5)
        d.line([(192, 124), (235, 124)], fill=(255, 255, 255, 255), width=4)
        d.polygon([(198, 168), (240, 155), (282, 168), (288, 186), (240, 176), (192, 186)], fill=self.PRIMARY_YELLOW, outline=ink, width=4)
        if mouth_open:
            d.rectangle([212, 178, 268, 195], fill=(255, 255, 255, 255), outline=ink, width=3)

    def _draw_supporting_anime(self, d: ImageDraw.ImageDraw, char_type: str, mouth_open: bool):
        ink = self.INK_BLACK
        d.polygon([(145, 235), (335, 235), (355, 660), (125, 660)], fill=self.GRIT_DARK, outline=ink, width=6)
        d.polygon([(190, 105), (290, 105), (280, 192), (240, 222), (200, 192)], fill=(205, 155, 125, 255), outline=ink, width=6)
        if char_type == "SHERIFF":
            d.polygon([(150, 115), (330, 115), (290, 20), (190, 20)], fill=self.INK_BLACK, outline=self.PRIMARY_YELLOW, width=4)
            d.ellipse([205, 145, 235, 168], outline=self.NEON_CYAN, width=3)
            d.ellipse([245, 145, 275, 168], outline=self.NEON_CYAN, width=3)
            d.rectangle([75, 310, 160, 420], fill=self.PRIMARY_YELLOW, outline=ink, width=5)
        font = self.get_font(22, bold=True)
        d.rectangle([150, 280, 330, 320], fill=self.PRIMARY_RED, outline=ink, width=4)
        d.text((168, 287), char_type, fill=(255, 255, 255, 255), font=font)

    def _draw_rig_belle(self, t: float) -> Image.Image:
        img = Image.new("RGBA", (880, 420), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        ink = self.INK_BLACK
        plume = int(110 + 40 * math.sin(t * math.pi * 8))
        d.polygon([(15, 220), (135 + plume, 170), (135 + plume, 270)], fill=self.NEON_CYAN)
        d.polygon([(115, 220), (670, 125), (850, 225), (650, 315), (145, 295)], fill=self.GRIT_GREY, outline=ink, width=7)
        d.polygon([(145, 295), (650, 315), (850, 225), (610, 235), (125, 245)], fill=self.INK_BLACK)
        d.polygon([(340, 295), (565, 305), (540, 370), (315, 345)], fill=self.PRIMARY_RED, outline=ink, width=6)
        d.chord([375, 70, 620, 205], 180, 360, fill=self.NEON_CYAN, outline=ink, width=6)
        return img

    def _draw_rig_skiff(self) -> Image.Image:
        img = Image.new("RGBA", (640, 320), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.polygon([(50, 150), (490, 90), (600, 240), (80, 260)], fill=self.GRIT_SLATE, outline=self.INK_BLACK, width=6)
        d.rectangle([120, 165, 520, 195], fill=self.PRIMARY_YELLOW)
        return img

    def _draw_rig_sabrina_cat(self, t: float) -> Image.Image:
        img = Image.new("RGBA", (240, 220), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        tail_wag = int(16 * math.sin(t * math.pi * 4))
        d.line([(60, 155), (25, 85 + tail_wag)], fill=self.INK_BLACK, width=14)
        d.ellipse([60, 100, 165, 185], fill=self.INK_BLACK, outline=self.NEON_CYAN, width=3)
        d.ellipse([120, 50, 195, 120], fill=self.INK_BLACK, outline=self.NEON_CYAN, width=3)
        d.polygon([(125, 62), (138, 18), (155, 55)], fill=self.INK_BLACK)
        d.polygon([(158, 55), (180, 18), (190, 62)], fill=self.INK_BLACK)
        d.ellipse([138, 75, 154, 91], fill=self.PRIMARY_YELLOW)
        d.ellipse([166, 75, 182, 91], fill=self.PRIMARY_YELLOW)
        return img


# ============================================================================
# 4. SCREENPLAY PARSER AND STAGING
# ============================================================================

class BelleScriptChoreographer:
    KNOWN_SPEAKERS = {
        "SHERIFF", "CAPTAIN", "CORA VALENTINE", "DEPUTY", "PETE 1", "PETE 2",
        "P.E.T.E. 1", "P.E.T.E. 2", "JULES", "NORTHRIDGE REP", "AUTOMATED DOCKING ASSISTANT",
        "INTERCOM", "BUSINESS MAN", "BUSINESS MAN (LEO)", "PARTY GUEST",
        "PRIVATE SECURITY OFFICER", "PETE 1 AND PETE 2"
    }

    @classmethod
    def load_screenplay(cls, pdf_path="Belle Overdrive.pdf") -> List[Beat]:
        lines = []
        if fitz is not None and os.path.exists(pdf_path):
            with fitz.open(pdf_path) as doc:
                for page in doc:
                    for ln in page.get_text("text").splitlines():
                        line = ln.strip()
                        if line and not re.match(r"^\d+\.$", line):
                            lines.append(line)
        if not lines:
            lines = cls._embedded_belle_overdrive_script()
        return cls._build_beats(lines)

    @classmethod
    def _scene_to_style(cls, heading: str) -> str:
        h = heading.upper()
        if "DESERT" in h: return "DESERT"
        if "BELLE KITCHEN" in h: return "KITCHEN"
        if "CANYON" in h: return "CANYON"
        if "COCKPIT" in h: return "COCKPIT"
        if "HULL" in h or "DEEP SPACE" in h: return "SPACE_GVR"
        if "ROOF" in h: return "ROOF"
        if "TRASH" in h: return "TRASH"
        if "BRUCILLA" in h: return "BRUCILLAS"
        if "PANTRY" in h or "MANSION KITCHEN" in h: return "PANTRY"
        if "SKIFF" in h: return "SKIFF"
        if "BALLROOM" in h: return "BALLROOM"
        if "FOYER" in h: return "FOYER"
        if "CARGO" in h: return "CARGO"
        return "DESERT"

    @classmethod
    def _determine_camera_grammar(cls, scene_heading: str, text: str) -> str:
        t_up = text.upper()
        s_up = scene_heading.upper()
        action_keywords = [
            "SHOOTS", "LASER BOLT", "HEAD ERUPTS", "STAMPEDE", "GRENADE", "EXPLODES",
            "COMBAT ROLL", "IMPALES", "DETACH", "PUFFER JETS", "CROWBAR", "KNIFE",
            "KICKS", "THIEF", "CORKSCREW"
        ]
        if any(k in t_up for k in action_keywords):
            return "DYNAMIC_ACTION"
        if s_up.startswith("EXT."):
            return "LONELY_EXT"
        return "CONFINED_INT"

    @classmethod
    def _build_beats(cls, lines: List[str]) -> List[Beat]:
        beats: List[Beat] = []
        cur_scene = "EXT. DESERT WASTELAND DAY"
        cur_speaker = None
        buf = []
        b_id = 1

        def flush(b_type, spk, txt_list):
            nonlocal b_id
            text = " ".join(txt_list).strip()
            if not text or text.upper() in {"BELLE OVERDRIVE", "WRITTEN BY", "AUTHOR", "ADDRESS", "PHONE", "E-MAIL", "(MORE)"}:
                return
            dur = max(2.6, min(7.2, len(text.split()) / 2.6))
            grammar = cls._determine_camera_grammar(cur_scene, text)
            b = Beat(
                beat_id=b_id,
                scene_heading=cur_scene,
                beat_type=b_type,
                speaker=spk if b_type == "DIALOGUE" else "NARRATOR",
                text=text,
                duration=round(dur, 2),
                bg_style=cls._scene_to_style(cur_scene),
                camera_grammar=grammar
            )
            cls._stage_beat(b)
            beats.append(b)
            b_id += 1

        for ln in lines:
            if ln.startswith(("EXT.", "INT.")):
                if buf:
                    flush("DIALOGUE" if cur_speaker else "ACTION", cur_speaker or "", buf)
                    buf.clear()
                cur_scene = ln
                cur_speaker = None
                continue

            clean_spk = re.sub(r"\(.*?\)", "", ln).strip()
            if clean_spk in cls.KNOWN_SPEAKERS and ln.isupper():
                if buf:
                    flush("DIALOGUE" if cur_speaker else "ACTION", cur_speaker or "", buf)
                    buf.clear()
                cur_speaker = clean_spk
                continue

            if cur_speaker and len(ln) > 46 and not ln.startswith("(") and any(
                k in ln for k in ["The Sheriff", "Cora ", "Pete ", "Jules ", "The man ", "The trio "]
            ):
                flush("DIALOGUE", cur_speaker, buf)
                buf.clear()
                cur_speaker = None

            buf.append(ln)
            if not cur_speaker and len(" ".join(buf)) > 175:
                flush("ACTION", "", buf)
                buf.clear()

        if buf:
            flush("DIALOGUE" if cur_speaker else "ACTION", cur_speaker or "", buf)

        return beats

    @classmethod
    def _stage_beat(cls, b: Beat):
        t_up = b.text.upper()
        st = b.bg_style
        grammar = b.camera_grammar

        if grammar == "LONELY_EXT":
            base_scale = 0.38
            base_y = 665
        elif grammar == "CONFINED_INT":
            base_scale = 1.18
            base_y = 610
        else:
            base_scale = 1.24
            base_y = 580

        if "CAT" in t_up or "KITTTY" in t_up or "FLOATING" in t_up or "SHADOW" in t_up or "SLIPSTREAM" in t_up:
            b.panel_mode = "HARUKI_DREAM"
        elif "HUNG" in t_up or "HANGED" in t_up or "THREE MILLION" in t_up:
            b.panel_mode = "SPLIT_DUEL"
        elif "CLONES" in t_up or "PUFFER JETS" in t_up or "UNISON" in t_up:
            b.panel_mode = "TRIPLE_BEBOP"
        elif "GREEN VALLEY" in t_up or "BRUCILLA" in t_up or "MILK" in t_up:
            b.panel_mode = "GUIDE_HUD"
        else:
            b.panel_mode = "FULL"

        cast = []
        if st == "DESERT":
            if b.beat_id <= 14 and "CORA" not in t_up:
                cast = [("SHERIFF", "SHERIFF", 640, base_y, "IDLE", "DEFAULT"), ("CAPTAIN", "CAPTAIN", 1280, base_y, "IDLE", "SULTRY")]
            else:
                cora_pose = "AIM" if grammar == "DYNAMIC_ACTION" else "IDLE"
                pete_pose = "DRINK_MILK" if "MILK" in t_up or "MACROS" in t_up else "FLEX"
                cast = [
                    ("CORA", "CORA", 680, base_y, cora_pose, "SULTRY"),
                    ("PETE_1", "PETE_1", 1040, base_y, pete_pose, "HIMBO_GRIN"),
                    ("PETE_2", "PETE_2", 1320, base_y, "FLEX", "HIMBO_GRIN")
                ]
        elif st == "KITCHEN":
            j_pose = "KNIFE_THROW" if grammar == "DYNAMIC_ACTION" else "DANCE"
            cast = [("JULES", "JULES", 1180, base_y, j_pose, "SULTRY")]
            if "CORA" in t_up or b.speaker == "CORA VALENTINE":
                cast.insert(0, ("CORA", "CORA", 680, base_y, "SHRUG", "SULTRY"))
        elif st == "COCKPIT":
            cast = [("CORA", "CORA", 640, base_y, "IDLE", "SULTRY"), ("NORTHRIDGE", "NORTHRIDGE", 1280, base_y - 30, "IDLE", "SULTRY")]
        elif st in ("SPACE_GVR", "ROOF"):
            cast = [
                ("BELLE", "BELLE", 960, 260, "IDLE", "DEFAULT"),
                ("CORA", "CORA", 960, base_y, "AIM", "SULTRY"),
                ("PETE_1", "PETE_1", 620, base_y, "FLEX", "HIMBO_GRIN"),
                ("PETE_2", "PETE_2", 1300, base_y, "FLEX", "HIMBO_GRIN")
            ]
        elif st in ("BRUCILLAS", "SKIFF", "TRASH"):
            j_pose = "KNIFE_THROW" if grammar == "DYNAMIC_ACTION" else "IDLE"
            j_expr = "MANIC" if "DYSON" in t_up or "MESSAGE" in t_up else "SULTRY"
            cast = [("JULES", "JULES", 700, base_y, j_pose, j_expr), ("LEO", "LEO", 1260, base_y, "IDLE", "ANGRY")]
            if "CAT" in t_up or "KITTTY" in t_up:
                cast.append(("SABRINA", "CAT", 980, base_y + 110, "IDLE", "DEFAULT"))
        elif st == "CARGO":
            cast = [
                ("SKIFF", "SKIFF", 1320, base_y - 20, "IDLE", "DEFAULT"),
                ("CORA", "CORA", 580, base_y, "SHRUG", "SULTRY"),
                ("JULES", "JULES", 940, base_y, "DANCE", "SULTRY"),
                ("SABRINA", "CAT", 1120, base_y + 120, "IDLE", "DEFAULT")
            ]
        else:
            cast = [
                ("CORA", "CORA", 680, base_y, "IDLE", "SULTRY"),
                ("PETE_1", "PETE_1", 1220, base_y, "FLEX", "HIMBO_GRIN")
            ]

        for idx, (aid, ctype, x, y, pose, expr) in enumerate(cast):
            speaking = (b.speaker and speaker_matches_actor(aid, b.speaker))
            s0 = base_scale * (1.06 if speaking else 1.0)
            s1 = base_scale * (1.16 if grammar == "DYNAMIC_ACTION" else (1.10 if speaking else 1.02))
            rot0 = -8.0 if grammar == "DYNAMIC_ACTION" and idx % 2 == 0 else 0.0
            rot1 = 6.0 if grammar == "DYNAMIC_ACTION" else 0.0
            dx = 95.0 if grammar == "DYNAMIC_ACTION" else (12.0 if grammar == "LONELY_EXT" else 22.0)

            b.tracks.append(ActorTrack(
                actor_id=aid,
                char_type=ctype,
                z_index=idx + 1,
                keyframes=[
                    Keyframe(0.0, x, y, s0, rot0, 1.0, pose, expr),
                    Keyframe(1.0, x + (dx if idx % 2 == 0 else -dx), y, s1, rot1, 1.0, pose, expr)
                ]
            ))

        if grammar == "DYNAMIC_ACTION":
            b.vfx.append(VFXEvent("LASER_BOLT", 0.15, 0.65, 480, 480, 1460, 440))
        if "HEAD ERUPTS" in t_up or "EXPLODES" in t_up or "GRENADE" in t_up:
            b.vfx.append(VFXEvent("PLASMA_BLAST", 0.25, 0.90, 1280, 480, 1280, 480))
        if b.panel_mode == "HARUKI_DREAM":
            b.vfx.append(VFXEvent("MURAKAMI_MOONS", 0.05, 0.98, 960, 180, 960, 180, ""))
        elif b.panel_mode == "GUIDE_HUD":
            guide_txt = (
                "GUIDE ENTRY #42: WARM DESERT MILK — Statistically fatal to morale, yet worshipped by the Barry-Pete clone genome."
                if "MILK" in t_up else
                "GUIDE ENTRY #809: GREEN VALLEY ROCKS — Where trillionaires retire inside neon-pink Victorian fortresses."
            )
            b.vfx.append(VFXEvent("GUIDE_POPUP", 0.08, 0.96, 960, 190, 960, 190, guide_txt))

    @classmethod
    def _embedded_belle_overdrive_script(cls) -> List[str]:
        return [
            "EXT. DESERT WASTELAND DAY",
            "Standing in the dirt is THE SHERIFF peering over tiny reading glasses at a piece of parchment.",
            "SHERIFF",
            "Captain, for the crimes of treason and violence against the will of God, you are sentenced to be hung from the neck until dead.",
            "CAPTAIN",
            "It's hanged. When it is people it is hanged. Objects are hung.",
            "The Sheriff casually draws a laser pistol and shoots the Captain square in the chest.",
            "CORA VALENTINE",
            "Seems like I didn't need to be here for all this... now if I can just get the reward I will get out of your way.",
            "A vibrant LASER BOLT snaps through the air. The SHERIFF'S head erupts in a smoldering cloud of ash and sparks.",
            "TWO CLONES P.E.T.E. 1 and P.E.T.E. 2 ride in wearing load bearing vests over cutoff shorts, sporting aggressive mullets and 1980s mustaches.",
            "Pete 1 rockets a plasma grenade over the crowd and explodes the skiff.",
            "PETE 1",
            "Much better post-workout recovery, Pete! Petes have to look out for Petes, and Petes need to be strong.",
            "PETE 2",
            "Thanks, Pete! You always look out for my macros.",
            "INT. BELLE KITCHEN CONTINUOUS",
            "Jules Etter dances in her blush pink oversized sweater and cat-eared headphones, then throws a pin knife from her sleeve to impale a bug.",
            "JULES",
            "Not for you, mister Bug.",
            "INT. BELLE COCKPIT LATER",
            "Outside the glass, the FTL SLIPSTREAM rushes by. A holographic projection of NORTHRIDGE REP KL Penelope springs up.",
            "NORTHRIDGE REP",
            "The location is the Green Valley Rocks Retirement Community. And we are authorized to offer three million credits.",
            "EXT. BELLE HULL LATER",
            "Cora and the Petes detach in EVA suits and fire PUFFER JETS toward the neon-pink Victorian asteroid mansion.",
            "EXT. BRUCILLA'S TRASH STATION CONTINUOUS",
            "Jules appears out of the shadows, three knives slipping from each of her sleeves into her hands.",
            "JULES",
            "Silly boy. You don't even need to be alive to send a message. Kittty!!! You want to come with me?",
            "INT. BELLE CARGO BAY CONTINUOUS",
            "A black cat comes from the shadow of the stairs following Jules. Cora sighs."
        ]


# ============================================================================
# 5. MASTER 1080P COMPOSITOR
# ============================================================================

class CinematicCompositor:
    def __init__(self, art_engine: AnimeCelArtEngine):
        self.art = art_engine
        self.font_title = art_engine.get_font(46, bold=True)
        self.font_speaker = art_engine.get_font(28, bold=True)
        self.font_dialogue = art_engine.get_font(26, bold=False)
        self.font_haruki = art_engine.get_font(26, serif=True)
        self.font_hud = art_engine.get_font(22, bold=True)

    @staticmethod
    def _interp(keyframes: List[Keyframe], t: float) -> Keyframe:
        """Interpolate continuous channels with smoothstep; retain discrete pose channels."""
        if not keyframes:
            return Keyframe()
        ordered = sorted(keyframes, key=lambda item: item.time_norm)
        t = float(np.clip(t, 0.0, 1.0))
        if t <= ordered[0].time_norm:
            return copy.copy(ordered[0])
        if t >= ordered[-1].time_norm:
            return copy.copy(ordered[-1])
        left, right = ordered[0], ordered[-1]
        for index in range(len(ordered) - 1):
            if ordered[index].time_norm <= t <= ordered[index + 1].time_norm:
                left, right = ordered[index], ordered[index + 1]
                break
        span = max(1e-9, right.time_norm - left.time_norm)
        u = float(np.clip((t - left.time_norm) / span, 0.0, 1.0))
        u = u * u * (3.0 - 2.0 * u)
        def blend(a, b):
            return float(a + (b - a) * u)
        return Keyframe(
            time_norm=t, x=blend(left.x, right.x), y=blend(left.y, right.y),
            scale=blend(left.scale, right.scale), rotation=blend(left.rotation, right.rotation),
            alpha=blend(left.alpha, right.alpha), pose=left.pose,
            expression=left.expression, flip_x=left.flip_x
        )

    def render_frame(self, beat: Beat, t_norm: float) -> Image.Image:
        canvas = self.art.render_background(beat.bg_style, beat.camera_grammar, t_norm)

        for tr in sorted(beat.tracks, key=lambda item: item.z_index):
            kf = self._interp(tr.keyframes, t_norm)
            if kf.alpha <= 0.01 or kf.scale <= 0.01:
                continue
            is_talking = (
                beat.beat_type == "DIALOGUE"
                and speaker_matches_actor(
                    tr.actor_id,
                    beat.speaker
                )
            )
            rig = self.art.render_character_rig(tr.char_type, kf.pose, kf.expression, is_talking, t_norm)
            if kf.flip_x:
                rig = rig.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            nw, nh = max(1, int(rig.width * kf.scale)), max(1, int(rig.height * kf.scale))
            rig = rig.resize((nw, nh), Image.Resampling.LANCZOS)
            if kf.alpha < 0.999:
                alpha = rig.getchannel("A").point(lambda value: int(value * float(np.clip(kf.alpha, 0.0, 1.0))))
                rig.putalpha(alpha)
            if abs(kf.rotation) > 0.5:
                rig = rig.rotate(kf.rotation, expand=True, resample=Image.Resampling.BICUBIC)

            px = int(kf.x - rig.width / 2)
            py = int(kf.y - rig.height / 2)
            layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
            layer.paste(rig, (px, py))
            canvas.alpha_composite(layer)

        draw = ImageDraw.Draw(canvas)
        self.art.render_foreground_occlusion(draw, beat.camera_grammar, beat.bg_style, t_norm)

        if beat.panel_mode == "SPLIT_DUEL":
            draw.line([(960, 0), (880, 1080)], fill=self.art.INK_BLACK, width=28)
            draw.line([(960, 0), (880, 1080)], fill=self.art.PRIMARY_RED, width=8)
        elif beat.panel_mode == "TRIPLE_BEBOP":
            draw.line([(640, 0), (590, 1080)], fill=self.art.INK_BLACK, width=24)
            draw.line([(640, 0), (590, 1080)], fill=self.art.PRIMARY_YELLOW, width=5)
            draw.line([(1280, 0), (1230, 1080)], fill=self.art.INK_BLACK, width=24)
            draw.line([(1280, 0), (1230, 1080)], fill=self.art.NEON_CYAN, width=5)

        for vfx in beat.vfx:
            if vfx.time_start <= t_norm <= vfx.time_end:
                lt = (t_norm - vfx.time_start) / max(0.001, vfx.time_end - vfx.time_start)
                self._draw_vfx(draw, vfx, lt)

        self._draw_ui_chrome(draw, beat, t_norm)
        return canvas

    def _draw_vfx(self, d: ImageDraw.ImageDraw, vfx: VFXEvent, lt: float):
        if vfx.vfx_type == "LASER_BOLT":
            cx = vfx.start_x + (vfx.end_x - vfx.start_x) * lt
            cy = vfx.start_y + (vfx.end_y - vfx.start_y) * lt
            tx = vfx.start_x + (vfx.end_x - vfx.start_x) * max(0.0, lt - 0.30)
            ty = vfx.start_y + (vfx.end_y - vfx.start_y) * max(0.0, lt - 0.30)
            d.line([(tx, ty), (cx, cy)], fill=self.art.NEON_MAGENTA, width=26)
            d.line([(tx, ty), (cx, cy)], fill=(255, 255, 255, 255), width=8)

        elif vfx.vfx_type == "PLASMA_BLAST":
            r = int(45 + 290 * math.sin(lt * math.pi))
            cx, cy = int(vfx.start_x), int(vfx.start_y)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=self.art.PRIMARY_RED, outline=self.art.PRIMARY_YELLOW, width=10)
            self.art.draw_takashi_flower_neon(d, cx, cy, int(r * 0.6), rot_deg=lt * 180)

        elif vfx.vfx_type == "MURAKAMI_MOONS":
            # A quiet celestial insert, not a quotation card or title gag.
            self.art.draw_haruki_twin_moons(d, 1580, 220, 66)

        elif vfx.vfx_type == "GUIDE_POPUP":
            d.rounded_rectangle([310, 120, 1610, 245], radius=16, fill=(12, 16, 22, 240), outline=self.art.NEON_CYAN, width=4)
            d.rectangle([335, 135, 680, 170], fill=self.art.PRIMARY_RED)
            d.text((348, 139), "FIELD NOTE / ARCHIVE", fill=(255, 255, 255, 255), font=self.font_hud)
            d.text((335, 185), vfx.label_text[:96], fill=self.art.NEON_CYAN, font=self.font_hud)
        elif vfx.vfx_type == "EVA_PUFFER":
            cx = int(vfx.start_x + (vfx.end_x - vfx.start_x) * lt)
            cy = int(vfx.start_y + (vfx.end_y - vfx.start_y) * lt)
            radius = max(8, int(34 * (1.0 - lt * 0.45)))
            d.ellipse((cx-radius, cy-radius, cx+radius, cy+radius),
                      fill=(188, 207, 211, int(180 * (1.0-lt))), outline=(226, 232, 224, 210), width=3)
        elif vfx.vfx_type == "SPEED_LINES":
            for index in range(18):
                y = int((index * 71 + lt * 900) % 1080)
                d.line((0, y, 450 + index * 17, y - 90), fill=(205, 214, 220, 115), width=2)
        elif vfx.vfx_type == "BEBOP_FREEZE":
            d.rectangle((0, 0, 1920, 1080), outline=(221, 226, 230, 180), width=8)


    def _draw_ui_chrome(self, d: ImageDraw.ImageDraw, beat: Beat, t_norm: float):
        """Minimal editorial overlay: scene metadata and restrained dialogue subtitles."""
        top = (17, 20, 27, 205)
        line = (194, 169, 119, 210)
        d.rectangle((0, 0, 1920, 58), fill=top)
        d.line((0, 58, 1920, 58), fill=line, width=2)
        d.text((54, 17), f"{beat.scene_heading.upper()}   /   {beat.beat_id:03d}",
               fill=(218, 211, 194, 245), font=self.font_hud)

        visible_chars = max(1, int(len(beat.text) * min(1.0, max(0.0, t_norm) / 0.65)))
        visible_text = beat.text[:visible_chars]
        words = visible_text.split()
        lines, current = [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if d.textbbox((0, 0), candidate, font=self.font_dialogue)[2] <= 1500:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        if not lines:
            return

        panel_top = 895 if beat.beat_type == "DIALOGUE" else 930
        d.rounded_rectangle((145, panel_top, 1775, 1050), radius=8,
                            fill=(11, 13, 18, 218), outline=(194, 169, 119, 185), width=2)
        if beat.beat_type == "DIALOGUE":
            d.text((180, panel_top + 12), beat.speaker.upper(),
                   fill=(205, 177, 125, 255), font=self.font_speaker)
            text_y = panel_top + 54
        else:
            text_y = panel_top + 18
        for index, line_text in enumerate(lines[:2]):
            d.text((180, text_y + index * 42), line_text,
                   fill=(239, 237, 228, 255), font=self.font_dialogue)


# ============================================================================
# 6. VIDEO & PROCEDURAL AUDIO EXPORTER
# ============================================================================

class StudioExporter:
    """Frame-accurate export with validated output, progress reporting and safe cleanup."""

    @classmethod
    def export_mp4(cls, beats: List[Beat], compositor: CinematicCompositor,
                   audio_eng: ProceduralAudioEngine, out_path: str, fps: int = 24,
                   progress_cb=None, cancel_event=None):
        if not beats:
            raise ValueError("There are no beats to export.")
        if fps < 1 or fps > 120:
            raise ValueError("Frame rate must be between 1 and 120 FPS.")
        errors, _ = validate_project(beats)
        if errors:
            raise ValueError("Project validation failed:\n" + "\n".join(errors[:12]))

        output = Path(out_path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        raw_output = output.with_name(output.stem + "_rendering" + output.suffix)
        writer = cv2.VideoWriter(str(raw_output), cv2.VideoWriter_fourcc(*"mp4v"),
                                 float(fps), (1920, 1080))
        if not writer.isOpened():
            raise RuntimeError(f"Unable to initialize video writer for: {raw_output}")

        total_frames = sum(max(1, int(round(float(beat.duration) * fps))) for beat in beats)
        frame_number = 0
        audio_tracks = []
        audio_clips = []
        elapsed = 0.0
        completed_raw = False
        try:
            for beat_index, beat in enumerate(beats):
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Export cancelled.")
                if VideoFileClip is not None and AudioFileClip is not None:
                    if beat_index == 0 or beat.scene_heading != beats[beat_index - 1].scene_heading:
                        audio_tracks.append((audio_eng.generate_sfx("HARUKI_JAZZ_CHORD", 2.2), elapsed))
                    for event in beat.vfx:
                        sound = {"LASER_BOLT": "LASER_BOLT", "PLASMA_BLAST": "PLASMA_BLAST",
                                 "GUIDE_POPUP": "GUIDE_POPUP", "EVA_PUFFER": "EVA_PUFFER",
                                 "BEBOP_FREEZE": "BEBOP_FREEZE"}.get(event.vfx_type)
                        if sound:
                            audio_tracks.append((audio_eng.generate_sfx(sound, 0.8),
                                                 elapsed + float(event.time_start) * beat.duration))

                frames = max(1, int(round(float(beat.duration) * fps)))
                for frame_index in range(frames):
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("Export cancelled.")
                    t_norm = frame_index / max(1, frames - 1)
                    rgb = compositor.render_frame(beat, t_norm).convert("RGB")
                    bgr = cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)
                    writer.write(bgr)
                    frame_number += 1
                    if progress_cb and (frame_number % max(1, fps // 2) == 0 or frame_number == total_frames):
                        progress_cb(int(frame_number * 92 / max(1, total_frames)),
                                    f"Rendering {frame_number:,} / {total_frames:,} frames")
                elapsed += float(beat.duration)
            writer.release()
            completed_raw = True

            if audio_tracks and VideoFileClip is not None and AudioFileClip is not None and CompositeAudioClip is not None:
                try:
                    video = VideoFileClip(str(raw_output))
                    for wav_path, start_time in audio_tracks:
                        if wav_path and os.path.exists(wav_path):
                            clip = AudioFileClip(wav_path)
                            clip = clip.with_start(start_time) if hasattr(clip, "with_start") else clip.set_start(start_time)
                            audio_clips.append(clip)
                    if audio_clips:
                        mixed = CompositeAudioClip(audio_clips)
                        final_video = video.with_audio(mixed) if hasattr(video, "with_audio") else video.set_audio(mixed)
                        final_video.write_videofile(str(output), codec="libx264", audio_codec="aac",
                                                    fps=fps, logger=None, threads=max(1, (os.cpu_count() or 2) - 1))
                        final_video.close()
                        mixed.close()
                    else:
                        shutil.copy2(raw_output, output)
                    video.close()
                except Exception as exc:
                    LOGGER.exception("Audio mux failed; preserving rendered video without audio")
                    shutil.copy2(raw_output, output)
                    if progress_cb:
                        progress_cb(96, f"Audio mux unavailable; video retained: {exc}")
            else:
                shutil.copy2(raw_output, output)
            if progress_cb:
                progress_cb(100, f"Export complete: {output}")
            return str(output)
        finally:
            try:
                writer.release()
            except Exception:
                pass
            for clip in audio_clips:
                try:
                    clip.close()
                except Exception:
                    pass
            if raw_output.exists():
                try:
                    raw_output.unlink()
                except OSError:
                    LOGGER.warning("Could not remove temporary render: %s", raw_output)


# ============================================================================
# 8. ADDITIVE PRODUCTION ENHANCEMENTS
# PROJECT MANAGEMENT, VALIDATION, SPEAKER ALIASES, AUDIO CACHE
# ============================================================================

# ---------------------------------------------------------------------------
# 8.1 CENTRALIZED CHARACTER / SPEAKER RECOGNITION
# ---------------------------------------------------------------------------

SPEAKER_ALIASES = {
    "CORA": ["CORA", "CORA VALENTINE"],
    "JULES": ["JULES", "JULES ETTER"],
    "PETE_1": ["PETE 1", "P.E.T.E. 1", "PETE_1"],
    "PETE_2": ["PETE 2", "P.E.T.E. 2", "PETE_2"],
    "SHERIFF": ["SHERIFF"],
    "CAPTAIN": ["CAPTAIN"],
    "DEPUTY": ["DEPUTY"],
    "NORTHRIDGE": ["NORTHRIDGE REP"],
    "LEO": ["BUSINESS MAN", "BUSINESS MAN (LEO)"],
    "GUARD": ["PRIVATE SECURITY OFFICER"],
    "INTERCOM": ["INTERCOM"],
}


def speaker_matches_actor(actor_id, speaker):
    if not speaker:
        return False

    actor_id = actor_id.upper().strip()
    speaker = speaker.upper().strip()

    aliases = SPEAKER_ALIASES.get(
        actor_id, [actor_id]
    )

    return any(
        alias.upper() == speaker
        for alias in aliases
    )


# ---------------------------------------------------------------------------
# 8.2 AUDIO CACHE
# Content-addressed caching is implemented directly by ProceduralAudioEngine.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 8.3 PROJECT SERIALIZATION
# ---------------------------------------------------------------------------

PROJECT_FORMAT_VERSION = 2


def serialize_project(beats):
    return {
        "application": "BELLE OVERDRIVE",
        "format_version": PROJECT_FORMAT_VERSION,
        "saved_at": datetime.now().isoformat(),
        "resolution": {
            "width": 1920,
            "height": 1080
        },
        "fps": 24,
        "beats": [
            asdict(beat) for beat in beats
        ]
    }


def deserialize_project(data):
    if data.get("application") != "BELLE OVERDRIVE":
        raise ValueError("This is not a Belle Overdrive project.")

    version = data.get("format_version")

    if version not in (1, PROJECT_FORMAT_VERSION):
        raise ValueError(f"Unsupported project version: {version}. Supported versions: 1 and {PROJECT_FORMAT_VERSION}.")

    beats = []

    for bd in data.get("beats", []):

        tracks = []

        for td in bd.get("tracks", []):

            keyframes = [
                Keyframe(**kf)
                for kf in td.get("keyframes", [])
            ]

            tracks.append(
                ActorTrack(
                    actor_id=td["actor_id"],
                    char_type=td["char_type"],
                    z_index=td.get("z_index", 1),
                    keyframes=keyframes
                )
            )

        vfx = [
            VFXEvent(**vd)
            for vd in bd.get("vfx", [])
        ]

        beats.append(
            Beat(
                beat_id=bd["beat_id"],
                scene_heading=bd["scene_heading"],
                beat_type=bd["beat_type"],
                speaker=bd["speaker"],
                text=bd["text"],
                duration=bd["duration"],
                bg_style=bd["bg_style"],
                camera_grammar=bd["camera_grammar"],
                panel_mode=bd.get("panel_mode", "FULL"),
                tracks=tracks,
                vfx=vfx
            )
        )

    return beats


# ---------------------------------------------------------------------------
# 8.4 PROJECT VALIDATION ENGINE
# ---------------------------------------------------------------------------

def validate_project(beats):
    errors = []
    warnings = []

    if not beats:
        errors.append("Project contains no screenplay beats.")
        return errors, warnings

    beat_ids = set()

    for beat in beats:

        if beat.beat_id in beat_ids:
            warnings.append(
                f"Duplicate beat ID: {beat.beat_id}"
            )

        beat_ids.add(beat.beat_id)

        if not math.isfinite(float(beat.duration)) or beat.duration <= 0:
            errors.append(f"Beat {beat.beat_id}: Duration must be finite and greater than zero.")

        if not beat.text.strip():
            warnings.append(
                f"Beat {beat.beat_id}: Empty screenplay text."
            )

        if beat.camera_grammar not in (
            "LONELY_EXT",
            "CONFINED_INT",
            "DYNAMIC_ACTION"
        ):
            warnings.append(
                f"Beat {beat.beat_id}: Unknown camera grammar."
            )

        if beat.panel_mode not in (
            "FULL",
            "SPLIT_DUEL",
            "TRIPLE_BEBOP",
            "GUIDE_HUD",
            "HARUKI_DREAM"
        ):
            warnings.append(
                f"Beat {beat.beat_id}: Unknown panel mode."
            )

        for track in beat.tracks:

            if not track.keyframes:
                warnings.append(
                    f"{track.actor_id}: No keyframes in beat "
                    f"{beat.beat_id}."
                )
                continue

            times = [
                k.time_norm for k in track.keyframes
            ]

            if times != sorted(times):
                errors.append(
                    f"{track.actor_id}: Keyframes are unsorted "
                    f"in beat {beat.beat_id}."
                )

            for k in track.keyframes:

                values = [
                    k.time_norm,
                    k.x,
                    k.y,
                    k.scale,
                    k.rotation,
                    k.alpha
                ]

                if not all(math.isfinite(v) for v in values):
                    errors.append(
                        f"{track.actor_id}: Non-finite keyframe value."
                    )

                if not 0 <= k.time_norm <= 1:
                    warnings.append(
                        f"{track.actor_id}: Keyframe outside beat range."
                    )

                if k.scale <= 0:
                    errors.append(
                        f"{track.actor_id}: Invalid scale."
                    )

                if not 0 <= k.alpha <= 1:
                    warnings.append(
                        f"{track.actor_id}: Alpha outside 0-1."
                    )

        for effect in beat.vfx:
            if not all(math.isfinite(value) for value in (effect.time_start, effect.time_end,
                                                           effect.start_x, effect.start_y,
                                                           effect.end_x, effect.end_y)):
                errors.append(f"Beat {beat.beat_id}: VFX contains non-finite values.")
            if not 0.0 <= effect.time_start <= 1.0 or not 0.0 <= effect.time_end <= 1.0:
                errors.append(f"Beat {beat.beat_id}: VFX timing must be normalized to 0-1.")
            if effect.time_end < effect.time_start:
                errors.append(f"Beat {beat.beat_id}: Invalid VFX timing.")

    return errors, warnings


# ---------------------------------------------------------------------------
# 8.5 NATIVE PROJECT SAVE / LOAD
# ---------------------------------------------------------------------------

def atomic_write_project(path: str, project: dict) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(project, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _persist_sprite_assets(self, project_path: str) -> dict:
    """Copy imported sprites beside the project and store portable relative paths."""
    if not getattr(self.art, "custom_sprites", None):
        return {}
    target = Path(project_path).expanduser().resolve()
    asset_dir = target.parent / f"{target.stem}_assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    saved = {}
    for actor_type, sprite in self.art.custom_sprites.items():
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", actor_type) or "actor"
        asset_path = asset_dir / f"{safe_name}.png"
        temporary = asset_path.with_suffix(".tmp.png")
        sprite.convert("RGBA").save(temporary, format="PNG", optimize=True)
        os.replace(temporary, asset_path)
        saved[actor_type] = os.path.relpath(asset_path, target.parent)
    return saved


def _restore_sprite_assets(self, project_path: str, data: dict) -> None:
    self.art.custom_sprites.clear()
    self.art.custom_sprite_paths.clear()
    sprite_map = data.get("assets", {}).get("sprites", {})
    base = Path(project_path).expanduser().resolve().parent
    for actor_type, relative_path in sprite_map.items():
        candidate = (base / relative_path).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            LOGGER.warning("Ignoring sprite path outside project directory: %s", candidate)
            continue
        try:
            sprite = Image.open(candidate).convert("RGBA")
        except (OSError, ValueError) as exc:
            LOGGER.warning("Could not load sprite asset %s for %s: %s", candidate, actor_type, exc)
            continue
        self.art.custom_sprites[actor_type] = sprite
        self.art.custom_sprite_paths[actor_type] = str(candidate)


def save_belle_project(self, path=None, quiet=False):
    if path is None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Belle Overdrive Project", self.project_path or "Belle_Overdrive_Project.belle",
            "Belle Project (*.belle);;JSON Files (*.json)")
    if not path:
        return False
    errors, warnings = validate_project(self.beats)
    if errors:
        if not quiet:
            QMessageBox.critical(self, "Project Validation Failed", "\n".join(errors[:20]))
        return False
    try:
        project = serialize_project(self.beats)
        project["assets"] = {"sprites": self._persist_sprite_assets(path)}
        atomic_write_project(path, project)
        if not quiet:
            message = f"Project saved successfully:\n{path}"
            if warnings:
                message += f"\n\nWarnings: {len(warnings)}\n" + "\n".join(warnings[:8])
            QMessageBox.information(self, "Project Saved", message)
        if not quiet:
            self.project_path = path
        self._dirty = False
        return True
    except Exception as exc:
        LOGGER.exception("Project save failed")
        if not quiet:
            QMessageBox.critical(self, "Save Error", str(exc))
        return False


def load_belle_project(self):
    path, _ = QFileDialog.getOpenFileName(
        self,
        "Open Belle Overdrive Project",
        "",
        "Belle Project (*.belle *.json)"
    )

    if not path:
        return

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        new_beats = deserialize_project(data)

        errors, warnings = validate_project(new_beats)

        if errors:
            QMessageBox.critical(
                self,
                "Project Validation Failed",
                "\n".join(errors[:20])
            )
            return

        self._restore_sprite_assets(path, data)
        self.beats = new_beats
        self.project_path = path
        self.cur_t = 0.0
        self._dirty = False
        self._undo_stack = []
        self._redo_stack = []

        self.slider.blockSignals(True)
        self.slider.setValue(0)
        self.slider.blockSignals(False)

        self._refresh_beat_list()

        if self.beats:
            self.list_beats.blockSignals(True)
            self.list_beats.setCurrentRow(0)
            self.list_beats.blockSignals(False)
            self._select_beat(0)

        message = f"Loaded project:\n{path}"

        if warnings:
            message += (
                f"\n\nWarnings: {len(warnings)}"
                "\n" + "\n".join(warnings[:8])
            )

        QMessageBox.information(
            self,
            "Project Loaded",
            message
        )

    except Exception as exc:
        QMessageBox.critical(
            self,
            "Load Error",
            str(exc)
        )


# ============================================================================
# 7. DIRECTOR WORKSTATION
# ============================================================================

class BelleDirectorWorkstation(QMainWindow):
    def __init__(self, pdf_path="Belle Overdrive.pdf"):
        super().__init__()
        self.setWindowTitle("Belle Overdrive | Director Workstation")
        self.resize(1680, 960)

        self.art = AnimeCelArtEngine()
        self.compositor = CinematicCompositor(self.art)
        self.audio_eng = ProceduralAudioEngine()
        self.beats = BelleScriptChoreographer.load_screenplay(pdf_path)
        if not self.beats:
            raise RuntimeError("No screenplay content was found or could be generated.")

        self.cur_beat_idx = 0
        self.cur_t = 0.0
        self.playing = False

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)

        self._init_gui()
        self._refresh_beat_list()
        self._select_beat(0)

    def _init_gui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        split = QSplitter(Qt.Horizontal)
        layout.addWidget(split)

        # LEFT: Screenplay Beat List
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("SCREENPLAY / SHOT LIST"))
        self.list_beats = QListWidget()
        self.list_beats.currentRowChanged.connect(self._select_beat)
        ll.addWidget(self.list_beats)
        split.addWidget(left)

        # CENTER: 16:9 Viewport & Scrubber
        center = QWidget()
        cl = QVBoxLayout(center)
        self.lbl_view = QLabel()
        self.lbl_view.setFixedSize(960, 540)
        self.lbl_view.setStyleSheet("background:#0a0a0e; border:3px solid #ffd018;")
        cl.addWidget(self.lbl_view, alignment=Qt.AlignCenter)

        t_row = QHBoxLayout()
        self.btn_play = QPushButton("Play")
        self.btn_play.clicked.connect(self._toggle_play)
        t_row.addWidget(self.btn_play)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.valueChanged.connect(self._scrub)
        t_row.addWidget(self.slider)

        self.lbl_clock = QLabel("t = 0.00")
        t_row.addWidget(self.lbl_clock)
        cl.addLayout(t_row)

        exp_row = QHBoxLayout()
        btn_scene = QPushButton("Export Current Scene MP4")
        btn_scene.clicked.connect(self._export_scene)
        btn_full = QPushButton("Export Full Movie MP4")
        btn_full.clicked.connect(self._export_all)
        exp_row.addWidget(btn_scene)
        exp_row.addWidget(btn_full)
        cl.addLayout(exp_row)

        self.prog = QProgressBar()
        cl.addWidget(self.prog)
        split.addWidget(center)

        # RIGHT: camera, composition and character controls
        right = QWidget()
        rl = QVBoxLayout(right)

        cam_box = QGroupBox("CAMERA & COMPOSITION")
        cf = QFormLayout(cam_box)
        self.combo_grammar = QComboBox()
        self.combo_grammar.addItems(["LONELY_EXT", "CONFINED_INT", "DYNAMIC_ACTION"])
        self.combo_grammar.currentTextChanged.connect(self._change_grammar)
        cf.addRow("Shot Grammar:", self.combo_grammar)

        self.combo_panel = QComboBox()
        self.combo_panel.addItems(["FULL", "HARUKI_DREAM", "SPLIT_DUEL", "TRIPLE_BEBOP", "GUIDE_HUD"])
        self.combo_panel.currentTextChanged.connect(self._change_panel_mode)
        cf.addRow("Panel / Mood:", self.combo_panel)

        self.combo_bg = QComboBox()
        self.combo_bg.addItems(["DESERT", "KITCHEN", "CANYON", "COCKPIT", "SPACE_GVR", "ROOF", "BRUCILLAS", "PANTRY", "SKIFF", "TRASH", "BALLROOM", "FOYER", "CARGO"])
        self.combo_bg.currentTextChanged.connect(self._change_bg_style)
        cf.addRow("Set Location:", self.combo_bg)
        rl.addWidget(cam_box)

        rig_box = QGroupBox("CHARACTER / KEYFRAME")
        rig_l = QVBoxLayout(rig_box)
        self.list_actors = QListWidget()
        self.list_actors.currentRowChanged.connect(self._select_actor)
        rig_l.addWidget(self.list_actors)

        add_row = QHBoxLayout()
        self.combo_cast = QComboBox()
        self.combo_cast.addItems(["CORA", "JULES", "PETE_1", "PETE_2", "SHERIFF", "CAPTAIN", "DEPUTY", "NORTHRIDGE", "LEO", "GUARD", "BELLE", "SKIFF", "CAT"])
        btn_add_cast = QPushButton("Add Actor")
        btn_add_cast.clicked.connect(self._add_cast_member)
        add_row.addWidget(self.combo_cast)
        add_row.addWidget(btn_add_cast)
        rig_l.addLayout(add_row)

        kf_form = QFormLayout()
        self.combo_pose = QComboBox()
        self.combo_pose.addItems(["IDLE", "AIM", "ACTION", "SHRUG", "DANCE", "FLEX", "DRINK_MILK", "KNIFE_THROW"])
        self.combo_expr = QComboBox()
        self.combo_expr.addItems(["SULTRY", "UNBOTHERED", "ANGRY", "MANIC", "HIMBO_GRIN", "DEFAULT"])
        self.spin_x = QSpinBox()
        self.spin_x.setRange(-800, 2600)
        self.spin_x.setSingleStep(25)
        self.spin_y = QSpinBox()
        self.spin_y.setRange(-800, 1800)
        self.spin_y.setSingleStep(25)
        self.spin_scale = QDoubleSpinBox()
        self.spin_scale.setRange(0.15, 4.0)
        self.spin_scale.setSingleStep(0.05)
        self.chk_flip = QCheckBox("Flip Horizontal")

        kf_form.addRow("Rig Pose:", self.combo_pose)
        kf_form.addRow("Anime Expression:", self.combo_expr)
        kf_form.addRow("Stage X:", self.spin_x)
        kf_form.addRow("Stage Y:", self.spin_y)
        kf_form.addRow("Scale:", self.spin_scale)
        kf_form.addRow("", self.chk_flip)
        rig_l.addLayout(kf_form)

        btn_apply_kf = QPushButton("Set Keyframe")
        btn_apply_kf.clicked.connect(self._apply_keyframe)
        rig_l.addWidget(btn_apply_kf)
        rl.addWidget(rig_box)

        vfx_box = QGroupBox("EFFECTS")
        vl = QHBoxLayout(vfx_box)
        self.combo_vfx = QComboBox()
        self.combo_vfx.addItems(["MURAKAMI_MOONS", "LASER_BOLT", "PLASMA_BLAST", "GUIDE_POPUP"])
        btn_vfx = QPushButton("Add Effect")
        btn_vfx.clicked.connect(self._add_vfx)
        vl.addWidget(self.combo_vfx)
        vl.addWidget(btn_vfx)
        rl.addWidget(vfx_box)

        split.addWidget(right)
        split.setSizes([390, 960, 340])

    def _refresh_beat_list(self):
        self.list_beats.clear()
        for b in self.beats:
            tag = f"[{b.camera_grammar[:3]}]"
            self.list_beats.addItem(f"#{b.beat_id:03d} {tag} {b.speaker}: {b.text[:38]}...")

    def _select_beat(self, idx: int):
        if not (0 <= idx < len(self.beats)):
            return
        self.cur_beat_idx = idx
        b = self.beats[idx]

        self.combo_grammar.blockSignals(True)
        self.combo_grammar.setCurrentText(b.camera_grammar)
        self.combo_grammar.blockSignals(False)

        self.combo_panel.blockSignals(True)
        self.combo_panel.setCurrentText(b.panel_mode)
        self.combo_panel.blockSignals(False)

        self.combo_bg.blockSignals(True)
        self.combo_bg.setCurrentText(b.bg_style)
        self.combo_bg.blockSignals(False)

        self.list_actors.clear()
        for tr in b.tracks:
            self.list_actors.addItem(f"{tr.actor_id} ({tr.char_type})")
        if b.tracks:
            self.list_actors.setCurrentRow(0)
        self._render_preview()

    def _select_actor(self, row: int):
        b = self.beats[self.cur_beat_idx]
        if 0 <= row < len(b.tracks):
            kf = CinematicCompositor._interp(b.tracks[row].keyframes, self.cur_t)
            self.combo_pose.setCurrentText(kf.pose)
            self.combo_expr.setCurrentText(kf.expression)
            self.spin_x.setValue(int(kf.x))
            self.spin_y.setValue(int(kf.y))
            self.spin_scale.setValue(float(kf.scale))
            self.chk_flip.setChecked(kf.flip_x)

    def _apply_keyframe(self):
        b = self.beats[self.cur_beat_idx]
        row = self.list_actors.currentRow()
        if 0 <= row < len(b.tracks):
            tr = b.tracks[row]
            tr.keyframes = [k for k in tr.keyframes if abs(k.time_norm - self.cur_t) > 1e-6]
            tr.keyframes.append(Keyframe(
                time_norm=round(self.cur_t, 6),
                x=float(self.spin_x.value()),
                y=float(self.spin_y.value()),
                scale=float(self.spin_scale.value()),
                rotation=0.0,
                alpha=1.0,
                pose=self.combo_pose.currentText(),
                expression=self.combo_expr.currentText(),
                flip_x=self.chk_flip.isChecked()
            ))
            tr.keyframes.sort(key=lambda k: k.time_norm)
            self._render_preview()

    def _change_grammar(self, grammar: str):
        b = self.beats[self.cur_beat_idx]
        b.camera_grammar = grammar
        target_scale = 0.38 if grammar == "LONELY_EXT" else (1.18 if grammar == "CONFINED_INT" else 1.24)
        for tr in b.tracks:
            for kf in tr.keyframes:
                kf.scale = target_scale
        self._select_actor(self.list_actors.currentRow())
        self._render_preview()

    def _add_cast_member(self):
        b = self.beats[self.cur_beat_idx]
        ctype = self.combo_cast.currentText()
        sc = 0.38 if b.camera_grammar == "LONELY_EXT" else 1.15
        b.tracks.append(ActorTrack(
            actor_id=ctype,
            char_type=ctype,
            z_index=len(b.tracks) + 1,
            keyframes=[Keyframe(0.0, 960, 580, sc, 0.0, 1.0, "IDLE", "SULTRY"), Keyframe(1.0, 985, 580, sc, 0.0, 1.0, "IDLE", "SULTRY")]
        ))
        self._select_beat(self.cur_beat_idx)

    def _add_vfx(self):
        b = self.beats[self.cur_beat_idx]
        vt = self.combo_vfx.currentText()
        label = "" if vt != "GUIDE_POPUP" else "NAVIGATION / SYSTEM NOTICE"
        b.vfx.append(VFXEvent(vt, 0.05, 0.95, 520, 480, 1420, 480, label))
        self._render_preview()

    def _change_panel_mode(self, mode: str):
        self.beats[self.cur_beat_idx].panel_mode = mode
        self._render_preview()

    def _change_bg_style(self, style: str):
        self.beats[self.cur_beat_idx].bg_style = style
        self._render_preview()

    def _scrub(self, val: int):
        self.cur_t = val / 100.0
        self.lbl_clock.setText(f"t = {self.cur_t:.2f}")
        self._select_actor(self.list_actors.currentRow())
        self._render_preview()

    def _toggle_play(self):
        if self.playing:
            self.timer.stop()
            self.playing = False
            self.btn_play.setText("Play")
        else:
            self.playing = True
            self.btn_play.setText("Pause")
            self.timer.start(42)

    def _tick(self):
        b = self.beats[self.cur_beat_idx]
        step = max(1, int((0.042 / max(0.5, b.duration)) * 100))
        nxt = self.slider.value() + step
        if nxt > 100:
            if self.cur_beat_idx + 1 < len(self.beats):
                self.list_beats.setCurrentRow(self.cur_beat_idx + 1)
                self.slider.setValue(0)
            else:
                self._toggle_play()
                self.slider.setValue(0)
        else:
            self.slider.setValue(nxt)

    def _render_preview(self):
        b = self.beats[self.cur_beat_idx]
        frame = self.compositor.render_frame(b, self.cur_t)
        prev = frame.resize((960, 540), Image.Resampling.LANCZOS).convert("RGBA")
        qimg = QImage(prev.tobytes("raw", "RGBA"), 960, 540, QImage.Format_RGBA8888)
        self.lbl_view.setPixmap(QPixmap.fromImage(qimg))

    def _export_scene(self):
        heading = self.beats[self.cur_beat_idx].scene_heading
        sub = [b for b in self.beats if b.scene_heading == heading]
        out = re.sub(r"[^A-Za-z0-9_]+", "_", heading).strip("_") + ".mp4"
        StudioExporter.export_mp4(
            sub, self.compositor, self.audio_eng, out, fps=24,
            progress_cb=lambda p, m: (self.prog.setValue(p), QApplication.processEvents())
        )
        QMessageBox.information(self, "Export Complete", f"Saved scene to:\n{os.path.abspath(out)}")

    def _export_all(self):
        out = "Belle_Overdrive_Animatic.mp4"
        StudioExporter.export_mp4(
            self.beats, self.compositor, self.audio_eng, out, fps=24,
            progress_cb=lambda p, m: (self.prog.setValue(p), QApplication.processEvents())
        )
        QMessageBox.information(self, "Export Complete", f"Saved full animatic to:\n{os.path.abspath(out)}")


def validate_current_project(self):
    errors, warnings = validate_project(self.beats)
    report = [f"Beats: {len(self.beats)}", f"Errors: {len(errors)}", f"Warnings: {len(warnings)}"]
    if errors:
        report.extend(["\nERRORS:"] + errors[:20])
    if warnings:
        report.extend(["\nWARNINGS:"] + warnings[:20])
    if not errors and not warnings:
        report.append("\nProject is structurally healthy.")
    QMessageBox.information(self, "Project Validation Report", "\n".join(report))

BelleDirectorWorkstation._validate_current_project = validate_current_project


# ============================================================================
# 9. EDITOR HISTORY, AUTOSAVE AND BACKGROUND RENDERING
# ============================================================================

class RenderWorker(QThread):
    progress = pyqtSignal(int, str)
    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, beats, compositor, audio_engine, output_path, fps=24, parent=None):
        super().__init__(parent)
        self.beats = copy.deepcopy(beats)
        self.compositor = compositor
        self.audio_engine = audio_engine
        self.output_path = output_path
        self.fps = fps
        self._cancel = __import__("threading").Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        try:
            result = StudioExporter.export_mp4(
                self.beats, self.compositor, self.audio_engine, self.output_path,
                fps=self.fps, progress_cb=lambda value, message: self.progress.emit(value, message),
                cancel_event=self._cancel)
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


def _project_snapshot(self):
    payload = serialize_project(self.beats)
    payload.pop("saved_at", None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _push_history(self):
    if not hasattr(self, "_undo_stack"):
        self._undo_stack, self._redo_stack = [], []
    snapshot = _project_snapshot(self)
    if self._undo_stack and self._undo_stack[-1] == snapshot:
        return
    self._undo_stack.append(snapshot)
    self._undo_stack = self._undo_stack[-100:]
    self._redo_stack.clear()
    self._dirty = True


def _restore_snapshot(self, snapshot):
    data = json.loads(snapshot)
    self.beats = deserialize_project(data)
    self.cur_beat_idx = min(self.cur_beat_idx, max(0, len(self.beats) - 1))
    self.cur_t = float(np.clip(self.cur_t, 0.0, 1.0))
    self.slider.blockSignals(True)
    self.slider.setValue(int(self.cur_t * 100))
    self.slider.blockSignals(False)
    self._refresh_beat_list()
    if self.beats:
        self.list_beats.setCurrentRow(self.cur_beat_idx)
    self._dirty = True


def _undo(self):
    if not self._undo_stack:
        return
    self._redo_stack.append(_project_snapshot(self))
    snapshot = self._undo_stack.pop()
    _restore_snapshot(self, snapshot)


def _redo(self):
    if not self._redo_stack:
        return
    self._undo_stack.append(_project_snapshot(self))
    snapshot = self._redo_stack.pop()
    _restore_snapshot(self, snapshot)


def _start_render(self, beats, suggested_name):
    if getattr(self, "_render_worker", None) is not None and self._render_worker.isRunning():
        QMessageBox.warning(self, "Render in progress", "Wait for the current render to finish.")
        return
    output, _ = QFileDialog.getSaveFileName(self, "Export video", suggested_name, "MP4 Video (*.mp4)")
    if not output:
        return
    self.prog.setValue(0)
    self._render_worker = RenderWorker(beats, self.compositor, self.audio_eng, output, fps=24, parent=self)
    self._render_worker.progress.connect(self._on_render_progress)
    self._render_worker.succeeded.connect(self._on_render_succeeded)
    self._render_worker.failed.connect(self._on_render_failed)
    self._render_worker.start()
    self.btn_play.setEnabled(False)


def _on_render_progress(self, value, message):
    self.prog.setValue(value)
    self.statusBar().showMessage(message)


def _on_render_succeeded(self, path):
    self.btn_play.setEnabled(True)
    self.statusBar().showMessage(f"Export complete: {path}", 10000)
    QMessageBox.information(self, "Export complete", f"Saved video to:\n{path}")


def _on_render_failed(self, message):
    self.btn_play.setEnabled(True)
    self.statusBar().showMessage("Export failed", 10000)
    QMessageBox.critical(self, "Export failed", message)


def _autosave(self):
    if not getattr(self, "_dirty", False) or not self.beats:
        return
    base = self.project_path or str(Path.home() / "Belle_Overdrive_Autosave.belle")
    autosave_path = base + ".autosave"
    if save_belle_project(self, autosave_path, quiet=True):
        self.statusBar().showMessage(f"Autosaved {datetime.now().strftime('%H:%M:%S')}", 4000)


def _import_sprite(self):
    path, _ = QFileDialog.getOpenFileName(self, "Import character sprite", "",
                                          "Images (*.png *.webp *.tif *.tiff)")
    if not path:
        return
    row = self.list_actors.currentRow()
    if not (0 <= row < len(self.beats[self.cur_beat_idx].tracks)):
        QMessageBox.warning(self, "Select actor", "Select a staged actor before importing a sprite.")
        return
    try:
        sprite = Image.open(path).convert("RGBA")
        if sprite.width < 16 or sprite.height < 16:
            raise ValueError("Sprite dimensions are too small.")
        actor_type = self.beats[self.cur_beat_idx].tracks[row].char_type
        self.art.custom_sprites[actor_type] = sprite
        self.art.custom_sprite_paths[actor_type] = path
        self._dirty = True
        self.statusBar().showMessage(f"Sprite assigned to {actor_type}: {Path(path).name}", 8000)
        self._render_preview()
    except Exception as exc:
        QMessageBox.critical(self, "Sprite import failed", str(exc))


_original_workstation_init_gui = BelleDirectorWorkstation._init_gui

def _production_init_gui(self):
    _original_workstation_init_gui(self)
    from PyQt5.QtWidgets import QAction
    self.project_path = None
    self._dirty = False
    self._undo_stack, self._redo_stack = [], []
    project_menu = self.menuBar().addMenu("Project")
    save_action = QAction("Save Project", self)
    save_action.setShortcut("Ctrl+S")
    save_action.triggered.connect(lambda checked=False: save_belle_project(self))
    load_action = QAction("Open Project", self)
    load_action.setShortcut("Ctrl+O")
    load_action.triggered.connect(lambda checked=False: load_belle_project(self))
    validate_action = QAction("Validate", self)
    validate_action.triggered.connect(lambda checked=False: self._validate_current_project())
    project_menu.addActions([save_action, load_action, validate_action])

    edit_menu = self.menuBar().addMenu("Edit")
    undo_action = QAction("Undo", self)
    undo_action.setShortcut("Ctrl+Z")
    undo_action.triggered.connect(self._undo)
    redo_action = QAction("Redo", self)
    redo_action.setShortcut("Ctrl+Shift+Z")
    redo_action.triggered.connect(self._redo)
    edit_menu.addActions([undo_action, redo_action])

    sprite_action = QAction("Import Selected Actor Sprite…", self)
    sprite_action.triggered.connect(self._import_sprite)
    assets_menu = self.menuBar().addMenu("Assets")
    assets_menu.addAction(sprite_action)

    self._autosave_timer = QTimer(self)
    self._autosave_timer.timeout.connect(self._autosave)
    self._autosave_timer.start(120000)


BelleDirectorWorkstation._init_gui = _production_init_gui
BelleDirectorWorkstation._push_history = _push_history
BelleDirectorWorkstation._undo = _undo
BelleDirectorWorkstation._redo = _redo
BelleDirectorWorkstation._import_sprite = _import_sprite
BelleDirectorWorkstation._on_render_progress = _on_render_progress
BelleDirectorWorkstation._on_render_succeeded = _on_render_succeeded
BelleDirectorWorkstation._on_render_failed = _on_render_failed
BelleDirectorWorkstation._autosave = _autosave
BelleDirectorWorkstation._start_render = _start_render
BelleDirectorWorkstation._persist_sprite_assets = _persist_sprite_assets
BelleDirectorWorkstation._restore_sprite_assets = _restore_sprite_assets

# Record editor changes before mutating the project.
_original_apply_keyframe = BelleDirectorWorkstation._apply_keyframe
_original_change_grammar = BelleDirectorWorkstation._change_grammar
_original_add_cast = BelleDirectorWorkstation._add_cast_member
_original_add_vfx = BelleDirectorWorkstation._add_vfx
_original_change_panel = BelleDirectorWorkstation._change_panel_mode
_original_change_bg = BelleDirectorWorkstation._change_bg_style

def _tracked(method):
    def wrapper(self, *args, **kwargs):
        self._push_history()
        return method(self, *args, **kwargs)
    return wrapper

BelleDirectorWorkstation._apply_keyframe = _tracked(_original_apply_keyframe)
BelleDirectorWorkstation._change_grammar = _tracked(_original_change_grammar)
BelleDirectorWorkstation._add_cast_member = _tracked(_original_add_cast)
BelleDirectorWorkstation._add_vfx = _tracked(_original_add_vfx)
BelleDirectorWorkstation._change_panel_mode = _tracked(_original_change_panel)
BelleDirectorWorkstation._change_bg_style = _tracked(_original_change_bg)

# Export operations run off the GUI thread; the interface remains responsive.
def _export_scene_async(self):
    heading = self.beats[self.cur_beat_idx].scene_heading
    subset = [beat for beat in self.beats if beat.scene_heading == heading]
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", heading).strip("_") or "scene"
    self._start_render(subset, safe_name + ".mp4")

def _export_all_async(self):
    self._start_render(self.beats, "Belle_Overdrive_Animatic.mp4")

BelleDirectorWorkstation._export_scene = _export_scene_async
BelleDirectorWorkstation._export_all = _export_all_async

def _close_workstation(self, event):
    worker = getattr(self, "_render_worker", None)
    if worker is not None and worker.isRunning():
        worker.cancel()
        if not worker.wait(5000):
            self.statusBar().showMessage("Render cancellation is still in progress. Close again when it finishes.")
            event.ignore()
            return
    if getattr(self, "_dirty", False):
        # Keep shutdown deterministic; the two-minute autosave remains available
        # if the user chooses not to write a named project.
        self._autosave()
    event.accept()

BelleDirectorWorkstation.closeEvent = _close_workstation


# Frame-based timeline. The slider now maps to actual output frames rather than 101
# arbitrary percentage positions.
_original_select_beat_precise = BelleDirectorWorkstation._select_beat

def _timeline_frame_count(self):
    beat = self.beats[self.cur_beat_idx]
    return max(1, int(round(float(beat.duration) * self.fps)) - 1)

def _select_beat_frame_accurate(self, idx):
    if not (0 <= idx < len(self.beats)):
        return
    self.cur_beat_idx = idx
    self.cur_t = 0.0
    max_frame = max(1, int(round(float(self.beats[idx].duration) * self.fps)) - 1)
    self.slider.blockSignals(True)
    self.slider.setRange(0, max_frame)
    self.slider.setValue(0)
    self.slider.blockSignals(False)
    _original_select_beat_precise(self, idx)
    self.lbl_clock.setText(f"00:00.000 / {self.beats[idx].duration:06.3f}s")

def _scrub_frame_accurate(self, frame_index):
    max_frame = max(1, self.slider.maximum())
    frame_index = int(np.clip(frame_index, 0, max_frame))
    self.cur_t = frame_index / max_frame
    seconds = frame_index / float(self.fps)
    self.lbl_clock.setText(f"{seconds:06.3f}s / {self.beats[self.cur_beat_idx].duration:06.3f}s")
    self._select_actor(self.list_actors.currentRow())
    self._render_preview()

def _tick_frame_accurate(self):
    next_frame = self.slider.value() + 1
    if next_frame > self.slider.maximum():
        if self.cur_beat_idx + 1 < len(self.beats):
            self.list_beats.setCurrentRow(self.cur_beat_idx + 1)
        else:
            self.timer.stop()
            self.playing = False
            self.btn_play.setText("Play")
            self.slider.setValue(0)
    else:
        self.slider.setValue(next_frame)

_original_init_workstation = BelleDirectorWorkstation.__init__
def _init_workstation_with_fps(self, pdf_path="Belle Overdrive.pdf"):
    self.fps = 24
    _original_init_workstation(self, pdf_path)

BelleDirectorWorkstation.__init__ = _init_workstation_with_fps
BelleDirectorWorkstation._select_beat = _select_beat_frame_accurate
BelleDirectorWorkstation._scrub = _scrub_frame_accurate
BelleDirectorWorkstation._tick = _tick_frame_accurate


if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    win = BelleDirectorWorkstation("Belle Overdrive.pdf")
    win.show()
    sys.exit(app.exec_())