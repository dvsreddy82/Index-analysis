"""BANKNIFTY monitor screen: the index and all its constituent banks in one view —
index header with breadth, a heatmap sized by weight, the points each bank adds
or takes away, alerts, and a full table with sparklines.  Data comes from
banknifty_service.py; this file only draws."""
from __future__ import annotations

import html

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QPushButton, QSizePolicy,
                               QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from theme import C, card, label, toolbar

SPARK_ROLE = Qt.ItemDataRole.UserRole + 20
RANGE_ROLE = Qt.ItemDataRole.UserRole + 21


def heat(pct: float | None) -> QColor:
    """Green / red intensity by % change (saturates at ±3%)."""
    if pct is None:
        return QColor(C["panel2"])
    a = min(abs(pct) / 3, 1)
    base = (22, 163, 74) if pct >= 0 else (220, 38, 38)
    bg = (17, 24, 39)
    return QColor(*[int(bg[i] + (base[i] - bg[i]) * (0.25 + 0.75 * a)) for i in range(3)])


def squarify(items: list[tuple[str, float]], rect: QRectF) -> dict[str, QRectF]:
    """Squarified treemap layout: (key, size) → rectangles filling `rect`."""
    items = [(k, v) for k, v in sorted(items, key=lambda t: -t[1]) if v > 0]
    total = sum(v for _, v in items) or 1
    out: dict[str, QRectF] = {}
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    scale = w * h / total
    rest = [(k, v * scale) for k, v in items]

    def worst(row, side):
        s = sum(a for _, a in row)
        return max(max(side * side * a / (s * s), (s * s) / (side * side * a)) for _, a in row)

    while rest:
        side = min(w, h)
        row = [rest.pop(0)]
        while rest and worst(row + [rest[0]], side) <= worst(row, side):
            row.append(rest.pop(0))
        s = sum(a for _, a in row)
        if w >= h:                       # lay the row along the left edge
            cw = s / h
            cy = y
            for k, a in row:
                out[k] = QRectF(x, cy, cw, a / cw)
                cy += a / cw
            x, w = x + cw, w - cw
        else:                            # along the top edge
            ch = s / w
            cx = x
            for k, a in row:
                out[k] = QRectF(cx, y, a / ch, ch)
                cx += a / ch
            y, h = y + ch, h - ch
    return out


class Heatmap(QWidget):
    clicked = Signal(str)

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.rows: list[dict] = []
        self.rects: dict[str, QRectF] = {}

    def set_rows(self, rows):
        self.rows = rows
        self.update()

    def mousePressEvent(self, e):
        for k, r in self.rects.items():
            if r.contains(e.position()):
                self.clicked.emit(k)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(C["panel"]))
        if not self.rows:
            p.setPen(QColor(C["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Loading constituents…")
            return
        self.rects = squarify([(q["symbol"], q["weight"]) for q in self.rows], QRectF(self.rect()).adjusted(2, 2, -2, -2))
        by = {q["symbol"]: q for q in self.rows}
        big, mid, small = QFont(self.font()), QFont(self.font()), QFont(self.font())
        big.setBold(True)
        big.setPixelSize(17)
        mid.setBold(True)
        mid.setPixelSize(13)
        small.setPixelSize(11)
        for k, r in self.rects.items():
            q = by[k]
            box = r.adjusted(1.5, 1.5, -1.5, -1.5)
            p.fillRect(box, heat(q["pct"]))
            p.setPen(QPen(QColor(C["bg"]), 2))
            p.drawRect(box)
            p.setPen(QColor("#ffffff"))
            lines = [(k, big if box.height() > 70 and box.width() > 90 else mid)]
            if q["pct"] is not None:
                lines.append((f"{q['pct']:+.2f}%", mid))
            if box.height() > 70:
                lines.append((f"{q['last']:,.2f}" if q["last"] else "—", small))
            if box.height() > 92 and q.get("contrib") is not None:
                lines.append((f"{q['contrib']:+.0f} pts · {q['weight'] * 100:.1f}%", small))
            total_h = sum(p.fontMetrics().height() + 2 for _ in lines) + 6
            yy = box.center().y() - total_h / 2
            for text, f in lines:
                p.setFont(f)
                fh = p.fontMetrics().height()
                p.drawText(QRectF(box.x() + 3, yy, box.width() - 6, fh), Qt.AlignmentFlag.AlignCenter,
                           p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, int(box.width() - 6)))
                yy += fh + 2


class ContributionChart(QWidget):
    """Horizontal bars: index points each bank added or took away today."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.rows: list[dict] = []
        self.actual: float | None = None
        self.explained = 0.0

    def set_rows(self, rows, actual, explained):
        self.rows = sorted([q for q in rows if q.get("contrib") is not None], key=lambda q: -q["contrib"])
        self.actual, self.explained = actual, explained
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(C["panel"]))
        if not self.rows:
            return
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(C["text"]))
        hdr = f"Points added to BANKNIFTY today · total {self.explained:+.0f}"
        if self.actual is not None:
            hdr += f" (index {self.actual:+.0f})"
        p.drawText(QPointF(8, 16), hdr)
        p.setFont(self.font())
        L, R, T = 92, 52, 26
        W = self.width() - L - R
        n = len(self.rows)
        bh = (self.height() - T - 6) / n
        m = max(abs(q["contrib"]) for q in self.rows) or 1
        zero = L + W / 2
        p.setPen(QPen(QColor(C["faint"]), 1))
        p.drawLine(QPointF(zero, T), QPointF(zero, self.height() - 4))
        for i, q in enumerate(self.rows):
            y = T + i * bh
            v = q["contrib"]
            w = abs(v) / m * W / 2
            col = QColor(C["up"] if v >= 0 else C["down"])
            p.fillRect(QRectF(zero if v >= 0 else zero - w, y + bh * 0.18, w, bh * 0.64), col)
            p.setPen(QColor(C["text"]))
            p.drawText(QRectF(4, y, L - 8, bh), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       q["symbol"])
            p.setPen(QColor(C["muted"]))
            tx = zero + w + 4 if v >= 0 else zero - w - 52
            p.drawText(QRectF(tx, y, 48, bh), (Qt.AlignmentFlag.AlignLeft if v >= 0 else Qt.AlignmentFlag.AlignRight)
                       | Qt.AlignmentFlag.AlignVCenter, f"{v:+.0f}")


class CellDelegate(QStyledItemDelegate):
    """Sparkline and day-range bar cells."""

    def paint(self, p: QPainter, opt, idx):
        spark = idx.data(SPARK_ROLE)
        rng = idx.data(RANGE_ROLE)
        if spark is None and rng is None:
            return super().paint(p, opt, idx)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = opt.rect.adjusted(6, 5, -6, -5)
        if spark:
            vals, prev = spark
            lo, hi = min(vals + ([prev] if prev else [])), max(vals + ([prev] if prev else []))
            span = (hi - lo) or 1
            yv = lambda v: r.bottom() - (v - lo) / span * r.height()
            if prev:
                p.setPen(QPen(QColor(C["faint"]), 1, Qt.PenStyle.DotLine))
                p.drawLine(QPointF(r.left(), yv(prev)), QPointF(r.right(), yv(prev)))
            p.setPen(QPen(QColor(C["up"] if vals[-1] >= (prev or vals[0]) else C["down"]), 1.4))
            n = len(vals)
            pts = [QPointF(r.left() + i / max(n - 1, 1) * r.width(), yv(v)) for i, v in enumerate(vals)]
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
        elif rng is not None:
            pos = rng
            p.fillRect(QRectF(r.left(), r.center().y() - 2, r.width(), 4), QColor(C["border"]))
            x = r.left() + max(0, min(pos, 100)) / 100 * r.width()
            p.setBrush(QColor(C["accent"]))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x, r.center().y()), 4, 4)
        p.restore()


class NumItem(QTableWidgetItem):
    """Sorts by its numeric value, not its text."""

    def __init__(self, text: str, value):
        super().__init__(text)
        self.value = value if value is not None else float("-inf")

    def __lt__(self, other):
        return self.value < getattr(other, "value", float("-inf"))


COLS = ["Bank", "Weight", "LTP", "Change", "% chg", "Pts to index", "Day range (L → H)", "vs VWAP", "Rel. volume",
        "Volume", "Today"]


class BankNiftyDashboard(QWidget):
    refresh_requested = Signal()
    refit_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        bar, hl = toolbar()
        self.status = label("Loading BANKNIFTY constituents…")
        hl.addWidget(self.status, 1)
        b = QPushButton("Re-fit weights")
        b.setToolTip("Re-estimate each bank's weight from the last ~6 sessions of 1-minute returns")
        b.clicked.connect(self.refit_requested.emit)
        hl.addWidget(b)
        b = QPushButton("Refresh", objectName="primary")
        b.clicked.connect(self.refresh_requested.emit)
        hl.addWidget(b)
        lay.addWidget(bar)

        f, l = card()
        self.head = label("")
        l.addWidget(self.head)
        lay.addWidget(f)

        mid = QHBoxLayout()
        mid.setSpacing(8)
        f, l = card("Heatmap · size = weight in BANKNIFTY, colour = today's move")
        self.heatmap = Heatmap()
        self.heatmap.clicked.connect(self._select)
        l.addWidget(self.heatmap)
        mid.addWidget(f, 3)
        f, l = card("Contribution")
        self.contrib = ContributionChart()
        l.addWidget(self.contrib)
        mid.addWidget(f, 2)
        lay.addLayout(mid, 3)

        self.alerts = label("")
        lay.addWidget(self.alerts)

        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setItemDelegate(CellDelegate(self.table))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.SortOrder.DescendingOrder)        # heaviest bank first
        lay.addWidget(self.table, 4)
        lay.addWidget(label(f"<span style='color:{C['muted']}'>Weights are estimated from 1-minute returns (NSE no "
                            "longer publishes live weights) and re-fitted daily; points-to-index = weight × % change × "
                            "BANKNIFTY's previous close. Relative volume compares today's volume so far with the "
                            "average of the last 5 sessions at the same time. Read-only analytics.</span>", 11))

    def _select(self, sym: str):
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.data(Qt.ItemDataRole.UserRole) == sym:
                self.table.selectRow(r)
                self.table.scrollToItem(it)

    def set_status(self, text: str):
        self.status.setText(text)

    def set_snapshot(self, s: dict):
        i, rows = s["index"], s["rows"]
        mut = C["muted"]
        if i.get("last"):
            col = C["up"] if (i.get("chg") or 0) >= 0 else C["down"]
            rng = ""
            if i.get("high") and i.get("low") and i["high"] > i["low"]:
                pos = (i["last"] - i["low"]) / (i["high"] - i["low"]) * 100
                rng = f" · at {pos:.0f}% of the day's range"
            lifts = sorted([q for q in rows if q.get("contrib") is not None], key=lambda q: -q["contrib"])
            top = ", ".join(f"{q['symbol']} {q['contrib']:+.0f}" for q in lifts[:3] if q["contrib"] > 0)
            drag = ", ".join(f"{q['symbol']} {q['contrib']:+.0f}" for q in lifts[::-1][:3] if q["contrib"] < 0)
            self.head.setText(
                f"<span style='font-size:22px;font-weight:800'>BANKNIFTY {i['last']:,.2f}</span> &nbsp;"
                f"<span style='font-size:16px;font-weight:700;color:{col}'>"
                + (f"{i['chg']:+,.2f} ({i['pct']:+.2f}%)" if i.get("chg") is not None else "") + "</span>"
                f"<span style='color:{mut}'> &nbsp; O {i.get('open') or 0:,.1f} · H {i.get('high') or 0:,.1f} · "
                f"L {i.get('low') or 0:,.1f}{rng}</span><br>"
                f"<span style='color:{mut}'>Breadth</span> <b style='color:{C['up']}'>{s['adv']}</b> up / "
                f"<b style='color:{C['down']}'>{s['dec']}</b> down · <b>{s['up_weight']:.0f}%</b> of the weight is up"
                f" &nbsp; <span style='color:{mut}'>Lifting:</span> <span style='color:{C['up']}'>{top or '—'}</span>"
                f" &nbsp; <span style='color:{mut}'>Dragging:</span> <span style='color:{C['down']}'>{drag or '—'}</span>")
        self.heatmap.set_rows(rows)
        self.contrib.set_rows(rows, i.get("chg"), s["explained"])
        al = s["alerts"]
        self.alerts.setText("".join(
            f"<span style='background:{'#450a0a' if lv == 'red' else '#3b2306'};color:"
            f"{'#fecaca' if lv == 'red' else '#fde68a'};border-radius:4px;padding:2px 8px'>"
            f"{'🔴' if lv == 'red' else '🟠'} {html.escape(t)}</span>&nbsp; " for lv, t in al)
            or f"<span style='color:{mut}'>No alerts: no bank moved ±2%, heavyweights agree with the index, breadth "
               "is not narrow.</span>")
        self._table(rows)

    def _table(self, rows):
        t = self.table
        sel = t.currentRow()
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for r, q in enumerate(sorted(rows, key=lambda q: -q["weight"])):
            def num(text, val, color=None):
                it = NumItem(text, val)
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if color:
                    it.setForeground(QColor(color))
                return it
            up = C["up"] if (q["pct"] or 0) >= 0 else C["down"]
            name = QTableWidgetItem(f"{q['symbol']}  ·  {q['name']}")
            name.setData(Qt.ItemDataRole.UserRole, q["symbol"])
            name.setForeground(QColor(C["text"]))
            if q.get("live"):
                name.setToolTip("Live Shoonya ticks")
            t.setItem(r, 0, name)
            t.setItem(r, 1, num(f"{q['weight'] * 100:.1f}%", q["weight"]))
            t.setItem(r, 2, num(f"{q['last']:,.2f}" if q["last"] else "—", q["last"]))
            t.setItem(r, 3, num(f"{q['chg']:+,.2f}" if q["chg"] is not None else "—", q["chg"], up))
            t.setItem(r, 4, num(f"{q['pct']:+.2f}%" if q["pct"] is not None else "—", q["pct"], up))
            t.setItem(r, 5, num(f"{q['contrib']:+.1f}" if q["contrib"] is not None else "—", q["contrib"],
                                C["up"] if (q["contrib"] or 0) >= 0 else C["down"]))
            rg = QTableWidgetItem("")
            rg.setData(RANGE_ROLE, q["range_pos"])
            rg.setToolTip(f"Low {q.get('low') or 0:,.2f} · High {q.get('high') or 0:,.2f}")
            t.setItem(r, 6, rg)
            vv = q["vs_vwap"]
            t.setItem(r, 7, num(f"{vv:+.2f}%" if vv is not None else "—", vv,
                                None if vv is None else C["up"] if vv >= 0 else C["down"]))
            rv = q.get("rvol")
            t.setItem(r, 8, num(f"{rv:.1f}×" if rv else "—", rv, C["warn"] if rv and rv >= 2 else None))
            vol = q.get("volume")
            t.setItem(r, 9, num(f"{vol / 1e5:,.1f} L" if vol else "—", vol))
            sp = QTableWidgetItem("")
            sp.setData(SPARK_ROLE, (q["spark"], q.get("prev")) if q["spark"] else None)
            t.setItem(r, 10, sp)
        t.setSortingEnabled(True)
        if 0 <= sel < t.rowCount():
            t.selectRow(sel)
