# Changelog

## 1.0.0 — 2026-10-10

First public release.

### New
- Cut out the middle of a clip: split at the playhead (`S`), then hover a piece
  and click × (or press `Delete`) to drop it, and + to bring it back. Splits can
  be dragged, or double-clicked to remove them. Playback skips cut pieces, and the
  export joins the rest frame-accurately.
- Snapping: trim handles, splits and the playhead snap to each other, to the
  clip's ends and to whole seconds when zoomed in. Hold `Shift` to drag freely.
- Redesigned interface: light and dark appearances that follow the system
  (`--light` / `--dark` to force one), traffic-light window controls, a centred
  document title, segmented controls, an on/off switch for audio.
- Photos-style crop overlay: corner brackets, rule-of-thirds grid, and a live
  `W × H` readout while dragging. Edges are grabbable along their full length.
- QuickTime-style yellow trim frame on the filmstrip, plus frame-step buttons.
- Empty state with a drop target and a *Choose File…* button.
- *Show in Folder* link after an export.
- New app icon (`assets/nanosnip.svg`).
- `--version` and `--help`.
- `pyproject.toml`, so `pipx install .` works on any distro.
- Test suite for timecode formatting and ffmpeg command building.

### Fixed
- Crop position and size fields showed up empty with Qt 6.12.
- Lossless trims to MP4 and MOV started at the keyframe before the trim point,
  which could add several seconds of unwanted footage at the start.
- Timecodes could read `00:60.000` instead of `01:00.000` when rounding.
- Resizing the frameless window from its edges now works everywhere, not only
  where no widget covered the border.
- Sidebar no longer clips on the right; labels no longer sit on grey boxes.
- Pop-up menus now show their chevron.

### Changed
- `install.sh` installs from the folder it lives in. It no longer searches
  `~/Downloads` or `~/Desktop`, and no longer deletes files there.
- JetBrains Mono is no longer needed; timecodes use Inter's tabular figures.
