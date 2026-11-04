"""Options tab: option-chain read + screened positional selling structures for
NIFTY / BANKNIFTY.  Data and maths live in options.py; this file only draws."""
from __future__ import annotations

import html

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from live_view import C
from options import _edge

CE_COLOR, PE_COLOR = "#ef4444", "#22c55e"   # call writers cap the upside, put writers the downside
DISCLAIMER = ("Screening tool, not investment advice. Probabilities and expectancy are model estimates "
              "(lognormal, today's IV / last-20-session realised vol) and ignore gaps, IV changes, slippage, "
              "brokerage and margin. Option selling can lose many times the premium collected — size every "
              "trade to a loss you can afford and decide for yourself.")


def _label(text: str = "", size: int | None = None) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    if size:
        lbl.setStyleSheet(f"font-size:{size}px")
    return lbl


def _card(title: str) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setStyleSheet(f"QFrame#oc {{ background:{C['panel']}; border:1px solid {C['border']}; border-radius:8px; }}")
    f.setObjectName("oc")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(12, 10, 12, 12)
    lay.setSpacing(6)
    if title:
        t = QLabel(title.upper())
        t.setStyleSheet(f"font-size:11px; font-weight:700; color:{C['muted']}; letter-spacing:1px")
        lay.addWidget(t)
    return f, lay


def _bul(items, color=C["text"]) -> str:
    return "".join(f"<div style='margin:3px 0;color:{color}'>• {html.escape(i)}</div>" for i in items)


def _n(v, fmt="{:,.0f}", none="—"):
    return none if v is None else fmt.format(v)


def _rs(v) -> str:
    return "—" if v is None else f"{'−' if v < 0 else ''}₹{abs(v):,.0f}"


class OIChart(QWidget):
    """Call / put open interest per strike around spot, with spot, max pain,
    the ±1σ expected move and the top structure's short strikes marked."""

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(230)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.res: dict | None = None

    def set_result(self, res: dict):
        self.res = res
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(C["panel"]))
        r = self.res
        if not r or not r.get("oi_table"):
            p.setPen(QColor(C["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No option-chain data yet")
            return
        rows = r["oi_table"]
        L, R, T, B = 56, 12, 22, self.height() - 34
        W = self.width() - L - R
        top = max(max(x["ce_oi"], x["pe_oi"]) for x in rows) or 1
        lo_k, hi_k = rows[0]["strike"], rows[-1]["strike"]
        span = (hi_k - lo_k) or 1
        xk = lambda k: L + (k - lo_k) / span * W
        bw = max(2.0, W / len(rows) * 0.36)

        # ±1σ band
        if r.get("sigma_move"):
            a, b = xk(max(lo_k, r["spot"] - r["sigma_move"])), xk(min(hi_k, r["spot"] + r["sigma_move"]))
            p.fillRect(QRectF(a, T, b - a, B - T), QColor(56, 189, 248, 22))
        # grid + y labels
        p.setPen(QPen(QColor(C["border"]), 1))
        for i in range(5):
            y = B - (B - T) * i / 4
            p.drawLine(QPointF(L, y), QPointF(L + W, y))
            p.setPen(QColor(C["muted"]))
            p.drawText(QRectF(0, y - 8, L - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       f"{top * i / 4 / 1000:,.0f}k" if top >= 10000 else f"{top * i / 4:,.0f}")
            p.setPen(QPen(QColor(C["border"]), 1))
        # bars
        every = max(1, len(rows) // 12)
        for i, x in enumerate(rows):
            cx = xk(x["strike"])
            for val, off, col in ((x["ce_oi"], -bw, CE_COLOR), (x["pe_oi"], 0, PE_COLOR)):
                h = (B - T) * val / top
                p.fillRect(QRectF(cx + off, B - h, bw, h), QColor(col))
            if i % every == 0:
                p.setPen(QColor(C["muted"]))
                p.drawText(QRectF(cx - 30, B + 4, 60, 14), Qt.AlignmentFlag.AlignCenter, f"{x['strike']:,.0f}")
        # spot / max pain / short strikes
        def vline(k, color, label, style=Qt.PenStyle.SolidLine, yoff=0):
            if not lo_k <= k <= hi_k:
                return
            x = xk(k)
            p.setPen(QPen(QColor(color), 1.4, style))
            p.drawLine(QPointF(x, T), QPointF(x, B))
            p.drawText(QRectF(x - 60, 2 + yoff, 120, 16), Qt.AlignmentFlag.AlignCenter, label)
        vline(r["spot"], C["accent"], f"spot {r['spot']:,.0f}")
        vline(r["max_pain"], "#facc15", f"max pain {r['max_pain']:,.0f}", Qt.PenStyle.DashLine,
              yoff=0 if abs(r["max_pain"] - r["spot"]) > span / 8 else 12)
        if r.get("strategies"):
            for l in r["strategies"][0]["legs"]:
                if l["side"] == "SELL":
                    vline(l["strike"], "#e879f9", f"short {l['kind']} {l['strike']:,.0f}", Qt.PenStyle.DotLine)
        p.setPen(QColor(C["muted"]))
        p.drawText(QRectF(L, B + 18, W, 14), Qt.AlignmentFlag.AlignLeft,
                   "■ call OI (red)  ■ put OI (green)  · shaded = ±1σ expected move to expiry  "
                   "· dotted = top structure's short strikes")


class OptionsDashboard(QWidget):
    refresh_requested = Signal(str, str)     # underlying, expiry ("" = auto)

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.res: dict | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        head = QFrame()
        head.setStyleSheet(f"QFrame {{ background:{C['panel']}; border:1px solid {C['border']}; border-radius:8px; }}"
                           "QLabel { border:none; background:transparent; }")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(12, 6, 12, 6)
        self.sym = QButtonGroup(self)
        for i, (code, name) in enumerate((("NIFTY", "NIFTY"), ("BANKNIFTY", "BANKNIFTY"))):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setProperty("code", code)
            self.sym.addButton(b)
            hl.addWidget(b)
        self.sym.buttonClicked.connect(lambda _: self._request(auto_expiry=True))
        self.expiry = QComboBox()
        self.expiry.setMinimumContentsLength(11)
        self.expiry.activated.connect(lambda _: self._request())
        hl.addWidget(QLabel("Expiry"))
        hl.addWidget(self.expiry)
        self.info = _label("Select an index to load its option chain")
        hl.addWidget(self.info, 1)
        self.auto = QCheckBox("Auto-refresh")
        self.auto.setChecked(True)
        hl.addWidget(self.auto)
        btn = QPushButton("Refresh", objectName="primary")
        btn.clicked.connect(lambda: self._request())
        hl.addWidget(btn)
        lay.addWidget(head)

        body = QWidget(objectName="root")
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(8)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)

        top = QHBoxLayout()
        top.setSpacing(8)
        f, l = _card("Option-chain read")
        self.read = _label()
        l.addWidget(self.read)
        l.addStretch(1)
        top.addWidget(f, 3)
        f, l = _card("Selling environment")
        self.verdict = _label()
        l.addWidget(self.verdict)
        l.addStretch(1)
        top.addWidget(f, 4)
        self.body.addLayout(top)

        f, l = _card("Open interest by strike")
        self.chart = OIChart()
        l.addWidget(self.chart)
        self.body.addWidget(f)

        self.strat_host = QVBoxLayout()
        self.strat_host.setSpacing(8)
        self.body.addLayout(self.strat_host)
        disc = _label(f"<span style='color:{C['muted']}'>{html.escape(DISCLAIMER)}</span>", 11)
        self.body.addWidget(disc)
        self.body.addStretch(1)

    # ------------------------------------------------------------ requests
    def underlying(self) -> str:
        b = self.sym.checkedButton()
        return b.property("code") if b else "NIFTY"

    def _request(self, auto_expiry: bool = False):
        exp = "" if auto_expiry or self.expiry.currentIndex() < 0 else self.expiry.currentText()
        self.info.setText(f"<span style='color:{C['muted']}'>Loading {self.underlying()} option chain…</span>")
        self.refresh_requested.emit(self.underlying(), exp)

    def request(self):
        """Refresh with the current selection (used by the auto-refresh timer)."""
        if self.auto.isChecked():
            self._request()

    def set_error(self, msg: str):
        self.info.setText(f"<span style='color:{C['down']}'>Option chain failed: {html.escape(msg[:200])}</span>")

    # ------------------------------------------------------------ render
    def set_result(self, r: dict):
        if r["underlying"] != self.underlying():
            return                      # a stale reply for the index we just switched away from
        self.res = r
        self.expiry.blockSignals(True)
        self.expiry.clear()
        self.expiry.addItems(r["expiries"][:8])
        self.expiry.setCurrentText(r["expiry"])
        self.expiry.blockSignals(False)
        self.info.setText(
            f"<b style='font-size:14px'>{html.escape(r['name'])} {r['spot']:,.2f}</b>"
            f" &nbsp;<span style='color:{C['muted']}'>expiry {r['expiry']} · {r['dte']:.1f} days left · "
            f"lot {r['lot']} · NSE {html.escape(str(r['timestamp']))}</span>")
        self._render_read(r)
        self._render_verdict(r)
        self.chart.set_result(r)
        self._render_strategies(r)

    def _render_read(self, r):
        m = C["muted"]
        def row(k, v, note=""):
            return (f"<tr><td style='color:{m};padding:2px 10px 2px 0'>{k}</td><td><b>{v}</b></td>"
                    f"<td style='color:{m};padding-left:10px'>{note}</td></tr>")
        cw = ", ".join(f"{w['strike']:,.0f}" for w in r["call_walls"])
        pw = ", ".join(f"{w['strike']:,.0f}" for w in r["put_walls"])
        vrp = r.get("vrp")
        bias_c = {"bullish": C["up"], "bearish": C["down"]}.get(r["bias"], C["text"])
        self.read.setText(
            "<table>"
            + row("ATM IV", _n(r["atm_iv"], "{:.1f}%"), f"ATM strike {r['atm']:,.0f}")
            + row("Realised vol", f"{_n(r['hv10'], '{:.1f}%')} / {_n(r['hv20'], '{:.1f}%')}", "10 / 20 sessions")
            + row("IV ÷ realised", _n(vrp, "{:.2f}×"), "&gt;1.15 rich · &lt;0.95 cheap")
            + row("Expected move", f"±{_n(r['sigma_move'])} pts",
                  f"1σ to expiry · ATM straddle {r['straddle']:,.0f}")
            + row("1σ range", f"{r['spot'] - (r['sigma_move'] or 0):,.0f} – {r['spot'] + (r['sigma_move'] or 0):,.0f}")
            + row("PCR (OI)", _n(r["pcr"], "{:.2f}"), f"today's OI change PCR {_n(r['pcr_chg'], '{:.2f}')}")
            + row("Max pain", f"{r['max_pain']:,.0f}")
            + row("Call OI walls", cw, "resistance writers defend")
            + row("Put OI walls", pw, "support writers defend")
            + row("Put–call skew", _n(r["skew"], "{:+.1f} vol pts"), "1σ put IV − 1σ call IV")
            + row("India VIX", _n(r["vix"], "{:.2f}"), f"{_n(r['vix_pct'], '{:.0f}')}th pct (1y) · last close")
            + row("Trend / ADX", f"{html.escape(str(r['trend']))} / {_n(r['adx'], '{:.0f}')}")
            + row("Bias", f"<span style='color:{bias_c}'>{r['bias'].upper()}</span>", "trend + PCR + OI change")
            + "</table>")

    def _render_verdict(self, r):
        col = C["up"] if r["score"] >= 3 else "#f59e0b" if r["score"] >= 1 else C["down"]
        icon = {1: ("✔", C["up"]), 0: ("●", "#f59e0b"), -1: ("✖", C["down"])}
        items = "".join(f"<div style='margin:4px 0'><span style='color:{icon[s][1]}'>{icon[s][0]}</span> "
                        f"{html.escape(t)}</div>" for s, t in r["checks"])
        self.verdict.setText(f"<div style='font-size:16px;font-weight:700;color:{col}'>{r['verdict']}</div>"
                             f"<div style='color:{C['muted']};margin-bottom:4px'>score {r['score']:+d}</div>{items}")

    def _render_strategies(self, r):
        while self.strat_host.count():
            w = self.strat_host.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not r["strategies"]:
            f, l = _card("Structures")
            l.addWidget(_label("No structure could be priced (missing IV or quotes — market may be closed)."))
            self.strat_host.addWidget(f)
            return
        for rank, s in enumerate(r["strategies"]):
            self.strat_host.addWidget(self._strategy_card(s, rank, r))

    def _strategy_card(self, s, rank, r) -> QFrame:
        if _edge(s) < 0:
            tag, tag_c = "NEGATIVE EXPECTANCY — NO EDGE", C["down"]
        elif rank == 0 and s["fit"] > 0 and r["score"] >= 1:
            tag, tag_c = "BEST FIT", C["up"]
        elif s["fit"] > 0:
            tag, tag_c = "ALTERNATIVE", C["accent"]
        else:
            tag, tag_c = "WEAK FIT", C["muted"]
        f, l = _card("")
        risk = "defined risk" if s["defined_risk"] else "<span style='color:#f97316'>UNDEFINED RISK</span>"
        l.addWidget(_label(f"<span style='font-size:15px;font-weight:700'>{s['name']}</span> &nbsp;"
                           f"<span style='color:{tag_c};font-weight:700'>{tag}</span> &nbsp;"
                           f"<span style='color:{C['muted']}'>{risk} · fit {s['fit']:+d}</span>"))
        legs = "".join(
            f"<tr><td style='color:{C['down'] if lg['side'] == 'SELL' else C['up']};padding-right:10px'>"
            f"<b>{lg['side']}</b></td><td style='padding-right:10px'>{lg['strike']:,.0f} {lg['kind']}</td>"
            f"<td style='padding-right:10px'>@ {lg['price']:,.2f}</td>"
            f"<td style='color:{C['muted']}'>IV {_n(lg['iv'], '{:.1f}%')}</td></tr>" for lg in s["legs"])
        m = C["muted"]
        ev = s.get("ev_real_rs")
        ev_c = C["up"] if ev and ev > 0.1 * s["credit_rs"] else C["down"] if ev and ev < -0.1 * s["credit_rs"] else C["text"]
        stats = (
            f"<table>"
            f"<tr><td style='color:{m};padding-right:8px'>Net credit</td><td><b>{s['credit_pts']:,.2f} pts · "
            f"₹{s['credit_rs']:,}/lot</b></td></tr>"
            f"<tr><td style='color:{m}'>Max loss</td><td><b>"
            + (f"₹{s['max_loss_rs']:,}/lot" if s["max_loss_rs"] is not None else "unlimited")
            + f"</b></td></tr>"
            f"<tr><td style='color:{m}'>Breakevens</td><td><b>{' – '.join(f'{b:,}' for b in s['breakevens'])}</b></td></tr>"
            f"<tr><td style='color:{m}'>Win probability</td><td><b>{s['pop']:.0f}%</b> at IV · "
            f"{_n(s.get('pop_real'), '{:.0f}%')} at realised vol</td></tr>"
            f"<tr><td style='color:{m}'>Expectancy</td><td><b style='color:{ev_c}'>"
            + (f"{'+' if ev >= 0 else '−'}₹{abs(ev):,}/lot" if ev is not None else "—")
            + f"</b> at realised vol</td></tr>"
            f"<tr><td style='color:{m}'>Theta / vega</td><td>{_rs(s['theta_rs_day'])}/day earned · "
            f"{_rs(s['vega_rs'])} per +1 IV pt</td></tr>"
            f"<tr><td style='color:{m}'>Worst-5% avg</td><td>{_rs(s['cvar_rs'])}/lot</td></tr>"
            f"</table>")
        row = QHBoxLayout()
        row.setSpacing(16)
        row.addWidget(_label(f"<table>{legs}</table>"), 2, Qt.AlignmentFlag.AlignTop)
        row.addWidget(_label(stats), 3, Qt.AlignmentFlag.AlignTop)
        l.addLayout(row)
        l.addWidget(_label(f"<div style='color:{C['accent']};font-weight:700;margin-top:4px'>Why this can work</div>"
                           + _bul(s["why"])))
        l.addWidget(_label(f"<div style='color:#f97316;font-weight:700'>What can go wrong</div>"
                           + _bul(s["risks"], "#fcd9b6")))
        l.addWidget(_label(f"<div style='color:{C['up']};font-weight:700'>Management plan (positional)</div>"
                           + _bul(s["exit"])))
        return f
