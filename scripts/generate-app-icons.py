"""Render AirFrame's vector mark into the native application icon containers.

Run on macOS with the project's Qt-capable Python. Apple's iconutil writes the
ICNS container, including its native small-size encodings. ICO embeds PNGs.
Portable builds consume the generated assets and do not run this helper.
"""
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ASSETS = Path(__file__).resolve().parents[1] / "airframe" / "assets"


def png(renderer, size):
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    encoded = QByteArray()
    buffer = QBuffer(encoded)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError(f"Failed to render {size}px icon")
    return bytes(encoded)


def main():
    iconutil = shutil.which("iconutil")
    if iconutil is None:
        raise RuntimeError("Generate the macOS app icon on a Mac with iconutil installed")
    renderer = QSvgRenderer(str(ASSETS / "airframe.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid AirFrame vector icon")
    images = {size: png(renderer, size)
              for size in (16, 24, 32, 48, 64, 128, 256, 512, 1024)}
    (ASSETS / "airframe.png").write_bytes(images[1024])

    with tempfile.TemporaryDirectory(prefix="airframe-icons-") as temporary:
        iconset = Path(temporary) / "AirFrame.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            (iconset / f"icon_{size}x{size}.png").write_bytes(images[size])
            (iconset / f"icon_{size}x{size}@2x.png").write_bytes(images[size * 2])
        subprocess.run([iconutil, "-c", "icns", str(iconset), "-o",
                        str(ASSETS / "airframe.icns")], check=True)

    sizes = (16, 24, 32, 48, 64, 128, 256)
    offset = 6 + 16 * len(sizes)
    entries = []
    for size in sizes:
        data = images[size]
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256,
                                   0, 0, 1, 32, len(data), offset))
        offset += len(data)
    (ASSETS / "airframe.ico").write_bytes(
        struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries)
        + b"".join(images[size] for size in sizes))
    print("Rendered airframe.png, airframe.icns and airframe.ico from airframe.svg")


if __name__ == "__main__":
    main()
