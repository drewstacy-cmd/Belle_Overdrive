"""
belle_integrations_gui.py -- menus and background jobs for belle_integrations.py.

Imported lazily by belle_integrations.install() while the main window is being built,
so the non-GUI layer stays importable (and testable) without PyQt5.
"""
from __future__ import annotations

import copy
import logging
import os
import re
import sys
import threading
import time
import webbrowser
from pathlib import Path

from PyQt5.QtCore import QThread, QTimer, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QAction, QFileDialog, QInputDialog, QMessageBox

import belle_integrations as bi

LOGGER = logging.getLogger("belle_overdrive.integrations.gui")

ART_FILTER = "Art files (*.png *.webp *.tif *.tiff *.psd *.svg *.ai *.pdf *.kra *.ora);;All files (*)"


class Job(QThread):
    progress = pyqtSignal(int, str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel_event = threading.Event()

    def run(self):
        try:
            self.done.emit(self.fn(lambda v, m: self.progress.emit(int(v), str(m)), self.cancel_event))
        except InterruptedError:
            self.failed.emit("Cancelled.")
        except Exception as exc:  # reported to the user, full trace to the log
            LOGGER.exception("Background job failed")
            self.failed.emit(str(exc))


def _start(ws, fn, on_done, title="Working"):
    job = Job(fn, ws)
    ws._bi_jobs.append(job)

    def finish():
        if job in ws._bi_jobs:
            ws._bi_jobs.remove(job)

    job.progress.connect(lambda v, m: (ws.prog.setValue(v), ws.statusBar().showMessage(m)))
    job.done.connect(lambda result: (finish(), on_done(result)))
    job.failed.connect(lambda msg: (finish(), QMessageBox.critical(ws, title, msg)))
    job.start()
    return job


def _selected_actor(ws):
    beat = ws.beats[ws.cur_beat_idx]
    row = ws.list_actors.currentRow()
    if not (0 <= row < len(beat.tracks)):
        QMessageBox.warning(ws, "Select actor", "Select a staged actor first.")
        return None
    return beat.tracks[row]


def _combo_items(combo):
    return [combo.itemText(i) for i in range(combo.count())]


def _ask_char_pose(ws):
    chars = _combo_items(ws.combo_cast)
    track = ws.beats[ws.cur_beat_idx].tracks[ws.list_actors.currentRow()] if ws.list_actors.currentRow() >= 0 else None
    start = chars.index(track.char_type) if track and track.char_type in chars else 0
    char, ok = QInputDialog.getItem(ws, "Actor", "Character:", chars, start, False)
    if not ok:
        return None
    poses = ["ANY"] + _combo_items(ws.combo_pose) + ["TALK"]
    pose, ok = QInputDialog.getItem(ws, "Pose", "Use this clip for pose (ANY = every pose):", poses, 0, False)
    return (char, pose) if ok else None


def _done_refresh(ws, ctx, message):
    ctx.sprites.scan()
    ctx.backgrounds.scan()
    ws._render_preview()
    QMessageBox.information(ws, "Done", message)


# ---------------------------------------------------------------- round trip

def edit_actor(ws, ctx, tool):
    track = _selected_actor(ws)
    if track is None:
        return
    if not ctx.tools.locate(tool):
        QMessageBox.warning(ws, "Tool not found",
                            f"{bi.TOOL_SPECS[tool][0]} was not found. Use Assets > Integration Status to set its path.")
        return
    char = track.char_type
    path = ctx.root / "roundtrip" / f"{char}.png"
    if not (ws.art.custom_sprite_paths.get(char) == str(path) and path.exists()):
        kf = ws._bi_ns["CinematicCompositor"]._interp(track.keyframes, ws.cur_t)
        ws.art.render_character_rig(char, kf.pose, kf.expression, False, ws.cur_t).save(path)
    ctx.watch[str(path)] = (os.stat(path).st_mtime_ns, char)
    ctx.tools.launch(tool, [str(path)])
    ws.statusBar().showMessage(f"Editing {char} in {bi.TOOL_SPECS[tool][0]}. Save the file and the preview updates.", 12000)


def edit_background(ws, ctx, tool):
    if not ctx.tools.locate(tool):
        QMessageBox.warning(ws, "Tool not found", f"{bi.TOOL_SPECS[tool][0]} was not found.")
        return
    style = ws.beats[ws.cur_beat_idx].bg_style
    target = ctx.root / "backgrounds" / f"{style}.png"
    if not target.exists():
        ws.art._procedural_background(ws.art, style, "LONELY_EXT", 0.5).convert("RGBA").save(target)
        ctx.backgrounds.scan()
    ctx.tools.launch(tool, [str(target)])
    ws.statusBar().showMessage(f"Editing {style} background. Save to update the preview.", 12000)


def import_sprite_any(ws, ctx):
    path, _ = QFileDialog.getOpenFileName(ws, "Import actor art", "", ART_FILTER)
    if not path:
        return
    track = _selected_actor(ws)
    if track is None:
        return
    try:
        img = bi.fit_sprite(bi.load_any_image(path, tools=ctx.tools), trim=True, max_h=720)
        if img.width < 16 or img.height < 16:
            raise ValueError("Sprite dimensions are too small.")
        ws.art.custom_sprites[track.char_type] = img
        ws.art.custom_sprite_paths[track.char_type] = path
        ws._dirty = True
        ws.statusBar().showMessage(f"Sprite assigned to {track.char_type}: {Path(path).name}", 8000)
        ws._render_preview()
    except Exception as exc:
        QMessageBox.critical(ws, "Sprite import failed", str(exc))


def poll(ws, ctx):
    changed = False
    for path, (stamp, char) in list(ctx.watch.items()):
        try:
            st = os.stat(path).st_mtime_ns
        except OSError:
            continue
        if st != stamp and time.time() - st / 1e9 > 0.6:  # let the editor finish writing
            ctx.watch[path] = (st, char)
            try:
                ws.art.custom_sprites[char] = bi.fit_sprite(bi.load_any_image(path, tools=ctx.tools), trim=False, max_h=None)
                ws.art.custom_sprite_paths[char] = path
                ws._dirty = True
                changed = True
                ws.statusBar().showMessage(f"Reloaded {char} from {Path(path).name}", 5000)
            except Exception as exc:
                LOGGER.warning("Could not reload %s: %s", path, exc)
    if ctx.sprites.refresh_if_changed() | ctx.backgrounds.refresh_if_changed():
        changed = True
    if changed:
        ws._render_preview()


# ---------------------------------------------------------------- packages

def _fps(ws):
    return int(getattr(ws, "fps", 24))


def ae_package(ws, ctx, ns):
    folder = QFileDialog.getExistingDirectory(ws, "Choose a folder for the After Effects package")
    if not folder:
        return
    target = Path(folder) / "Belle_Overdrive_AE"
    beats, fps = copy.deepcopy(ws.beats), _fps(ws)

    def work(progress, cancel):
        return bi.export_after_effects_package(ctx, beats, ws.compositor, ws.audio_eng, target, fps, progress, cancel,
                                               ns.get("validate_project"))

    def done(jsx):
        ask = QMessageBox.question(ws, "After Effects package ready",
                                   f"Written to:\n{target}\n\nOpen After Effects now? Then run:\nFile > Scripts > Run Script File > belle_import.jsx")
        if ask == QMessageBox.Yes:
            if sys.platform.startswith("win") and ctx.tools.locate("after_effects"):
                ctx.tools.launch("after_effects", [], ["-r", jsx])
            else:
                ctx.tools.launch("after_effects", [])

    _start(ws, work, done, "After Effects package failed")


def premiere_package(ws, ctx, ns):
    folder = QFileDialog.getExistingDirectory(ws, "Choose a folder for the Premiere Pro package")
    if not folder:
        return
    target = Path(folder) / "Belle_Overdrive_Premiere"
    beats, fps = copy.deepcopy(ws.beats), _fps(ws)

    def work(progress, cancel):
        return bi.export_premiere_package(ctx, beats, ws.compositor, ws.audio_eng, ns["StudioExporter"], target, fps,
                                          progress, cancel)

    _start(ws, work, lambda xml: QMessageBox.information(
        ws, "Premiere Pro package ready",
        f"Written to:\n{target}\n\nIn Premiere: File > Import > belle_sequence.xml (cut per beat), then import "
        "captions.srt and markers.csv."), "Premiere package failed")


def audition_mixdown(ws, ctx):
    if not ctx.tools.locate("audition"):
        QMessageBox.warning(ws, "Tool not found", "Adobe Audition was not found.")
        return
    beats, fps = copy.deepcopy(ws.beats), _fps(ws)
    out = str(ctx.root / "roundtrip" / "mixdown.wav")
    _start(ws, lambda p, c: bi.mixdown(beats, ctx.audio, ws.audio_eng, fps, out),
           lambda path: ctx.tools.launch("audition", [path]) if path else QMessageBox.information(
               ws, "No audio", "This project has no sound cues to mix."), "Mixdown failed")


# ---------------------------------------------------------------- free tools

def clip_from_tool(ws, ctx, kind):
    filters = {"blender": "3D / Blender (*.blend *.fbx *.glb *.gltf)",
               "other": "Synfig / Pencil2D / video (*.sif *.sifz *.pclx *.mov *.webm *.gif *.mp4 *.apng)"}
    src, _ = QFileDialog.getOpenFileName(ws, "Choose source", "", filters[kind])
    if not src:
        return
    choice = _ask_char_pose(ws)
    if not choice:
        return
    fps, ok = QInputDialog.getInt(ws, "Clip frame rate", "Frames per second:", 12, 1, 60)
    if not ok:
        return
    end = 0
    if kind == "blender":
        end, ok = QInputDialog.getInt(ws, "Frame range", "Last frame (0 = use the file's own range):", 0, 0, 100000)
        if not ok:
            return
    char, pose = choice
    _start(ws, lambda p, c: bi.render_clip(ctx, src, char, pose, fps, 1 if end else 0, end),
           lambda msg: _done_refresh(ws, ctx, msg), "Clip render failed")
    ws.statusBar().showMessage("Rendering clip in the background…")


def clip_from_folder(ws, ctx):
    folder = QFileDialog.getExistingDirectory(ws, "Folder of PNG/WebP frames")
    if not folder:
        return
    choice = _ask_char_pose(ws)
    if not choice:
        return
    fps, ok = QInputDialog.getInt(ws, "Clip frame rate", "Frames per second:", 12, 1, 60)
    if ok:
        try:
            _done_refresh(ws, ctx, bi.import_frame_folder(ctx, folder, choice[0], choice[1], fps))
        except Exception as exc:
            QMessageBox.critical(ws, "Import failed", str(exc))


# ---------------------------------------------------------------- fonts

def choose_font(ws, ctx):
    def scan(progress, cancel):
        return bi.scan_fonts([ctx.root / "fonts"])

    def pick(fonts):
        if not fonts:
            QMessageBox.information(
                ws, "No fonts found",
                f"No fonts were found. Activate fonts in Creative Cloud (Adobe Fonts) or drop .ttf/.otf files into:\n"
                f"{ctx.root / 'fonts'}\n(or name them title.ttf / body.ttf / serif.ttf).")
            return
        role, ok = QInputDialog.getItem(ws, "Font role", "Apply to:", ["title", "body", "serif"], 0, False)
        if not ok:
            return
        names = list(fonts)[:600]
        name, ok = QInputDialog.getItem(ws, "Font", f"{len(fonts)} fonts found:", names, 0, False)
        if ok:
            ctx.settings.setdefault("fonts", {})[role] = fonts[name]
            bi.save_settings(ctx.settings)
            ws.compositor.__init__(ws.art)
            ws._render_preview()

    _start(ws, scan, pick, "Font scan failed")
    ws.statusBar().showMessage("Scanning fonts…")


# ---------------------------------------------------------------- openverse

def _clear_stem(folder: Path, stem: str):
    if folder.is_dir():
        for f in folder.iterdir():
            if f.stem.upper() == stem.upper():
                f.unlink(missing_ok=True)


def openverse(ws, ctx):
    query, ok = QInputDialog.getText(ws, "Openverse", "Search CC0 / public-domain / CC-BY media for:")
    if not ok or not query.strip():
        return
    kind, ok = QInputDialog.getItem(ws, "Openverse", "Type:", ["images", "audio"], 0, False)
    if not ok:
        return

    def found(results):
        if not results:
            QMessageBox.information(ws, "Openverse", "No results (anonymous access is rate-limited; try again later).")
            return
        labels = [f'{r.get("title") or "Untitled"} - {r.get("creator") or "unknown"} [{r.get("license")}]' for r in results]
        label, ok = QInputDialog.getItem(ws, "Openverse results", "Choose an asset:", labels, 0, False)
        if not ok:
            return
        item = results[labels.index(label)]
        beat = ws.beats[ws.cur_beat_idx]
        row = ws.list_actors.currentRow()
        char = beat.tracks[row].char_type if 0 <= row < len(beat.tracks) else None
        if kind == "images":
            targets = [f"Background for {beat.bg_style}"] + ([f"Actor sprite for {char}"] if char else []) + ["Downloads only"]
        else:
            targets = list(bi.SFX_NAMES) + ["Music bed (score)"]
        target, ok = QInputDialog.getItem(ws, "Use as", "Use this asset as:", targets, 0, False)
        if not ok:
            return
        url = item.get("url") or ""
        ext = Path(url.split("?")[0]).suffix.lower() or (".png" if kind == "images" else ".mp3")
        if target.startswith("Background"):
            folder, stem = ctx.root / "backgrounds", beat.bg_style
        elif target.startswith("Actor"):
            folder, stem = ctx.root / "downloads", f"{char}_{item.get('id', 'asset')[:8]}"
        elif target.startswith("Downloads"):
            folder, stem = ctx.root / "downloads", re.sub(r"[^A-Za-z0-9_-]+", "_", item.get("title") or "asset")[:40]
        elif target.startswith("Music"):
            folder, stem = ctx.root / "music", "score"
        else:
            folder, stem = ctx.root / "sfx", target
        dest = folder / f"{stem}{ext}"

        def fetch(progress, cancel):
            if folder.name in ("backgrounds", "sfx", "music"):
                _clear_stem(folder, stem)
            bi.download_asset(url, dest)
            ctx.add_credit({"file": str(dest.relative_to(ctx.root)), "title": item.get("title"),
                            "creator": item.get("creator"), "license": f'{item.get("license")} {item.get("license_version") or ""}'.strip(),
                            "license_url": item.get("license_url"), "source_url": item.get("foreign_landing_url") or url})
            return str(dest)

        def downloaded(path):
            if target.startswith("Actor"):
                try:
                    img = bi.fit_sprite(bi.load_any_image(path, tools=ctx.tools), trim=True, max_h=720)
                    ws.art.custom_sprites[char] = img
                    ws.art.custom_sprite_paths[char] = path
                    ws._dirty = True
                except Exception as exc:
                    QMessageBox.warning(ws, "Sprite", f"Downloaded, but could not use it as a sprite: {exc}")
            _done_refresh(ws, ctx, f"Saved {path}\nAttribution recorded in CREDITS.json (included in exports).")

        _start(ws, fetch, downloaded, "Download failed")

    _start(ws, lambda p, c: bi.openverse_search(query.strip(), kind), found, "Openverse search failed")
    ws.statusBar().showMessage("Searching Openverse…")


# ---------------------------------------------------------------- status

def integration_status(ws, ctx):
    lines = []
    for key, label, path in ctx.tools.status():
        lines.append(f"{'FOUND  ' if path else 'missing'}  {label}: {path or '-'}")
    lines += ["", f"Asset library: {ctx.root}", f"Settings file: {bi.settings_path()}",
              f"FFmpeg export: {'on' if ctx.settings.get('use_ffmpeg', True) else 'off'}"]
    box = QMessageBox(ws)
    box.setWindowTitle("Integration status")
    box.setText("\n".join(lines))
    set_btn = box.addButton("Set a tool path…", QMessageBox.ActionRole)
    box.addButton(QMessageBox.Close)
    box.exec_()
    if box.clickedButton() is set_btn:
        keys = list(bi.TOOL_SPECS)
        key, ok = QInputDialog.getItem(ws, "Tool", "Which tool?", keys, 0, False)
        if ok:
            path, _ = QFileDialog.getOpenFileName(ws, f"Locate {bi.TOOL_SPECS[key][0]}")
            if path:
                ctx.settings.setdefault("paths", {})[key] = path
                bi.save_settings(ctx.settings)
                ctx.tools._cache.pop(key, None)


# ---------------------------------------------------------------- menus

def build_menus(ws, ns, ctx):
    ws._bi, ws._bi_ns, ws._bi_jobs = ctx, ns, []
    bar = ws.menuBar()

    def safe(fn):
        def run(checked=False):
            try:
                fn()
            except Exception as exc:
                LOGGER.exception("Integration action failed")
                QMessageBox.critical(ws, "Action failed", str(exc))
        return run

    def add(menu, text, fn):
        action = QAction(text, ws)
        action.triggered.connect(safe(fn))
        menu.addAction(action)

    adobe = bar.addMenu("Adobe")
    add(adobe, "Edit Selected Actor in Photoshop…", lambda: edit_actor(ws, ctx, "photoshop"))
    add(adobe, "Edit Current Background in Photoshop…", lambda: edit_background(ws, ctx, "photoshop"))
    add(adobe, "Import Illustrator / PSD / SVG / PDF as Actor Sprite…", lambda: import_sprite_any(ws, ctx))
    adobe.addSeparator()
    add(adobe, "Send Animatic to After Effects…", lambda: ae_package(ws, ctx, ns))
    add(adobe, "Export Premiere Pro Package…", lambda: premiere_package(ws, ctx, ns))
    add(adobe, "Open Audio Mixdown in Audition…", lambda: audition_mixdown(ws, ctx))
    adobe.addSeparator()
    add(adobe, "Choose Adobe Fonts / Custom Fonts…", lambda: choose_font(ws, ctx))

    free = bar.addMenu("Free Tools")
    for key in ("krita", "gimp"):
        label = bi.TOOL_SPECS[key][0]
        add(free, f"Edit Selected Actor in {label}…", lambda k=key: edit_actor(ws, ctx, k))
        add(free, f"Edit Current Background in {label}…", lambda k=key: edit_background(ws, ctx, k))
    free.addSeparator()
    add(free, "Render Blender / Mixamo FBX / glTF → Actor Clip…", lambda: clip_from_tool(ws, ctx, "blender"))
    add(free, "Render Synfig / Pencil2D / Video / GIF → Actor Clip…", lambda: clip_from_tool(ws, ctx, "other"))
    add(free, "Import Frame Folder as Actor Clip…", lambda: clip_from_folder(ws, ctx))

    assets = None
    for act in bar.actions():
        if act.text().replace("&", "") == "Assets" and act.menu() is not None:
            assets = act.menu()
    assets = assets or bar.addMenu("Assets")
    assets.addSeparator()
    add(assets, "Open Asset Library Folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(ctx.root))))
    add(assets, "Rescan Asset Library", lambda: (ctx.sprites.scan(), ctx.backgrounds.scan(), ws._render_preview()))
    add(assets, "Search Openverse (CC0 / CC-BY)…", lambda: openverse(ws, ctx))
    sources = assets.addMenu("Free Asset Sources (opens browser)")
    for label, url in bi.FREE_SOURCES:
        add(sources, label, lambda u=url: webbrowser.open(u))
    assets.addSeparator()
    add(assets, "Integration Status…", lambda: integration_status(ws, ctx))

    ws._bi_timer = QTimer(ws)
    ws._bi_timer.timeout.connect(lambda: poll(ws, ctx))
    ws._bi_timer.start(1500)
