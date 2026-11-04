"""Trades & P&L tab: live positions with management alerts, orders, fills,
strategy history, performance analytics, a "why I lost" diagnosis and a trade
journal.  All numbers come from trades_engine.py; this file only draws."""
from __future__ import annotations

import html

import pandas as pd
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from theme import C
from options_view import _card, _label
from trades_engine import _bucket_verdict as te_bucket

PERIODS = [("Today", 0), ("Last 7 days", 7), ("Last 30 days", 30), ("Last 90 days", 90),
           ("This year", -1), ("All time", None)]
JOURNAL_TAGS = ["Followed plan", "Good setup", "Lucky win", "No stop", "Moved / ignored stop", "Oversized",
                "FOMO entry", "Revenge trade", "Ignored event", "Against trend", "Booked too early",
                "Held to expiry", "Averaged a loser", "Adjusted / rolled"]
ALERT_STYLE = {"stop": (C["down"], "■ EXIT"), "book": (C["up"], "■ BOOK"), "warn": ("#f59e0b", "■ WATCH")}


def rs(v, sign=True) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    s = f"₹{abs(v):,.0f}"
    return (("+" if v > 0 else "−" if v < 0 else "") + s) if sign else s


def pnl_color(v) -> str:
    return C["up"] if (v or 0) > 0 else C["down"] if (v or 0) < 0 else C["text"]


class Num(QTableWidgetItem):
    def __init__(self, text: str, value=None, color: str | None = None):
        super().__init__(text)
        self.setData(Qt.ItemDataRole.UserRole, float("-inf") if value is None else float(value))
        if color:
            self.setForeground(QColor(color))
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        return self.data(Qt.ItemDataRole.UserRole) < other.data(Qt.ItemDataRole.UserRole)


def make_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSortingEnabled(True)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def fill_table(t: QTableWidget, rows: list[list]):
    t.setSortingEnabled(False)
    t.setRowCount(len(rows))
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            t.setItem(i, j, cell if isinstance(cell, QTableWidgetItem) else QTableWidgetItem("" if cell is None else str(cell)))
    t.setSortingEnabled(True)


class EquityChart(QWidget):
    """Cumulative net P&L line over daily P&L bars."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.df: pd.DataFrame | None = None

    def set_data(self, df: pd.DataFrame):
        self.df = df
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(C["panel"]))
        df = self.df
        if df is None or df.empty:
            p.setPen(QColor(C["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No closed trades in this period yet")
            return
        L, R, T, B = 70, 12, 14, self.height() - 26
        W = self.width() - L - R
        vals = list(df["cum"]) + list(df["net"]) + [0]
        lo, hi = min(vals), max(vals)
        if hi == lo:
            hi, lo = hi + 1, lo - 1
        y = lambda v: B - (v - lo) / (hi - lo) * (B - T)
        n = len(df)
        x = lambda i: L + (i + 0.5) * W / n
        p.setPen(QPen(QColor(C["border"]), 1))
        for k in range(5):
            v = lo + (hi - lo) * k / 4
            p.drawLine(QPointF(L, y(v)), QPointF(L + W, y(v)))
            p.setPen(QColor(C["muted"]))
            p.drawText(QRectF(0, y(v) - 8, L - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, rs(v))
            p.setPen(QPen(QColor(C["border"]), 1))
        p.setPen(QPen(QColor(C["muted"]), 1, Qt.PenStyle.DashLine))
        p.drawLine(QPointF(L, y(0)), QPointF(L + W, y(0)))
        bw = max(2.0, W / n * 0.6)
        for i, v in enumerate(df["net"]):
            top, bot = sorted((y(v), y(0)))
            p.fillRect(QRectF(x(i) - bw / 2, top, bw, max(bot - top, 1)), QColor(C["up"] if v >= 0 else C["down"]).darker(130))
        path = QPainterPath()
        for i, v in enumerate(df["cum"]):
            pt = QPointF(x(i), y(v))
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        p.setPen(QPen(QColor(C["accent"]), 2))
        p.drawPath(path)
        p.setPen(QColor(C["muted"]))
        idx = list(df.index)
        for i in sorted({0, n // 2, n - 1}):
            p.drawText(QRectF(x(i) - 45, B + 6, 90, 14), Qt.AlignmentFlag.AlignCenter, pd.Timestamp(idx[i]).strftime("%d-%b-%y"))


class StrategyDialog(QDialog):
    """One strategy: legs, market context at entry, rule hits and journal entry."""

    def __init__(self, s: dict, journal: dict | None, explain: dict, parent=None):
        super().__init__(parent)
        self.s = s
        self.setWindowTitle(f"{s['structure']} — {s['und']} {s['expiry'] or ''}")
        self.setMinimumSize(760, 640)
        lay = QVBoxLayout(self)
        col = pnl_color(s["net"])
        lay.addWidget(_label(
            f"<span style='font-size:16px;font-weight:700'>{html.escape(s['structure'])}</span> "
            f"<span style='color:{C['muted']}'>{s['und']} · expiry {s['expiry'] or '—'} · opened "
            f"{s['entry_time']:%d-%b %H:%M} · {s['status']}</span><br>"
            f"Net <b style='color:{col}'>{rs(s['net'])}</b> (gross {rs(s['pnl'])}, charges {rs(-s['charges'])}) · "
            f"credit {rs(s['credit'], False)} · max loss {rs(s['max_loss'], False) if s['max_loss'] else 'unlimited' if not s['defined_risk'] else '—'}"
            f" · DTE at entry {s['dte_entry'] if s['dte_entry'] is not None else '—'}"))
        legs = make_table(["Side", "Contract", "Qty", "Entry", "Exit", "Exit reason", "Entry time", "Exit time", "P&L"])
        fill_table(legs, [[l["side"], l["tsym"], Num(str(l["qty"]), l["qty"]), Num(f"{l['entry_px']:,.2f}", l["entry_px"]),
                           Num("—" if l["exit_px"] is None else f"{l['exit_px']:,.2f}", l["exit_px"]), l["exit_reason"],
                           f"{l['entry_time']:%d-%b %H:%M}", "" if l["exit_time"] is None else f"{l['exit_time']:%d-%b %H:%M}",
                           Num(rs(l["pnl"]), l["pnl"], pnl_color(l["pnl"]))] for l in s["legs"]])
        legs.setMinimumHeight(min(56 + 30 * len(s["legs"]), 240))
        legs.setMaximumHeight(56 + 30 * len(s["legs"]))
        lay.addWidget(legs)
        c = s["ctx"]
        if c:
            src = "captured live from the Options tab" if c.get("source") != "reconstructed" else \
                  "reconstructed from daily data (option IV not available)"
            items = [("Spot", c.get("spot"), "{:,.0f}"), ("ATM IV", c.get("atm_iv"), "{:.1f}%"),
                     ("Realised vol 20d", c.get("hv20"), "{:.1f}%"), ("IV ÷ realised", c.get("vrp"), "{:.2f}×"),
                     ("India VIX", c.get("vix"), "{:.2f}"), ("Environment score", c.get("score"), "{:+d}"),
                     ("Bias", c.get("bias"), "{}"), ("Trend / ADX", c.get("trend"), "{}"), ("ADX", c.get("adx"), "{:.0f}"),
                     ("Call / put OI wall", f"{c.get('call_wall') or '—'} / {c.get('put_wall') or '—'}"
                      if c.get("call_wall") or c.get("put_wall") else None, "{}"),
                     ("Events before expiry", ", ".join(c.get("events") or []) or None, "{}")]
            txt = " · ".join(f"<span style='color:{C['muted']}'>{k}</span> <b>{f.format(v)}</b>"
                             for k, v, f in items if v is not None)
            lay.addWidget(_label(f"<b>Market at entry</b> <span style='color:{C['muted']}'>({src})</span><br>{txt}"))
        if s["flags"]:
            lay.addWidget(_label("<b style='color:#f97316'>What went against the plan</b>" + "".join(
                f"<div style='margin:3px 0'>• <b>{html.escape(explain[f][0])}</b> — "
                f"<span style='color:{C['muted']}'>{html.escape(explain[f][2])}</span></div>"
                for f in s["flags"] if f in explain)))
        # journal
        j = journal or {}
        box, bl = _card("Journal")
        grid = QGridLayout()
        self.tags = {}
        for i, t in enumerate(JOURNAL_TAGS):
            cb = QCheckBox(t)
            cb.setChecked(t in (j.get("tags") or []))
            self.tags[t] = cb
            grid.addWidget(cb, i // 4, i % 4)
        bl.addLayout(grid)
        row = QHBoxLayout()
        row.addWidget(QLabel("Rating (1–5)"))
        self.rating = QSpinBox()
        self.rating.setRange(0, 5)
        self.rating.setValue(int(j.get("rating") or 0))
        row.addWidget(self.rating)
        row.addStretch(1)
        bl.addLayout(row)
        self.notes = QPlainTextEdit(j.get("notes", ""))
        self.notes.setPlaceholderText("Why did you take it? What was the plan? What happened? What will you do differently?")
        bl.addWidget(self.notes)
        lay.addWidget(box, 1)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def journal(self) -> dict:
        return {"strategy_id": self.s["id"], "tags": [t for t, cb in self.tags.items() if cb.isChecked()],
                "rating": self.rating.value(), "notes": self.notes.toPlainText().strip(),
                "und": self.s["und"], "structure": self.s["structure"], "entry_time": self.s["entry_time"]}


class TradesDashboard(QWidget):
    sync_requested = Signal()                 # pull from Shoonya + recompute
    recompute_requested = Signal()            # recompute from stored data only (period change)
    import_requested = Signal(str)
    journal_saved = Signal(dict)

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.data: dict = {}
        self.explain: dict = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        head = QFrame()
        head.setStyleSheet(f"QFrame {{ background:{C['panel']}; border:1px solid {C['border']}; border-radius:8px; }}"
                           "QLabel { border:none; background:transparent; }")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 6, 12, 6)
        self.status = _label("Connect Shoonya to pull positions, orders and trades.")
        hl.addWidget(self.status, 1)
        self.period = QComboBox()
        for name, days in PERIODS:
            self.period.addItem(name, days)
        self.period.setCurrentIndex(2)
        self.period.currentIndexChanged.connect(lambda _: self.recompute_requested.emit())
        hl.addWidget(QLabel("Period"))
        hl.addWidget(self.period)
        self.auto = QCheckBox("Auto-sync")
        self.auto.setChecked(True)
        hl.addWidget(self.auto)
        b = QPushButton("Import CSV…")
        b.setToolTip("Import older trades from a broker trade-history export (date, symbol, buy/sell, qty, price)")
        b.clicked.connect(self._import)
        hl.addWidget(b)
        b = QPushButton("Export CSV…")
        b.clicked.connect(self._export)
        hl.addWidget(b)
        b = QPushButton("Sync now", objectName="primary")
        b.clicked.connect(self.sync_requested.emit)
        hl.addWidget(b)
        lay.addWidget(head)

        self.kpi = _label()
        kf, kl = _card("")
        kl.addWidget(self.kpi)
        lay.addWidget(kf)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)

        # 1 positions
        w = QWidget(objectName="root")
        v = QVBoxLayout(w)
        self.alerts = _label()
        v.addWidget(self.alerts)
        self.pos_table = make_table(["Strategy", "Expiry", "Contract", "Net qty", "Avg", "LTP", "Leg P&L", "Strategy P&L",
                                     "Credit captured", "Spot"])
        v.addWidget(self.pos_table, 1)
        self.tabs.addTab(w, "Open positions")
        # 2 orders
        self.ord_table = make_table(["Time", "Contract", "Side", "Qty", "Price", "Type", "Product", "Filled", "Avg fill",
                                     "Status", "Reason"])
        self.tabs.addTab(self.ord_table, "Orders (today)")
        # 3 fills
        self.fill_table = make_table(["Time", "Contract", "Side", "Qty", "Price", "Value", "Order no", "Source",
                                      "Context at entry"])
        self.tabs.addTab(self.fill_table, "Fills")
        # 4 history
        w = QWidget(objectName="root")
        v = QVBoxLayout(w)
        v.addWidget(_label(f"<span style='color:{C['muted']}'>Legs of the same underlying and expiry opened on the same "
                           "day form one strategy. Double-click a row for legs, market context at entry, what went "
                           "against the plan, and your journal entry.</span>", 11))
        self.hist = make_table(["Opened", "Closed", "Underlying", "Structure", "Expiry", "DTE", "Lots", "Credit",
                                "Net P&L", "% of credit", "Days held", "Exit", "Env at entry", "Mistakes"])
        self.hist.cellDoubleClicked.connect(self._open_strategy)
        v.addWidget(self.hist, 1)
        self.tabs.addTab(w, "Trade history")
        # 5 performance
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        w = QWidget(objectName="root")
        self.perf = QVBoxLayout(w)
        f, l = _card("Equity curve (cumulative net P&L) and daily P&L")
        self.equity = EquityChart()
        l.addWidget(self.equity)
        self.perf.addWidget(f)
        self.stats = _label()
        f, l = _card("Statistics")
        l.addWidget(self.stats)
        self.perf.addWidget(f)
        self.bd_grid = QGridLayout()
        self.perf.addLayout(self.bd_grid)
        self.perf.addStretch(1)
        sc.setWidget(w)
        self.tabs.addTab(sc, "Performance")
        # 6 why I lost
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        w = QWidget(objectName="root")
        self.why = QVBoxLayout(w)
        sc.setWidget(w)
        self.tabs.addTab(sc, "Why I lost")
        # 7 journal
        w = QWidget(objectName="root")
        v = QVBoxLayout(w)
        self.tag_summary = _label()
        v.addWidget(self.tag_summary)
        self.jt = make_table(["Opened", "Underlying", "Structure", "Net P&L", "Rating", "Tags", "Notes"])
        self.jt.cellDoubleClicked.connect(lambda r, _c: self._open_strategy_id(self.jt.item(r, 0).data(Qt.ItemDataRole.UserRole + 1)))
        v.addWidget(self.jt, 1)
        self.tabs.addTab(w, "Journal")

    # ------------------------------------------------------------ helpers
    def period_days(self):
        return self.period.currentData()

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import trade history", "", "CSV files (*.csv)")
        if path:
            self.import_requested.emit(path)

    def _export(self):
        if not self.data.get("strategies"):
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export trades", "trades.csv", "CSV files (*.csv)")
        if not path:
            return
        rows = []
        for s in self.data["strategies"]:
            for l in s["legs"]:
                rows.append({"strategy": s["id"], "structure": s["structure"], "status": s["status"],
                             "strategy_net": round(s["net"], 2), "side": l["side"], "contract": l["tsym"],
                             "qty": l["qty"], "entry_time": l["entry_time"], "entry_px": l["entry_px"],
                             "exit_time": l["exit_time"], "exit_px": l["exit_px"], "exit_reason": l["exit_reason"],
                             "leg_pnl": l["pnl"], "charges": round(l["charges"], 2), "mistakes": ";".join(s["flags"])})
        pd.DataFrame(rows).to_csv(path, index=False)
        self.status.setText(f"Exported {len(rows)} legs to {html.escape(path)}")

    def _open_strategy(self, row, _col):
        self._open_strategy_id(self.hist.item(row, 0).data(Qt.ItemDataRole.UserRole + 1))

    def _open_strategy_id(self, sid):
        s = next((x for x in self.data.get("strategies", []) if x["id"] == sid), None)
        if s is None:
            return
        dlg = StrategyDialog(s, self.data.get("journal", {}).get(sid), self.explain, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.journal_saved.emit(dlg.journal())

    # ------------------------------------------------------------ render
    def set_status(self, text: str):
        self.status.setText(text)

    def set_data(self, d: dict):
        self.data = d
        self.explain = d.get("explain", {})
        self.status.setText(d.get("status", ""))
        self._kpis(d)
        self._positions(d)
        self._orders(d)
        self._fills(d)
        self._history(d)
        self._performance(d)
        self._why(d)
        self._journal(d)

    def _kpis(self, d):
        m, lim, today = d.get("metrics") or {}, d.get("limits") or {}, d.get("today") or {}
        def cell(k, v, col=C["text"], note=""):
            return (f"<td style='padding:0 18px 0 0'><span style='color:{C['muted']};font-size:11px'>{k}</span><br>"
                    f"<b style='font-size:15px;color:{col}'>{v}</b>"
                    f"<span style='color:{C['muted']};font-size:11px'> {note}</span></td>")
        wr = m.get("win_rate")
        self.kpi.setText(
            "<table><tr>"
            + cell("Today (booked + open)", rs(today.get("total")), pnl_color(today.get("total")))
            + cell("Open MTM", rs(today.get("open")), pnl_color(today.get("open")))
            + cell("Margin used", rs(lim.get("margin_used"), False), C["text"],
                   f"of {rs(lim.get('available'), False)} avail." if lim.get("available") is not None else "")
            + cell(f"{self.period.currentText()} net", rs(m.get("net")), pnl_color(m.get("net")),
                   f"{m.get('trades', 0)} trades")
            + cell("Win rate", "—" if wr is None else f"{wr:.0f}%")
            + cell("Expectancy / trade", rs(m.get("expectancy")), pnl_color(m.get("expectancy")))
            + cell("Profit factor", "—" if m.get("profit_factor") is None else f"{m['profit_factor']:.2f}")
            + cell("Max drawdown", rs(m.get("max_drawdown")), C["down"] if (m.get("max_drawdown") or 0) < 0 else C["text"])
            + cell("Open strategies", str(m.get("open", 0)))
            + "</tr></table>")

    def _positions(self, d):
        groups = d.get("positions") or []
        lines = []
        rows = []
        for g in groups:
            name = f"{g['und']} {g['structure']}"
            for kind, text in g["alerts"]:
                col, tag = ALERT_STYLE[kind]
                lines.append(f"<div style='margin:3px 0'><b style='color:{col}'>{tag}</b> <b>{html.escape(name)}</b> "
                             f"{g['expiry'] or ''}: {html.escape(text)}</div>")
            for l in g["legs"]:
                leg = (l["lp"] - l["avg"]) * l["qty"] if l["qty"] else l["rpnl"]
                rows.append([name, str(g["expiry"] or ""), l["tsym"], Num(str(l["qty"]), l["qty"]),
                             Num(f"{l['avg']:,.2f}", l["avg"]), Num(f"{l['lp']:,.2f}", l["lp"]),
                             Num(rs(leg), leg, pnl_color(leg)),
                             Num(rs(g["open_pnl"] + g["realised"]), g["open_pnl"] + g["realised"],
                                 pnl_color(g["open_pnl"] + g["realised"])),
                             Num("—" if g["captured"] is None else f"{g['captured'] * 100:.0f}%", g["captured"]),
                             Num("—" if not g["spot"] else f"{g['spot']:,.0f}", g["spot"])])
        if not groups:
            lines = [f"<span style='color:{C['muted']}'>No positions today.</span>"]
        elif not lines:
            lines = [f"<span style='color:{C['up']}'>All open positions are inside the plan — no action needed.</span>"]
        self.alerts.setText("<b>Management plan checks</b> <span style='color:" + C["muted"] +
                            "'>(book at ~50% of credit · exit at 1.5× credit or a breach of a short strike · "
                            "close/roll 1–2 days before expiry)</span>" + "".join(lines))
        fill_table(self.pos_table, rows)

    def _orders(self, d):
        rows = []
        for o in d.get("orders") or []:
            st = str(o.get("status", ""))
            col = C["down"] if st.upper() in ("REJECTED", "CANCELED", "CANCELLED") else C["up"] if st.upper() == "COMPLETE" else None
            it = QTableWidgetItem(st)
            if col:
                it.setForeground(QColor(col))
            rows.append([o.get("norentm", ""), o.get("tsym", ""), "BUY" if str(o.get("trantype")).startswith("B") else "SELL",
                         Num(str(o.get("qty", "")), _f(o.get("qty"))), Num(str(o.get("prc", "")), _f(o.get("prc"))),
                         o.get("prctyp", ""), o.get("prd", ""), Num(str(o.get("fillshares", "") or 0), _f(o.get("fillshares"))),
                         Num(str(o.get("avgprc", "") or ""), _f(o.get("avgprc"))), it, o.get("rejreason", "")])
        fill_table(self.ord_table, rows)

    def _fills(self, d):
        rows = []
        for f in d.get("fills") or []:
            c = f.get("ctx") or {}
            ctx = ("" if not c else
                   f"score {c['score']:+d} · IV÷RV {c.get('vrp') or '—'} · VIX {c.get('vix') or '—'}" if c.get("score") is not None
                   else f"reconstructed · HV20 {c.get('hv20') or '—'} · ADX {c.get('adx') or '—'}")
            t = pd.Timestamp(f["time"])
            rows.append([t.tz_convert("Asia/Kolkata").strftime("%d-%b %H:%M:%S") if t.tzinfo else t.strftime("%d-%b %H:%M:%S"),
                         f["tsym"], "BUY" if f["side"] == "B" else "SELL", Num(str(f["qty"]), f["qty"]),
                         Num(f"{f['price']:,.2f}", f["price"]), Num(rs(f["price"] * f["qty"], False), f["price"] * f["qty"]),
                         f.get("order_no", ""), f.get("source", ""), ctx])
        fill_table(self.fill_table, rows)

    def _history(self, d):
        rows = []
        for s in reversed(d.get("strategies") or []):
            c = s["ctx"]
            env = (c.get("verdict") or "").split(" ")[0] if c.get("verdict") else \
                  te_bucket(c) if c.get("score") is not None else \
                  ("reconstructed" if c.get("source") == "reconstructed" else "—")
            first = QTableWidgetItem(f"{s['entry_time']:%d-%b-%y %H:%M}")
            first.setData(Qt.ItemDataRole.UserRole + 1, s["id"])
            exit_ = "open" if s["status"] == "open" else ("expired" if s["expired"] else "closed")
            mistakes = QTableWidgetItem(", ".join(self.explain.get(f, (f,))[0].split(" (")[0] for f in s["flags"]))
            mistakes.setToolTip("\n".join("• " + self.explain.get(f, (f,))[0] for f in s["flags"]))
            if s["flags"]:
                mistakes.setForeground(QColor("#f97316"))
            rows.append([first, "" if s["exit_time"] is None else f"{s['exit_time']:%d-%b-%y %H:%M}", s["und"],
                         s["structure"], str(s["expiry"] or ""), Num(str(s["dte_entry"] if s["dte_entry"] is not None else "—"),
                                                                    s["dte_entry"]),
                         Num(str(s["lots"]), s["lots"]), Num(rs(s["credit"], False), s["credit"]),
                         Num(rs(s["net"]), s["net"], pnl_color(s["net"])),
                         Num("—" if s["captured"] is None else f"{s['captured'] * 100:.0f}%", s["captured"]),
                         Num(f"{s['holding_days']:.1f}", s["holding_days"]), exit_, env, mistakes])
        fill_table(self.hist, rows)

    def _performance(self, d):
        self.equity.set_data(d.get("equity"))
        m = d.get("metrics") or {}
        if not m.get("trades"):
            self.stats.setText(f"<span style='color:{C['muted']}'>No closed trades in this period.</span>")
        else:
            def r(k, v):
                return f"<tr><td style='color:{C['muted']};padding-right:14px'>{k}</td><td><b>{v}</b></td></tr>"
            b, w = m.get("best"), m.get("worst")
            self.stats.setText(
                "<table><tr><td valign=top style='padding-right:40px'><table>"
                + r("Closed strategies", f"{m['trades']} ({m['wins']} wins / {m['losses']} losses)")
                + r("Win rate", f"{m['win_rate']:.1f}%")
                + r("Average win / loss", f"{rs(m['avg_win'])} / {rs(m['avg_loss'])}")
                + r("Payoff ratio", "—" if m["payoff"] is None else f"{m['payoff']:.2f}")
                + r("Break-even win rate", "—" if m["payoff"] is None else f"{100 / (1 + m['payoff']):.0f}%")
                + r("Expectancy per trade", rs(m["expectancy"]))
                + "</table></td><td valign=top><table>"
                + r("Gross P&L", rs(m["gross"])) + r("Charges (est.)", rs(-m["charges"])) + r("Net P&L", rs(m["net"]))
                + r("Profit factor", "—" if m["profit_factor"] is None else f"{m['profit_factor']:.2f}")
                + r("Max drawdown", rs(m["max_drawdown"])) + r("Longest losing streak", str(m["max_losing_streak"]))
                + r("Average holding", f"{m['avg_holding_days']:.1f} days")
                + r("Best / worst", f"{rs(b['net']) if b else '—'} / {rs(w['net']) if w else '—'}")
                + "</table></td></tr></table>")
        while self.bd_grid.count():
            it = self.bd_grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        for i, (name, rows) in enumerate((d.get("breakdowns") or {}).items()):
            f, l = _card(name)
            t = make_table(["Bucket", "Trades", "Win %", "Net", "Avg"])
            fill_table(t, [[r["bucket"], Num(str(r["trades"]), r["trades"]), Num(f"{r['win_rate']:.0f}%", r["win_rate"]),
                            Num(rs(r["net"]), r["net"], pnl_color(r["net"])), Num(rs(r["avg"]), r["avg"], pnl_color(r["avg"]))]
                           for r in rows])
            t.setMinimumHeight(min(60 + 26 * len(rows), 260))
            l.addWidget(t)
            self.bd_grid.addWidget(f, i // 2, i % 2)

    def _why(self, d):
        while self.why.count():
            it = self.why.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        fs = d.get("findings") or []
        m = d.get("metrics") or {}
        if not m.get("trades"):
            self.why.addWidget(_label(f"<span style='color:{C['muted']}'>No closed trades in this period yet. "
                                      "The diagnosis fills in as trades close.</span>"))
        elif not fs:
            self.why.addWidget(_label(f"<span style='color:{C['up']}'>No rule-breaking patterns found in this period.</span>"))
        else:
            worst = fs[0]
            self.why.addWidget(_label(
                f"<div style='font-size:15px'>Biggest leak: <b style='color:{C['down']}'>{html.escape(worst['title'])}</b> "
                f"cost you <b>{rs(-worst['lost'], False)}</b> across {worst['trades']} trade(s).</div>"
                f"<div style='color:{C['muted']}'>Findings are ranked by rupees lost on the trades they apply to. "
                "One trade can show up under several findings.</div>"))
        for fnd in fs:
            f, l = _card("")
            cmp_ = ""
            if fnd.get("win_rate_others") is not None and fnd.get("avg_others") is not None:
                cmp_ = (f"Win rate {fnd['win_rate']:.0f}% vs {fnd['win_rate_others']:.0f}% on your other trades; "
                        f"average {rs(fnd['avg'])} vs {rs(fnd['avg_others'])}.")
            l.addWidget(_label(
                f"<span style='font-size:14px;font-weight:700'>{html.escape(fnd['title'])}</span> "
                f"<span style='color:{C['muted']}'>· {fnd['trades']} trade(s), {fnd['share']:.0f}% of trades · "
                f"net {rs(fnd['net'])} · lost {rs(fnd['lost'])}</span>"
                f"<div style='margin-top:4px'>{html.escape(fnd['why'])}</div>"
                + (f"<div style='color:{C['muted']}'>{cmp_}</div>" if cmp_ else "")
                + f"<div style='margin-top:4px;color:{C['up']}'><b>Fix:</b> {html.escape(fnd['fix'])}</div>"
                + (f"<div style='color:{C['muted']};font-size:11px'>Worst examples: {html.escape(', '.join(fnd['examples']))}</div>"
                   if fnd.get("examples") else "")))
            self.why.addWidget(f)
        self.why.addStretch(1)

    def _journal(self, d):
        j = d.get("journal") or {}
        strat = {s["id"]: s for s in d.get("strategies") or []}
        rows = []
        tag_pnl: dict[str, list[float]] = {}
        for sid, e in j.items():
            s = strat.get(sid)
            net = s["net"] if s else None
            for t in e.get("tags") or []:
                if net is not None:
                    tag_pnl.setdefault(t, []).append(net)
            first = QTableWidgetItem(pd.Timestamp(e.get("entry_time")).strftime("%d-%b-%y %H:%M") if e.get("entry_time") else "")
            first.setData(Qt.ItemDataRole.UserRole + 1, sid)
            rows.append([first, e.get("und", ""), e.get("structure", ""), Num(rs(net), net, pnl_color(net)),
                         Num(str(e.get("rating") or ""), e.get("rating")), ", ".join(e.get("tags") or []),
                         (e.get("notes") or "").replace("\n", " ")[:200]])
        fill_table(self.jt, rows)
        if tag_pnl:
            parts = sorted(((t, sum(v), len(v)) for t, v in tag_pnl.items()), key=lambda x: x[1])
            self.tag_summary.setText("<b>P&L by journal tag</b> (this period): " + " · ".join(
                f"{html.escape(t)} <b style='color:{pnl_color(n)}'>{rs(n)}</b> ({c})" for t, n, c in parts))
        else:
            self.tag_summary.setText(f"<span style='color:{C['muted']}'>Tag your trades (double-click in Trade history) "
                                     "and this shows which habits make or cost money.</span>")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
