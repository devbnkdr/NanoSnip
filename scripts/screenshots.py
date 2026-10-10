#!/usr/bin/env python3
"""Regenerate the README screenshots in docs/ without touching your desktop.

    QT_QPA_PLATFORM=offscreen python3 scripts/screenshots.py

Renders a synthetic clip with ffmpeg, opens it in NanoSnip, applies a mirror,
crop and trim, and grabs the window in both appearances.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, QTimer

import nanosnip

DOCS = ROOT / "docs"


def make_clip(folder: Path) -> Path:
    clip = folder / "sunset.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", ("gradients=s=1920x1080:c0=0x0a2a6b:c1=0xff6a3d:c2=0xffc857:"
                               "c3=0x3a1c71:n=4:speed=0.02:d=12:r=30"),
         "-f", "lavfi", "-i", "sine=f=330:d=12",
         "-vf", "format=yuv420p", "-c:v", "libx264", "-crf", "22", "-c:a", "aac",
         "-shortest", str(clip)],
        check=True,
    )
    return clip


def main() -> None:
    DOCS.mkdir(exist_ok=True)
    app = nanosnip.QApplication(sys.argv)
    nanosnip.setup_app(app, ["--dark"])
    work = Path(tempfile.mkdtemp(prefix="nanosnip-shots-"))
    clip = make_clip(work)
    jobs = [(nanosnip.DARK, "screenshot-dark.png", clip), (nanosnip.LIGHT, "screenshot-light.png", clip),
            (nanosnip.DARK, "empty-dark.png", None), (nanosnip.LIGHT, "empty-light.png", None)]

    def run(i: int = 0) -> None:
        if i == len(jobs):
            app.quit()
            return
        theme, name, media = jobs[i]
        nanosnip.apply_theme(app, theme)
        win = nanosnip.MainWindow(media)
        win.resize(1340, 840)
        win.show()

        def edit() -> None:
            if media:
                win.hflip_btn.setChecked(True)
                win.view.set_crop(QRectF(300, 120, 1320, 840))
                win.trim.set_range(2.1, 9.4)
                win._trim_changed()
                win.seek(5.0)

        def snap() -> None:
            win.grab().save(str(DOCS / name))
            print("wrote", DOCS / name)
            win.close()
            run(i + 1)

        QTimer.singleShot(3200, edit)
        QTimer.singleShot(8000 if media else 600, snap)

    QTimer.singleShot(0, run)
    app.exec()


if __name__ == "__main__":
    main()
