"""Option-chain view for NIFTY / BANKNIFTY: a mirrored Calls | Strike | Puts table
(market-standard layout) with OI bars, change in OI, LTP change, IV, Greeks,
bid/ask, volume, build-up and strike PCR; ITM shading, a spot divider and the
key OI strikes highlighted; a summary strip; and OI / change-in-OI / IV-smile
charts.  Data comes from options.analyze_chain()["chain"] (Shoonya real-time
when connected, else the NSE snapshot)."""
from __future__ import annotations

import html

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QSizePolicy, QStyledItemDelegate, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from theme import C

CE_COLOR, PE_COLOR = "#ef4444", "#22c55e"
ITM_BG = QColor(250, 204, 21, 18)          # in-the-money side tint
ATM_BG = QColor(56, 189, 248, 40)
BAR_ROLE = Qt.ItemDataRole.UserRole + 10   # (fraction 0..1, color) for the OI bar delegate
BUILD_COLORS = {"Long build-up": C["up"], "Short build-up": C["down"], "Short covering": "#38bdf8",
                "Long unwinding": "#f59e0b"}
BUILD_SHORT = {"Long build-up": "Long ↑", "Short build-up": "Short ↑", "Short covering": "Sh. cover",
               "Long unwinding": "Long ↓"}

# column groups: (key, header, group)  — calls are mirrored, so the CE list reads outside → in
CE_COLS = [("buildup", "Build-up", "build"), ("vega", "Vega", "greeks"), ("theta", "Theta", "greeks"),
           ("gamma", "Gamma", "greeks"), ("delta", "Delta", "ivd"), ("iv", "IV", "ivd"), ("vol", "Volume", "vol"),
           ("bid", "Bid", "quote"), ("ask", "Ask", "quote"), ("chg_oi", "Chg OI", "core"), ("oi", "OI", "core"),
           ("ltp", "LTP", "core")]
PE_COLS = list(reversed(CE_COLS))
GROUPS = [("core", "OI / LTP", True), ("ivd", "IV && Delta", True), ("vol", "Volume", True),
          ("build", "Build-up", True), ("greeks", "Gamma / Theta / Vega", False), ("quote", "Bid / Ask", False)]
RANGES = [("±10", 10), ("±15", 15), ("±20", 20), ("±30", 30), ("All", 999)]


def _k(v: float) -> str:
    """Compact contracts: 1.2L / 45.3K / 812."""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e7:
        return f"{v / 1e7:.2f}Cr"
    if a >= 1e5:
        return f"{v / 1e5:.2f}L"
    if a >= 1e3:
        return f"{v / 1e3:.1f}K"
    return f"{v:,.0f}"


def _f(v, fmt="{:,.2f}"):
    return "—" if v is None else fmt.format(v)


class BarDelegate(QStyledItemDelegate):
    """Draws a horizontal bar behind the cell text (OI / change in OI)."""

    def paint(self, p: QPainter, opt, idx):
        bg = idx.data(Qt.ItemDataRole.BackgroundRole)      # stylesheets ignore item backgrounds: paint it here
        if bg is not None:
            p.fillRect(opt.rect, bg.color() if hasattr(bg, "color") else QColor(bg))
        bar = idx.data(BAR_ROLE)
        if bar:
            frac, color, from_right = bar
            r = opt.rect.adjusted(2, 4, -2, -4)
            w = max(1.0, r.width() * min(abs(frac), 1.0))
            col = QColor(color)
            col.setAlpha(70)
            x = r.right() - w if from_right else r.left()
            p.fillRect(QRectF(x, r.top(), w, r.height()), col)
        super().paint(p, opt, idx)


class ChainTable(QTableWidget):
    def __init__(self):
        super().__init__()
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(24)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setWordWrap(False)
        self.setItemDelegate(BarDelegate(self))
        self.setStyleSheet(f"QTableWidget {{ background:{C['panel2']}; color:{C['text']}; border:1px solid {C['border']};"
                           f"border-radius:6px; gridline-color:{C['border']}; font-size:12px; }}"
                           f"QTableWidget::item {{ border-bottom:1px solid #161f2e; padding:0 4px; }}"
                           f"QTableWidget::item:selected {{ background:#1e3a5f; }}"
                           f"QHeaderView::section {{ background:{C['panel']}; color:{C['muted']}; border:none;"
                           f"border-bottom:1px solid {C['border']}; padding:4px; font-size:11px; font-weight:600; }}")
        self.cols: list[tuple[str, str, str]] = []

    def configure(self, groups: set[str]):
        ce = [("ce", k, h) for k, h, g in CE_COLS if g in groups]
        pe = [("pe", k, h) for k, h, g in PE_COLS if g in groups]
        self.cols = ce + [("mid", "strike", "Strike  (PCR)")] + pe
        self.setColumnCount(len(self.cols))
        self.setHorizontalHeaderLabels([h for _, _, h in self.cols])
        hh = self.horizontalHeader()
        hh.setMinimumSectionSize(64)        # wide layouts scroll sideways instead of eliding numbers
        for i, (side, key, _) in enumerate(self.cols):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
            if key == "strike":
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
                self.setColumnWidth(i, 120)
            elif key in ("ltp", "buildup"):
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
                self.setColumnWidth(i, 140 if key == "ltp" else 86)
            it = self.horizontalHeaderItem(i)
            it.setForeground(QColor(CE_COLOR if side == "ce" else PE_COLOR if side == "pe" else C["text"]))

    def fill(self, r: dict, rows: list[dict]):
        S = r["spot"]
        mx = {p: max([x[f"{p}_oi"] for x in rows] or [1]) or 1 for p in ("ce", "pe")}
        mxc = max([abs(x[f"{p}_chg_oi"]) for x in rows for p in ("ce", "pe")] or [1]) or 1
        top = {p: sorted((x[f"{p}_oi"] for x in rows), reverse=True)[:3] for p in ("ce", "pe")}
        # spot divider sits between the strikes either side of spot
        spot_at = next((i for i, x in enumerate(rows) if x["strike"] > S), len(rows))
        self.setRowCount(len(rows) + 1)
        self.clearSpans()
        bold = QFont(self.font())
        bold.setBold(True)
        atm = r.get("atm")
        out_r = 0
        for i, x in enumerate(rows + [None]):
            if i == spot_at:
                self._spot_row(out_r, r)
                out_r += 1
            if x is None:
                break
            k = x["strike"]
            for c, (side, key, _) in enumerate(self.cols):
                it = QTableWidgetItem()
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if side == "mid":
                    pcr = x.get("pcr")
                    it.setText(f"{k:,.0f}" + (f"   {pcr:.2f}" if pcr is not None else ""))
                    it.setFont(bold)
                    it.setBackground(QColor(C["panel"]) if k != atm else ATM_BG)
                    if pcr is not None:
                        it.setForeground(QColor(C["up"] if pcr > 1.2 else C["down"] if pcr < 0.8 else C["text"]))
                    it.setToolTip(f"Strike {k:,.0f} · PCR {pcr:.2f}" if pcr is not None else f"Strike {k:,.0f}")
                    self.setItem(out_r, c, it)
                    continue
                itm = (k < S) if side == "ce" else (k > S)
                if itm:
                    it.setBackground(ITM_BG)
                if k == atm:
                    it.setBackground(ATM_BG)
                self._cell(it, side, key, x, mx, mxc, top)
                self.setItem(out_r, c, it)
            out_r += 1
        self.setRowCount(out_r)

    def _spot_row(self, row: int, r: dict):
        it = QTableWidgetItem(f"▶  SPOT {r['spot']:,.2f}   ·   ATM {r.get('atm', 0):,.0f}   ·   "
                              f"±1σ to expiry {r['sigma_move']:,.0f} pts" if r.get("sigma_move") else
                              f"▶  SPOT {r['spot']:,.2f}")
        it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        it.setForeground(QColor("#0b0f17"))
        it.setBackground(QColor(C["accent"]))
        f = QFont(self.font())
        f.setBold(True)
        it.setFont(f)
        self.setItem(row, 0, it)
        self.setSpan(row, 0, 1, self.columnCount())
        self.spot_row = row

    def _cell(self, it, side, key, x, mx, mxc, top):
        p = side
        v = x.get(f"{p}_{key}")
        tip = (f"{'CALL' if p == 'ce' else 'PUT'} {x['strike']:,.0f}\n"
               f"LTP {_f(x[f'{p}_ltp'])} ({_f(x[f'{p}_chg'], '{:+,.2f}')}"
               f"{', ' + _f(x[f'{p}_chg_pct'], '{:+.1f}%') if x[f'{p}_chg_pct'] is not None else ''})\n"
               f"Bid {_f(x[f'{p}_bid'])} · Ask {_f(x[f'{p}_ask'])}\n"
               f"OI {x[f'{p}_oi']:,.0f} · change {x[f'{p}_chg_oi']:+,.0f}"
               f"{' (' + _f(x[f'{p}_oi_pct'], '{:+.1f}%') + ')' if x[f'{p}_oi_pct'] is not None else ''}\n"
               f"Volume {x[f'{p}_vol']:,.0f} · IV {_f(x[f'{p}_iv'], '{:.2f}%')}\n"
               f"Δ {_f(x[f'{p}_delta'], '{:+.3f}')} · Γ {_f(x[f'{p}_gamma'], '{:.5f}')} · "
               f"Θ {_f(x[f'{p}_theta'], '{:+.2f}')}/day · Vega {_f(x[f'{p}_vega'], '{:.2f}')}\n"
               f"Intrinsic {_f(x[f'{p}_intrinsic'])} · time value {_f(x[f'{p}_time_value'])}\n"
               f"{x[f'{p}_buildup'] or ''}")
        it.setToolTip(tip)
        if key == "oi":
            it.setText(_k(v))
            it.setData(BAR_ROLE, (v / mx[p], CE_COLOR if p == "ce" else PE_COLOR, p == "ce"))
            if v and v in top[p]:
                f = QFont(self.font())
                f.setBold(True)
                it.setFont(f)
                it.setForeground(QColor(CE_COLOR if p == "ce" else PE_COLOR))
        elif key == "chg_oi":
            it.setText(("+" if v > 0 else "") + _k(v))
            it.setForeground(QColor(C["up"] if v > 0 else C["down"] if v < 0 else C["muted"]))
            it.setData(BAR_ROLE, (v / mxc, C["up"] if v > 0 else C["down"], p == "ce"))
        elif key == "ltp":
            pct = x[f"{p}_chg_pct"]
            it.setText(_f(v) + (f"  {pct:+.1f}%" if pct is not None else ""))
            it.setForeground(QColor(C["up"] if x[f"{p}_chg"] > 0 else C["down"] if x[f"{p}_chg"] < 0 else C["text"]))
        elif key == "buildup":
            it.setText(BUILD_SHORT.get(v, ""))
            it.setForeground(QColor(BUILD_COLORS.get(v, C["muted"])))
        elif key == "iv":
            it.setText(_f(v, "{:.1f}"))
        elif key == "delta":
            it.setText(_f(v, "{:+.2f}"))
        elif key == "gamma":
            it.setText(_f(v * 1e4 if v is not None else None, "{:.2f}"))
            it.setToolTip(tip + "\n(Gamma shown ×10,000)")
        elif key in ("theta", "vega"):
            it.setText(_f(v, "{:+.1f}" if key == "theta" else "{:.1f}"))
        elif key == "vol":
            it.setText(_k(v))
        elif key in ("bid", "ask"):
            it.setText(_f(v))
        it.setData(Qt.ItemDataRole.UserRole, v)


class StrikeChart(QWidget):
    """OI, change in OI or IV smile per strike, with spot and max-pain markers."""

    def __init__(self, mode: str):
        super().__init__()
        self.mode = mode
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.r: dict | None = None
        self.rows: list[dict] = []

    def set_data(self, r: dict, rows: list[dict]):
        self.r, self.rows = r, rows
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(C["panel2"]))
        rows, r = self.rows, self.r
        title = {"oi": "Open interest by strike", "chg": "Change in open interest today",
                 "iv": "IV smile (implied volatility by strike)"}[self.mode]
        p.setPen(QColor(C["text"]))
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.drawText(QPointF(10, 18), title)
        p.setFont(self.font())
        if not rows:
            p.setPen(QColor(C["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No option-chain data yet")
            return
        L, R, T, B = 60, 36, 30, self.height() - 34
        W = self.width() - L - R
        lo_k, hi_k = rows[0]["strike"], rows[-1]["strike"]
        span = (hi_k - lo_k) or 1
        xk = lambda k: L + (k - lo_k) / span * W
        n = len(rows)
        bw = max(1.5, W / n * 0.38)
        if self.mode == "iv":
            vals = [v for x in rows for v in (x["ce_iv"], x["pe_iv"]) if v]
            lo_v, hi_v = (min(vals), max(vals)) if vals else (0, 1)
            pad = (hi_v - lo_v) * 0.1 or 1
            lo_v, hi_v = lo_v - pad, hi_v + pad
        elif self.mode == "oi":
            lo_v, hi_v = 0, max(max(x["ce_oi"], x["pe_oi"]) for x in rows) or 1
        else:
            m = max(max(abs(x["ce_chg_oi"]), abs(x["pe_chg_oi"])) for x in rows) or 1
            lo_v, hi_v = -m, m
        yv = lambda v: B - (v - lo_v) / (hi_v - lo_v) * (B - T)
        # grid
        for i in range(5):
            v = lo_v + (hi_v - lo_v) * i / 4
            y = yv(v)
            p.setPen(QPen(QColor(C["border"]), 1))
            p.drawLine(QPointF(L, y), QPointF(L + W, y))
            p.setPen(QColor(C["muted"]))
            p.drawText(QRectF(0, y - 8, L - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{v:.1f}%" if self.mode == "iv" else _k(v))
        # ±1σ band
        if r.get("sigma_move"):
            a, b = xk(max(lo_k, r["spot"] - r["sigma_move"])), xk(min(hi_k, r["spot"] + r["sigma_move"]))
            p.fillRect(QRectF(a, T, b - a, B - T), QColor(56, 189, 248, 18))
        if self.mode in ("oi", "chg"):
            key = "oi" if self.mode == "oi" else "chg_oi"
            for x in rows:
                cx = xk(x["strike"])
                for val, off, col in ((x[f"ce_{key}"], -bw, CE_COLOR), (x[f"pe_{key}"], 0, PE_COLOR)):
                    y0, y1 = yv(0), yv(val)
                    c = QColor(col)
                    if self.mode == "chg" and val < 0:
                        c.setAlpha(120)
                    p.fillRect(QRectF(cx + off, min(y0, y1), bw, abs(y1 - y0)), c)
        else:
            for key, col in (("ce_iv", CE_COLOR), ("pe_iv", PE_COLOR)):
                pts = [(xk(x["strike"]), yv(x[key])) for x in rows if x[key]]
                p.setPen(QPen(QColor(col), 2))
                for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                    p.drawLine(QPointF(ax, ay), QPointF(bx, by))
                for ax, ay in pts:
                    p.drawEllipse(QPointF(ax, ay), 2, 2)
        # strike labels
        every = max(1, n // 12)
        p.setPen(QColor(C["muted"]))
        for i, x in enumerate(rows):
            if i % every == 0:
                cx = xk(x["strike"])
                p.drawText(QRectF(cx - 32, B + 4, 64, 14), Qt.AlignmentFlag.AlignCenter, f"{x['strike']:,.0f}")

        def vline(k, color, label, style, yoff=0):
            if k is None or not lo_k <= k <= hi_k:
                return
            x = xk(k)
            p.setPen(QPen(QColor(color), 1.3, style))
            p.drawLine(QPointF(x, T), QPointF(x, B))
            p.drawText(QRectF(x - 70, T - 2 + yoff, 140, 14), Qt.AlignmentFlag.AlignCenter, label)
        vline(r["spot"], C["accent"], f"spot {r['spot']:,.0f}", Qt.PenStyle.SolidLine)
        if self.mode != "iv":
            vline(r.get("max_pain"), "#facc15", f"max pain {r['max_pain']:,.0f}", Qt.PenStyle.DashLine, 13)
        p.setPen(QColor(C["muted"]))
        legend = {"oi": "■ calls (red) ■ puts (green) · shaded = ±1σ expected move",
                  "chg": "■ calls ■ puts · above 0 = OI added today, below 0 = OI cut",
                  "iv": "● call IV (red) ● put IV (green) · higher put IV on the left = downside skew"}[self.mode]
        p.drawText(QRectF(L, B + 18, W, 14), Qt.AlignmentFlag.AlignLeft, legend)


class ChainView(QWidget):
    """Summary strip + controls + chain table."""

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.r: dict | None = None
        self._centered_for = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)
        self.summary = QLabel("Loading option chain…")
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet(f"QLabel {{ background:{C['panel']}; border:1px solid {C['border']};"
                                   "border-radius:8px; padding:6px 10px; font-size:12px; }")
        lay.addWidget(self.summary)
        self.read = QLabel("")
        self.read.setTextFormat(Qt.TextFormat.RichText)
        self.read.setWordWrap(True)
        self.read.setStyleSheet(f"font-size:11px;color:{C['muted']};padding:0 4px")
        lay.addWidget(self.read)

        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("<b>Strikes</b>"))
        self.rng = QButtonGroup(self)
        for i, (name, n) in enumerate(RANGES):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setChecked(n == 15)
            b.setFixedWidth(46)
            self.rng.addButton(b, n)
            ctl.addWidget(b)
        self.rng.idClicked.connect(lambda _: self.refresh())
        ctl.addSpacing(12)
        ctl.addWidget(QLabel("<b>Columns</b>"))
        self.groups: dict[str, QCheckBox] = {}
        for g, name, on in GROUPS:
            cb = QCheckBox(name)
            cb.setChecked(on)
            cb.toggled.connect(lambda _: self._reconfigure())
            self.groups[g] = cb
            ctl.addWidget(cb)
        ctl.addStretch(1)
        b = QPushButton("⌖ Centre on ATM")
        b.clicked.connect(self.center)
        ctl.addWidget(b)
        lay.addLayout(ctl)

        self.table = ChainTable()
        lay.addWidget(self.table, 1)
        legend = QLabel(
            f"<span style='color:{C['muted']}'>Calls left, puts right. Yellow tint = in the money; blue = ATM; "
            f"bold = top-3 OI strikes; bars = OI / change in OI relative to the largest. Build-up: "
            f"<span style='color:{C['up']}'>Long ↑</span> price↑ OI↑ · <span style='color:{C['down']}'>Short ↑</span>"
            f" price↓ OI↑ (writers adding) · <span style='color:#38bdf8'>Sh. cover</span> price↑ OI↓ · "
            f"<span style='color:#f59e0b'>Long ↓</span> price↓ OI↓. Strike PCR = put OI ÷ call OI "
            f"(green &gt; 1.2, red &lt; 0.8). Gamma ×10,000; theta in ₹ per unit per day. Hover any cell for "
            f"details.</span>")
        legend.setWordWrap(True)
        legend.setStyleSheet("font-size:10px")
        lay.addWidget(legend)
        self._reconfigure()

    def _groups(self) -> set[str]:
        return {g for g, cb in self.groups.items() if cb.isChecked()} | {"core"}

    def _reconfigure(self):
        self.groups["core"].setChecked(True)
        self.groups["core"].setEnabled(False)
        self.table.configure(self._groups())
        self.refresh()

    def rows(self) -> list[dict]:
        r = self.r
        if not r or not r.get("chain"):
            return []
        n = self.rng.checkedId()
        rows = r["chain"]
        i = min(range(len(rows)), key=lambda j: abs(rows[j]["strike"] - r["spot"]))
        return rows[max(i - n, 0): i + n + 1]

    def set_result(self, r: dict):
        self.r = r
        self.refresh()
        key = (r["underlying"], r["expiry"])
        if self._centered_for != key:
            self._centered_for = key
            self.center()

    def center(self):
        row = getattr(self.table, "spot_row", None)
        if row is not None:
            self.table.scrollToItem(self.table.item(row, 0), QAbstractItemView.ScrollHint.PositionAtCenter)

    def refresh(self):
        r = self.r
        if not r:
            return
        rows = self.rows()
        sb = self.table.verticalScrollBar().value()
        self.table.fill(r, rows)
        self.table.verticalScrollBar().setValue(sb)
        self._summary(r)

    def _summary(self, r: dict):
        s = r.get("chain_summary") or {}
        lot = r.get("lot") or 1
        straddle = r.get("straddle")
        mut = C["muted"]

        def cell(k, v, col=C["text"]):
            return (f"<td style='padding:2px 14px 2px 0'><span style='color:{mut};font-size:10px'>{k}</span><br>"
                    f"<b style='color:{col};font-size:13px'>{v}</b></td>")
        pcr = s.get("pcr")
        pcr_col = C["up"] if pcr and pcr > 1.2 else C["down"] if pcr and pcr < 0.8 else C["text"]
        em = r.get("sigma_move")
        cells = [
            cell("Spot", f"{r['spot']:,.2f}", C["accent"]),
            cell("ATM", f"{r['atm']:,.0f}"),
            cell("ATM IV", _f(r.get("atm_iv"), "{:.2f}%")),
            cell("ATM straddle", f"{straddle:,.1f} pts · ₹{straddle * lot:,.0f}" if straddle else "—"),
            cell("Expected move (±1σ)", f"±{em:,.0f} → {r['spot'] - em:,.0f}–{r['spot'] + em:,.0f}" if em else "—"),
            cell("PCR (OI)", _f(pcr), pcr_col),
            cell("PCR (Δ OI)", _f(s.get("pcr_chg"))),
            cell("Max pain", _f(s.get("max_pain"), "{:,.0f}"), "#facc15"),
            cell("Support (max put OI)", _f(s.get("support"), "{:,.0f}"), PE_COLOR),
            cell("Resistance (max call OI)", _f(s.get("resistance"), "{:,.0f}"), CE_COLOR),
            cell("Call OI · Δ", f"{_k(s.get('ce_oi'))} · {_k(s.get('ce_chg_oi'))}"),
            cell("Put OI · Δ", f"{_k(s.get('pe_oi'))} · {_k(s.get('pe_chg_oi'))}"),
            cell("VIX", _f(r.get("vix"))),
            cell("Days left", f"{r['dte']:.1f}"),
            cell("Lot", f"{lot}"),
        ]
        self.summary.setText("<table><tr>" + "".join(cells[:8]) + "</tr><tr>" + "".join(cells[8:]) + "</tr></table>")
        # plain-language read of where writers are positioned
        parts = []
        if s.get("support") and s.get("resistance"):
            parts.append(f"OI range <b>{s['support']:,.0f} – {s['resistance']:,.0f}</b> (largest put and call "
                         "open interest: where writers are most committed).")
        if s.get("sup_add") or s.get("res_add"):
            parts.append("Fresh writing today: " + " · ".join(
                x for x in (f"puts at <b style='color:{PE_COLOR}'>{s['sup_add']:,.0f}</b>" if s.get("sup_add") else "",
                            f"calls at <b style='color:{CE_COLOR}'>{s['res_add']:,.0f}</b>" if s.get("res_add") else "")
                if x) + ".")
        if pcr:
            mood = ("put writers dominate (supportive)" if pcr > 1.2 else
                    "call writers dominate (capping)" if pcr < 0.8 else "balanced")
            parts.append(f"PCR {pcr:.2f}: {mood}.")
        if r.get("skew") is not None:
            parts.append(f"Skew {r['skew']:+.1f} vol pts (put IV − call IV at ±1σ).")
        self.read.setText(" ".join(parts) + f" <span style='color:{mut}'>· {html.escape(str(r.get('timestamp') or ''))}"
                          "</span>")
