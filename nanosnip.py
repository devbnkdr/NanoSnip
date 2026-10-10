#!/usr/bin/env python3
"""
NanoSnip — mirror, trim, crop. Nothing else.

A deliberately small editor for photos and video:
  * mirror horizontally / vertically
  * trim and cut (video): split at the playhead, drop the pieces you don't want
  * crop
  * export, keeping audio

Requires: python, pyside6, qt6-multimedia-ffmpeg, ffmpeg
"""

from __future__ import annotations

import atexit
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QRectF,
    QSize,
    QSizeF,
    Qt,
    QThread,
    QTimer,
    QUrl,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QTransform,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QGraphicsVideoItem

    HAS_QTMM = True
except ImportError:  # pragma: no cover - depends on local Qt build
    HAS_QTMM = False


APP_NAME = "NanoSnip"
__version__ = "1.0.0"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".avif"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".wmv", ".flv", ".mpg",
              ".mpeg", ".ts", ".m2ts", ".gif"}

USAGE = f"""\
{APP_NAME} {__version__} — mirror, trim, crop. Nothing else.

usage: nanosnip [FILE] [--light | --dark] [--native-frame]

  FILE            photo or video to open
  --light         force the light appearance
  --dark          force the dark appearance
  --native-frame  use the desktop's title bar instead of NanoSnip's own
  --version       print the version and exit
"""


# ================================================================ theme ======
@dataclass(frozen=True)
class Theme:
    dark: bool
    window: str          # header, sidebar, transport
    canvas: str          # the well the media sits in
    control: str         # buttons and fields at rest
    control_hover: str
    control_press: str
    segment: str         # segmented-control track, switch off-state
    segment_hover: str
    thumb: str           # the selected segment
    thumb_border: str
    separator: str       # hairlines
    field_border: str
    popup: str
    text: str
    secondary: str
    tertiary: str
    accent: str
    accent_hover: str
    accent_press: str
    trim: str            # the yellow trim frame, as in QuickTime and Photos
    danger: str
    success: str
    lights_idle: str     # window buttons when the window is in the background
    veil: int            # alpha of the shade over trimmed-away frames


LIGHT = Theme(
    dark=False,
    window="#FAFAFC", canvas="#E8E8ED",
    control="#FFFFFF", control_hover="#F2F2F5", control_press="#E8E8ED",
    segment="#EBEBF0", segment_hover="#E1E1E7", thumb="#FFFFFF", thumb_border="#D4D4DA",
    separator="#E3E3E8", field_border="#D9D9DE", popup="#FFFFFF",
    text="#1D1D1F", secondary="#6E6E73", tertiary="#A1A1A6",
    accent="#0071E3", accent_hover="#0A7CF0", accent_press="#0062C4",
    trim="#FFC800", danger="#E0352B", success="#248A3D",
    lights_idle="#DCDCE0", veil=110,
)

DARK = Theme(
    dark=True,
    window="#1E1E20", canvas="#0F0F11",
    control="#2C2C2F", control_hover="#36363A", control_press="#414145",
    segment="#2A2A2D", segment_hover="#333337", thumb="#5A5A5F", thumb_border="#5A5A5F",
    separator="#2E2E32", field_border="#3A3A3E", popup="#2A2A2D",
    text="#F5F5F7", secondary="#A1A1A6", tertiary="#6C6C71",
    accent="#0A84FF", accent_hover="#2491FF", accent_press="#0071E3",
    trim="#FFD60A", danger="#FF453A", success="#32D74B",
    lights_idle="#4A4A4E", veil=150,
)

T: Theme = DARK  # the active theme; painted widgets read it at paint time

GRIP = 6  # frameless resize margin, px
SANS_STACK = ["SF Pro Text", "Inter", "Inter Variable", "Helvetica Neue", "Noto Sans",
              "Cantarell"]


def tabular(font: QFont) -> QFont:
    """Same-width digits, so timecodes and pixel counts don't jitter."""
    try:
        font.setFeature(QFont.Tag.fromString("tnum"), 1)
    except (AttributeError, TypeError):  # Qt < 6.7
        pass
    return font


def mix(a: QColor, b: QColor, k: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * k),
        round(a.green() + (b.green() - a.green()) * k),
        round(a.blue() + (b.blue() - a.blue()) * k),
    )


# ================================================================== probing ==
def which_or_die(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(name)
    return path


@dataclass
class MediaInfo:
    path: Path
    is_video: bool
    width: int
    height: int
    duration: float = 0.0
    has_audio: bool = False
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""

    @property
    def name(self) -> str:
        return self.path.name


def probe(path: Path) -> MediaInfo:
    """Read dimensions/duration with ffprobe, falling back to QImage."""
    ext = path.suffix.lower()
    looks_video = ext in VIDEO_EXTS

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            raw = subprocess.run(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_format", "-show_streams", str(path)],
                capture_output=True, text=True, timeout=30, check=False,
            ).stdout
            data = json.loads(raw) if raw.strip() else {}
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            data = {}

        streams = data.get("streams", [])
        vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
        astream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        if vstream:
            width = int(vstream.get("width") or 0)
            height = int(vstream.get("height") or 0)
            duration = 0.0
            for src in (vstream.get("duration"), data.get("format", {}).get("duration")):
                try:
                    duration = float(src)
                    break
                except (TypeError, ValueError):
                    continue
            fps = 0.0
            rate = vstream.get("avg_frame_rate") or vstream.get("r_frame_rate") or "0/1"
            try:
                num, _, den = rate.partition("/")
                fps = float(num) / float(den) if float(den) else 0.0
            except (ValueError, ZeroDivisionError):
                fps = 0.0

            # A still image probes as a 1-frame video stream with no duration.
            is_video = looks_video and duration > 0.05

            # Honour rotation metadata so the crop box matches what you see.
            rotation = 0
            for sd in vstream.get("side_data_list", []) or []:
                if "rotation" in sd:
                    try:
                        rotation = int(float(sd["rotation"]))
                    except (TypeError, ValueError):
                        rotation = 0
            if abs(rotation) % 180 == 90:
                width, height = height, width

            if width and height:
                return MediaInfo(
                    path=path, is_video=is_video, width=width, height=height,
                    duration=max(duration, 0.0), has_audio=astream is not None,
                    fps=fps, vcodec=vstream.get("codec_name", ""),
                    acodec=(astream or {}).get("codec_name", ""),
                )

    img = QImage(str(path))
    if img.isNull():
        raise ValueError(f"Cannot read {path.name}")
    return MediaInfo(path=path, is_video=False, width=img.width(), height=img.height())


ENCODERS = [
    # ffmpeg id, menu label, one-line note shown under the menu
    ("h264_nvenc", "H.264 · NVIDIA", "Encoded on the GPU. Fastest, plays everywhere."),
    ("hevc_nvenc", "HEVC · NVIDIA", "Encoded on the GPU. Smaller files."),
    ("libx264", "H.264", "Plays everywhere."),
    ("libx265", "HEVC", "Smaller files, slower to encode."),
    ("libvpx-vp9", "VP9 · WebM", "Open format for the web."),
]
ENCODER_NOTES = {eid: note for eid, _, note in ENCODERS}


def available_encoders() -> list[tuple[str, str]]:
    """Return [(ffmpeg encoder id, label)] present in this ffmpeg build."""
    found = ""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            found = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                                   capture_output=True, text=True, timeout=20,
                                   check=False).stdout
        except (subprocess.SubprocessError, OSError):
            found = ""

    out = [(eid, label) for eid, label, _ in ENCODERS if f" {eid} " in found]
    return out or [("libx264", "H.264")]


CODEC_NAMES = {"h264": "H.264", "hevc": "HEVC", "vp9": "VP9", "vp8": "VP8", "av1": "AV1",
               "aac": "AAC", "opus": "Opus", "mp3": "MP3", "prores": "ProRes",
               "mpeg4": "MPEG-4", "flac": "FLAC", "vorbis": "Vorbis", "gif": "GIF"}


def codec_name(raw: str) -> str:
    return CODEC_NAMES.get(raw, raw.upper())


def fmt_time(seconds: float, with_ms: bool = True) -> str:
    total_ms = round(max(0.0, seconds) * 1000)
    total_s, ms = divmod(total_ms, 1000)
    hours, rem = divmod(total_s, 3600)
    minutes, secs = divmod(rem, 60)
    base = f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
    return f"{base}.{ms:03d}" if with_ms else base


MIN_SEGMENT = 0.1  # s: the shortest piece a split can leave behind


def kept_segments(bounds: list[float], removed: list[bool]) -> list[tuple[float, float]]:
    """The pieces that survive the cuts, with neighbouring kept pieces joined up.

    bounds holds the edges of every piece (start, splits…, end) and removed has one flag per piece.
    """
    out: list[tuple[float, float]] = []
    for (a, b), gone in zip(pairwise(bounds), removed):
        if gone or b - a <= 0:
            continue
        if out and abs(out[-1][1] - a) < 1e-6:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


def snap_time(t: float, targets, tolerance: float) -> float | None:
    """The target closest to t, if one lies within tolerance."""
    best = None
    for c in targets:
        if abs(c - t) <= tolerance and (best is None or abs(c - t) < abs(best - t)):
            best = c
    return best


# ================================================================== preview ==
MIN_CROP = 24.0
FIT_MARGIN = 28  # px of canvas around the media


class PreviewView(QGraphicsView):
    """Scene coordinates == source pixels, so the crop box is literal."""

    cropChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setBackgroundBrush(QColor(T.canvas))
        self.setMouseTracking(True)

        self.info: MediaInfo | None = None
        self.item = None
        self.crop: QRectF | None = None
        self.ratio: float | None = None
        self.hflip = False
        self.vflip = False
        self.overlay: QWidget | None = None  # the empty state, kept the size of the view

        self._drag: str | None = None
        self._grab_offset = QPointF()
        self._origin: QPointF | None = None

    # -- content -------------------------------------------------------------
    def clear_media(self) -> None:
        self._scene.clear()
        self.item = None
        self.info = None
        self.crop = None

    def set_item(self, item, info: MediaInfo) -> None:
        self._scene.clear()
        self.info = info
        self.item = item
        self._scene.addItem(item)
        self._scene.setSceneRect(0, 0, info.width, info.height)
        self.crop = None
        self.apply_mirror()
        self.fit()
        self.cropChanged.emit()

    def show_pixmap(self, pix: QPixmap) -> None:
        """Swap the displayed frame without resetting crop/mirror state."""
        if isinstance(self.item, QGraphicsPixmapItem) and self.info:
            self.item.setPixmap(pix)
            self.apply_mirror()

    def fit(self) -> None:
        """Fit the media with a margin of canvas around it, so it floats in the well."""
        if not self.info:
            return
        vp = self.viewport()
        room_w = max(1, vp.width() - FIT_MARGIN * 2)
        room_h = max(1, vp.height() - FIT_MARGIN * 2)
        s = min(room_w / self.info.width, room_h / self.info.height)
        self.setTransform(QTransform.fromScale(s, s))
        self.centerOn(self.info.width / 2, self.info.height / 2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.overlay:
            self.overlay.setGeometry(self.rect())
        self.fit()

    # -- mirror --------------------------------------------------------------
    def set_mirror(self, horizontal: bool, vertical: bool) -> None:
        self.hflip, self.vflip = horizontal, vertical
        self.apply_mirror()

    def apply_mirror(self) -> None:
        if not (self.item and self.info):
            return
        cx, cy = self.info.width / 2.0, self.info.height / 2.0
        t = QTransform()
        t.translate(cx, cy)
        t.scale(-1 if self.hflip else 1, -1 if self.vflip else 1)
        t.translate(-cx, -cy)
        self.item.setTransform(t)

    # -- crop ----------------------------------------------------------------
    def set_crop(self, rect: QRectF | None) -> None:
        self.crop = self._clamp(rect) if rect else None
        self.viewport().update()
        self.cropChanged.emit()

    def set_ratio(self, ratio: float | None) -> None:
        self.ratio = ratio
        if ratio and self.crop:
            r = QRectF(self.crop)
            r.setHeight(r.width() / ratio)
            self.set_crop(r)

    def select_all(self) -> None:
        if not self.info:
            return
        rect = QRectF(0, 0, self.info.width, self.info.height)
        if self.ratio:
            if rect.width() / rect.height() > self.ratio:
                w = rect.height() * self.ratio
                rect = QRectF((self.info.width - w) / 2, 0, w, rect.height())
            else:
                h = rect.width() / self.ratio
                rect = QRectF(0, (self.info.height - h) / 2, rect.width(), h)
        self.set_crop(rect)

    def _clamp(self, rect: QRectF) -> QRectF:
        if not self.info:
            return rect
        bounds = QRectF(0, 0, self.info.width, self.info.height)
        r = rect.normalized()
        r.setWidth(max(MIN_CROP, min(r.width(), bounds.width())))
        r.setHeight(max(MIN_CROP, min(r.height(), bounds.height())))
        x = min(max(r.x(), 0.0), bounds.width() - r.width())
        y = min(max(r.y(), 0.0), bounds.height() - r.height())
        return QRectF(x, y, r.width(), r.height())

    def _scene_px(self) -> float:
        """One viewport pixel expressed in scene units."""
        m = self.transform().m11()
        return 1.0 / m if m else 1.0

    def drawForeground(self, painter: QPainter, _rect: QRectF) -> None:
        if not (self.info and self.crop):
            return
        # Work in viewport pixels so line weights stay constant at any zoom.
        vt = self.viewportTransform()
        full = vt.mapRect(QRectF(0, 0, self.info.width, self.info.height))
        r = vt.mapRect(self.crop)
        painter.save()
        painter.resetTransform()
        painter.setRenderHint(QPainter.Antialiasing)

        # Shade everything outside the box.
        shade = QPainterPath()
        shade.setFillRule(Qt.OddEvenFill)
        shade.addRect(full)
        shade.addRect(r)
        painter.fillPath(shade, QColor(0, 0, 0, 135))

        # Rule-of-thirds grid, stronger while the box is being moved.
        painter.setPen(QPen(QColor(255, 255, 255, 110 if self._drag else 55), 1))
        for i in (1, 2):
            x = r.left() + r.width() * i / 3
            y = r.top() + r.height() * i / 3
            painter.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            painter.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        painter.setPen(QPen(QColor(255, 255, 255, 230), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))

        # Photos-style corner brackets and edge bars.
        t, o = 3.0, 2.0
        ln = min(22.0, r.width() / 3, r.height() / 3)
        bars = [
            QRectF(r.left() - o, r.top() - o, ln, t), QRectF(r.left() - o, r.top() - o, t, ln),
            QRectF(r.right() + o - ln, r.top() - o, ln, t),
            QRectF(r.right() + o - t, r.top() - o, t, ln),
            QRectF(r.left() - o, r.bottom() + o - t, ln, t),
            QRectF(r.left() - o, r.bottom() + o - ln, t, ln),
            QRectF(r.right() + o - ln, r.bottom() + o - t, ln, t),
            QRectF(r.right() + o - t, r.bottom() + o - ln, t, ln),
        ]
        mid = 18.0
        if r.width() > ln * 2 + mid + 8:
            bars += [QRectF(r.center().x() - mid / 2, r.top() - o, mid, t),
                     QRectF(r.center().x() - mid / 2, r.bottom() + o - t, mid, t)]
        if r.height() > ln * 2 + mid + 8:
            bars += [QRectF(r.left() - o, r.center().y() - mid / 2, t, mid),
                     QRectF(r.right() + o - t, r.center().y() - mid / 2, t, mid)]
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 70))
        for b in bars:
            painter.drawRoundedRect(b.translated(0, 1), 1.2, 1.2)
        painter.setBrush(QColor("#FFFFFF"))
        for b in bars:
            painter.drawRoundedRect(b, 1.2, 1.2)

        # Live size readout while dragging.
        if self._drag:
            crop = self.crop
            text = f"{round(crop.width())} × {round(crop.height())}"
            f = tabular(QFont(self.font()))
            f.setPixelSize(12)
            f.setWeight(QFont.DemiBold)
            painter.setFont(f)
            w = QFontMetrics(f).horizontalAdvance(text) + 20
            pill = QRectF(r.center().x() - w / 2, r.bottom() - 36, w, 24)
            if r.height() < 60:
                pill.moveTop(r.bottom() + 10)
            painter.setBrush(QColor(0, 0, 0, 165))
            painter.drawRoundedRect(pill, 12, 12)
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(pill, Qt.AlignCenter, text)
        painter.restore()

    # -- interaction ---------------------------------------------------------
    def _hit(self, pos: QPointF) -> str | None:
        if not self.crop:
            return None
        r = self.crop
        px = self._scene_px()
        tol, corner = px * 10, px * 24
        if not (r.left() - tol <= pos.x() <= r.right() + tol
                and r.top() - tol <= pos.y() <= r.bottom() + tol):
            return None
        dl, dr = abs(pos.x() - r.left()), abs(pos.x() - r.right())
        dt, db = abs(pos.y() - r.top()), abs(pos.y() - r.bottom())
        vert = ("t" if dt <= db else "b") if min(dt, db) <= tol else ""
        horz = ("l" if dl <= dr else "r") if min(dl, dr) <= tol else ""
        # The brackets are long, so treat their whole length as the corner.
        if vert and not horz and min(dl, dr) <= corner:
            horz = "l" if dl <= dr else "r"
        if horz and not vert and min(dt, db) <= corner:
            vert = "t" if dt <= db else "b"
        if vert or horz:
            return vert + horz
        return "move" if r.contains(pos) else None

    def mousePressEvent(self, event) -> None:
        if not self.info or event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        pos = self.mapToScene(event.position().toPoint())
        hit = self._hit(pos)
        if hit == "move":
            self._drag = "move"
            self._grab_offset = pos - self.crop.topLeft()
        elif hit:
            self._drag = hit
        else:
            self._drag = "new"
            self._origin = pos
            self.set_crop(QRectF(pos, QSizeF(MIN_CROP, MIN_CROP)))
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        pos = self.mapToScene(event.position().toPoint())
        if not self._drag:
            hit = self._hit(pos) if self.crop else None
            cursors = {
                "tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor,
                "tr": Qt.SizeBDiagCursor, "bl": Qt.SizeBDiagCursor,
                "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
                "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor,
                "move": Qt.SizeAllCursor,
            }
            self.setCursor(cursors.get(hit, Qt.CrossCursor))
            return super().mouseMoveEvent(event)

        if self._drag == "move":
            self.set_crop(QRectF(pos - self._grab_offset, self.crop.size()))
        elif self._drag == "new":
            rect = QRectF(self._origin, pos).normalized()
            if self.ratio and rect.width() > 1:
                rect.setHeight(rect.width() / self.ratio)
            self.set_crop(rect)
        else:
            self.set_crop(self._resize(QRectF(self.crop), self._drag, pos))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._drag = None
        self._origin = None
        self.viewport().update()
        super().mouseReleaseEvent(event)

    def _resize(self, r: QRectF, handle: str, pos: QPointF) -> QRectF:
        if "l" in handle:
            r.setLeft(min(pos.x(), r.right() - MIN_CROP))
        if "r" in handle:
            r.setRight(max(pos.x(), r.left() + MIN_CROP))
        if "t" in handle:
            r.setTop(min(pos.y(), r.bottom() - MIN_CROP))
        if "b" in handle:
            r.setBottom(max(pos.y(), r.top() + MIN_CROP))

        if self.ratio:
            if handle in ("l", "r"):
                h = r.width() / self.ratio
                cy = r.center().y()
                r.setTop(cy - h / 2)
                r.setHeight(h)
            elif handle in ("t", "b"):
                w = r.height() * self.ratio
                cx = r.center().x()
                r.setLeft(cx - w / 2)
                r.setWidth(w)
            else:
                h = r.width() / self.ratio
                if "t" in handle:
                    r.setTop(r.bottom() - h)
                else:
                    r.setHeight(h)
        return r


# ================================================================ filmstrip ==
class ThumbWorker(QThread):
    """Pulls evenly spaced frames out of a video, one at a time, in the background."""

    ready = Signal(int, QImage)

    def __init__(self, path: Path, duration: float, count: int, height: int = 76) -> None:
        super().__init__()
        self.path = path
        self.duration = max(duration, 0.01)
        self.count = max(1, count)
        self.height = height
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return
        # Middle first, then outwards — the useful frames appear soonest.
        order = sorted(range(self.count), key=lambda i: abs(i - self.count / 2))
        for i in order:
            if self._stop:
                return
            t = self.duration * (i + 0.5) / self.count
            try:
                data = subprocess.run(
                    [ffmpeg, "-v", "quiet", "-noaccurate_seek", "-ss", f"{t:.3f}",
                     "-i", str(self.path), "-frames:v", "1",
                     "-vf", f"scale=-2:{self.height}", "-f", "image2", "-c:v", "png", "-"],
                    capture_output=True, timeout=20, check=False,
                ).stdout
            except (subprocess.SubprocessError, OSError):
                continue
            if self._stop or not data:
                continue
            img = QImage()
            if img.loadFromData(data, "PNG") and not img.isNull():
                self.ready.emit(i, img)


class Filmstrip(QWidget):
    """A grab-and-drag timeline: thumbnails, two end handles, split points, snapping."""

    rangeChanged = Signal(float, float)
    cutsChanged = Signal()
    seeked = Signal(float)
    scrubbing = Signal(bool)

    PAD = 18
    STRIP_H = 56
    HANDLE_W = 12
    BORDER = 3.0
    TOP = 20
    BADGE = 20     # remove / restore button on a piece, px
    SNAP_PX = 7    # how close a drag has to come to a target before it snaps

    def __init__(self) -> None:
        super().__init__()
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.playhead = 0.0
        # Split points inside [start, end], sorted, and one removed-flag per piece.
        self.splits: list[float] = []
        self.removed: list[bool] = [False]

        self.path: Path | None = None
        self.thumbs: list[QPixmap | None] = []
        self.count = 0
        self._worker: ThumbWorker | None = None

        self._drag: str | None = None
        self._grab_dt = 0.0
        self._hover: str | None = None
        self._hover_piece: int | None = None
        self._anchor = 0.0               # playhead when a drag began: a snap target
        self._snap_x: float | None = None

        self._rebuild = QTimer(self)
        self._rebuild.setSingleShot(True)
        self._rebuild.setInterval(220)
        self._rebuild.timeout.connect(self._start_thumbs)

        self.setFixedHeight(self.TOP + self.STRIP_H + 26)
        self.setMouseTracking(True)
        self.setEnabled(False)

    # -- state ---------------------------------------------------------------
    def set_source(self, path: Path | None, duration: float) -> None:
        self._stop_thumbs()
        self.path = path
        self.duration = max(duration, 0.0)
        self.start, self.end, self.playhead = 0.0, self.duration, 0.0
        self.splits, self.removed = [], [False]
        self.thumbs = []
        self.count = 0
        self.setEnabled(bool(path) and self.duration > 0)
        if self.isEnabled():
            self._rebuild.start()
        self.update()

    def set_range(self, start: float, end: float) -> None:
        self.start = max(0.0, min(start, self.duration))
        self.end = max(self.start + 0.04, min(end, self.duration))
        # Pieces that fall outside the new range go with it.
        while self.splits and self.splits[0] < self.start + MIN_SEGMENT:
            self.splits.pop(0)
            self.removed.pop(0)
        while self.splits and self.splits[-1] > self.end - MIN_SEGMENT:
            self.splits.pop()
            self.removed.pop()
        if all(self.removed):
            self.removed = [False] * len(self.removed)
        self.update()

    def set_playhead(self, t: float) -> None:
        self.playhead = max(0.0, min(t, self.duration))
        self.update()

    # -- pieces --------------------------------------------------------------
    def bounds(self) -> list[float]:
        return [self.start, *self.splits, self.end]

    def pieces(self) -> list[tuple[float, float]]:
        return list(pairwise(self.bounds()))

    def kept(self) -> list[tuple[float, float]]:
        return kept_segments(self.bounds(), self.removed)

    def kept_duration(self) -> float:
        return sum(b - a for a, b in self.kept())

    def has_cuts(self) -> bool:
        return any(self.removed)

    def piece_at(self, t: float) -> int:
        for i, (a, b) in enumerate(self.pieces()):
            if a <= t < b:
                return i
        return len(self.removed) - 1

    def split_at(self, t: float) -> bool:
        """Split the piece under t in two. False when t is too close to an edge."""
        b = self.bounds()
        for i in range(len(b) - 1):
            if b[i] + MIN_SEGMENT <= t <= b[i + 1] - MIN_SEGMENT:
                self.splits.insert(i, t)
                self.removed.insert(i, self.removed[i])
                self.cutsChanged.emit()
                self.update()
                return True
        return False

    def remove_split(self, j: int) -> None:
        """Join the two pieces either side of split j; the result is kept if either was."""
        if 0 <= j < len(self.splits):
            self.removed[j] = self.removed[j] and self.removed[j + 1]
            del self.removed[j + 1]
            del self.splits[j]
            self.cutsChanged.emit()
            self.update()

    def toggle_piece(self, i: int) -> bool:
        """Cut piece i out, or bring it back. At least one piece always stays."""
        if not (0 <= i < len(self.removed)) or not self.splits:
            return False
        if not self.removed[i] and self.removed.count(False) <= 1:
            return False
        self.removed[i] = not self.removed[i]
        self.cutsChanged.emit()
        self.update()
        return True

    def clear_cuts(self) -> None:
        self.splits, self.removed = [], [False]
        self.cutsChanged.emit()
        self.update()

    def is_dragging(self) -> bool:
        return self._drag is not None

    def skip_target(self, t: float) -> float | None:
        """If t sits inside a cut-out piece, where playback should continue instead."""
        pieces = self.pieces()
        i = self.piece_at(t)
        if not self.removed[i] or not (pieces[i][0] <= t < pieces[i][1]):
            return None
        while i < len(pieces) and self.removed[i]:
            i += 1
        return pieces[i][0] if i < len(pieces) else None

    # -- thumbnails ----------------------------------------------------------
    def _stop_thumbs(self) -> None:
        if self._worker:
            self._worker.ready.disconnect()
            self._worker.stop()
            self._worker = None

    def _start_thumbs(self) -> None:
        if not (self.path and self.duration > 0):
            return
        self._stop_thumbs()
        strip = self._strip()
        count = max(6, min(40, int(strip.width() / 84)))
        if count == self.count and any(t is not None for t in self.thumbs):
            return
        self.count = count
        self.thumbs = [None] * count
        # Twice the strip height, so frames stay sharp on HiDPI screens.
        self._worker = ThumbWorker(self.path, self.duration, count, self.STRIP_H * 2)
        self._worker.ready.connect(self._thumb_ready)
        self._worker.start()

    def _thumb_ready(self, index: int, img: QImage) -> None:
        if 0 <= index < len(self.thumbs):
            self.thumbs[index] = QPixmap.fromImage(img)
            self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.isEnabled():
            self._rebuild.start()

    # -- geometry ------------------------------------------------------------
    def _strip(self) -> QRectF:
        return QRectF(self.PAD, self.TOP,
                      max(1.0, self.width() - self.PAD * 2), self.STRIP_H)

    def _x(self, t: float) -> float:
        s = self._strip()
        if self.duration <= 0:
            return s.left()
        return s.left() + s.width() * (t / self.duration)

    def _t(self, x: float) -> float:
        s = self._strip()
        if s.width() <= 0:
            return 0.0
        return max(0.0, min(self.duration, (x - s.left()) / s.width() * self.duration))

    def _handle_rect(self, which: str) -> QRectF:
        s = self._strip()
        x = self._x(self.start if which == "start" else self.end)
        left = x - self.HANDLE_W if which == "start" else x
        return QRectF(left, s.top(), self.HANDLE_W, s.height())

    def _badge_rect(self, i: int) -> QRectF | None:
        a, b = self.pieces()[i]
        x0, x1 = self._x(a), self._x(b)
        if x1 - x0 < self.BADGE + 8:
            return None
        s = self._strip()
        cx = (x0 + x1) / 2
        return QRectF(cx - self.BADGE / 2, s.top() + 6, self.BADGE, self.BADGE)

    def _zone(self, pos: QPointF) -> str:
        if not self.isEnabled():
            return "none"
        for which in ("start", "end"):
            if self._handle_rect(which).adjusted(-6, -4, 6, 4).contains(pos):
                return which
        s = self._strip()
        band = s.adjusted(0, -4, 0, 4)
        if band.contains(pos):
            for j, t in enumerate(self.splits):
                if abs(pos.x() - self._x(t)) <= 5:
                    return f"split:{j}"
            if self.splits:
                for i in range(len(self.removed)):
                    r = self._badge_rect(i)
                    if r is not None and r.adjusted(-3, -3, 3, 3).contains(pos):
                        return f"badge:{i}"
        sel = QRectF(self._x(self.start), s.top(),
                     self._x(self.end) - self._x(self.start), s.height())
        if sel.contains(pos) and not self.splits:
            return "move"
        return "scrub"

    def _snapped(self, t: float, skip: tuple[str, ...] = ()) -> float:
        """t pulled onto the nearest edge, split, playhead or whole second; Shift turns it off."""
        self._snap_x = None
        if self.duration <= 0 or QApplication.keyboardModifiers() & Qt.ShiftModifier:
            return t
        px_per_s = self._strip().width() / self.duration
        targets = {"zero": 0.0, "duration": self.duration, "start": self.start,
                   "end": self.end, "playhead": self._anchor}
        targets.update({f"split:{j}": v for j, v in enumerate(self.splits)})
        values = [v for k, v in targets.items() if k not in skip]
        if px_per_s >= 40:  # whole seconds, once they are far enough apart to aim at
            values += [float(int(t)), float(int(t) + 1)]
        hit = snap_time(t, values, self.SNAP_PX / px_per_s)
        if hit is None:
            return t
        self._snap_x = self._x(hit)
        return hit

    # -- painting ------------------------------------------------------------
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        strip = self._strip()
        radius = 6.0

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(T.segment))
        p.drawRoundedRect(strip, radius, radius)

        if not self.isEnabled():
            return

        x0, x1 = self._x(self.start), self._x(self.end)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(strip, radius, radius)
        p.setClipPath(clip)
        self._paint_frames(p, strip)
        # Shade what gets cut, rather than hiding it.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, T.veil))
        p.drawRect(QRectF(strip.left(), strip.top(), x0 - strip.left(), strip.height()))
        p.drawRect(QRectF(x1, strip.top(), strip.right() - x1, strip.height()))
        self._paint_cut_pieces(p, strip)
        p.restore()

        self._paint_selection(p, strip, x0, x1)
        self._paint_splits(p, strip)
        self._paint_badges(p)
        self._paint_playhead(p, strip)
        self._paint_snap(p, strip)
        self._paint_labels(p, strip, x0, x1)

    def _paint_frames(self, p: QPainter, band: QRectF) -> None:
        if not self.count:
            return
        slot_w = band.width() / self.count
        for i, pix in enumerate(self.thumbs):
            slot = QRectF(band.left() + i * slot_w, band.top(), slot_w + 0.6, band.height())
            if pix is None or pix.isNull():
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(T.segment if i % 2 else T.segment_hover))
                p.drawRect(slot)
                continue
            scale = max(slot.width() / pix.width(), slot.height() / pix.height())
            sw, sh = slot.width() / scale, slot.height() / scale
            src = QRectF((pix.width() - sw) / 2, (pix.height() - sh) / 2, sw, sh)
            p.drawPixmap(slot, pix, src)

    def _paint_cut_pieces(self, p: QPainter, strip: QRectF) -> None:
        """Cut-out pieces: the same shade as trimmed ends, plus diagonal hatching."""
        hatch = QPen(QColor(255, 255, 255, 60), 1.5)
        for (a, b), gone in zip(self.pieces(), self.removed):
            if not gone:
                continue
            r = QRectF(self._x(a), strip.top(), self._x(b) - self._x(a), strip.height())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, min(235, T.veil + 70)))
            p.drawRect(r)
            p.save()
            p.setClipRect(r)
            p.setPen(hatch)
            x = r.left() - r.height()
            while x < r.right():
                p.drawLine(QPointF(x, r.bottom()), QPointF(x + r.height(), r.top()))
                x += 9
            p.restore()

    def _paint_selection(self, p: QPainter, strip: QRectF, x0: float, x1: float) -> None:
        """The yellow frame: thick handles at the ends, thin rails along the top and bottom."""
        hw, bw = self.HANDLE_W, self.BORDER
        outer = QRectF(x0 - hw, strip.top(), (x1 - x0) + hw * 2, strip.height())
        inner = QRectF(x0, strip.top() + bw, max(0.0, x1 - x0), strip.height() - bw * 2)
        frame = QPainterPath()
        frame.addRoundedRect(outer, 6, 6)
        hole = QPainterPath()
        hole.addRoundedRect(inner, 2, 2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(T.trim))
        p.drawPath(frame.subtracted(hole))

        for which, direction in (("start", -1), ("end", 1)):
            r = self._handle_rect(which)
            active = self._drag == which or self._hover == which
            pen = QPen(QColor(0, 0, 0, 215 if active else 150), 2.0)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            cx, cy = r.center().x(), r.center().y()
            d = 1.8 * direction
            p.drawPolyline([QPointF(cx - d, cy - 5), QPointF(cx + d, cy),
                            QPointF(cx - d, cy + 5)])

    def _paint_splits(self, p: QPainter, strip: QRectF) -> None:
        """A split is a yellow rail across the strip with a grip in the middle."""
        for j, t in enumerate(self.splits):
            x = self._x(t)
            active = self._drag == f"split:{j}" or self._hover == f"split:{j}"
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T.trim))
            p.drawRect(QRectF(x - 1.25, strip.top(), 2.5, strip.height()))
            grip = QRectF(x - 4, strip.center().y() - 10, 8, 20)
            p.drawRoundedRect(grip, 3, 3)
            p.setPen(QPen(QColor(0, 0, 0, 215 if active else 140), 1.2))
            for dy in (-4, 0, 4):
                y = grip.center().y() + dy
                p.drawLine(QPointF(x - 1.8, y), QPointF(x + 1.8, y))

    def _paint_badges(self, p: QPainter) -> None:
        """Remove (×) on the piece under the mouse; restore (+) on every cut piece."""
        if not self.splits:
            return
        for i, gone in enumerate(self.removed):
            if not (gone or i == self._hover_piece):
                continue
            r = self._badge_rect(i)
            if r is None:
                continue
            hot = self._hover == f"badge:{i}"
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T.trim) if hot else QColor(0, 0, 0, 175))
            p.drawEllipse(r)
            pen = QPen(QColor(0, 0, 0) if hot else QColor("#FFFFFF"), 1.8)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            c, k = r.center(), 4.0
            if gone:   # restore
                p.drawLine(QPointF(c.x() - k, c.y()), QPointF(c.x() + k, c.y()))
                p.drawLine(QPointF(c.x(), c.y() - k), QPointF(c.x(), c.y() + k))
            else:      # remove
                k = 3.4
                p.drawLine(QPointF(c.x() - k, c.y() - k), QPointF(c.x() + k, c.y() + k))
                p.drawLine(QPointF(c.x() - k, c.y() + k), QPointF(c.x() + k, c.y() - k))

    def _paint_playhead(self, p: QPainter, strip: QRectF) -> None:
        x = self._x(self.playhead)
        top, bottom = strip.top() - 5, strip.bottom() + 5
        for colour, width in ((QColor(0, 0, 0, 80), 4.5), (QColor("#FFFFFF"), 2.5)):
            pen = QPen(colour, width)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(x, top), QPointF(x, bottom))

    def _paint_snap(self, p: QPainter, strip: QRectF) -> None:
        if self._snap_x is None or not self._drag:
            return
        pen = QPen(QColor(T.accent), 1.5, Qt.DashLine)
        p.setPen(pen)
        p.drawLine(QPointF(self._snap_x, strip.top() - 8), QPointF(self._snap_x, strip.bottom() + 8))

    def _paint_labels(self, p: QPainter, strip: QRectF, x0: float, x1: float) -> None:
        f = tabular(QFont(self.font()))
        f.setPixelSize(11)
        p.setFont(f)
        p.setPen(QColor(T.tertiary))
        p.drawText(QRectF(strip.left(), 0, 220, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   fmt_time(self.start))
        p.drawText(QRectF(strip.right() - 220, 0, 220, 14), Qt.AlignRight | Qt.AlignVCenter,
                   fmt_time(self.end))

        f.setWeight(QFont.DemiBold)
        p.setFont(f)
        p.setPen(QColor(T.secondary))
        cx = min(max((x0 + x1) / 2, strip.left() + 60), strip.right() - 60)
        text = fmt_time(self.kept_duration())
        if self.has_cuts():
            text += "  kept"
        p.drawText(QRectF(cx - 100, strip.bottom() + 8, 200, 16), Qt.AlignCenter, text)

    # -- interaction ---------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if not self.isEnabled() or event.button() != Qt.LeftButton:
            return
        pos = event.position()
        zone = self._zone(pos)
        if zone.startswith("badge:"):
            self.toggle_piece(int(zone.split(":")[1]))
            return
        self._anchor = self.playhead
        self._drag = zone
        if zone == "move":
            self._grab_dt = self._t(pos.x()) - self.start
        self.scrubbing.emit(True)
        self.update()
        self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if not self._drag:
            zone = self._zone(pos)
            piece = self.piece_at(self._t(pos.x())) if self._strip().contains(pos) else None
            if zone != self._hover or piece != self._hover_piece:
                self._hover, self._hover_piece = zone, piece
                self.update()
            self.setCursor({
                "start": Qt.SizeHorCursor, "end": Qt.SizeHorCursor,
                "move": Qt.OpenHandCursor, "scrub": Qt.PointingHandCursor,
            }.get(zone, Qt.SizeHorCursor if zone.startswith("split:")
                  else Qt.PointingHandCursor if zone.startswith("badge:") else Qt.ArrowCursor))
            return

        t = self._t(pos.x())
        first = self.splits[0] if self.splits else self.end
        last = self.splits[-1] if self.splits else self.start
        gap = MIN_SEGMENT if self.splits else 0.04
        if self._drag == "start":
            self.start = min(self._snapped(t, ("start",)), first - gap)
            self.seeked.emit(self.start)
        elif self._drag == "end":
            self.end = max(self._snapped(t, ("end",)), last + gap)
            self.seeked.emit(self.end)
        elif self._drag.startswith("split:"):
            j = int(self._drag.split(":")[1])
            b = self.bounds()   # b[j] and b[j + 2] are the neighbours of split j
            t = self._snapped(t, (f"split:{j}",))
            self.splits[j] = max(b[j] + MIN_SEGMENT, min(t, b[j + 2] - MIN_SEGMENT))
            self.seeked.emit(self.splits[j])
            self.cutsChanged.emit()
        elif self._drag == "move":
            length = self.end - self.start
            new_start = max(0.0, min(t - self._grab_dt, self.duration - length))
            self.start, self.end = new_start, new_start + length
            self.seeked.emit(self.start)
            self.setCursor(Qt.ClosedHandCursor)
        else:
            self.seeked.emit(self._snapped(t, ("playhead",)))

        if self._drag in ("start", "end", "move"):
            self.rangeChanged.emit(self.start, self.end)
        self.update()

    def mouseReleaseEvent(self, _event) -> None:
        was = self._drag
        self._drag = None
        self._snap_x = None
        if was:
            self.scrubbing.emit(False)
        self.setCursor(Qt.ArrowCursor)
        self.update()

    def leaveEvent(self, _event) -> None:
        self._hover = None
        self._hover_piece = None
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        if not self.isEnabled():
            return
        zone = self._zone(event.position())
        if zone.startswith("badge:"):        # the second click of a quick double tap
            self.toggle_piece(int(zone.split(":")[1]))
        elif zone.startswith("split:"):      # double-click a split to remove it
            self.remove_split(int(zone.split(":")[1]))
        else:                                # elsewhere: back to the full length
            self.set_range(0.0, self.duration)
            self.rangeChanged.emit(self.start, self.end)
            self.update()


# ================================================================== export ===
@dataclass
class ExportOptions:
    start: float = 0.0
    end: float = 0.0
    hflip: bool = False
    vflip: bool = False
    crop: tuple[int, int, int, int] | None = None  # w, h, x, y
    keep_audio: bool = True
    encoder: str = "libx264"
    quality: str = "balanced"
    # The kept pieces when the clip has cuts in it: [(start, end), …]. One piece or
    # None means a plain trim from start to end.
    segments: list[tuple[float, float]] | None = None
    extras: list[str] = field(default_factory=list)


QUALITY = {"high": (18, 19), "balanced": (21, 23), "small": (25, 28)}


def encoder_args(encoder: str, quality: str) -> list[str]:
    crf, cq = QUALITY.get(quality, QUALITY["balanced"])
    if encoder.endswith("_nvenc"):
        return ["-c:v", encoder, "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
    if encoder == "libvpx-vp9":
        return ["-c:v", encoder, "-crf", str(crf + 10), "-b:v", "0", "-row-mt", "1"]
    if encoder == "libx265":
        return ["-c:v", encoder, "-preset", "medium", "-crf", str(crf + 4),
                "-tag:v", "hvc1"]
    return ["-c:v", encoder, "-preset", "medium", "-crf", str(crf)]


def build_command(info: MediaInfo, opts: ExportOptions, out: Path) -> list[str]:
    cmd = [which_or_die("ffmpeg"), "-hide_banner", "-y"]
    audio = info.has_audio and opts.keep_audio
    cuts = opts.segments if opts.segments and len(opts.segments) > 1 else None

    filters = []
    if opts.hflip:
        filters.append("hflip")
    if opts.vflip:
        filters.append("vflip")
    if opts.crop:
        w, h, x, y = opts.crop
        filters.append(f"crop={w}:{h}:{x}:{y}")

    if cuts:
        # Several kept pieces: trim each one out and join them back together. Re-encoded,
        # so every cut lands on its exact frame. Reading stops after the last piece.
        if cuts[-1][1] < info.duration - 0.001:
            cmd += ["-to", f"{cuts[-1][1]:.3f}"]
        cmd += ["-i", str(info.path)]
        graph, inputs = [], []
        for i, (a, b) in enumerate(cuts):
            graph.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
            inputs.append(f"[v{i}]")
            if audio:
                graph.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
                inputs.append(f"[a{i}]")
        graph.append("".join(inputs) + f"concat=n={len(cuts)}:v=1:a={1 if audio else 0}[joined]"
                     + ("[aout]" if audio else ""))
        video_out = "[joined]"
        if filters:
            graph.append(f"[joined]{','.join(filters)}[vout]")
            video_out = "[vout]"
        cmd += ["-filter_complex", ";".join(graph), "-map", video_out]
        if audio:
            cmd += ["-map", "[aout]"]
    else:
        if opts.start > 0.001:
            cmd += ["-ss", f"{opts.start:.3f}"]
        if opts.end < info.duration - 0.001:
            cmd += ["-to", f"{opts.end:.3f}"]
        cmd += ["-i", str(info.path)]
        if filters:
            cmd += ["-vf", ",".join(filters)]

    if cuts or filters:
        cmd += encoder_args(opts.encoder, opts.quality)
        if opts.encoder != "libvpx-vp9":
            cmd += ["-pix_fmt", "yuv420p"]
        if audio:
            cmd += (["-c:a", "libopus", "-b:a", "160k"] if out.suffix.lower() == ".webm"
                    else ["-c:a", "aac", "-b:a", "192k"])
    else:
        # Trim-only: copy the streams, lossless and near-instant.
        cmd += ["-c", "copy"]
        if out.suffix.lower() not in (".mp4", ".m4v", ".mov"):
            # MP4 and MOV start playback exactly at the trim point through an edit list.
            # Other containers can't, so shift their timestamps to zero instead (playback
            # then starts at the keyframe just before the trim point).
            cmd += ["-avoid_negative_ts", "make_zero"]

    if not audio:
        cmd += ["-an"]
    if out.suffix.lower() in (".mp4", ".m4v", ".mov"):
        cmd += ["-movflags", "+faststart"]

    cmd += opts.extras
    cmd += ["-progress", "pipe:1", "-nostats", str(out)]
    return cmd


class ExportWorker(QThread):
    progress = Signal(int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, cmd: list[str], total: float) -> None:
        super().__init__()
        self.cmd = cmd
        self.total = max(total, 0.001)
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    def run(self) -> None:
        tail: list[str] = []
        try:
            self._proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except OSError as exc:
            self.failed.emit(str(exc))
            return

        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
            if m:
                done = int(m.group(1)) / 1_000_000.0
                self.progress.emit(max(0, min(99, int(done / self.total * 100))))

        code = self._proc.wait()
        if self._cancelled:
            self.failed.emit("Export cancelled.")
        elif code == 0:
            self.progress.emit(100)
            self.finished_ok.emit(self.cmd[-1])
        else:
            noise = ("frame=", "bitrate=", "out_time", "total_size", "speed=",
                     "progress=", "dup_frames", "drop_frames", "stream_")
            msg = [ln for ln in tail if not ln.startswith(noise)]
            self.failed.emit("\n".join(msg[-8:]) or f"ffmpeg exited with code {code}")

    def cancel(self) -> None:
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


# ================================================================= widgets ===
class TrafficLights(QWidget):
    """Close, minimise, zoom — drawn the way macOS draws them."""

    D, GAP = 12.0, 8.0
    COLOURS = ("#FF5F57", "#FEBC2E", "#28C840")

    def __init__(self, window: QMainWindow) -> None:
        super().__init__()
        self._win = window
        self._hover = False
        self._pressed: int | None = None
        self.setFixedSize(int(self.D * 3 + self.GAP * 2 + 4), 28)
        self.setToolTip("")

    def _centres(self) -> list[QPointF]:
        y = self.height() / 2
        return [QPointF(2 + self.D / 2 + i * (self.D + self.GAP), y) for i in range(3)]

    def _which(self, pos: QPointF) -> int | None:
        for i, c in enumerate(self._centres()):
            if abs(pos.x() - c.x()) <= self.D / 2 + 2 and abs(pos.y() - c.y()) <= self.D / 2 + 3:
                return i
        return None

    def enterEvent(self, _event) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, _event) -> None:
        self._hover = False
        self.update()

    def mousePressEvent(self, event) -> None:
        self._pressed = self._which(event.position())
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        hit = self._which(event.position())
        pressed, self._pressed = self._pressed, None
        self.update()
        if hit is not None and hit == pressed:
            (self._win.close, self._win.showMinimized, self._win._toggle_max)[hit]()

    def mouseDoubleClickEvent(self, event) -> None:
        event.accept()  # never let a double-click on the buttons maximise the window

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        lit = self._hover or self._win.isActiveWindow()
        r = self.D / 2
        for i, c in enumerate(self._centres()):
            base = QColor(self.COLOURS[i] if lit else T.lights_idle)
            if self._pressed == i:
                base = base.darker(118)
            p.setPen(QPen(base.darker(112), 0.8))
            p.setBrush(base)
            p.drawEllipse(c, r - 0.4, r - 0.4)
            if not self._hover:
                continue
            ink = QColor(0, 0, 0, 150)
            pen = QPen(ink, 1.3)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            if i == 0:
                p.drawLine(QPointF(c.x() - 2.6, c.y() - 2.6), QPointF(c.x() + 2.6, c.y() + 2.6))
                p.drawLine(QPointF(c.x() + 2.6, c.y() - 2.6), QPointF(c.x() - 2.6, c.y() + 2.6))
            elif i == 1:
                p.drawLine(QPointF(c.x() - 3.2, c.y()), QPointF(c.x() + 3.2, c.y()))
            else:
                p.setPen(Qt.NoPen)
                p.setBrush(ink)
                p.drawPolygon([QPointF(c.x() - 3, c.y() - 3), QPointF(c.x() + 1.6, c.y() - 3),
                               QPointF(c.x() - 3, c.y() + 1.6)])
                p.drawPolygon([QPointF(c.x() + 3, c.y() + 3), QPointF(c.x() - 1.6, c.y() + 3),
                               QPointF(c.x() + 3, c.y() - 1.6)])


class TitleBar(QFrame):
    """Header that also moves the window, so the app draws its own chrome."""

    def __init__(self, window: QMainWindow) -> None:
        super().__init__()
        self._win = window

    def _interactive(self, pos) -> bool:
        child = self.childAt(pos.toPoint())
        return isinstance(child, (QAbstractButton, QComboBox, TrafficLights))

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and not self._interactive(event.position()):
            handle = self._win.windowHandle()
            if handle and self._win.frameless:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._win.frameless and not self._interactive(event.position()):
            self._win._toggle_max()
        super().mouseDoubleClickEvent(event)


class EdgeGrip(QObject):
    """Resizes a frameless window from its edges.

    Watches the window's own QWindow, which sees every mouse move before the
    widgets underneath do, so the edges work even where children cover them.
    """

    CURSORS: ClassVar[dict] = {
        Qt.LeftEdge: Qt.SizeHorCursor, Qt.RightEdge: Qt.SizeHorCursor,
        Qt.TopEdge: Qt.SizeVerCursor, Qt.BottomEdge: Qt.SizeVerCursor,
        Qt.LeftEdge | Qt.TopEdge: Qt.SizeFDiagCursor,
        Qt.RightEdge | Qt.BottomEdge: Qt.SizeFDiagCursor,
        Qt.RightEdge | Qt.TopEdge: Qt.SizeBDiagCursor,
        Qt.LeftEdge | Qt.BottomEdge: Qt.SizeBDiagCursor,
    }

    def __init__(self, window: QMainWindow) -> None:
        super().__init__(window)
        self._win = window
        self._overriding = False

    def _edge(self, pos: QPointF):
        if self._win.isMaximized() or self._win.isFullScreen():
            return None
        w, h = self._win.width(), self._win.height()
        edges = Qt.Edge(0)
        if pos.x() <= GRIP:
            edges |= Qt.LeftEdge
        if pos.x() >= w - GRIP:
            edges |= Qt.RightEdge
        if pos.y() <= GRIP:
            edges |= Qt.TopEdge
        if pos.y() >= h - GRIP:
            edges |= Qt.BottomEdge
        return edges or None

    def _set_cursor(self, edge) -> None:
        if edge:
            cursor = self.CURSORS.get(edge, Qt.ArrowCursor)
            if self._overriding:
                QApplication.changeOverrideCursor(cursor)
            else:
                QApplication.setOverrideCursor(cursor)
                self._overriding = True
        elif self._overriding:
            QApplication.restoreOverrideCursor()
            self._overriding = False

    def eventFilter(self, _obj, event) -> bool:
        kind = event.type()
        if kind == QEvent.MouseMove and not event.buttons():
            self._set_cursor(self._edge(event.position()))
        elif kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            edge = self._edge(event.position())
            if edge:
                self._set_cursor(None)
                self._win.windowHandle().startSystemResize(edge)
                return True
        elif kind == QEvent.Leave:
            self._set_cursor(None)
        return False


class Section(QWidget):
    """A titled group. No box, no shadow — space and a hairline do the work."""

    def __init__(self, title: str, rule: bool = True, trailing: tuple = ()) -> None:
        super().__init__()
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 18 if rule else 0, 0, 0)
        col.setSpacing(10)

        if rule:
            line = QFrame()
            line.setObjectName("rule")
            line.setFixedHeight(1)
            col.addWidget(line)
            col.addSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(14)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        head.addWidget(label)
        head.addStretch(1)
        for w in trailing:
            head.addWidget(w)
        col.addLayout(head)

        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        col.addLayout(self.body)

    def add(self, widget) -> None:
        if isinstance(widget, QWidget):
            self.body.addWidget(widget)
        else:
            self.body.addLayout(widget)


class Segmented(QFrame):
    """An Apple-style segmented control. Exclusive (pick one) or not (any combination)."""

    def __init__(self, items: list[tuple[str, object]], exclusive: bool = True) -> None:
        super().__init__()
        self.setObjectName("segment")
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(2)
        self.group = QButtonGroup(self)
        self.group.setExclusive(exclusive)
        self.buttons: list[QPushButton] = []
        for text, key in items:
            b = QPushButton(text)
            b.setObjectName("segmentItem")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setProperty("key", key)
            self.group.addButton(b)
            row.addWidget(b, 1)
            self.buttons.append(b)

    def value(self):
        b = self.group.checkedButton()
        return b.property("key") if b else None

    def set_value(self, key) -> None:
        for b in self.buttons:
            if b.property("key") == key:
                b.setChecked(True)


class Switch(QAbstractButton):
    """An on/off switch with a sliding knob."""

    def __init__(self, checked: bool = False) -> None:
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(40, 24)
        self._pos = 1.0 if checked else 0.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._step)
        self.toggled.connect(self._animate)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def _step(self, value) -> None:
        self._pos = float(value)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(40, 24)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if not self.isEnabled():
            p.setOpacity(0.4)
        track = QRectF(1, 1, self.width() - 2, self.height() - 2)
        radius = track.height() / 2
        p.setPen(Qt.NoPen)
        off = QColor(T.thumb_border if not T.dark else T.control_press)
        p.setBrush(mix(off, QColor(T.accent), self._pos))
        p.drawRoundedRect(track, radius, radius)
        d = track.height() - 4
        x = track.left() + 2 + (track.width() - 4 - d) * self._pos
        knob = QRectF(x, track.top() + 2, d, d)
        p.setBrush(QColor(0, 0, 0, 45))
        p.drawEllipse(knob.translated(0, 0.8))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(knob)


class EmptyState(QWidget):
    """What the canvas shows before anything is open."""

    def __init__(self, on_open) -> None:
        super().__init__()
        self._hot = False
        col = QVBoxLayout(self)
        col.setAlignment(Qt.AlignCenter)
        col.setSpacing(0)

        icon = QLabel()
        icon.setPixmap(make_icon().pixmap(QSize(96, 96)))
        icon.setAlignment(Qt.AlignCenter)
        col.addWidget(icon)
        col.addSpacing(18)

        title = QLabel("Drop a photo or video")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignCenter)
        col.addWidget(title)
        col.addSpacing(6)

        body = QLabel("Mirror, trim, crop. Nothing else.")
        body.setObjectName("emptyBody")
        body.setAlignment(Qt.AlignCenter)
        col.addWidget(body)
        col.addSpacing(22)

        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("Choose File…")
        btn.setObjectName("pill")
        btn.setFixedHeight(34)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(on_open)
        row.addWidget(btn)
        row.addStretch(1)
        col.addLayout(row)
        col.addSpacing(10)

        hint = QLabel("or press Ctrl+O")
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignCenter)
        col.addWidget(hint)

    def set_hot(self, hot: bool) -> None:
        if hot != self._hot:
            self._hot = hot
            self.update()

    def paintEvent(self, _event) -> None:
        if not self._hot:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        accent = QColor(T.accent)
        fill = QColor(accent)
        fill.setAlpha(22)
        pen = QPen(accent, 2, Qt.DashLine)
        pen.setDashPattern([4, 3])
        p.setPen(pen)
        p.setBrush(fill)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(24, 24, -24, -24), 18, 18)


def glyph_icon(kind: str, colour: str | None = None, size: int = 22) -> QIcon:
    """Small vector marks drawn in code so no icon font is needed."""
    scale = 2  # draw at 2x so icons stay crisp on HiDPI screens
    pix = QPixmap(size * scale, size * scale)
    pix.setDevicePixelRatio(scale)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    ink = QColor(colour or T.text)
    m = size / 22.0
    fill_pen = QPen(ink, 1.6 * m)
    fill_pen.setJoinStyle(Qt.RoundJoin)
    line_pen = QPen(ink, 1.5 * m)
    line_pen.setCapStyle(Qt.RoundCap)
    line_pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(fill_pen)
    p.setBrush(ink)

    if kind == "play":
        p.drawPolygon([QPointF(8 * m, 5.5 * m), QPointF(17.5 * m, 11 * m),
                       QPointF(8 * m, 16.5 * m)])
    elif kind == "pause":
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(6.5 * m, 5 * m, 3.4 * m, 12 * m), 1.2 * m, 1.2 * m)
        p.drawRoundedRect(QRectF(12.1 * m, 5 * m, 3.4 * m, 12 * m), 1.2 * m, 1.2 * m)
    elif kind in ("back", "fwd"):
        if kind == "back":
            p.translate(22 * m, 0)
            p.scale(-1, 1)
        p.drawPolygon([QPointF(6.5 * m, 7 * m), QPointF(13 * m, 11 * m),
                       QPointF(6.5 * m, 15 * m)])
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(14.2 * m, 6.5 * m, 1.9 * m, 9 * m), 0.9 * m, 0.9 * m)
    elif kind in ("fliph", "flipv"):
        if kind == "flipv":
            p.translate(11 * m, 11 * m)
            p.rotate(90)
            p.translate(-11 * m, -11 * m)
        p.drawPolygon([QPointF(3.5 * m, 16 * m), QPointF(9 * m, 6 * m),
                       QPointF(9 * m, 16 * m)])
        p.setBrush(Qt.NoBrush)
        p.setPen(line_pen)
        p.drawPolygon([QPointF(18.5 * m, 16 * m), QPointF(13 * m, 6 * m),
                       QPointF(13 * m, 16 * m)])
        dash = QPen(ink, 1.2 * m, Qt.DashLine)
        dash.setDashPattern([1.5, 1.5])
        p.setPen(dash)
        p.drawLine(QPointF(11 * m, 3 * m), QPointF(11 * m, 19 * m))
    p.end()
    return QIcon(pix)


def polish_combo(box: QComboBox) -> QComboBox:
    """Rounded, padded pop-up list instead of the platform default."""
    box.setItemDelegate(QStyledItemDelegate(box))
    box.setCursor(Qt.PointingHandCursor)
    box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(6)
    popup = box.view().window()
    popup.setWindowFlags(popup.windowFlags() | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
    popup.setAttribute(Qt.WA_TranslucentBackground)
    return box


def spin(maximum: int = 99999) -> QSpinBox:
    box = QSpinBox()
    box.setRange(0, maximum)
    box.setAlignment(Qt.AlignCenter)
    box.setFont(tabular(QFont(box.font())))
    return box


# =================================================================== window ==
class MainWindow(QMainWindow):
    def __init__(self, initial: Path | None = None, frameless: bool = True) -> None:
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle(APP_NAME)
        self.resize(1340, 860)
        self.setMinimumSize(1020, 660)
        self.setAcceptDrops(True)

        self.frameless = frameless
        if frameless:
            self.setWindowFlag(Qt.FramelessWindowHint, True)
        self._grip: EdgeGrip | None = None

        self.info: MediaInfo | None = None
        self.player = None
        self.audio = None
        self.worker: ExportWorker | None = None
        self._syncing = False
        self.static_mode = False
        self._primed = False
        self._got_frame = False
        self._playing = False
        self._last_export: Path | None = None

        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.setInterval(110)
        self._frame_timer.timeout.connect(self._load_static_frame)
        self._pending_seek = 0.0

        self._build_ui()
        self._build_shortcuts()
        self._paint_icons()
        self._set_enabled(False)
        if initial:
            self.load(initial)

    # -- construction --------------------------------------------------------
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        row.addWidget(self._build_stage(), 1)
        row.addWidget(self._build_sidebar(), 0)

    def _build_stage(self) -> QWidget:
        stage = QWidget()
        stage.setObjectName("stage")
        col = QVBoxLayout(stage)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)

        col.addWidget(self._build_header())

        self.view = PreviewView()
        self.view.cropChanged.connect(self._crop_to_boxes)
        col.addWidget(self.view, 1)

        self.empty = EmptyState(self.pick_file)
        self.empty.setParent(self.view)
        self.view.overlay = self.empty

        col.addWidget(self._build_transport())
        self.transport_bar.hide()
        return stage

    def _build_header(self) -> QWidget:
        head = TitleBar(self)
        head.setObjectName("header")
        head.setFixedHeight(52)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 0, 14, 0)
        hl.setSpacing(0)

        # Equal-width sides keep the title truly centred.
        left = QWidget()
        left.setFixedWidth(150)
        ll = QHBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(8)
        if self.frameless:
            self.lights = TrafficLights(self)
            ll.addWidget(self.lights)
        else:
            badge = QLabel()
            badge.setPixmap(make_icon().pixmap(QSize(20, 20)))
            ll.addWidget(badge)
            name = QLabel(APP_NAME)
            name.setObjectName("appName")
            ll.addWidget(name)
        ll.addStretch(1)
        hl.addWidget(left)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.setContentsMargins(0, 0, 0, 0)
        self.file_label = QLabel(APP_NAME)
        self.file_label.setObjectName("fileName")
        self.file_label.setAlignment(Qt.AlignCenter)
        titles.addWidget(self.file_label)
        self.src_label = QLabel("No file open")
        self.src_label.setObjectName("fileMeta")
        self.src_label.setAlignment(Qt.AlignCenter)
        self.src_label.setFont(tabular(QFont(self.src_label.font())))
        titles.addWidget(self.src_label)
        hl.addStretch(1)
        hl.addLayout(titles)
        hl.addStretch(1)

        right = QWidget()
        right.setFixedWidth(150)
        rl = QHBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addStretch(1)
        self.open_btn = QPushButton("Open…")
        self.open_btn.setToolTip("Open a photo or video  ·  Ctrl+O")
        self.open_btn.setCursor(Qt.PointingHandCursor)
        self.open_btn.clicked.connect(self.pick_file)
        rl.addWidget(self.open_btn)
        hl.addWidget(right)
        return head

    def _toggle_max(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _build_transport(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("transport")
        col = QVBoxLayout(bar)
        col.setContentsMargins(20, 12, 20, 12)
        col.setSpacing(6)

        controls = QGridLayout()
        controls.setColumnStretch(0, 1)
        controls.setColumnStretch(2, 1)

        clock = QHBoxLayout()
        clock.setSpacing(6)
        self.time_label = QLabel("00:00.000")
        self.time_label.setObjectName("time")
        self.time_label.setFont(tabular(QFont(self.time_label.font())))
        clock.addWidget(self.time_label)
        self.dur_label = QLabel("")
        self.dur_label.setObjectName("timeMuted")
        self.dur_label.setFont(tabular(QFont(self.dur_label.font())))
        clock.addWidget(self.dur_label)
        clock.addStretch(1)
        controls.addLayout(clock, 0, 0)

        deck = QHBoxLayout()
        deck.setSpacing(10)
        self.back_btn = QPushButton()
        self.back_btn.setToolTip("Back one frame  ·  ←")
        self.back_btn.clicked.connect(lambda: self.nudge(-1))
        self.play_btn = QPushButton()
        self.play_btn.setObjectName("play")
        self.play_btn.setFixedSize(36, 36)
        self.play_btn.setToolTip("Play or pause  ·  Space")
        self.play_btn.clicked.connect(self.toggle_play)
        self.fwd_btn = QPushButton()
        self.fwd_btn.setToolTip("Forward one frame  ·  →")
        self.fwd_btn.clicked.connect(lambda: self.nudge(1))
        for b in (self.back_btn, self.fwd_btn):
            b.setObjectName("icon")
            b.setFixedSize(30, 30)
        for b in (self.back_btn, self.play_btn, self.fwd_btn):
            b.setIconSize(QSize(20, 20))
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            deck.addWidget(b)
        controls.addLayout(deck, 0, 1)

        trims = QHBoxLayout()
        trims.setSpacing(2)
        trims.addStretch(1)
        self.split_btn = QPushButton("Split")
        self.split_btn.setToolTip("Split the clip at the playhead  ·  S\n"
                                  "Then hover a piece and click × to cut it out  ·  Delete")
        self.in_btn = QPushButton("Set Start")
        self.out_btn = QPushButton("Set End")
        self.reset_trim_btn = QPushButton("Full Clip")
        self.in_btn.setToolTip("Move the start to the playhead  ·  I")
        self.out_btn.setToolTip("Move the end to the playhead  ·  O")
        self.reset_trim_btn.setToolTip("Restore the whole clip and remove every cut")
        for b, slot in ((self.split_btn, self.split_here),
                        (self.in_btn, self.set_in),
                        (self.out_btn, self.set_out),
                        (self.reset_trim_btn, self.reset_trim)):
            b.setObjectName("ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(slot)
            trims.addWidget(b)
        controls.addLayout(trims, 0, 2)
        col.addLayout(controls)

        self.trim = Filmstrip()
        self.trim.setToolTip("Drag the yellow ends to trim  ·  S splits at the playhead  ·  "
                             "hover a piece and click × to cut it out\n"
                             "Drag a split to move it  ·  double-click a split to remove it  ·  "
                             "edges snap to each other, hold Shift to drag freely")
        self.trim.seeked.connect(self.seek)
        self.trim.rangeChanged.connect(self._trim_changed)
        self.trim.cutsChanged.connect(self._trim_changed)
        self.trim.scrubbing.connect(self._scrub_state)
        col.addWidget(self.trim)

        self.transport_bar = bar
        return bar

    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(300)
        outer = QVBoxLayout(side)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        holder.setObjectName("sideBody")
        col = QVBoxLayout(holder)
        col.setContentsMargins(20, 18, 20, 16)
        col.setSpacing(0)

        col.addWidget(self._sec_mirror())
        col.addWidget(self._sec_crop())
        self.output_sec = self._sec_output()
        col.addWidget(self.output_sec)
        col.addStretch(1)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)
        outer.addWidget(self._build_export_dock())
        return side

    def _sec_mirror(self) -> Section:
        sec = Section("Mirror", rule=False)
        self.mirror_seg = Segmented([("Horizontal", "h"), ("Vertical", "v")], exclusive=False)
        self.hflip_btn, self.vflip_btn = self.mirror_seg.buttons
        self.hflip_btn.setToolTip("Mirror left to right  ·  H")
        self.vflip_btn.setToolTip("Mirror top to bottom  ·  V")
        for b in self.mirror_seg.buttons:
            b.setIconSize(QSize(16, 16))
            b.toggled.connect(self._mirror_changed)
        sec.add(self.mirror_seg)
        return sec

    def _sec_crop(self) -> Section:
        self.crop_all_btn = QPushButton("Reset")
        self.crop_all_btn.setToolTip("Snap the box back to the full frame  ·  Ctrl+A")
        self.crop_all_btn.clicked.connect(self.view.select_all)
        self.crop_clear_btn = QPushButton("Clear")
        self.crop_clear_btn.setToolTip("Remove the crop box entirely  ·  Esc")
        self.crop_clear_btn.clicked.connect(lambda: self.view.set_crop(None))
        for b in (self.crop_all_btn, self.crop_clear_btn):
            b.setObjectName("link")
            b.setCursor(Qt.PointingHandCursor)
        sec = Section("Crop", trailing=(self.crop_all_btn, self.crop_clear_btn))

        aspect = QHBoxLayout()
        aspect.setSpacing(10)
        label = QLabel("Aspect")
        label.setObjectName("rowLabel")
        aspect.addWidget(label)
        self.ratio_box = polish_combo(QComboBox())
        for text, key in (("Freeform", None), ("Original", "orig"), ("Square", 1.0),
                          ("16:9", 16 / 9), ("9:16", 9 / 16), ("4:3", 4 / 3),
                          ("3:4", 3 / 4), ("21:9", 21 / 9)):
            self.ratio_box.addItem(text, key)
        self.ratio_box.currentIndexChanged.connect(self._ratio_changed)
        aspect.addWidget(self.ratio_box, 1)
        sec.add(aspect)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(8)
        self.crop_x, self.crop_y = spin(), spin()
        self.crop_w, self.crop_h = spin(), spin()
        for row, c, cap, box, tip in ((0, 0, "X", self.crop_x, "Left edge, px"),
                                      (0, 1, "Y", self.crop_y, "Top edge, px"),
                                      (1, 0, "W", self.crop_w, "Width, px"),
                                      (1, 1, "H", self.crop_h, "Height, px")):
            lab = QLabel(cap)
            lab.setObjectName("axis")
            lab.setFixedWidth(14)
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            box.setToolTip(tip)
            grid.addWidget(lab, row, c * 2)
            grid.addWidget(box, row, c * 2 + 1)
            box.valueChanged.connect(self._boxes_to_crop)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        sec.add(grid)

        self.crop_note = QLabel("Drag the corners on the frame, or type exact pixels. "
                                "The box follows the mirror.")
        self.crop_note.setObjectName("hint")
        self.crop_note.setWordWrap(True)
        sec.add(self.crop_note)
        return sec

    def _sec_output(self) -> Section:
        sec = Section("Video")

        self.encoder_box = polish_combo(QComboBox())
        for eid, label in available_encoders():
            self.encoder_box.addItem(label, eid)
        self.encoder_box.currentIndexChanged.connect(self._encoder_changed)
        sec.add(self.encoder_box)
        self.encoder_note = QLabel("")
        self.encoder_note.setObjectName("hint")
        self.encoder_note.setWordWrap(True)
        sec.add(self.encoder_note)
        self._encoder_changed()

        self.quality_seg = Segmented([("Best", "high"), ("Balanced", "balanced"),
                                      ("Smaller", "small")])
        self.quality_seg.set_value("balanced")
        self.quality_seg.setToolTip("Best quality · balanced · smallest file")
        sec.add(self.quality_seg)

        audio = QHBoxLayout()
        label = QLabel("Keep audio")
        label.setObjectName("rowLabel")
        audio.addWidget(label)
        audio.addStretch(1)
        self.audio_btn = Switch(checked=True)
        self.audio_btn.setToolTip("Untick to export a silent video")
        audio.addWidget(self.audio_btn)
        sec.add(audio)
        return sec

    def _build_export_dock(self) -> QWidget:
        dock = QFrame()
        dock.setObjectName("dock")
        col = QVBoxLayout(dock)
        col.setContentsMargins(20, 14, 20, 18)
        col.setSpacing(10)

        self.plan_label = QLabel("")
        self.plan_label.setObjectName("plan")
        self.plan_label.setWordWrap(True)
        col.addWidget(self.plan_label)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        col.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        self.status.setOpenExternalLinks(False)
        self.status.linkActivated.connect(self._reveal_export)
        self.status.hide()
        col.addWidget(self.status)

        self.export_btn = QPushButton("Export…")
        self.export_btn.setObjectName("primary")
        self.export_btn.setFixedHeight(38)
        self.export_btn.setToolTip("Write the edited file  ·  Ctrl+E")
        self.export_btn.setCursor(Qt.PointingHandCursor)
        self.export_btn.clicked.connect(self.export)
        col.addWidget(self.export_btn)
        return dock

    def _paint_icons(self) -> None:
        """(Re)draw every glyph in the current theme's ink."""
        self.play_btn.setIcon(glyph_icon("pause" if self._playing else "play", T.text))
        self.back_btn.setIcon(glyph_icon("back", T.secondary))
        self.fwd_btn.setIcon(glyph_icon("fwd", T.secondary))
        self.hflip_btn.setIcon(glyph_icon("fliph", T.text))
        self.vflip_btn.setIcon(glyph_icon("flipv", T.text))

    def retheme(self) -> None:
        self.view.setBackgroundBrush(QColor(T.canvas))
        self._paint_icons()
        for w in self.findChildren(QWidget):
            w.update()

    # -- window chrome -------------------------------------------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self.frameless and not self._grip and self.windowHandle():
            self._grip = EdgeGrip(self)
            self.windowHandle().installEventFilter(self._grip)

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.ActivationChange, QEvent.WindowStateChange) \
                and hasattr(self, "lights"):
            self.lights.update()

    def _build_shortcuts(self) -> None:
        def act(seq, slot):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(slot)
            self.addAction(a)

        act("Ctrl+O", self.pick_file)
        act("Ctrl+E", self.export)
        act("Space", self.toggle_play)
        act("I", self.set_in)
        act("O", self.set_out)
        act("S", self.split_here)
        act("Delete", self.toggle_here)
        act("Backspace", self.toggle_here)
        act("H", lambda: self.hflip_btn.toggle())
        act("V", lambda: self.vflip_btn.toggle())
        act("Left", lambda: self.nudge(-1))
        act("Right", lambda: self.nudge(1))
        act("Shift+Left", lambda: self.nudge(-1, big=True))
        act("Shift+Right", lambda: self.nudge(1, big=True))
        act("Ctrl+A", self.view.select_all)
        act("Escape", lambda: self.view.set_crop(None))

    # -- loading -------------------------------------------------------------
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.empty.set_hot(True)

    def dragLeaveEvent(self, _event) -> None:
        self.empty.set_hot(False)

    def dropEvent(self, event) -> None:
        self.empty.set_hot(False)
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.load(Path(url.toLocalFile()))
                break

    def pick_file(self) -> None:
        patterns = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS | VIDEO_EXTS))
        start = self.info.path.parent if self.info else Path.home()
        path, _ = QFileDialog.getOpenFileName(
            self, "Open photo or video", str(start),
            f"Media ({patterns});;All files (*)",
        )
        if path:
            self.load(Path(path))

    def _set_title(self, text: str) -> None:
        metrics = QFontMetrics(self.file_label.font())
        self.file_label.setText(metrics.elidedText(text, Qt.ElideMiddle, 460))

    def load(self, path: Path) -> None:
        try:
            info = probe(path)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "Can't open that", str(exc))
            return

        self._teardown_player()
        self.info = info
        self.static_mode = False
        self._primed = False
        self._got_frame = False
        self._playing = False
        self.empty.hide()
        self.status.hide()
        self._set_title(info.name)
        self.setWindowTitle(f"{info.name} — {APP_NAME}")

        bits = [f"{info.width} × {info.height}"]
        if info.is_video:
            bits.append(fmt_time(info.duration, False))
            if info.fps:
                bits.append(f"{info.fps:.0f} fps")
            bits.append(codec_name(info.vcodec) if info.vcodec else "video")
            bits.append(codec_name(info.acodec) if info.has_audio else "silent")
        else:
            bits.append(info.path.suffix.lstrip(".").upper() or "photo")
        self.src_label.setText("  ·  ".join(bits))

        if info.is_video and HAS_QTMM:
            item = QGraphicsVideoItem()
            item.setAspectRatioMode(Qt.IgnoreAspectRatio)
            item.setSize(QSizeF(info.width, info.height))
            self.view.set_item(item, info)

            self.audio = QAudioOutput(self)
            self.player = QMediaPlayer(self)
            self.player.setVideoOutput(item)
            self.player.setAudioOutput(self.audio)
            self.player.positionChanged.connect(self._position_changed)
            self.player.playbackStateChanged.connect(self._playstate_changed)
            self.player.errorOccurred.connect(self._player_error)
            self.player.mediaStatusChanged.connect(self._media_status)

            # If nothing ever reaches the sink (NVIDIA/Wayland backend trouble),
            # fall back to ffmpeg stills so scrubbing still shows a frame.
            self._got_frame = False
            try:
                item.videoSink().videoFrameChanged.connect(self._frame_seen)
            except (AttributeError, RuntimeError):
                pass
            QTimer.singleShot(2600, self._check_video_alive)

            self.player.setSource(QUrl.fromLocalFile(str(info.path)))
        else:
            pix = self._grab_frame(0.0) if info.is_video else QPixmap(str(info.path))
            item = QGraphicsPixmapItem(pix)
            item.setTransformationMode(Qt.SmoothTransformation)
            self.view.set_item(item, info)

        self.trim.set_source(info.path if info.is_video else None,
                             info.duration if info.is_video else 0.0)
        self.transport_bar.setVisible(info.is_video)
        self.view.select_all()
        self._set_enabled(True)
        self._paint_icons()
        self._update_time()
        self._update_plan()

    def _teardown_player(self) -> None:
        if self.player:
            self.player.stop()
            try:
                self.player.setVideoOutput(None)
            except (TypeError, RuntimeError):
                pass
            self.player.deleteLater()
        self.player = None
        self.audio = None
        self.view.clear_media()

    def _frame_seen(self, *_args) -> None:
        self._got_frame = True

    def _media_status(self, status) -> None:
        # A paused QMediaPlayer will not render a seek until it has decoded at
        # least once, so nudge it the moment the file is ready.
        if status == QMediaPlayer.LoadedMedia and self.player and not self._primed:
            self._primed = True
            self.player.play()
            QTimer.singleShot(90, self._finish_prime)

    def _finish_prime(self) -> None:
        if self.player:
            self.player.pause()
            self.player.setPosition(0)
            self.trim.set_playhead(0.0)

    def _check_video_alive(self) -> None:
        if self.info and self.info.is_video and self.player and not self._got_frame:
            self._switch_to_static()

    def _switch_to_static(self) -> None:
        """Drop QtMultimedia and drive the preview from ffmpeg stills instead."""
        if not self.info or self.static_mode:
            return
        self.static_mode = True
        crop = QRectF(self.view.crop) if self.view.crop else None
        at = self.trim.playhead
        self._teardown_player()

        item = QGraphicsPixmapItem(self._grab_frame(at))
        item.setTransformationMode(Qt.SmoothTransformation)
        self.view.set_item(item, self.info)
        self.view.set_mirror(self.hflip_btn.isChecked(), self.vflip_btn.isChecked())
        if crop:
            self.view.set_crop(crop)
        self._pending_seek = at

        self.play_btn.setEnabled(False)
        self.play_btn.setToolTip("Playback unavailable for this file — scrubbing works")
        self._say("Preview is showing still frames. Trimming, cropping and export "
                  "are unaffected.")

    def _player_error(self, _error, message: str) -> None:
        self._say(f"Playback: {message or 'unsupported stream'}. "
                  "Editing and export still work.")

    # -- static-frame fallback ----------------------------------------------
    def _grab_frame(self, t: float) -> QPixmap:
        if not (self.info and shutil.which("ffmpeg")):
            return QPixmap()
        try:
            out = subprocess.run(
                [which_or_die("ffmpeg"), "-v", "quiet", "-ss", f"{t:.3f}",
                 "-i", str(self.info.path), "-frames:v", "1",
                 "-f", "image2", "-c:v", "png", "-"],
                capture_output=True, timeout=30, check=False,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            return QPixmap()
        pix = QPixmap()
        pix.loadFromData(out, "PNG")
        return pix

    def _load_static_frame(self) -> None:
        pix = self._grab_frame(self._pending_seek)
        if not pix.isNull():
            self.view.show_pixmap(pix)

    # -- playback ------------------------------------------------------------
    def toggle_play(self) -> None:
        if not self.player:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            pos = self.player.position() / 1000.0
            if pos < self.trim.start - 0.05 or pos >= self.trim.end - 0.02:
                self.player.setPosition(int(self.trim.start * 1000))
            self.player.play()

    def seek(self, t: float) -> None:
        if self.player:
            self.player.setPosition(int(t * 1000))
            self.trim.set_playhead(t)
            self.view.viewport().update()
        else:
            self._pending_seek = t
            self.trim.set_playhead(t)
            self._frame_timer.start()
        self._update_time(t)

    def nudge(self, direction: int, big: bool = False) -> None:
        if not (self.info and self.info.is_video):
            return
        step = 1.0 if big else (1.0 / self.info.fps if self.info.fps > 1 else 0.04)
        self.seek(self.trim.playhead + direction * step)

    def _position_changed(self, ms: int) -> None:
        t = ms / 1000.0
        playing = bool(self.player) and \
            self.player.playbackState() == QMediaPlayer.PlayingState
        if playing and not self.trim.is_dragging():
            kept = self.trim.kept()
            jump = self.trim.skip_target(t)
            if t >= kept[-1][1] - 0.01:              # past the last kept frame: back to the top
                self.player.pause()
                t = kept[0][0]
                self.player.setPosition(int(t * 1000))
            elif jump is not None:                   # play straight over cut-out pieces
                t = jump
                self.player.setPosition(int(t * 1000))
        self.trim.set_playhead(t)
        self._update_time(t)

    def _playstate_changed(self, state) -> None:
        self._playing = state == QMediaPlayer.PlayingState
        self.play_btn.setIcon(glyph_icon("pause" if self._playing else "play", T.text))

    def _update_time(self, t: float | None = None) -> None:
        if not self.info:
            self.time_label.setText("00:00.000")
            self.dur_label.setText("")
            return
        if t is None:
            t = self.player.position() / 1000.0 if self.player else self._pending_seek
        self.time_label.setText(fmt_time(t))
        self.dur_label.setText(f"/  {fmt_time(self.info.duration)}")

    def set_in(self) -> None:
        if self.info and self.info.is_video:
            self.trim.set_range(self.trim.playhead, self.trim.end)
            self._update_plan()

    def set_out(self) -> None:
        if self.info and self.info.is_video:
            self.trim.set_range(self.trim.start, self.trim.playhead)
            self._update_plan()

    def reset_trim(self) -> None:
        if self.info and self.info.is_video:
            self.trim.set_range(0.0, self.info.duration)
            self.trim.clear_cuts()
            self._update_plan()

    def split_here(self) -> None:
        if not (self.info and self.info.is_video):
            return
        if not self.trim.split_at(self.trim.playhead):
            self._say("Move the playhead a little further from the edges to split there.")

    def toggle_here(self) -> None:
        if not (self.info and self.info.is_video):
            return
        if not self.trim.splits:
            self._say("Press S to split at the playhead first, then cut a piece out.")
        elif not self.trim.toggle_piece(self.trim.piece_at(self.trim.playhead)):
            self._say("At least one piece has to stay in.")

    def _scrub_state(self, active: bool) -> None:
        if active and self.player and \
                self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()

    def _trim_changed(self, *_args) -> None:
        self._update_plan()

    # -- edit state ----------------------------------------------------------
    def _mirror_changed(self) -> None:
        self.view.set_mirror(self.hflip_btn.isChecked(), self.vflip_btn.isChecked())
        self._update_plan()

    def _ratio_changed(self) -> None:
        key = self.ratio_box.currentData()
        if key == "orig":
            ratio = (self.info.width / self.info.height) if self.info else None
        else:
            ratio = key
        self.view.set_ratio(ratio)

    def _encoder_changed(self) -> None:
        self.encoder_note.setText(ENCODER_NOTES.get(self.encoder_box.currentData(), ""))

    def _crop_to_boxes(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        crop = self.view.crop
        for box in (self.crop_x, self.crop_y, self.crop_w, self.crop_h):
            box.setEnabled(crop is not None)
        if crop:
            if self.info:
                self.crop_x.setMaximum(self.info.width)
                self.crop_y.setMaximum(self.info.height)
                self.crop_w.setMaximum(self.info.width)
                self.crop_h.setMaximum(self.info.height)
            self.crop_x.setValue(round(crop.x()))
            self.crop_y.setValue(round(crop.y()))
            self.crop_w.setValue(round(crop.width()))
            self.crop_h.setValue(round(crop.height()))
        self._syncing = False
        self._update_plan()

    def _boxes_to_crop(self) -> None:
        if self._syncing or not self.info:
            return
        self._syncing = True
        self.view.set_crop(QRectF(self.crop_x.value(), self.crop_y.value(),
                                  max(MIN_CROP, self.crop_w.value()),
                                  max(MIN_CROP, self.crop_h.value())))
        self._syncing = False
        self._update_plan()

    def _even_crop(self) -> tuple[int, int, int, int] | None:
        """Round the crop to even numbers — yuv420p requires it."""
        if not (self.view.crop and self.info):
            return None
        r = self.view.crop
        x = max(0, round(r.x()))
        y = max(0, round(r.y()))
        w = round(r.width())
        h = round(r.height())
        w = min(w - (w % 2), self.info.width - x)
        h = min(h - (h % 2), self.info.height - y)
        w -= w % 2
        h -= h % 2
        if w < 2 or h < 2:
            return None
        if (x, y, w, h) == (0, 0, self.info.width, self.info.height):
            return None
        return (w, h, x, y)

    def _update_plan(self) -> None:
        if not self.info:
            self.plan_label.setText("")
            return
        steps = []
        if self.hflip_btn.isChecked():
            steps.append("Mirrored horizontally")
        if self.vflip_btn.isChecked():
            steps.append("Mirrored vertically")
        crop = self._even_crop()
        if crop:
            steps.append(f"Cropped to {crop[0]} × {crop[1]}")
        kept = self.trim.kept() if self.info.is_video else []
        if self.info.is_video and self.trim.has_cuts():
            gaps = sum(1 for i, gone in enumerate(self.trim.removed)
                       if gone and (i == 0 or not self.trim.removed[i - 1]))
            steps.append(f"{gaps} cut{'s' if gaps != 1 else ''}, keeping "
                         f"{fmt_time(self.trim.kept_duration(), False)}")
        elif self.info.is_video and (self.trim.start > 0.001
                                     or self.trim.end < self.info.duration - 0.001):
            steps.append(f"Trimmed to {fmt_time(self.trim.end - self.trim.start, False)}")

        if not steps:
            self.plan_label.setText("No changes yet. Export makes a copy.")
            return
        text = "  ·  ".join(steps)
        if len(kept) > 1:
            text += "\nRe-encoded, so every cut lands on its exact frame."
        elif self.info.is_video and not crop and not self.hflip_btn.isChecked() \
                and not self.vflip_btn.isChecked():
            text += "\nLossless — streams are copied, not re-encoded."
        self.plan_label.setText(text)

    def _set_enabled(self, on: bool) -> None:
        video = bool(on and self.info and self.info.is_video)
        for w in (self.mirror_seg, self.ratio_box, self.crop_all_btn,
                  self.crop_clear_btn, self.export_btn):
            w.setEnabled(on)
        for w in (self.play_btn, self.back_btn, self.fwd_btn, self.split_btn,
                  self.in_btn, self.out_btn,
                  self.reset_trim_btn, self.encoder_box, self.quality_seg):
            w.setEnabled(video)
        self.play_btn.setToolTip("Play or pause  ·  Space")
        self.audio_btn.setEnabled(video and bool(self.info and self.info.has_audio))
        self.output_sec.setVisible(video)
        for box in (self.crop_x, self.crop_y, self.crop_w, self.crop_h):
            box.setEnabled(on and self.view.crop is not None)
        if not on:
            self.empty.show()

    # -- export --------------------------------------------------------------
    def _suggest_name(self) -> str:
        stem = self.info.path.stem
        ext = self.info.path.suffix
        if self.info.is_video:
            enc = self.encoder_box.currentData() or "libx264"
            ext = ".webm" if enc == "libvpx-vp9" else ".mp4"
        return f"{stem}-edit{ext}"

    def export(self) -> None:
        if not self.info or (self.worker and self.worker.isRunning()):
            return

        target, _ = QFileDialog.getSaveFileName(
            self, "Export as", str(self.info.path.parent / self._suggest_name()))
        if not target:
            return
        out = Path(target)
        if out.resolve() == self.info.path.resolve():
            QMessageBox.warning(self, "Pick a different name",
                                "Exporting over the source file would corrupt it.")
            return

        if self.info.is_video:
            self._export_video(out)
        else:
            self._export_image(out)

    def _export_image(self, out: Path) -> None:
        img = QImage(str(self.info.path))
        if img.isNull():
            self._say("Could not read the image.", "err")
            return
        h, v = self.hflip_btn.isChecked(), self.vflip_btn.isChecked()
        if h or v:
            try:
                img = img.flipped(
                    (Qt.Horizontal if h else Qt.Orientation(0))
                    | (Qt.Vertical if v else Qt.Orientation(0)))
            except (AttributeError, TypeError):  # Qt < 6.9
                img = img.mirrored(h, v)
        crop = self._even_crop()
        if crop:
            w, h, x, y = crop
            img = img.copy(x, y, w, h)
        if img.save(str(out)):
            self._export_done(str(out))
        else:
            self._say(f"Could not write {out.name}. Try a .png or .jpg name.", "err")

    def _export_video(self, out: Path) -> None:
        kept = self.trim.kept() or [(0.0, self.info.duration)]
        opts = ExportOptions(
            start=kept[0][0],
            end=kept[-1][1] if kept[-1][1] > 0 else self.info.duration,
            segments=kept if len(kept) > 1 else None,
            hflip=self.hflip_btn.isChecked(),
            vflip=self.vflip_btn.isChecked(),
            crop=self._even_crop(),
            keep_audio=self.audio_btn.isChecked(),
            encoder=self.encoder_box.currentData() or "libx264",
            quality=self.quality_seg.value() or "balanced",
        )
        try:
            cmd = build_command(self.info, opts, out)
        except FileNotFoundError:
            QMessageBox.critical(self, "ffmpeg is missing",
                                 "NanoSnip needs ffmpeg on your PATH to export video.")
            return

        self.progress.setValue(0)
        self.progress.show()
        self._say("Exporting…")
        self.export_btn.setText("Cancel Export")
        self.export_btn.setObjectName("cancel")
        self._restyle(self.export_btn)
        self.export_btn.clicked.disconnect()
        self.export_btn.clicked.connect(self._cancel_export)

        self.worker = ExportWorker(cmd, sum(b - a for a, b in kept))
        self.worker.progress.connect(self.progress.setValue)
        self.worker.finished_ok.connect(self._export_done)
        self.worker.failed.connect(self._export_failed)
        self.worker.start()

    def _cancel_export(self) -> None:
        if self.worker:
            self.worker.cancel()

    def _reset_export_button(self) -> None:
        self.export_btn.setText("Export…")
        self.export_btn.setObjectName("primary")
        self._restyle(self.export_btn)
        self.export_btn.clicked.disconnect()
        self.export_btn.clicked.connect(self.export)

    def _restyle(self, widget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _say(self, text: str, tone: str = "info", rich: bool = False) -> None:
        self.status.setTextFormat(Qt.RichText if rich else Qt.PlainText)
        self.status.setText(text)
        self.status.setProperty("tone", tone)
        self._restyle(self.status)
        self.status.show()

    def _export_done(self, path: str) -> None:
        self.progress.setValue(100)
        QTimer.singleShot(900, self.progress.hide)
        self._last_export = Path(path)
        name = html.escape(self._last_export.name)
        self._say(f"Saved “{name}” &nbsp;"
                  f"<a href='reveal' style='color:{T.accent}; text-decoration:none'>"
                  f"Show in Folder</a>", "ok", rich=True)
        self._reset_export_button()

    def _export_failed(self, message: str) -> None:
        self.progress.hide()
        self._say(message, "info" if message == "Export cancelled." else "err")
        self._reset_export_button()

    def _reveal_export(self, _link: str) -> None:
        if self._last_export:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_export.parent)))

    def closeEvent(self, event) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(2000)
        self.trim.set_source(None, 0.0)
        self._teardown_player()
        super().closeEvent(event)


# ==================================================================== style ==
_ASSETS: Path | None = None


def asset_dir() -> Path:
    """A private temp folder for the few bitmaps the stylesheet needs."""
    global _ASSETS
    if _ASSETS is None:
        _ASSETS = Path(tempfile.mkdtemp(prefix="nanosnip-"))
        atexit.register(shutil.rmtree, _ASSETS, True)
    return _ASSETS


def chevron_png(colour: str) -> str:
    """The up/down chevron Apple uses on pop-up buttons, written at 1x and 2x."""
    base = asset_dir() / f"chevron-{colour.lstrip('#')}"
    for scale, suffix in ((1, ""), (2, "@2x")):
        pix = QPixmap(8 * scale, 12 * scale)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(scale, scale)
        pen = QPen(QColor(colour), 1.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline([QPointF(1.5, 4.5), QPointF(4, 2), QPointF(6.5, 4.5)])
        p.drawPolyline([QPointF(1.5, 7.5), QPointF(4, 10), QPointF(6.5, 7.5)])
        p.end()
        pix.save(f"{base}{suffix}.png")
    return f"{base}.png".replace("\\", "/")


def stylesheet(t: Theme) -> str:
    chevron = chevron_png(t.secondary)
    return f"""
* {{ outline: none; }}
QWidget {{ color: {t.text}; font-size: 13px; }}
QMainWindow, #root {{ background: {t.window}; }}
QLabel {{ background: transparent; }}

#header {{ background: {t.window}; border-bottom: 1px solid {t.separator}; }}
#appName {{ font-weight: 600; }}
#fileName {{ font-size: 13px; font-weight: 600; }}
#fileMeta {{ color: {t.secondary}; font-size: 11px; }}
#stage {{ background: {t.canvas}; }}

#emptyTitle {{ font-size: 21px; font-weight: 600; }}
#emptyBody {{ color: {t.secondary}; font-size: 13px; }}
#emptyHint {{ color: {t.tertiary}; font-size: 12px; }}

#transport {{ background: {t.window}; border-top: 1px solid {t.separator}; }}
#time {{ font-size: 16px; font-weight: 600; }}
#timeMuted {{ color: {t.tertiary}; font-size: 12px; }}

#sidebar {{ background: {t.window}; border-left: 1px solid {t.separator}; }}
#sideBody {{ background: {t.window}; }}
#sectionTitle {{ font-size: 13px; font-weight: 600; }}
#rowLabel {{ color: {t.secondary}; }}
#axis {{ color: {t.tertiary}; font-size: 11px; font-weight: 600; }}
#hint {{ color: {t.tertiary}; font-size: 11px; }}
#rule {{ background: {t.separator}; border: none; }}
#dock {{ background: {t.window}; border-top: 1px solid {t.separator}; }}
#plan {{ color: {t.secondary}; font-size: 12px; }}
#status {{ color: {t.secondary}; font-size: 12px; }}
#status[tone="ok"] {{ color: {t.success}; }}
#status[tone="err"] {{ color: {t.danger}; }}

QPushButton {{
    background: {t.control};
    border: 1px solid {t.field_border};
    border-radius: 7px;
    padding: 5px 12px;
    font-weight: 500;
}}
QPushButton:hover {{ background: {t.control_hover}; }}
QPushButton:pressed {{ background: {t.control_press}; }}
QPushButton:disabled {{ color: {t.tertiary}; }}

#ghost {{ background: transparent; border: 1px solid transparent; color: {t.secondary}; }}
#ghost:hover {{ background: {t.control_hover}; color: {t.text}; }}
#ghost:pressed {{ background: {t.control_press}; }}
#ghost:disabled {{ background: transparent; color: {t.tertiary}; }}

#link {{ background: transparent; border: none; padding: 2px 0px; color: {t.accent};
         font-weight: 500; }}
#link:hover {{ color: {t.accent_hover}; background: transparent; }}
#link:disabled {{ color: {t.tertiary}; }}

#icon {{ background: transparent; border: none; border-radius: 15px; padding: 0px; }}
#icon:hover {{ background: {t.control_hover}; }}
#icon:pressed {{ background: {t.control_press}; }}
#play {{ background: {t.control}; border: 1px solid {t.field_border}; border-radius: 18px;
         padding: 0px; }}
#play:hover {{ background: {t.control_hover}; }}
#play:pressed {{ background: {t.control_press}; }}

#primary, #pill {{ background: {t.accent}; border: none; color: #FFFFFF;
                   font-weight: 600; border-radius: 9px; }}
#pill {{ padding: 0px 20px; border-radius: 16px; }}
#primary:hover, #pill:hover {{ background: {t.accent_hover}; }}
#primary:pressed, #pill:pressed {{ background: {t.accent_press}; }}
#primary:disabled {{ background: {t.control_press}; color: {t.tertiary}; }}
#cancel {{ background: {t.control}; border: 1px solid {t.field_border}; color: {t.danger};
           font-weight: 600; border-radius: 9px; }}

#segment {{ background: {t.segment}; border-radius: 8px; }}
#segmentItem {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 4px 4px;
    font-size: 12px;
    font-weight: 500;
}}
#segmentItem:hover {{ background: {t.segment_hover}; }}
#segmentItem:checked {{ background: {t.thumb}; border: 1px solid {t.thumb_border}; }}
#segmentItem:disabled {{ color: {t.tertiary}; background: transparent;
                         border-color: transparent; }}

QComboBox, QSpinBox {{
    background: {t.control};
    border: 1px solid {t.field_border};
    border-radius: 7px;
    padding: 4px 8px;
    min-height: 20px;
    selection-background-color: {t.accent};
    selection-color: #FFFFFF;
}}
QComboBox {{ padding-right: 24px; }}
/* step buttons hidden by zero width rather than NoButtons: with a border set,
   Qt 6.12 shrinks the text field of a NoButtons spin box to 1px */
QSpinBox::up-button, QSpinBox::down-button {{ width: 0px; border: none; }}
QComboBox:hover, QSpinBox:hover {{ background: {t.control_hover}; }}
QComboBox:focus, QSpinBox:focus {{ border-color: {t.accent}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
                        width: 22px; border: none; }}
QComboBox::down-arrow {{ image: url("{chevron}"); width: 8px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {t.popup};
    border: 1px solid {t.field_border};
    border-radius: 9px;
    padding: 4px;
    outline: none;
}}
QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 0px 8px; border-radius: 5px;
                                     color: {t.text}; }}
QComboBox QAbstractItemView::item:selected {{ background: {t.accent}; color: #FFFFFF; }}
QSpinBox:disabled, QComboBox:disabled {{ color: {t.tertiary}; background: {t.window}; }}

QProgressBar {{ background: {t.segment}; border: none; border-radius: 2px; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 2px; }}

QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.thumb_border}; border-radius: 3px;
                               min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{ background: {t.popup}; color: {t.text}; border: 1px solid {t.field_border};
            border-radius: 6px; padding: 5px 8px; font-size: 12px; }}
"""


def build_palette(t: Theme) -> QPalette:
    """Fusion draws dialogs and pop-ups from the palette, so match it to the theme."""
    pal = QPalette()
    roles = {
        QPalette.Window: t.window, QPalette.WindowText: t.text,
        QPalette.Base: t.control, QPalette.AlternateBase: t.control_hover,
        QPalette.Text: t.text, QPalette.Button: t.control, QPalette.ButtonText: t.text,
        QPalette.Highlight: t.accent, QPalette.HighlightedText: "#FFFFFF",
        QPalette.ToolTipBase: t.popup, QPalette.ToolTipText: t.text,
        QPalette.PlaceholderText: t.tertiary, QPalette.Link: t.accent,
        QPalette.Light: t.control_hover, QPalette.Midlight: t.control_hover,
        QPalette.Mid: t.field_border, QPalette.Dark: t.field_border,
        QPalette.Shadow: "#000000",
    }
    for role, colour in roles.items():
        pal.setColor(role, QColor(colour))
    for role in (QPalette.Text, QPalette.ButtonText, QPalette.WindowText):
        pal.setColor(QPalette.Disabled, role, QColor(t.tertiary))
    return pal


def apply_theme(app: QApplication, theme: Theme) -> None:
    global T
    T = theme
    app.setPalette(build_palette(theme))
    app.setStyleSheet(stylesheet(theme))
    for w in app.topLevelWidgets():
        if isinstance(w, MainWindow):
            w.retheme()


def system_theme(app: QApplication) -> Theme:
    try:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Light:
            return LIGHT
    except AttributeError:  # Qt < 6.5
        pass
    return DARK  # dark suits an editor, so it's also the fallback


def make_icon() -> QIcon:
    """The app mark: crop corners framing a play button, on a blue squircle."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 96, 128, 192, 256, 512):
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(size / 1024, size / 1024)

        grad = QLinearGradient(QPointF(100, 100), QPointF(924, 924))
        grad.setColorAt(0.0, QColor("#3AA0FF"))
        grad.setColorAt(1.0, QColor("#5856D6"))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(100, 100, 824, 824), 186, 186)

        pen = QPen(QColor("#FFFFFF"), 56)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPolyline([QPointF(286, 426), QPointF(286, 286), QPointF(426, 286)])
        p.drawPolyline([QPointF(738, 598), QPointF(738, 738), QPointF(598, 738)])

        p.setPen(QPen(QColor("#FFFFFF"), 44, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(QColor("#FFFFFF"))
        p.drawPolygon([QPointF(458, 406), QPointF(458, 618), QPointF(640, 512)])
        p.end()
        icon.addPixmap(pix)
    return icon


def setup_app(app: QApplication, argv: list[str]) -> None:
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("nanosnip")
    app.setStyle("Fusion")

    # The system font here is Monospace 10pt on some KDE setups; don't inherit it.
    font = QFont()
    font.setFamilies(SANS_STACK)
    font.setPointSizeF(10.0)
    font.setHintingPreference(QFont.PreferVerticalHinting)
    app.setFont(font)
    app.setWindowIcon(make_icon())

    if "--light" in argv:
        apply_theme(app, LIGHT)
    elif "--dark" in argv:
        apply_theme(app, DARK)
    else:
        apply_theme(app, system_theme(app))
        try:
            app.styleHints().colorSchemeChanged.connect(
                lambda _scheme: apply_theme(app, system_theme(app)))
        except AttributeError:  # Qt < 6.5
            pass


def main() -> int:
    if "--version" in sys.argv:
        print(f"{APP_NAME} {__version__}")
        return 0
    if "-h" in sys.argv or "--help" in sys.argv:
        print(USAGE, end="")
        return 0

    app = QApplication(sys.argv)
    setup_app(app, sys.argv)

    missing = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    if missing:
        QMessageBox.critical(None, "Missing dependency",
                             "NanoSnip needs ffmpeg to read and write media.\n\n"
                             "Arch / CachyOS:   sudo pacman -S ffmpeg\n"
                             "Debian / Ubuntu:  sudo apt install ffmpeg\n"
                             "Fedora:           sudo dnf install ffmpeg\n\n"
                             f"Not found: {', '.join(missing)}")
        return 1

    initial = None
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        candidate = Path(args[0]).expanduser()
        if candidate.exists():
            initial = candidate.resolve()
        else:
            print(f"nanosnip: {args[0]}: no such file", file=sys.stderr)

    frameless = "--native-frame" not in sys.argv
    win = MainWindow(initial, frameless=frameless)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
