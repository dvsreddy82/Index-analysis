"""Draw the app icon (1024 px) and build packaging/AppIcon.icns with sips + iconutil."""
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

HERE = Path(__file__).resolve().parent


def draw(size=1024) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    m = size * 0.09                                   # macOS icon grid margin
    r = QRectF(m, m, size - 2 * m, size - 2 * m)
    path = QPainterPath()
    path.addRoundedRect(r, size * 0.18, size * 0.18)
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    g.setColorAt(0, QColor("#0f2a44"))
    g.setColorAt(1, QColor("#0b0f17"))
    p.fillPath(path, g)
    p.setPen(QPen(QColor("#1f3b57"), size * 0.006))
    p.drawPath(path)
    # candles
    x0, w = r.left() + r.width() * 0.16, r.width() * 0.085
    candles = [(0.62, 0.48, 0.70, 0.42, False), (0.50, 0.40, 0.58, 0.34, True), (0.44, 0.52, 0.60, 0.38, False),
               (0.46, 0.30, 0.50, 0.26, True), (0.32, 0.22, 0.38, 0.17, True)]
    for i, (o, c, lo, hi, up) in enumerate(candles):
        x = x0 + i * r.width() * 0.145
        col = QColor("#22c55e" if up else "#ef4444")
        y = lambda v: r.top() + r.height() * v
        p.setPen(QPen(col, size * 0.012))
        p.drawLine(QPointF(x + w / 2, y(hi)), QPointF(x + w / 2, y(lo)))
        top, bot = min(y(o), y(c)), max(y(o), y(c))
        p.fillRect(QRectF(x, top, w, max(bot - top, size * 0.01)), col)
    # rising trend line
    pen = QPen(QColor("#38bdf8"), size * 0.022)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    pts = [QPointF(r.left() + r.width() * f, r.top() + r.height() * v)
           for f, v in ((0.12, 0.80), (0.38, 0.70), (0.56, 0.74), (0.88, 0.50))]
    for a, b in zip(pts, pts[1:]):
        p.drawLine(a, b)
    p.end()
    return img


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841  (QImage text/AA needs a GUI app)
    master = HERE / "icon_1024.png"
    draw().save(str(master))
    iconset = HERE / "AppIcon.iconset"
    iconset.mkdir(exist_ok=True)
    for s in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = s * scale
            name = f"icon_{s}x{s}{'@2x' if scale == 2 else ''}.png"
            subprocess.run(["sips", "-z", str(px), str(px), str(master), "--out", str(iconset / name)],
                           check=True, capture_output=True)
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "AppIcon.icns")], check=True)
    print("wrote", HERE / "AppIcon.icns")


if __name__ == "__main__":
    main()
