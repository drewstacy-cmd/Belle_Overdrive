# Belle Overdrive Director Workstation

A revised, single-file Python/PyQt5 animatic and scene-authoring application. The existing screenplay parser, procedural scene construction, character tracks, VFX, preview compositor, audio generation and MP4 export remain in place. This revision adds production-oriented editing, persistence and render workflows.

## Requirements

- Python 3.10 or newer
- Install Python dependencies:

```bash
python -m pip install -r requirements.txt
```

- FFmpeg is recommended for H.264/AAC output through MoviePy. OpenCV's `mp4v` writer remains the fallback video path.
- PyMuPDF reads `Belle Overdrive.pdf` when present. If it is not installed or the PDF is not found, the application uses the embedded sample screenplay.

## Run

Place `Belle Overdrive.pdf` beside `Belle_Overdrive_Director.py` if you want the parser to use the PDF, then run:

```bash
python Belle_Overdrive_Director.py
```

## What changed

### Rendering and presentation
- Replaced the default high-saturation palette with a restrained slate, oxblood, brass, petrol-blue and desaturated-cyan grade.
- Reduced oversized decorative elements and removed the cartoon face from the flower motif, turning it into a restrained graphic medallion.
- Reworked the frame overlay into a compact scene slug and subtitle panel instead of large arcade-style UI chrome.
- Added alpha compositing for actor tracks and eased interpolation between keyframes.
- Added deterministic starfield generation without mutating NumPy's global random state.
- Added an import path for custom transparent character sprites. Imported sprites are copied into a project-relative asset directory when a project is saved.

### Editing and project management
- Added frame-based timeline scrubbing at the project's 24 FPS output rate rather than a 101-position percentage slider.
- Increased keyframe timestamp precision and removed the old 5%-of-scene deletion tolerance.
- Added undo/redo history for keyframe, staging, camera-grammar, panel and background edits.
- Added two-minute autosave for dirty projects.
- Project writes are atomic. Project format version 2 remains able to load version 1 project files.
- Validation now checks duration, keyframe values and VFX timing for invalid numeric values.

### Audio and export
- Rebuilt the WAV cache around content-addressed filenames, so changed synthesis parameters cannot silently reuse stale audio.
- Made procedural effects deterministic, which makes renders reproducible.
- Added EVA puffer and freeze effects to the synthesizer.
- Added output-path and frame-rate checks, writer initialization checks, cancellation checks and cleanup handling.
- Moved video rendering to a QThread worker so the interface remains responsive during export.
- Added compatibility handling for MoviePy 1.x and 2.x audio methods. If muxing fails, the rendered video is retained without audio rather than discarded.

## Compatibility notes

- Existing scene, panel, character, pose and effect identifiers are retained to preserve older project data.
- Version 1 `.belle` / JSON projects remain readable. New saves use format version 2.
- Existing procedural character rigs remain available. For a genuinely higher-detail character finish, import production PNG/WebP sprites with transparent backgrounds through **Assets → Import Selected Actor Sprite**. The original geometric rigs are still the fallback when no custom sprite is assigned.
- Imported sprites are stored beside the project in a `<project-name>_assets` directory and referenced by relative path. Keep that directory with the `.belle` project when moving or sharing it.

## Verification performed

- Python syntax compilation passed.
- Screenplay parser smoke test passed: 16 embedded beats loaded.
- Project validation passed on the embedded screenplay.
- Version 2 serialization round-trip passed; version 1 compatibility load passed.
- Keyframe interpolation test passed.
- Deterministic WAV generation and cache reuse test passed.
- 1920×1080 frame-render smoke test passed.
- Sprite asset save/restore smoke test passed.
- MP4 export smoke test passed.

The build environment used for these checks did not have PyQt5 installed, so the GUI itself could not be launched here. The source compiles, and the non-GUI rendering, audio, project and export paths were exercised independently. Install the listed requirements before launching the workstation.
