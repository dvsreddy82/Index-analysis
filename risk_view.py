"""Writer risk monitor screen: for NIFTY and BANKNIFTY side by side, in the
trader's required format — 1. threat matrix & regime, 2. active colour-coded
alerts with the exact signals, 3. execution directives.  Numbers come from
risk_engine.py; this file only draws."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QScrollArea, QVBoxLayout, QWidget

from theme import C, card, label, toolbar

LEVEL_STYLE = {"RED": ("#450a0a", "#ef4444", "#fecaca"), "AMBER": ("#3b2306", "#f59e0b", "#fde68a"),
               "GREEN": ("#052e16", "#22c55e", "#bbf7d0")}
REGIME_COLOR = {"Squeeze in Progress": "#ef4444", "Negative Gamma Trending": "#f59e0b",
                "Positive Gamma Range-Bound": "#22c55e"}
BIAS_COLOR = {"Bullish Delta": "#22c55e", "Bearish Delta": "#ef4444", "Neutral Mean-Reverting": "#e5e7eb"}
NOTE = ("Mechanical screens on exchange data using your rule set — not a forecast or investment advice. "
        "Intraday OI / IV / straddle changes compare with the chain ~30 min earlier (10–75 min window); "
        "participant OI is end-of-day (previous session). GEX assumes dealers are long calls and short puts, "
        "a convention Indian option flow does not always follow.")


def _esc(s) -> str:
    return html.escape(str(s))


class RiskColumn(QWidget):
    def __init__(self, name: str):
        super().__init__()
        self.setObjectName("root")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        f, l = card(f"{name} · 1. Threat matrix & regime")
        self.matrix = label(f"<span style='color:{C['muted']}'>Waiting for the option chain…</span>")
        l.addWidget(self.matrix)
        lay.addWidget(f)
        f, l = card(f"{name} · 2. Active alerts")
        self.alerts = label()
        l.addWidget(self.alerts)
        lay.addWidget(f)
        f, l = card(f"{name} · 3. Execution directives for the option writer")
        self.directives = label()
        l.addWidget(self.directives)
        lay.addWidget(f)
        lay.addStretch(1)

    def set_error(self, msg: str):
        self.matrix.setText(f"<span style='color:{C['down']}'>{_esc(msg)}</span>")

    def set_result(self, r: dict):
        mut = C["muted"]
        reg_c = REGIME_COLOR.get(r["regime"], C["text"])
        bias_c = BIAS_COLOR.get(r["bias"], C["text"])
        up_k, up_why = r["danger_up"]
        dn_k, dn_why = r["danger_down"]
        g = r["gex"]
        win = f"{r['window_min']:.0f} min" if r.get("window_min") else "not yet (needs ≥10 min of history)"
        src = {"shoonya": f"<span style='color:{C['up']}'>Shoonya real-time</span>",
               "nse": f"<span style='color:{C['warn']}'>NSE (≈1–2 min delayed)</span>",
               "offline": f"<span style='color:{C['down']}'>offline snapshot</span>"}.get(r.get("source"), r.get("source"))
        self.matrix.setText(
            f"<table width='100%' cellspacing='0' cellpadding='3'>"
            f"<tr><td style='color:{mut}' width='34%'>Market regime</td>"
            f"<td><b style='color:{reg_c};font-size:14px'>{r['regime']}</b></td></tr>"
            f"<tr><td style='color:{mut}'>Current bias</td><td><b style='color:{bias_c}'>{r['bias']}</b> "
            f"<span style='color:{mut}'>(flow/momentum {r['bias_score']:+.2f})</span></td></tr>"
            f"<tr><td style='color:{mut}'>Max danger strike (upside)</td><td><b style='color:{C['down']}'>"
            f"{'—' if up_k is None else f'{up_k:,.0f} CE'}</b> <span style='color:{mut}'>{_esc(up_why)}</span></td></tr>"
            f"<tr><td style='color:{mut}'>Max danger strike (downside)</td><td><b style='color:{C['up']}'>"
            f"{'—' if dn_k is None else f'{dn_k:,.0f} PE'}</b> <span style='color:{mut}'>{_esc(dn_why)}</span></td></tr>"
            f"</table>"
            f"<div style='color:{mut};font-size:11px;margin-top:6px'>Spot <b style='color:{C['text']}'>"
            f"{r['spot']:,.1f}</b> · expiry {r['expiry']} ({r['dte']:.1f}d) · ATM IV {r['atm_iv'] or 0:.2f}% · "
            f"VIX {r['vix'] if r['vix'] is not None else '—'} · 1σ to expiry {r['sigma']:,.0f} pts · "
            f"walls {r['put_wall'] or 0:,.0f} PE / {r['call_wall'] or 0:,.0f} CE · net GEX "
            f"{g['net'] / 1e7:+.2f} Cr/1%" + (f" (flip ≈ {g['flip']:,.0f})" if g.get("flip") else "") +
            f" · intraday window {win} · {src} {_esc(r.get('timestamp') or '')}</div>")
        cards = []
        for a in r["alerts"]:
            bg, border, fg = LEVEL_STYLE[a["level"]]
            tag = {"RED": "RED ALERT", "AMBER": "AMBER ALERT", "GREEN": "GREEN FLAG"}[a["level"]]
            sigs = "".join(f"<div style='margin:2px 0'>• {_esc(s)}</div>" for s in a["signals"])
            # a table cell paints a full-width block (QLabel's rich text ignores div backgrounds' extent)
            cards.append(f"<table width='100%' cellspacing='0' cellpadding='8' style='margin:4px 0'>"
                         f"<tr><td style='background:{bg};border-left:4px solid {border}'>"
                         f"<b style='color:{border}'>[{tag}]</b> <b style='color:{fg}'>{_esc(a['title'])}</b>"
                         f"<div style='color:{fg};margin-top:4px'>{sigs}</div>"
                         f"<div style='margin-top:4px;color:#ffffff'><b>→ {_esc(a['action'])}</b></div>"
                         f"</td></tr></table>")
        self.alerts.setText("".join(cards))
        sec = lambda t, items, c: (f"<div style='margin-top:6px'><b style='color:{c}'>{t}</b></div>"
                                   + "".join(f"<div style='margin:2px 0 2px 8px'>• {_esc(x)}</div>" for x in items))
        self.directives.setText(sec("Strict prohibitions", r["prohibitions"], C["down"])
                                + sec("Safe / permissible zones (beyond 0.10Δ and 1.5× expected move)", r["safe"],
                                      C["up"])
                                + sec("Existing position guidance", r["positions"], C["accent"]))


class RiskDashboard(QWidget):
    refresh_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        bar, hl = toolbar()
        self.status = label("Loading…")
        hl.addWidget(self.status, 1)
        self.auto = QCheckBox("Auto-refresh (30 s here, 3 min in the background)")
        self.auto.setChecked(True)
        hl.addWidget(self.auto)
        self.notify = QCheckBox("Sound + alert on new RED")
        self.notify.setChecked(True)
        hl.addWidget(self.notify)
        b = QPushButton("Refresh now", objectName="primary")
        b.clicked.connect(self.refresh_requested.emit)
        hl.addWidget(b)
        outer.addWidget(bar)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(objectName="root")
        v = QVBoxLayout(body)
        v.setContentsMargins(0, 0, 4, 0)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.cols = {"NIFTY": RiskColumn("NIFTY"), "BANKNIFTY": RiskColumn("BANKNIFTY")}
        for c in self.cols.values():
            row.addWidget(c, 1)
        v.addLayout(row)
        v.addWidget(label(f"<span style='color:{C['muted']}'>{_esc(NOTE)}</span>", 11))
        v.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        self.results: dict[str, dict] = {}

    def set_status(self, text: str):
        self.status.setText(text)

    def set_result(self, und: str, r: dict):
        self.results[und] = r
        self.cols[und].set_result(r)

    def set_error(self, und: str, msg: str):
        self.cols[und].set_error(msg)
