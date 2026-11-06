"""Realtime intraday charts for NIFTY and BANKNIFTY (TradingView Lightweight Charts).

The chart itself is chart_web/chart.html, drawn by TradingView's open-source
Lightweight Charts library (vendor/, Apache-2.0) inside a QWebEngineView.
This module keeps the 1-minute bars (seeded from Shoonya TPSeries or Yahoo,
updated tick-by-tick from the Shoonya websocket), pushes them to the page, and
passes the page's context from the app: daily support/resistance zones, option
OI walls and max pain, and your open option strikes.

The page computes timeframes (1m-1H, 1D), chart types, indicators (EMA, SMA,
Bollinger, Supertrend, session VWAP, RSI, MACD, ATR), levels (PDH/PDL/PDC, CPR,
pivots, Camarilla, previous week, opening range), drawings (horizontal and trend
lines, measure) and price alerts.  Page -> Python messages arrive as
console lines prefixed "PY:" (state to save, alerts fired, screenshots).

Times are sent as IST wall-clock encoded as UTC epoch seconds, so the chart's
axis reads in IST whatever the machine's time zone.
"""
from __future__ import annotations

import base64
import calendar
import json
import logging
import sys
import time as _time
from datetime import datetime, time as dtime
from pathlib import Path

import pandas as pd
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

import config
from live_view import key_zones
from theme import C

log = logging.getLogger(__name__)

Bar = tuple  # (datetime IST-naive, open, high, low, close)

SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)
SYMBOLS = (("^NSEI", "NIFTY 50"), ("NIFTY BANK", "BANKNIFTY"))
UND_SYMBOL = {"NIFTY": "^NSEI", "BANKNIFTY": "NIFTY BANK"}
# inside the packaged app the bundled files live under sys._MEIPASS
PAGE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "chart_web" / "chart.html"
STATE_FILE = config.APP_HOME / "chart_state.json"


def in_session(ts: datetime) -> bool:
    return SESSION_OPEN <= ts.time() < SESSION_CLOSE


def _epoch(ts) -> int:
    """IST-naive datetime/date -> 'IST as UTC' epoch seconds for the chart axis."""
    if isinstance(ts, pd.Timestamp):
        ts = ts.tz_localize(None).to_pydatetime() if ts.tzinfo else ts.to_pydatetime()
    return calendar.timegm(ts.timetuple())


class Series:
    """1-minute bars for one index, merged from history pulls and live ticks."""

    def __init__(self):
        self.bars: dict[datetime, list] = {}
        self.last: float | None = None
        self.last_ts: datetime | None = None
        self.quote_prev: float | None = None      # broker/NSE previous close, if known
        self._pushed_at = 0.0

    def merge(self, bars: list[Bar]):
        latest = max(self.bars) if self.bars else None
        for ts, o, h, l, c in bars:
            if not in_session(ts):
                continue
            ts = ts.replace(second=0, microsecond=0)
            cur = self.bars.get(ts)
            if cur and ts == latest:
                # keep the live-built extremes for the forming minute
                cur[1], cur[2], cur[3] = o, max(cur[2], h), min(cur[3], l)
            else:
                self.bars[ts] = [ts, o, h, l, c]
        if self.bars and (self.last_ts is None or _time.time() - self._pushed_at > 60):
            self.last = self.bars[self._latest()][4]   # no recent tick: history close is the latest

    def push(self, price: float, ts: datetime) -> bool:
        if price is None or not in_session(ts):
            return False
        m = ts.replace(second=0, microsecond=0)
        b = self.bars.get(m)
        if b is None:
            self.bars[m] = [m, price, price, price, price]
        else:
            b[2], b[3], b[4] = max(b[2], price), min(b[3], price), price
        self.last, self.last_ts, self._pushed_at = price, ts, _time.time()
        return True

    def _latest(self) -> datetime:
        return max(self.bars)

    def sorted(self) -> list[Bar]:
        return [tuple(self.bars[k]) for k in sorted(self.bars)]

    def sessions(self) -> list[list[Bar]]:
        out: dict = {}
        for b in self.sorted():
            out.setdefault(b[0].date(), []).append(b)
        return [out[d] for d in sorted(out)]


class _Page(QWebEnginePage):
    message = Signal(dict)

    def javaScriptConsoleMessage(self, level, msg, line, source):
        if msg.startswith("PY:"):
            try:
                self.message.emit(json.loads(msg[3:]))
            except ValueError:
                pass
        elif level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            log.warning("chart page: %s (line %s)", msg, line)


class RealtimeCharts(QWidget):
    reload_requested = Signal()
    alert_fired = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.series = {sym: Series() for sym, _ in SYMBOLS}
        self.daily: dict[str, list] = {}
        self.ctx: dict[str, dict] = {sym: {} for sym, _ in SYMBOLS}
        self.ready = False
        self._queue: list[str] = []
        self._sent_len = {sym: 0 for sym, _ in SYMBOLS}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)

        bar = QFrame()
        bar.setStyleSheet(f"QFrame {{ background:{C['panel']}; border:1px solid {C['border']}; border-radius:8px; }}"
                          "QLabel { border:none; background:transparent; }")
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(12, 4, 12, 4)
        title = QLabel("<b>Realtime chart</b> <span style='color:#94a3b8'>· NIFTY 50 &amp; BANKNIFTY · keys: 1–8 "
                       "timeframe, H line, T trend, M measure, A alert, Ctrl+Z undo, Esc cancel</span>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title.setStyleSheet("font-size:11px")
        hl.addWidget(title, 1)
        self.source = QLabel("")
        self.source.setTextFormat(Qt.TextFormat.RichText)
        self.source.setStyleSheet("font-size:11px")
        hl.addWidget(self.source)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self.reload_requested.emit)
        hl.addWidget(reload_btn)
        lay.addWidget(bar)

        self.view = QWebEngineView()
        self.page = _Page(self.view)
        self.page.setBackgroundColor(Qt.GlobalColor.black)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        self.page.message.connect(self._on_message)
        self.view.setPage(self.page)
        self.view.setUrl(QUrl.fromLocalFile(str(PAGE)))
        lay.addWidget(self.view, 1)

    # ---------------------------------------------------------- page bridge
    def _js(self, code: str):
        if self.ready:
            self.page.runJavaScript(code)
        else:
            self._queue.append(code)

    def _call(self, fn: str, *args):
        self._js(f"App.{fn}(" + ",".join(json.dumps(a, default=str) for a in args) + ")")

    def _on_message(self, m: dict):
        kind = m.get("type")
        if kind == "ready":
            self.ready = True
            try:
                state = json.loads(STATE_FILE.read_text())
            except (OSError, ValueError):
                state = None
            self.page.runJavaScript(f"App.init({json.dumps(state)})")
            for code in self._queue:
                self.page.runJavaScript(code)
            self._queue.clear()
            for sym, _ in SYMBOLS:
                self._send_bars(sym)
                self._call("setContext", sym, self.ctx[sym])
        elif kind == "save":
            try:
                STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
                STATE_FILE.write_text(json.dumps(m.get("state") or {}))
            except OSError as exc:
                log.warning("chart state not saved: %s", exc)
        elif kind == "alert":
            self.alert_fired.emit(m.get("message", "Price alert"))
        elif kind == "screenshot":
            self._save_png(m.get("sym", "chart"), m.get("data", ""))

    def _save_png(self, name: str, data_url: str):
        if "," not in data_url:
            return
        default = str(Path.home() / f"{name.replace(' ', '_')}_{datetime.now():%Y%m%d_%H%M}.png")
        path, _ = QFileDialog.getSaveFileName(self, f"Save {name} chart", default, "PNG image (*.png)")
        if path:
            Path(path).write_bytes(base64.b64decode(data_url.split(",", 1)[1]))

    # ---------------------------------------------------------- data in
    def _bars_json(self, sym: str) -> list:
        return [[_epoch(b[0]), round(b[1], 2), round(b[2], 2), round(b[3], 2), round(b[4], 2)]
                for b in self.series[sym].sorted()]

    def _send_bars(self, sym: str):
        bars = self._bars_json(sym)
        if not bars:
            return
        self._sent_len[sym] = len(bars)
        self._call("setBars", sym, bars, self.daily.get(sym))

    def set_history(self, data: dict[str, list[Bar]], source: str):
        for sym, bars in data.items():
            if sym in self.series:
                self.series[sym].merge(bars)
                self._send_bars(sym)
        self.set_source(source)

    def set_daily(self, sym: str, df: pd.DataFrame | None):
        if df is None or df.empty or sym not in self.series:
            return
        df = df.tail(800)
        self.daily[sym] = [[_epoch(ts.normalize()), round(float(o), 2), round(float(h), 2), round(float(l), 2),
                            round(float(c), 2)]
                           for ts, o, h, l, c in zip(df.index, df["open"], df["high"], df["low"], df["close"])]

    def set_source(self, source: str):
        live = source.lower().startswith("shoonya")
        col = C["up"] if live else "#f59e0b"
        self.source.setText(f"<span style='color:{col}'>● {source}</span>")
        self._call("setSource", source, live)

    def set_levels(self, levels: dict[str, dict]):
        for sym in self.ctx:
            doc = (levels or {}).get(sym)
            if not doc:
                continue
            sup, res = key_zones(doc)
            zones = [{"lo": z["low"], "hi": z["high"], "kind": "support"} for z in sup] + \
                    [{"lo": z["low"], "hi": z["high"], "kind": "resistance"} for z in res]
            self._set_ctx(sym, zones=zones)

    def set_option_levels(self, underlying: str, res: dict):
        sym = UND_SYMBOL.get(underlying)
        if sym:
            self._set_ctx(sym, options={"call_walls": res.get("call_walls") or [], "put_walls": res.get("put_walls") or [],
                                        "max_pain": res.get("max_pain"), "expiry": res.get("expiry")})

    def set_positions(self, groups: list[dict]):
        by: dict[str, list] = {sym: [] for sym in self.ctx}
        for g in groups or []:
            sym = UND_SYMBOL.get(g.get("und"))
            if not sym:
                continue
            for l in g.get("legs", []):
                if l.get("qty") and l.get("strike") and l.get("kind") in ("CE", "PE"):
                    lot = (config.NIFTY_LOT if g["und"] == "NIFTY" else config.BANKNIFTY_LOT) or 1
                    by[sym].append({"strike": l["strike"], "kind": l["kind"], "qty": l["qty"],
                                    "lots": round(l["qty"] / lot, 1), "expiry": str(g.get("expiry"))})
        for sym, legs in by.items():
            self._set_ctx(sym, positions=legs)

    def _set_ctx(self, sym: str, **kw):
        self.ctx[sym].update(kw)
        self._call("setContext", sym, kw)

    def set_prev_close(self, sym: str, prev: float | None):
        if sym in self.series and prev:
            self.series[sym].quote_prev = prev
            self.ctx[sym]["prev_close"] = prev

    def push_tick(self, sym: str, price: float, ts: datetime) -> bool:
        s = self.series.get(sym)
        if not (s and s.push(price, ts)):
            return False
        m = ts.replace(second=0, microsecond=0)
        b = s.bars[m]
        if len(s.bars) < self._sent_len[sym]:
            self._send_bars(sym)
        else:
            self._sent_len[sym] = len(s.bars)
            self._call("tick", sym, [_epoch(m), b[1], b[2], b[3], b[4]])
        return True

    def refit(self):
        """Re-apply the default view (after data arrived while the page was hidden)."""
        self._call("refit")

    def render(self):
        """Ticks are pushed as they arrive; kept for API compatibility."""
