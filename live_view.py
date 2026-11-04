"""Live one-screen dashboard: a tile per index, refreshed from NSE every few seconds.

Each tile combines the live quote (NSE allIndices / ETF APIs) with the latest
stored daily analysis (sr_levels): where the live price sits inside the
projected monthly range, whether it is testing a support/resistance zone right
now, the EOD signal, and the tracking ETF's live price.
"""
from __future__ import annotations

from datetime import date, datetime

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

import config

C = {
    "bg": "#0b0f17", "panel": "#111827", "panel2": "#0f172a", "border": "#1f2937",
    "text": "#e5e7eb", "muted": "#94a3b8", "accent": "#38bdf8", "up": "#22c55e", "down": "#ef4444",
}
GROUP_COLORS = {"Large cap": "#38bdf8", "Broad market": "#a78bfa", "Mid cap": "#f59e0b",
                "Small cap": "#f472b6", "Sectoral": "#2dd4bf"}
MIN_TILE_W, TILE_H = 196, 204   # tiles stretch to fill the row; height is fixed
CHART_DAYS = 30                  # bars in each tile's mini chart
LIVE_ZONE_ATR = 0.1    # live tag fires inside a key zone or within this many ATR of it
SHORT_NAMES = {"^NSEI": "NIFTY 50", "NIFTY FINANCIAL SERVICES": "FINNIFTY",
               "NIFTY FINANCIAL SERVICES 25/50": "Fin Services 25/50",
               "NIFTY500 MULTICAP 50:25:25": "Multicap 50:25:25",
               "NIFTY CONSUMER DURABLES": "Consumer Durables"}
SHORT_SIGNAL = {"ACCUMULATE": "ACCUM", "PROFIT": "BOOK PROFIT", "WATCH": "WATCH", "HOLD": "HOLD",
                "TIGHTEN": "TIGHTEN STOPS", "AVOID": "AVOID"}


def key_zones(doc: dict) -> tuple[list[dict], list[dict]]:
    """The two strongest support and resistance zones (daily zones are dense, so
    only the strongest are worth flagging live)."""
    top = lambda zs: sorted(zs, key=lambda z: -z["strength"])[:2]
    return top(doc.get("support_zones", [])), top(doc.get("resistance_zones", []))


def live_status(ltp: float, doc: dict | None) -> tuple[str, str]:
    """Classify the live price: outside the projected month range > testing a key
    zone > which third of the projected range it sits in."""
    if not doc or ltp is None:
        return "NO LEVELS — RUN SCAN", C["muted"]
    proj = doc.get("projection") or {}
    lo, hi = proj.get("lower"), proj.get("upper")
    if lo and ltp < lo:
        return "BELOW MONTHLY RANGE", "#a855f7"
    if hi and ltp > hi:
        return "ABOVE MONTHLY RANGE", "#f97316"
    tol = ((doc.get("signal") or {}).get("atr") or 0) * LIVE_ZONE_ATR
    sup, res = key_zones(doc)
    hits = [z for z in sup + res if z["low"] - tol <= ltp <= z["high"] + tol]
    if hits:
        # Nearest key zone decides: price on top of it = support, under it = resistance.
        z = min(hits, key=lambda z: abs(z["mid"] - ltp))
        return ("AT KEY SUPPORT", C["up"]) if ltp >= z["mid"] else ("AT KEY RESISTANCE", C["down"])
    if lo and hi and hi > lo:
        pos = (ltp - lo) / (hi - lo)
        if pos <= 1 / 3:
            return "DIP BAND (lower third)", "#4ade80"
        if pos >= 2 / 3:
            return "RALLY BAND (upper third)", "#f87171"
    return "MID-RANGE", C["muted"]


def nearest_levels(ltp: float, doc: dict | None) -> tuple[float | None, float | None]:
    if not doc or ltp is None:
        return None, None
    zones = doc.get("support_zones", []) + doc.get("resistance_zones", [])
    below = [z["mid"] for z in zones if z["mid"] < ltp]
    above = [z["mid"] for z in zones if z["mid"] > ltp]
    return (max(below) if below else None), (min(above) if above else None)


def fmt_px(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}" if v < 10000 else f"{v:,.0f}"


class RangeBar(QWidget):
    """Projected monthly range (low→high) with today's range and a live marker."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(18)
        self.lo = self.hi = self.ltp = self.day_lo = self.day_hi = None

    def set_values(self, lo, hi, ltp, day_lo, day_hi):
        self.lo, self.hi, self.ltp, self.day_lo, self.day_hi = lo, hi, ltp, day_lo, day_hi
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        track = QRectF(4, h / 2 - 3, w - 8, 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1e293b"))
        p.drawRoundedRect(track, 3, 3)
        if not (self.lo and self.hi and self.hi > self.lo and self.ltp):
            return

        def x(v):
            return track.left() + track.width() * min(max((v - self.lo) / (self.hi - self.lo), 0), 1)
        # Lower third green (accumulate), upper third red (book profit).
        third = track.width() / 3
        p.setBrush(QColor(34, 197, 94, 70))
        p.drawRoundedRect(QRectF(track.left(), track.top(), third, track.height()), 3, 3)
        p.setBrush(QColor(239, 68, 68, 70))
        p.drawRoundedRect(QRectF(track.right() - third, track.top(), third, track.height()), 3, 3)
        if self.day_lo and self.day_hi:
            p.setBrush(QColor(148, 163, 184, 120))
            p.drawRect(QRectF(x(self.day_lo), track.top() + 1, max(x(self.day_hi) - x(self.day_lo), 1),
                              track.height() - 2))
        out = self.ltp < self.lo or self.ltp > self.hi
        p.setPen(QPen(QColor("#f97316" if out else "#f8fafc"), 2))
        mx = x(self.ltp)
        p.drawLine(int(mx), 1, int(mx), h - 1)


Bar = tuple[date, float, float, float, float]   # (date, open, high, low, close)


class MiniChart(QWidget):
    """Compact daily candlestick chart: last CHART_DAYS bars (today's bar live),
    key support/resistance bands and a dotted live-price line."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(74)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.bars: list[Bar] = []
        self.zones: list[tuple[float, float, QColor]] = []
        self.last: float | None = None

    def set_data(self, bars: list[Bar], zones: list[tuple[float, float, QColor]], last: float | None):
        self.bars, self.zones, self.last = bars, zones, last
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 4, -30, -4)   # right gutter for price labels
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(15, 23, 42, 160))
        p.drawRoundedRect(QRectF(self.rect()), 4, 4)
        if len(self.bars) < 2:
            p.setPen(QColor(C["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "no history")
            return
        lo = min(b[3] for b in self.bars)
        hi = max(b[2] for b in self.bars)
        pad = (hi - lo) * 0.04 or hi * 0.002
        lo, hi = lo - pad, hi + pad

        def y(v):
            return r.bottom() - (v - lo) / (hi - lo) * r.height()

        for zlo, zhi, col in self.zones:          # bands, clipped to the visible range
            if zhi < lo or zlo > hi:
                continue
            top, bot = y(min(zhi, hi)), y(max(zlo, lo))
            p.fillRect(QRectF(r.left(), top, r.width(), max(bot - top, 1)), col)

        n = len(self.bars)
        step = r.width() / n
        body_w = max(step * 0.62, 1.5)
        for i, (_d, o, h, l, c) in enumerate(self.bars):
            cx = r.left() + step * (i + 0.5)
            col = QColor(C["up"] if c >= o else C["down"])
            if i == n - 1:
                col = col.lighter(130)
            p.setPen(QPen(col, 1))
            p.drawLine(QPointF(cx, y(h)), QPointF(cx, y(l)))
            top, bot = y(max(o, c)), y(min(o, c))
            p.fillRect(QRectF(cx - body_w / 2, top, body_w, max(bot - top, 1)), col)

        f = p.font()
        f.setPixelSize(9)
        p.setFont(f)
        p.setPen(QColor(C["muted"]))
        p.drawText(QRectF(r.right() + 2, r.top() - 4, 30, 12), Qt.AlignmentFlag.AlignLeft, fmt_axis(hi - pad))
        p.drawText(QRectF(r.right() + 2, r.bottom() - 8, 30, 12), Qt.AlignmentFlag.AlignLeft, fmt_axis(lo + pad))
        if self.last is not None and lo <= self.last <= hi:
            ly = y(self.last)
            p.setPen(QPen(QColor(248, 250, 252, 170), 1, Qt.PenStyle.DotLine))
            p.drawLine(QPointF(r.left(), ly), QPointF(r.right(), ly))


def fmt_axis(v: float) -> str:
    return f"{v / 1000:.1f}k" if v >= 10000 else f"{v:,.0f}" if v >= 1000 else f"{v:.1f}"


class Tile(QFrame):
    clicked = Signal(str)

    def __init__(self, symbol: str):
        super().__init__()
        self.symbol = symbol
        inst = config.INSTRUMENTS[symbol]
        self.setFixedHeight(TILE_H)
        # Preferred (not minimum) width: a hard minimum would let a stale column
        # count push the whole window wider before the grid re-flows.
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stripe = GROUP_COLORS.get(inst.group, C["accent"])
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(1)

        top = QHBoxLayout()
        self.name = QLabel(SHORT_NAMES.get(symbol, inst.name.replace("NIFTY ", "")))
        self.name.setStyleSheet("font-weight:700;font-size:12px;background:transparent")
        self.pct = QLabel("—")
        self.pct.setStyleSheet("font-weight:700;font-size:12px;background:transparent")
        top.addWidget(self.name, 1)
        top.addWidget(self.pct)
        lay.addLayout(top)

        row2 = QHBoxLayout()
        self.ltp = QLabel("—")
        self.ltp.setStyleSheet("font-size:17px;font-weight:800;background:transparent")
        self.chg30 = QLabel("")
        self.chg30.setStyleSheet(f"font-size:10px;color:{C['muted']};background:transparent")
        row2.addWidget(self.ltp, 1)
        row2.addWidget(self.chg30, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(row2)
        self.chart = MiniChart()
        lay.addWidget(self.chart, 1)
        self.bar = RangeBar()
        self.bar.setFixedHeight(12)
        lay.addWidget(self.bar)
        self.status = QLabel("")
        self.status.setStyleSheet("font-size:10px;font-weight:700;background:transparent")
        lay.addWidget(self.status)
        self.levels = QLabel("")
        self.levels.setStyleSheet(f"font-size:9px;color:{C['muted']};background:transparent")
        lay.addWidget(self.levels)
        self.etf = QLabel("")
        self.etf.setStyleSheet(f"font-size:9px;color:{C['text']};background:transparent")
        lay.addWidget(self.etf)
        self._style(0)

    def _style(self, pct: float | None):
        # Heat-map background: intensity scales with the day's move (saturates at ±2.5%).
        a = min(abs(pct or 0) / 2.5, 1) * 0.28
        rgb = "34,197,94" if (pct or 0) >= 0 else "239,68,68"
        self.setStyleSheet(
            f"Tile {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 rgba({rgb},{a:.2f}), "
            f"stop:1 {C['panel']}); border:1px solid {C['border']}; border-left:3px solid {self.stripe};"
            f"border-radius:7px; }} Tile:hover {{ border-color:{C['accent']}; }}")

    def update_data(self, q: dict | None, doc: dict | None, etf_q: dict | None, etf_sym: str | None,
                    bars: list[Bar] | None = None):
        if bars:
            sup, res = key_zones(doc) if doc else ([], [])
            zones = [(z["low"], z["high"], QColor(34, 197, 94, 55)) for z in sup] + \
                    [(z["low"], z["high"], QColor(239, 68, 68, 55)) for z in res]
            self.chart.set_data(bars, zones, (q or {}).get("last"))
            c30 = (bars[-1][4] / bars[0][4] - 1) * 100
            self.chg30.setText(f"<span style='color:{C['up'] if c30 >= 0 else C['down']}'>{c30:+.1f}%</span>"
                               f" {len(bars)}D")
        if not q or q.get("last") is None:
            return
        last, pct = q["last"], q.get("pct") or 0
        col = C["up"] if pct >= 0 else C["down"]
        self.ltp.setText(fmt_px(last))
        self.pct.setText(f"{pct:+.2f}%")
        self.pct.setStyleSheet(f"font-weight:700;font-size:12px;color:{col};background:transparent")
        self._style(pct)
        proj = (doc or {}).get("projection") or {}
        self.bar.set_values(proj.get("lower"), proj.get("upper"), last, q.get("low"), q.get("high"))
        txt, scol = live_status(last, doc)
        rec = ((doc or {}).get("signal") or {}).get("recommendation", "")
        rec = SHORT_SIGNAL.get(rec.split()[0], rec) if rec else ""
        self.status.setText(f"<span style='color:{scol}'>{txt}</span>"
                            + (f" <span style='color:{C['muted']};font-weight:400'>· EOD {rec}</span>"
                               if rec else ""))
        s, r = nearest_levels(last, doc)
        if s or r:
            lv = lambda v: f"{v:,.0f}" if v >= 1000 else f"{v:,.2f}"
            self.levels.setText(f"S {lv(s)} ({(s / last - 1) * 100:+.1f}%) · "
                                f"R {lv(r)} ({(r / last - 1) * 100:+.1f}%)" if s and r else
                                f"S {lv(s)}" if s else f"R {lv(r)}")
        if etf_sym and etf_q and etf_q.get("last") is not None:
            ep = etf_q.get("pct") or 0
            self.etf.setText(f"{etf_sym.removesuffix('.NS')} ₹{etf_q['last']:,.2f} "
                             f"<span style='color:{C['up'] if ep >= 0 else C['down']}'>{ep:+.2f}%</span>")
        self.setToolTip(f"{config.INSTRUMENTS[self.symbol].name}\nOpen {fmt_px(q.get('open'))} · "
                        f"High {fmt_px(q.get('high'))} · Low {fmt_px(q.get('low'))} · Prev {fmt_px(q.get('prev'))}"
                        + (f"\n52w {fmt_px(q.get('year_low'))} – {fmt_px(q.get('year_high'))}"
                           if q.get("year_high") else "")
                        + (f"\nAdv/Dec {q['adv']:.0f}/{q['dec']:.0f}" if q.get("adv") is not None else "")
                        + (f"\nProjected month range {fmt_px(proj.get('lower'))} – {fmt_px(proj.get('upper'))}"
                           if proj else "")
                        + "\nClick to open chart")

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.symbol)


class _Part:
    """One segment of the header line; setting its text re-renders the header."""

    def __init__(self, owner: "LiveDashboard"):
        self.owner, self.value = owner, ""

    def setText(self, text: str):
        self.value = text
        self.owner._render_header()

    def text(self) -> str:
        return self.value


class LiveDashboard(QWidget):
    open_symbol = Signal(str)
    refresh_requested = Signal()
    levels_requested = Signal()
    sync_requested = Signal()
    interval_changed = Signal(int)
    broker_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.levels: dict[str, dict] = {}
        self.history: dict[str, list[Bar]] = {}
        self.snapshot: dict | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        # ---- header strip. The market info is one word-wrapped label (not a row of
        # fixed labels) so the Live tab never forces a minimum window width; the
        # QTabWidget applies the widest page's minimum to every tab.
        head = QFrame()
        head.setStyleSheet(f"QFrame {{ background:{C['panel']}; border:1px solid {C['border']}; border-radius:8px; }}"
                           "QLabel { border:none; background:transparent; }")
        hv = QVBoxLayout(head)
        hv.setContentsMargins(12, 6, 12, 6)
        hv.setSpacing(4)
        self.info = QLabel("Connecting to NSE…")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hv.addWidget(self.info)
        self.mkt, self.ts = _Part(self), _Part(self)
        self.hdr = {k: _Part(self) for k in ("nifty", "bank", "vix", "breadth", "flows")}

        hl = QHBoxLayout()
        hl.setSpacing(8)
        legend = QLabel(" ".join(f"<span style='color:{c}'>▍</span>{g}" for g, c in GROUP_COLORS.items())
                        + f" &nbsp; <span style='color:{C['muted']}'>Chart = last {CHART_DAYS} daily candles "
                          "(today live) · green/red bands = key support/resistance · dotted = live price · "
                          "bar = projected monthly range (green third: accumulate · red third: book profit)</span>")
        legend.setStyleSheet("font-size:10px")
        legend.setWordWrap(True)
        legend.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hl.addWidget(legend, 1)
        self.interval = QComboBox()
        for s in (5, 10, 15, 30, 60):
            self.interval.addItem(f"Every {s}s", s)
        self.interval.setCurrentIndex(max(self.interval.findData(config.LIVE_REFRESH_SECONDS), 0))
        self.interval.currentIndexChanged.connect(lambda _: self.interval_changed.emit(self.interval.currentData()))
        self.pause = QPushButton("Pause")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(lambda on: self.pause.setText("Resume" if on else "Pause"))
        now_btn = QPushButton("Refresh")
        now_btn.clicked.connect(self.refresh_requested.emit)
        self.lvl_btn = QPushButton("Levels")
        self.lvl_btn.setToolTip("Recompute levels: re-run the daily analysis for every index "
                                "(updates zones, ranges, EOD signals)")
        self.lvl_btn.clicked.connect(self.levels_requested.emit)
        self.sync_btn = QPushButton("⟳ Sync EOD")
        self.sync_btn.setToolTip("Download the latest EOD data (prices, FII/DII, OI), then refresh charts and levels")
        self.sync_btn.clicked.connect(self.sync_requested.emit)
        self.broker = QPushButton("Connect Shoonya (real-time)")
        self.broker.setToolTip("Log in to Shoonya for real-time prices (read-only)")
        self.broker.clicked.connect(self.broker_requested.emit)
        for w in (self.interval, self.pause, now_btn, self.lvl_btn, self.sync_btn, self.broker):
            hl.addWidget(w)
        hv.addLayout(hl)
        lay.addWidget(head)

        # ---- tile grid (re-flows on resize)
        self.grid_host = QWidget()
        self.grid_host.setObjectName("root")
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.tiles: list[Tile] = []
        for sym in config.INDEX_SYMBOLS:
            t = Tile(sym)
            t.clicked.connect(self.open_symbol.emit)
            self.tiles.append(t)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.grid_host)
        self.scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self.scroll, 1)
        self._cols = 0
        self._reflow()

    def _render_header(self):
        parts = [self.mkt.value, self.ts.value] + [self.hdr[k].value for k in
                                                   ("nifty", "bank", "vix", "breadth", "flows")]
        sep = f"&nbsp;&nbsp;<span style='color:{C['border']}'>│</span>&nbsp;&nbsp;"
        self.info.setText(sep.join(p for p in parts if p))

    def paused(self) -> bool:
        return self.pause.isChecked()

    def _reflow(self):
        # As many columns as fit at the minimum tile width; tiles then stretch to fill the row.
        cols = max(1, (self.scroll.viewport().width() + 8) // (MIN_TILE_W + 8))
        if cols == self._cols:
            return
        self._cols = cols
        for t in self.tiles:
            self.grid.removeWidget(t)
        for i, t in enumerate(self.tiles):
            self.grid.addWidget(t, i // cols, i % cols)
        for c in range(self.grid.columnCount()):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow()

    def showEvent(self, e):
        super().showEvent(e)
        self._reflow()

    # ---------------------------------------------------------- data
    def set_history(self, history: dict[str, list[Bar]]):
        """Stored daily bars per index (oldest first); the live quote updates the last one."""
        self.history = history
        if self.snapshot:
            self.update_snapshot(self.snapshot)
        else:
            for t in self.tiles:
                t.update_data(None, self.levels.get(t.symbol), None, None, self.history.get(t.symbol))

    @staticmethod
    def _with_live_bar(bars: list[Bar], q: dict | None, session: date | None) -> list[Bar]:
        """Replace/append the bar for the current NSE session date with the live OHLC."""
        if not bars or not q or session is None or None in (q.get("open"), q.get("high"),
                                                              q.get("low"), q.get("last")):
            return bars[-CHART_DAYS:]
        o = q["open"] or q["last"]
        live = (session, o, max(q["high"], o, q["last"]), min(q["low"] or q["last"], o, q["last"]), q["last"])
        if bars[-1][0] == session:
            bars = bars[:-1] + [live]
        elif session > bars[-1][0]:
            bars = bars + [live]
        return bars[-CHART_DAYS:]

    def set_levels(self, levels: dict[str, dict]):
        self.levels = levels
        if self.snapshot:
            self.update_snapshot(self.snapshot)

    def set_flows(self, fii: float | None, dii: float | None, day: str = ""):
        if fii is None and dii is None:
            return
        f = lambda v: "—" if v is None else f"{v:+,.0f}"
        self.hdr["flows"].setText(f"FII <b style='color:{C['up'] if (fii or 0) >= 0 else C['down']}'>{f(fii)}</b> · "
                                  f"DII <b style='color:{C['up'] if (dii or 0) >= 0 else C['down']}'>{f(dii)}</b> cr"
                                  f" <span style='color:{C['muted']}'>{day}</span>")

    def update_snapshot(self, snap: dict):
        self.snapshot = snap
        q = snap.get("quotes", {})
        status = snap.get("market_status") or "—"
        open_ = status.lower() == "open"
        self.mkt.setText(f"<b><span style='color:{C['up'] if open_ else C['down']}'>●</span> "
                         f"MARKET {status.upper()}</b>")
        self.ts.setText(f"<span style='color:{C['muted']}'>NSE {snap.get('ts') or '—'} · "
                        f"polled {datetime.now():%H:%M:%S}</span>")
        for key, sym, label in (("nifty", "^NSEI", "NIFTY"), ("bank", "NIFTY BANK", "BANKNIFTY")):
            x = q.get(sym)
            if x and x.get("last") is not None:
                c = C["up"] if (x.get("pct") or 0) >= 0 else C["down"]
                self.hdr[key].setText(f"{label} <b>{x['last']:,.2f}</b> <span style='color:{c}'>{x['pct']:+.2f}%</span>")
        v = snap.get("vix")
        if v and v.get("last") is not None:
            c = C["down"] if (v.get("pct") or 0) > 0 else C["up"]
            self.hdr["vix"].setText(f"VIX <b>{v['last']:.2f}</b> <span style='color:{c}'>{v['pct']:+.2f}%</span>")
        n500 = q.get("NIFTY 500") or {}
        if n500.get("adv") is not None:
            adv, dec = n500["adv"], n500["dec"]
            self.hdr["breadth"].setText(f"NIFTY 500 A/D <b style='color:{C['up']}'>{adv:.0f}</b>/"
                                        f"<b style='color:{C['down']}'>{dec:.0f}</b>")
        try:
            session = datetime.strptime(snap.get("ts") or "", "%d-%b-%Y %H:%M").date()
        except ValueError:
            session = None
        for t in self.tiles:
            etf = config.INSTRUMENTS[t.symbol].etf
            bars = self._with_live_bar(self.history.get(t.symbol, []), q.get(t.symbol), session)
            t.update_data(q.get(t.symbol), self.levels.get(t.symbol), q.get(etf) if etf else None, etf, bars)
