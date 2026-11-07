"""Two screens for the market-watch engines (numbers from market_watch.py / watch_service.py;
these classes only draw):

* GlobalDashboard  — world indices, US futures, crude, dollar, yields, rupee and US VIX, the
  read for India's open, and the global alerts raised.
* SignalsDashboard — NIFTY and BANKNIFTY side by side: the latest 15-minute direction call
  with its reasons and writing idea, today's key levels and whether they held, today's calls
  graded, and the level-break alerts.
"""
from __future__ import annotations

import html
from datetime import datetime

import pandas as pd
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QPushButton, QScrollArea, QSizePolicy,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from theme import C, card, label, toolbar

BLOCKS = "▁▂▃▄▅▆▇█"
DIR_STYLE = {"BULLISH": (C["up"], "#052e16"), "BEARISH": (C["down"], "#450a0a"), "SIDEWAYS": (C["warn"], "#3b2306")}


def _esc(s) -> str:
    return html.escape(str(s))


def spark(vals: list[float], n: int = 24) -> str:
    v = [x for x in vals if x is not None]
    if len(v) < 2:
        return ""
    step = max(1, len(v) // n)
    v = v[::step][-n:]
    lo, hi = min(v), max(v)
    return "".join(BLOCKS[int((x - lo) / (hi - lo) * 7)] if hi > lo else BLOCKS[3] for x in v)


def _ist(t) -> str:
    if t is None:
        return "—"
    ts = pd.Timestamp(t)
    ts = (ts if ts.tzinfo else ts.tz_localize("UTC")).tz_convert("Asia/Kolkata")
    return f"{ts:%H:%M}" if ts.date() == datetime.now().date() else f"{ts:%d %b %H:%M}"


def alert_html(items: list[dict], empty: str) -> str:
    if not items:
        return f"<span style='color:{C['muted']}'>{_esc(empty)}</span>"
    rows = []
    for a in items:
        col = (C["down"] if a.get("level") in ("HIGH", "RED") else C["muted"] if a.get("level") == "INFO"
               else C["warn"])
        rows.append(f"<div style='margin-bottom:6px'><span style='color:{C['faint']}'>{_ist(a.get('raised_at'))}</span> "
                    f"<b style='color:{col}'>{_esc(a['title'])}</b><br><span style='color:{C['muted']}'>"
                    f"{_esc(a['text'])}</span></div>")
    return "".join(rows)


# ====================================================================== global markets
class GlobalDashboard(QWidget):
    refresh_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        bar, hl = toolbar()
        self.status = label("Checking world markets…")
        hl.addWidget(self.status, 1)
        b = QPushButton("Check now", objectName="primary")
        b.clicked.connect(self.refresh_requested.emit)
        hl.addWidget(b)
        lay.addWidget(bar)

        top = QHBoxLayout()
        top.setSpacing(8)
        f, l = card("Read for India's next open")
        self.cue = label(f"<span style='color:{C['muted']}'>Waiting for quotes…</span>")
        l.addWidget(self.cue)
        top.addWidget(f, 2)
        f, l = card("Global alerts (24 h)")
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setStyleSheet("QScrollArea{border:none;background:transparent}")
        self.alerts = label()
        self.alerts.setAlignment(Qt.AlignmentFlag.AlignTop)
        sc.setWidget(self.alerts)
        sc.setMinimumHeight(150)
        l.addWidget(sc)
        top.addWidget(f, 3)
        lay.addLayout(top)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Group", "Market", "Last", "Change", "Change %", "Today", "As of (IST)"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = self.table.horizontalHeader()
        for i in range(7):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.table, 1)
        lay.addWidget(label(f"<span style='color:{C['muted']}'>GIFT Nifty from sgxnifty.org, the rest from Yahoo Finance, checked every 5 min around the "
                            "clock. Change is against each market's previous close (US 10Y in basis points). An alert "
                            "fires when a move passes the threshold shown in the tooltip, again at 2× and 3×, and once "
                            "for a broad move (4+ markets ≥1% the same way). The open read weights GIFT Nifty most "
                            "(its gap vs NIFTY's close includes the futures premium), then US futures, Asia, the rupee, the dollar, crude and yields — context for the session, not a forecast."
                            "</span>", 11))

    def set_status(self, text: str):
        self.status.setText(text)

    def set_data(self, quotes: list[dict], cue: dict, alerts: list[dict]):
        s = cue.get("score", 0)
        col = C["up"] if s >= 25 else C["down"] if s <= -25 else C["warn"]
        drivers = "".join(f"<li>{_esc(d)}</li>" for d in cue.get("drivers", []))
        self.cue.setText(f"<span style='font-size:22px;font-weight:800;color:{col}'>{_esc(cue.get('read', '—'))}</span>"
                         f" <span style='color:{C['muted']}'>score {s:+d} (−100…+100)</span><br>"
                         f"<b>Writer read:</b> {_esc(cue.get('writer', ''))}"
                         + (f"<ul style='margin:4px 0 0 -20px'>{drivers}</ul>" if drivers else ""))
        self.alerts.setText(alert_html(alerts, "No major global moves in the last 24 hours."))
        t = self.table
        t.setRowCount(len(quotes))
        bold = QFont(self.font())
        bold.setBold(True)
        for r, q in enumerate(quotes):
            up = (q.get("chg") or 0) >= 0
            c = QColor(C["up"] if up else C["down"])
            if q.get("unit") == "bps":
                chg, pct = (f"{q['chg'] * 100:+.0f} bps" if q.get("chg") is not None else "—"), \
                    (f"{q['pct']:+.2f}%" if q.get("pct") is not None else "—")
                last = f"{q['last']:.3f}%" if q.get("last") is not None else "—"
            else:
                chg = f"{q['chg']:+,.2f}" if q.get("chg") is not None else "—"
                pct = f"{q['pct']:+.2f}%" if q.get("pct") is not None else "—"
                last = f"{q['last']:,.2f}" if q.get("last") is not None else "—"
            big = q.get("move") is not None and abs(q["move"]) >= q.get("threshold", 99)
            cells = [q["group"], q["name"], last, chg, pct, spark(q.get("spark") or []),
                     _ist(q.get("at")) + (" · stored" if q.get("offline") else "")]
            for ci, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if ci in (2, 3, 4):
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if ci in (3, 4, 5):
                    it.setForeground(c)
                if ci in (0, 6):
                    it.setForeground(QColor(C["muted"]))
                if big and ci in (1, 4):
                    it.setFont(bold)
                    if ci == 4:
                        it.setBackground(QColor("#052e16" if up else "#450a0a"))
                it.setToolTip(f"{q['name']} ({q.get('ticker')}) · alert threshold {q.get('threshold')}"
                              f"{' bps' if q.get('unit') == 'bps' else '%'} · session {q.get('session')}")
                t.setItem(r, ci, it)


# ====================================================================== direction & levels
def _grow(lbl):
    """Rich-text tables mis-report their height to the layout; let these labels take what they need."""
    lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
    return lbl


class SignalColumn(QWidget):
    def __init__(self, name: str):
        super().__init__()
        self.setObjectName("root")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        f, l = card(f"{name} · direction call (every 15 min)")
        self.call = label(f"<span style='color:{C['muted']}'>The first call is made at 9:30 am; calls run every "
                          "15 minutes until 3:15 pm.</span>")
        l.addWidget(self.call)
        lay.addWidget(f)
        f, l = card(f"{name} · key levels today")
        self.levels = _grow(label(f"<span style='color:{C['muted']}'>Loading levels…</span>"))
        l.addWidget(self.levels)
        lay.addWidget(f)
        f, l = card(f"{name} · calls today")
        self.history = _grow(label())
        l.addWidget(self.history)
        lay.addWidget(f)
        lay.addStretch(1)

    def _fit(self):
        """Box layouts ignore rich-text height-for-width; pin each label's height to its content."""
        for lbl in (self.call, self.levels, self.history):
            w = lbl.width() if lbl.width() > 50 else max(self.width() - 30, 300)
            lbl.setMinimumHeight(max(lbl.heightForWidth(w), 0))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit()

    def set_call(self, c: dict | None):
        if not c:
            return
        fg, bg = DIR_STYLE.get(c["dir"], (C["text"], C["panel"]))
        sig = "".join(
            f"<tr><td width='26' style='color:{C['up'] if s['score'] > 0 else C['down'] if s['score'] < 0 else C['faint']}'>"
            f"{s['score']:+d}</td><td style='color:{C['muted']}'>{_esc(s['text'])}</td></tr>" for s in c["signals"])
        src = {"shoonya": "Shoonya real-time", "nse": "NSE", "offline": "stored chain"}.get(c.get("source"), c.get("source"))
        self.call.setText(
            f"<span style='background:{bg};color:{fg};font-size:20px;font-weight:800;padding:2px 10px'>"
            f"&nbsp;{_esc(c['direction'])}&nbsp;</span> <span style='color:{C['muted']}'>confidence <b style='color:"
            f"{C['text']}'>{c['confidence']}</b> · score {c['score']:+d} · {c['slot'][-5:]} · spot {c['spot']:,.1f} · "
            f"{_esc(src)}</span><div style='margin-top:6px'><b>Writing idea:</b> {_esc(c['idea'])}</div>"
            f"<table cellspacing='0' cellpadding='2' style='margin-top:4px'>{sig}</table>")
        self._fit()

    def set_levels(self, levels: list[dict], status: dict, spot: float | None):
        if not levels:
            self.levels.setText(f"<span style='color:{C['muted']}'>No levels yet (needs daily history or a chain).</span>")
            return
        rows = []
        placed = spot is None
        for m in levels:
            if not placed and m["price"] < spot:
                rows.append(f"<tr><td align='right' style='color:{C['accent']}'><b>{spot:,.1f}</b></td><td style='color:{C['accent']}'>"
                            f"◀ spot</td><td></td><td></td></tr>")
                placed = True
            st = status.get(round(m["price"]))
            st_html = (f"<b style='color:{C['down'] if 'below' in st else C['up']}'>{_esc(st)}</b>" if st else
                       f"<span style='color:{C['faint']}'>holding</span>")
            name = m["name"] + (f" + {', '.join(m['also'])}" if m.get("also") else "")
            dist = (spot - m["price"]) / spot * 100 if spot else m.get("dist_pct", 0)
            rows.append(f"<tr><td align='right'><b>{m['price']:,.0f}</b></td><td style='color:{C['muted']}'>"
                        f"{_esc(name)}</td><td align='right' style='color:{C['faint']}'>{-dist:+.2f}%</td>"
                        f"<td>{st_html}</td></tr>")
        if not placed:
            rows.append(f"<tr><td align='right' style='color:{C['accent']}'><b>{spot:,.1f}</b></td><td style='color:{C['accent']}'>"
                            f"◀ spot</td><td></td><td></td></tr>")
        self.levels.setText(f"<table width='100%' cellspacing='0' cellpadding='3'>{''.join(rows)}</table>")
        self._fit()

    def set_history(self, calls: list[dict], hits: tuple[int, int]):
        if not calls:
            self.history.setText(f"<span style='color:{C['muted']}'>No calls yet today.</span>")
            return
        oc = {"right": (C["up"], "✓"), "wrong": (C["down"], "✗"), "flat": (C["faint"], "·")}
        rows = []
        for c in reversed(calls):
            col, mark = oc.get(c.get("outcome"), (C["faint"], "…"))
            fg = DIR_STYLE.get(c["dir"], (C["text"], ""))[0]
            after = f"{c['spot_after']:,.1f}" if c.get("spot_after") else "—"
            rows.append(f"<tr><td>{c['slot'][-5:]}</td><td style='color:{fg}'><b>{_esc(c['dir'])}</b></td>"
                        f"<td align='right'>{c['score']:+d}</td><td align='right'>{c['spot']:,.1f}</td>"
                        f"<td align='right' style='color:{C['muted']}'>{after}</td><td style='color:{col}'>{mark}</td></tr>")
        r, n = hits
        head = (f"<div style='color:{C['muted']}'>Graded at the next call: <b style='color:{C['text']}'>{r}/{n}</b>"
                f" right{f' ({r / n * 100:.0f}%)' if n else ''}</div>")
        self.history.setText(head + "<table width='100%' cellspacing='0' cellpadding='2'><tr style='color:"
                             f"{C['faint']}'><td>Time</td><td>Call</td><td align='right'>Score</td><td align='right'>"
                             "Spot</td><td align='right'>15 min later</td><td></td></tr>" + "".join(rows) + "</table>")
        self._fit()


class SignalsDashboard(QWidget):
    refresh_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        bar, hl = toolbar()
        self.status = label("Direction calls run every 15 minutes from 9:30 am; levels are checked on every "
                            "5-minute close.")
        hl.addWidget(self.status, 1)
        b = QPushButton("Make a call now", objectName="primary")
        b.setToolTip("Runs a direction call immediately (it is also stored and graded)")
        b.clicked.connect(self.refresh_requested.emit)
        hl.addWidget(b)
        lay.addWidget(bar)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        inner = QWidget(objectName="root")
        v = QVBoxLayout(inner)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.cols = {"NIFTY": SignalColumn("NIFTY"), "BANKNIFTY": SignalColumn("BANKNIFTY")}
        for c in self.cols.values():
            row.addWidget(c, 1)
        v.addLayout(row)
        f, l = card("Level breaks and writer signals today")
        self.alerts = label()
        l.addWidget(self.alerts)
        v.addWidget(f)
        v.addWidget(label(f"<span style='color:{C['muted']}'>Levels: previous-day high/low, the 9:15–9:30 opening "
                          "range, the daily support/resistance zones and the option-chain walls; a break needs a "
                          "5-minute close 0.05% through the level, and alerts once a day per direction. Direction "
                          "calls score the option chain now vs ~15 min ago (OI added near the money, PCR, wall "
                          "shifts, ATM IV), spot momentum, level breaks and the daily trend: ≥ +3 BULLISH, ≤ −3 "
                          "BEARISH, otherwise SIDEWAYS. Writer signals (with each risk check): profit targets, short-leg "
                          "delta, VIX / ATM IV spikes, expiry-day gamma, expected move, futures buildup, writing "
                          "conditions, OI wall shifts, PCR / skew, max pain, time-based exits and discipline. "
                          "Mechanical reads on exchange data — not a forecast or "
                          "investment advice.</span>", 11))
        v.addStretch(1)
        sc.setWidget(inner)
        lay.addWidget(sc, 1)

    def set_status(self, text: str):
        self.status.setText(text)

    def set_alerts(self, items: list[dict]):
        self.alerts.setText(alert_html(items, "No level breaks or writer signals today."))
