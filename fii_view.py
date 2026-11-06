"""FII / DII screen: institutional positioning read, option-writing posture with
strike zones, the walk-forward track record, every signal, flow charts and the
daily data.  Numbers come from fii_analysis.py; this file only draws."""
from __future__ import annotations

import html

import pandas as pd
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView,
                               QPushButton, QScrollArea, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from theme import C, card, label, toolbar

DISCLAIMER = ("Rule-based screening of published FII/DII data, not investment advice. Institutional flows are one "
              "input among many; positioning can change overnight and events override it. Size every trade for "
              "the loss you can take.")


class DailyChart(QWidget):
    """Bars and lines over session dates, left and right axes, hover readout."""

    def __init__(self, title: str, height: int = 230):
        super().__init__()
        self.title = title
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.series: list[dict] = []
        self.dates: list = []
        self.hover: int | None = None

    def set_series(self, dates, series: list[dict]):
        """series: {name, values (list aligned to dates), kind 'bar'|'line', color, axis 'left'|'right',
        fmt callable, neg_color?}"""
        self.dates, self.series = list(dates), series
        self.update()

    def mouseMoveEvent(self, e):
        n = len(self.dates)
        if n:
            L, R = 64, 64
            w = max(self.width() - L - R, 1)
            i = int((e.position().x() - L) / w * n)
            self.hover = i if 0 <= i < n else None
            self.update()

    def leaveEvent(self, _):
        self.hover = None
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(C["panel"]))
        f = QFont(self.font())
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(C["text"]))
        p.drawText(QPointF(10, 17), self.title)
        p.setFont(self.font())
        n = len(self.dates)
        if not n or not self.series:
            p.setPen(QColor(C["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No data yet — sync FII/DII")
            return
        L, R, T, B = 64, 64, 40, self.height() - 22
        W = self.width() - L - R
        rng = {}
        for ax in ("left", "right"):
            vals = [v for s in self.series if s.get("axis", "left") == ax for v in s["values"] if v is not None
                    and not pd.isna(v)]
            if not vals:
                continue
            lo, hi = min(vals), max(vals)
            if any(s.get("kind") == "bar" for s in self.series if s.get("axis", "left") == ax):
                lo, hi = min(lo, 0), max(hi, 0)
            pad = (hi - lo) * 0.08 or abs(hi) * 0.1 or 1
            rng[ax] = (lo - pad, hi + pad)

        def y(ax, v):
            lo, hi = rng[ax]
            return B - (v - lo) / (hi - lo) * (B - T)
        x = lambda i: L + (i + 0.5) * W / n
        # grid + axes
        for ax, side in (("left", 0), ("right", 1)):
            if ax not in rng:
                continue
            lo, hi = rng[ax]
            fmt = next((s.get("fmt") for s in self.series if s.get("axis", "left") == ax and s.get("fmt")), None) \
                or (lambda v: f"{v:,.0f}")
            for k in range(5):
                v = lo + (hi - lo) * k / 4
                yy = y(ax, v)
                if side == 0:
                    p.setPen(QPen(QColor(C["border"]), 1))
                    p.drawLine(QPointF(L, yy), QPointF(L + W, yy))
                p.setPen(QColor(C["muted"]))
                r = QRectF(0, yy - 8, L - 6, 16) if side == 0 else QRectF(L + W + 6, yy - 8, R - 6, 16)
                p.drawText(r, (Qt.AlignmentFlag.AlignRight if side == 0 else Qt.AlignmentFlag.AlignLeft)
                           | Qt.AlignmentFlag.AlignVCenter, fmt(v))
            if lo < 0 < hi and side == 0:
                p.setPen(QPen(QColor(C["faint"]), 1))
                p.drawLine(QPointF(L, y(ax, 0)), QPointF(L + W, y(ax, 0)))
        bars = [s for s in self.series if s.get("kind") == "bar"]
        bw = max(1.0, W / n * 0.8 / max(len(bars), 1))
        for bi, s in enumerate(bars):
            ax = s.get("axis", "left")
            for i, v in enumerate(s["values"]):
                if v is None or pd.isna(v):
                    continue
                col = QColor(s.get("neg_color") if v < 0 and s.get("neg_color") else s["color"])
                col.setAlpha(200)
                x0 = x(i) - bw * len(bars) / 2 + bi * bw
                p.fillRect(QRectF(x0, min(y(ax, 0), y(ax, v)), bw, abs(y(ax, v) - y(ax, 0))), col)
        for s in self.series:
            if s.get("kind") != "line":
                continue
            ax = s.get("axis", "left")
            p.setPen(QPen(QColor(s["color"]), s.get("width", 1.8), s.get("style", Qt.PenStyle.SolidLine)))
            prev = None
            for i, v in enumerate(s["values"]):
                if v is None or pd.isna(v):
                    prev = None
                    continue
                pt = QPointF(x(i), y(ax, v))
                if prev is not None:
                    p.drawLine(prev, pt)
                prev = pt
        # date labels
        p.setPen(QColor(C["muted"]))
        every = max(1, n // 8)
        for i in range(0, n, every):
            p.drawText(QRectF(x(i) - 30, B + 4, 60, 14), Qt.AlignmentFlag.AlignCenter, f"{self.dates[i]:%d %b}")
        # legend
        lx = L
        for s in self.series:
            p.fillRect(QRectF(lx, 24, 10, 8), QColor(s["color"]))
            p.setPen(QColor(C["muted"]))
            txt = s["name"]
            p.drawText(QPointF(lx + 14, 32), txt)
            lx += 22 + p.fontMetrics().horizontalAdvance(txt)
        # hover readout
        if self.hover is not None:
            i = self.hover
            p.setPen(QPen(QColor(C["faint"]), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x(i), T), QPointF(x(i), B))
            parts = [f"{self.dates[i]:%a %d %b}"]
            for s in self.series:
                v = s["values"][i]
                parts.append(f"{s['name']}: {'—' if v is None or pd.isna(v) else (s.get('fmt') or (lambda z: f'{z:,.0f}'))(v)}")
            txt = "   ".join(parts)
            w = p.fontMetrics().horizontalAdvance(txt) + 12
            bx = min(max(x(i) - w / 2, L), L + W - w)
            p.fillRect(QRectF(bx, T - 2, w, 16), QColor(11, 15, 23, 235))
            p.setPen(QColor(C["text"]))
            p.drawText(QRectF(bx, T - 2, w, 16), Qt.AlignmentFlag.AlignCenter, txt)


def _cr(v):
    return "—" if v is None or pd.isna(v) else f"{v:+,.0f}"


def _k(v):
    return "—" if v is None or pd.isna(v) else (f"{v / 1000:+,.0f}K" if abs(v) >= 1000 else f"{v:+,.0f}")


class FIIDashboard(QWidget):
    sync_requested = Signal()
    import_requested = Signal(str)
    zones_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.res: dict | None = None
        self.zone_res: dict[str, dict] = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        bar, hl = toolbar()
        self.fresh = label("")
        hl.addWidget(self.fresh, 1)
        b = QPushButton("Import CSV…")
        b.setToolTip("Import older FII/DII history (date, fii_cash_net, dii_cash_net, …)")
        b.clicked.connect(self._import)
        hl.addWidget(b)
        self.sync_btn = QPushButton("⟳ Check for new data", objectName="primary")
        self.sync_btn.clicked.connect(self.sync_requested.emit)
        hl.addWidget(self.sync_btn)
        outer.addWidget(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(objectName="root")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(8)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        f, l = card("Evening setup check · institutional squeeze loading")
        self.setup_lbl = label(f"<span style='color:{C['muted']}'>Waiting for FII/DII data…</span>")
        l.addWidget(self.setup_lbl)
        lay.addWidget(f)

        top = QHBoxLayout()
        top.setSpacing(8)
        f, l = card("Positioning read → option-writing posture")
        self.read = label()
        l.addWidget(self.read)
        l.addStretch(1)
        top.addWidget(f, 5)
        f, l = card("Strike zones (from the live option chain)")
        self.zones = label()
        l.addWidget(self.zones)
        self.zone_btn = QPushButton("Load option chains")
        self.zone_btn.clicked.connect(self.zones_requested.emit)
        l.addWidget(self.zone_btn, 0, Qt.AlignmentFlag.AlignLeft)
        l.addStretch(1)
        top.addWidget(f, 4)
        f, l = card("Track record on this data")
        self.evid = label()
        l.addWidget(self.evid)
        l.addStretch(1)
        top.addWidget(f, 3)
        lay.addLayout(top)

        self.warn = label()
        lay.addWidget(self.warn)

        f, l = card("Signals")
        self.sig = QTableWidget(0, 5)
        self.sig.setHorizontalHeaderLabels(["Signal", "Latest", "Detail", "Reading", "Score"])
        self.sig.verticalHeader().setVisible(False)
        self.sig.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.sig.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self.sig.horizontalHeader()
        for i, m in enumerate((QHeaderView.ResizeMode.ResizeToContents, QHeaderView.ResizeMode.ResizeToContents,
                               QHeaderView.ResizeMode.Stretch, QHeaderView.ResizeMode.Stretch,
                               QHeaderView.ResizeMode.ResizeToContents)):
            hh.setSectionResizeMode(i, m)
        self.sig.verticalHeader().setDefaultSectionSize(28)
        self.sig.setMinimumHeight(8 * 28 + 34)
        l.addWidget(self.sig)
        lay.addWidget(f)

        grid = QGridLayout()
        grid.setSpacing(8)
        self.ch_cash = DailyChart("Cash market: FII and DII net buying (₹ cr) · FII 20-session total")
        self.ch_fut = DailyChart("FII index futures: long share (%) and net contracts")
        self.ch_opt = DailyChart("FII index options: calls net and puts net (contracts)")
        self.ch_score = DailyChart("Positioning score (−100…+100) vs NIFTY")
        for i, ch in enumerate((self.ch_cash, self.ch_fut, self.ch_opt, self.ch_score)):
            f, l = card()
            l.addWidget(ch)
            grid.addWidget(f, i // 2, i % 2)
        lay.addLayout(grid)

        f, l = card("Daily data (latest 30 sessions)")
        self.tbl = QTableWidget(0, 11)
        self.tbl.setHorizontalHeaderLabels(["Date", "FII cash", "DII cash", "FII fut long %", "FII fut net",
                                            "FII calls net", "FII puts net", "Client long %", "PCR", "Score", "NIFTY"])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tbl.setMinimumHeight(30 * 24 + 30)
        l.addWidget(self.tbl)
        lay.addWidget(f)
        lay.addWidget(label(f"<span style='color:{C['muted']}'>{html.escape(DISCLAIMER)}</span>", 11))

    # ------------------------------------------------------------ inputs
    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "FII/DII history CSV", "", "CSV (*.csv)")
        if path:
            self.import_requested.emit(path)

    def set_freshness(self, text: str, busy: bool):
        self.fresh.setText(text)
        self.sync_btn.setEnabled(not busy)
        self.sync_btn.setText("Checking…" if busy else "⟳ Check for new data")

    def set_setup(self, s: dict | None, confirmations: list[dict] | None = None):
        """The once-per-evening setup: conditions, chain confirmation, track record, directive."""
        if not s:
            return
        mut = C["muted"]
        d = s["direction"]
        conds = "".join(f"<div>{'✅' if ok else '▫️'} {html.escape(name)}: <b>{html.escape(val)}</b></div>"
                        for name, val, ok in s["conditions"])
        conf = "".join(f"<div>{'✅' if c['confirmed'] else '▫️'} {html.escape(c['text'])}</div>"
                       for c in confirmations or [])
        rec = s.get("record") or {}
        key = d or s["lean"]
        g, a = rec.get(key) or {}, rec.get("all") or {}
        if g.get("n"):
            track = (f"After this {'up' if key == 'up' else 'down'} setup (<b>{g['n']}</b> times: "
                     f"{', '.join(g['dates'])}), BANKNIFTY closed <b>up next session {g['up']:.0f}%</b> of the time "
                     f"(avg {g['avg']:+.2f}%, gap-up {g['gap_up']:.0f}%, {g['big']} moves of ±1.5% or more) vs "
                     f"{a.get('up', 0):.0f}% up / avg {a.get('avg', 0):+.2f}% across all {a.get('n', 0)} sessions."
                     + (" <span style='color:#fdba74'>Very small sample — a lean, not a forecast.</span>"
                        if g["n"] < 15 else ""))
        else:
            track = f"<span style='color:{mut}'>This setup has not appeared in the stored history yet.</span>"
        if d:
            col, bg = ("#22c55e", "#052e16") if d == "up" else ("#ef4444", "#450a0a")
            head = (f"<div style='background:{bg};border-left:4px solid {col};padding:6px 10px'>"
                    f"<b style='color:{col};font-size:15px'>{'▲ UP' if d == 'up' else '▼ DOWN'} SETUP ACTIVE</b>"
                    f" <span style='color:{mut}'>for {s['date']:%a %d %b} close</span></div>")
            directive = ("Option writers: short <b>calls</b> are the exposed side — no naked calls overnight; hedge "
                         "or move them beyond 1.5σ; put writing below OI support fits the lean." if d == "up" else
                         "Option writers: short <b>puts</b> are the exposed side — no naked puts overnight; hedge "
                         "or move them beyond 1.5σ; call writing above OI resistance fits the lean.")
        else:
            head = (f"<b style='color:{mut}'>No setup for {s['date']:%a %d %b}</b> "
                    f"<span style='color:{mut}'>(nearest: {s['lean']} — conditions below)</span>")
            directive = ""
        self.setup_lbl.setText(
            head + f"<table width='100%'><tr><td width='50%' valign='top'>{conds}</td><td valign='top'>"
            f"{conf or f'<span style=\'color:{mut}\'>Closing option chain not loaded yet.</span>'}</td></tr></table>"
            f"<div style='margin-top:4px'>{track}</div>"
            + (f"<div style='margin-top:4px'><b>{directive}</b></div>" if directive else "")
            + f"<div style='color:{mut};font-size:11px'>Checked each evening once NSE publishes FII/DII data "
              "(≈5–9 pm IST); you get one alert per session when it fires.</div>")

    def set_zone_results(self, results: dict[str, dict]):
        self.zone_res.update({k: v for k, v in results.items() if v})
        self._render_zones()

    def set_result(self, res: dict):
        self.res = res
        if not res.get("available"):
            self.read.setText("No FII/DII data stored yet — click <b>Check for new data</b>.")
            return
        self._render_read()
        self._render_zones()
        self._render_evidence()
        self._render_signals()
        self._render_charts()
        self._render_table()

    # ------------------------------------------------------------ render
    def _render_read(self):
        r = self.res
        pb = r["playbook"]
        sc = r["score"]
        pos = (sc + 100) / 200 * 100
        gauge = (f"<table width='100%' cellspacing='0' cellpadding='0' style='margin:4px 0'><tr>"
                 f"<td width='{max(pos, 1):.0f}%' style='background:{r['color']};height:6px'></td>"
                 f"<td style='background:{C['border']};height:6px'></td></tr></table>")
        agree = f" · {r['agreement']:.0f}% of active signals agree" if r.get("agreement") is not None else ""
        self.read.setText(
            f"<div><span style='font-size:22px;font-weight:800;color:{r['color']}'>{r['bias']}</span>"
            f"&nbsp; <span style='font-size:15px;font-weight:700'>{sc:+.0f}</span>"
            f"<span style='color:{C['muted']}'> / 100 · conviction <b>{r['conviction']}</b>{agree} · "
            f"data to {r['as_of']:%a %d %b %Y} ({r['sessions']} sessions)</span></div>{gauge}"
            f"<div style='margin-top:6px'><b style='color:{C['accent']}'>Writing side:</b> <b>{pb['side']}</b> — "
            f"{pb['skew']}</div>"
            f"<div style='margin-top:4px'><b>Structures that fit:</b> {' · '.join(pb['structures'])}</div>"
            f"<div style='margin-top:4px;color:#fdba74'><b>Avoid:</b> {pb['avoid']}</div>")
        w = r["warnings"]
        self.warn.setText("".join(f"<div style='background:#3b1d0a;border:1px solid {C['warn']};border-radius:6px;"
                                  f"padding:6px 10px;margin:2px 0;color:#fde68a'>⚠ {html.escape(x)}</div>" for x in w))

    def _render_zones(self):
        from fii_analysis import strike_zones
        r = self.res
        if not r or not r.get("available"):
            return
        rows = []
        for und in ("NIFTY", "BANKNIFTY"):
            z = strike_zones(r["bias"], self.zone_res.get(und))
            if not z:
                rows.append(f"<tr><td><b>{und}</b></td><td colspan='3' style='color:{C['muted']}'>option chain not "
                            "loaded</td></tr>")
                continue
            put = (f"<b style='color:{C['up']}'>{z['put']:,.0f} PE</b> <span style='color:{C['muted']}'>"
                   f"({z['put_dist']:+.1f}%) · hedge {z['put_hedge']:,.0f}</span>") if z["use_put"] else \
                f"<span style='color:{C['muted']}'>skip / far only (≤0.10Δ)</span>"
            call = (f"<b style='color:{C['down']}'>{z['call']:,.0f} CE</b> <span style='color:{C['muted']}'>"
                    f"({z['call_dist']:+.1f}%) · hedge {z['call_hedge']:,.0f}</span>") if z["use_call"] else \
                f"<span style='color:{C['muted']}'>skip / far only (≤0.10Δ)</span>"
            rows.append(f"<tr><td><b>{und}</b><br><span style='color:{C['muted']}'>{z['spot']:,.0f} · {z['expiry']} · "
                        f"{z['dte']:.0f}d · 1σ {z['sigma']:,.0f}</span></td><td>{put}</td><td>{call}</td></tr>")
        self.zones.setText(
            "<table width='100%' cellspacing='0' cellpadding='4'><tr style='color:#94a3b8'><td></td><td>Put side "
            "(short / wing)</td><td>Call side (short / wing)</td></tr>" + "".join(rows) + "</table>"
            f"<div style='color:{C['muted']};font-size:11px;margin-top:4px'>Short strike = the farther of the "
            "bias-scaled expected move (0.8–1.4σ) and the OI wall on that side; wing ≈ 0.5σ beyond. Check liquidity "
            "and premium on the Option chain screen before using a zone.</div>")
        self.zone_btn.setVisible(len(self.zone_res) < 2)

    def _render_evidence(self):
        e = self.res.get("evidence") or {}
        if not e:
            self.evid.setText("Needs NIFTY daily history.")
            return

        def row(k, name):
            g = e.get(k) or {}
            if not g.get("n"):
                return f"<tr><td>{name}</td><td colspan='3' style='color:{C['muted']}'>no cases yet</td></tr>"
            return (f"<tr><td>{name} <span style='color:{C['muted']}'>({g['n']})</span></td>"
                    f"<td align='right'>{g['put_safe']:.0f}%</td><td align='right'>{g['call_safe']:.0f}%</td>"
                    f"<td align='right'>{g['range']:.0f}%</td></tr>")
        cur = self.res["score"]
        now = "bullish" if cur >= 10 else "bearish" if cur <= -10 else "neutral"
        self.evid.setText(
            "<table width='100%' cellspacing='0' cellpadding='3'><tr style='color:#94a3b8'><td>After a reading of…</td>"
            "<td align='right'>put side safe</td><td align='right'>call side safe</td><td align='right'>both</td></tr>"
            + row("bullish", "Bullish (≥ +10)") + row("neutral", "Neutral") + row("bearish", "Bearish (≤ −10)") +
            "</table>"
            f"<div style='color:{C['muted']};font-size:11px;margin-top:4px'>“Safe” = NIFTY did not move more than 1σ "
            f"(5-session) against that side over the next 5 sessions. Today's reading is <b>{now}</b>. "
            f"Walk-forward on {e.get('all', {}).get('n', 0)} sessions of stored data — a small sample; treat as a "
            "sanity check, not a promise.</div>")

    def _render_signals(self):
        comps = self.res["components"]
        t = self.sig
        t.setRowCount(len(comps))
        for i, c in enumerate(comps):
            sc = c["score"]
            col = C["up"] if sc > 0.25 else C["down"] if sc < -0.25 else C["muted"]
            cells = [c["label"] + (f"  ×{c['weight']:g}" if c["weight"] != 1 else ""), c["value"], c["detail"],
                     c["reading"], f"{'▮' * int(round(abs(sc) * 2)) or '·'}  {sc:+.1f}"]
            for j, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if j in (3, 4):
                    it.setForeground(QColor(col))
                if j == 0:
                    it.setToolTip(f"weight {c['weight']}")
                t.setItem(i, j, it)

    def _render_charts(self):
        f = self.res["frame"].tail(90)
        d = list(f.index)
        self.ch_cash.set_series(d, [
            {"name": "FII", "values": list(f["fii_cash"]), "kind": "bar", "color": C["fii"], "neg_color": "#1e40af",
             "fmt": lambda v: f"{v:,.0f}"},
            {"name": "DII", "values": list(f["dii_cash"]), "kind": "bar", "color": C["dii"], "neg_color": "#92400e",
             "fmt": lambda v: f"{v:,.0f}"},
            {"name": "FII 20-session total", "values": list(f["fii_cash_20d"]), "kind": "line", "color": "#e5e7eb",
             "axis": "right", "fmt": lambda v: f"{v / 1000:,.0f}K"}])
        self.ch_fut.set_series(d, [
            {"name": "Net contracts", "values": list(f["fii_fut_net"]), "kind": "bar", "color": C["up"],
             "neg_color": C["down"], "fmt": lambda v: f"{v / 1000:,.0f}K"},
            {"name": "Long share %", "values": list(f["fii_long_pct"]), "kind": "line", "color": C["accent"],
             "axis": "right", "fmt": lambda v: f"{v:.1f}%"}])
        self.ch_opt.set_series(d, [
            {"name": "Calls net", "values": list(f["fii_call_net"]), "kind": "line", "color": C["down"],
             "fmt": lambda v: f"{v / 1000:,.0f}K"},
            {"name": "Puts net", "values": list(f["fii_put_net"]), "kind": "line", "color": C["up"],
             "fmt": lambda v: f"{v / 1000:,.0f}K"},
            {"name": "Tilt (calls − puts)", "values": list(f["fii_opt_tilt"]), "kind": "line", "color": "#e879f9",
             "style": Qt.PenStyle.DashLine, "fmt": lambda v: f"{v / 1000:,.0f}K"}])
        series = [{"name": "Score", "values": list(f["score"]), "kind": "bar", "color": C["up"],
                   "neg_color": C["down"], "fmt": lambda v: f"{v:+.0f}"}]
        if "nifty" in f:
            series.append({"name": "NIFTY", "values": list(f["nifty"]), "kind": "line", "color": "#e5e7eb",
                           "axis": "right", "fmt": lambda v: f"{v:,.0f}"})
        self.ch_score.set_series(d, series)

    def _render_table(self):
        f = self.res["frame"].tail(30).iloc[::-1]
        t = self.tbl
        t.setRowCount(len(f))
        for i, (ts, r) in enumerate(f.iterrows()):
            vals = [f"{ts:%a %d %b}", _cr(r["fii_cash"]), _cr(r["dii_cash"]),
                    "—" if pd.isna(r["fii_long_pct"]) else f"{r['fii_long_pct']:.1f}", _k(r["fii_fut_net"]),
                    _k(r["fii_call_net"]), _k(r["fii_put_net"]),
                    "—" if pd.isna(r["client_long_pct"]) else f"{r['client_long_pct']:.1f}",
                    "—" if pd.isna(r["pcr"]) else f"{r['pcr']:.2f}", f"{r['score']:+.0f}",
                    "—" if "nifty" not in r or pd.isna(r["nifty"]) else f"{r['nifty']:,.0f}"]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if j in (1, 2) and not pd.isna(r["fii_cash" if j == 1 else "dii_cash"]):
                    it.setForeground(QColor(C["up"] if r["fii_cash" if j == 1 else "dii_cash"] > 0 else C["down"]))
                if j == 9:
                    it.setForeground(QColor(C["up"] if r["score"] >= 10 else C["down"] if r["score"] <= -10
                                            else C["text"]))
                t.setItem(i, j, it)
