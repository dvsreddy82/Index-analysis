"""NSE Quant Terminal — PySide6 desktop app.

Layout
------
 ┌ Sidebar ─────┬ Chart (Plotly in QWebEngineView) ─────────────┬ Signal panel ────┐
 │ symbol       │ candles + EMAs + S/R zones + pivots │ VolProf │ Decision card     │
 │ timeframe    │ RSI                                            │ Institutional     │
 │ overlays     │ FII vs DII cash histogram + FII L/S ratio      │ Key levels table  │
 │ sync / csv   │                                                │ Catalysts         │
 └──────────────┴────────────────────────────────────────────────┴───────────────────┘

Run:  python app.py
"""
from __future__ import annotations

import html
import logging
import sys
import traceback
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QFontDatabase
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QMainWindow, QMessageBox, QPushButton, QScrollArea, QSplitter,
                               QStatusBar, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

import config
from data_fetcher import DataService, import_fii_dii_csv
from indicators import analyze, to_sr_document
from live_view import CHART_DAYS, LiveDashboard
from options import UNDERLYINGS, analyze_chain
from options_view import OptionsDashboard

log = logging.getLogger("app")

# ------------------------------------------------------------------ theme
C = {
    "bg": "#0b0f17", "panel": "#111827", "panel2": "#0f172a", "border": "#1f2937",
    "text": "#e5e7eb", "muted": "#94a3b8", "accent": "#38bdf8",
    "up": "#22c55e", "down": "#ef4444", "fii": "#3b82f6", "dii": "#f59e0b",
    "ema20": "#facc15", "ema50": "#38bdf8", "ema200": "#c084fc",
}

QSS = f"""
* {{ font-size: 12px; color: {C['text']}; }}
QMainWindow, QWidget#root {{ background: {C['bg']}; }}
QFrame#sidebar {{ background: {C['panel2']}; border-right: 1px solid {C['border']}; }}
QFrame.card {{ background: {C['panel']}; border: 1px solid {C['border']}; border-radius: 8px; }}
QLabel.h1 {{ font-size: 16px; font-weight: 700; color: {C['accent']}; }}
QLabel.h2 {{ font-size: 11px; font-weight: 700; color: {C['muted']}; letter-spacing: 1px; }}
QLabel.muted {{ color: {C['muted']}; }}
QComboBox, QPushButton {{ background: {C['panel']}; border: 1px solid {C['border']};
    border-radius: 6px; padding: 6px 10px; }}
QComboBox QAbstractItemView {{ background: {C['panel']}; selection-background-color: #1e293b; }}
QPushButton:hover {{ border-color: {C['accent']}; }}
QPushButton:checked {{ background: #0c4a6e; border-color: {C['accent']}; }}
QPushButton#primary {{ background: #0369a1; border-color: #0284c7; font-weight: 600; }}
QPushButton#primary:hover {{ background: #0284c7; }}
QPushButton:disabled {{ color: #475569; }}
QCheckBox::indicator {{ width: 14px; height: 14px; }}
QTableWidget {{ background: {C['panel']}; gridline-color: {C['border']}; border: none; }}
QHeaderView::section {{ background: {C['panel2']}; color: {C['muted']}; border: none;
    border-bottom: 1px solid {C['border']}; padding: 4px; font-weight: 600; }}
QScrollArea {{ border: none; background: {C['bg']}; }}
QStatusBar {{ background: {C['panel2']}; color: {C['muted']}; }}
QSplitter::handle {{ background: {C['border']}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: {C['panel2']}; color: {C['muted']}; padding: 6px 16px; border: none;
    border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {C['text']}; border-bottom: 2px solid {C['accent']}; }}
"""


# ------------------------------------------------------------------ background tasks
class TaskSignals(QObject):
    progress = Signal(str)
    done = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    """Run `fn(progress_cb)` on the thread pool and report back via signals."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = TaskSignals()

    def run(self):
        try:
            self.signals.done.emit(self.fn(self.signals.progress.emit))
        except Exception as exc:
            log.exception("Background task failed")
            self.signals.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=3)}")


def pair_ratio(svc: DataService, symbol: str, daily: pd.DataFrame) -> dict | None:
    """Price ratio between an index and its tracking ETF (either direction),
    measured on the latest date both series have, to translate levels."""
    other = config.pair_of(symbol)
    if not other:
        return None
    od = svc.load_ohlcv(other, "1D")
    if od.empty:
        return None
    a = daily["close"].groupby(daily.index.normalize()).last()
    b = od["close"].groupby(od.index.normalize()).last()
    common = a.index.intersection(b.index)
    if common.empty:
        return None
    d = common[-1]
    return {"symbol": other, "name": config.SYMBOLS[other], "kind": config.INSTRUMENTS[other].kind,
            "ratio": float(b[d] / a[d]), "as_of": d.strftime("%Y-%m-%d")}


class NumItem(QTableWidgetItem):
    """Table cell that sorts by its numeric value instead of its text."""

    def __init__(self, text: str, value: float | None):
        super().__init__(text)
        self.setData(Qt.ItemDataRole.UserRole, float("-inf") if value is None else value)

    def __lt__(self, other):
        return self.data(Qt.ItemDataRole.UserRole) < other.data(Qt.ItemDataRole.UserRole)


# ------------------------------------------------------------------ chart
def _naive_ist(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return idx.tz_convert(config.IST).tz_localize(None) if idx.tz is not None else idx


def build_figure(res: dict, fii: pd.DataFrame, overlays: dict) -> go.Figure:
    df = res["frame"].copy()
    df.index = _naive_ist(df.index)
    intraday = res["timeframe"] != "1D"
    vp, mp = res["volume_profile"] or {}, res["monthly_pivots"] or {}

    fig = make_subplots(
        rows=3, cols=2, shared_xaxes=True, column_widths=[0.87, 0.13], row_heights=[0.62, 0.15, 0.23],
        horizontal_spacing=0.004, vertical_spacing=0.025,
        specs=[[{}, {}], [{}, None], [{"secondary_y": True}, None]],
    )

    # --- price
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"], name="Price",
        increasing=dict(line=dict(color=C["up"], width=1), fillcolor=C["up"]),
        decreasing=dict(line=dict(color=C["down"], width=1), fillcolor=C["down"])), row=1, col=1)
    if overlays.get("ema"):
        for n in (20, 50, 200):
            fig.add_trace(go.Scatter(x=df.index, y=df[f"ema{n}"], name=f"EMA {n}", mode="lines",
                                     line=dict(color=C[f"ema{n}"], width=1.2)), row=1, col=1)

    # --- S/R zones (opacity scales with strength)
    if overlays.get("zones"):
        top = max([z["strength"] for z in res["support_zones"] + res["resistance_zones"]] or [1])
        for kind, zones, rgb in (("S", res["support_zones"], "34,197,94"),
                                 ("R", res["resistance_zones"], "239,68,68")):
            for z in zones[:6]:
                a = 0.10 + 0.25 * z["strength"] / top
                fig.add_hrect(y0=z["low"], y1=z["high"], row=1, col=1, line_width=0,
                              fillcolor=f"rgba({rgb},{a:.2f})", layer="below")
                fig.add_annotation(x=1, xref="x domain", y=z["mid"], yref="y", xanchor="right",
                                   text=f"{kind} {z['mid']:.2f} · {z['touches']}t",
                                   showarrow=False, font=dict(size=9, color=f"rgb({rgb})"),
                                   bgcolor="rgba(11,15,23,0.6)")
    if overlays.get("pivots") and mp.get("standard"):
        for k in ("S2", "S1", "P", "R1", "R2"):
            v = mp["standard"][k]
            fig.add_hline(y=v, row=1, col=1, line=dict(color="#64748b", width=1, dash="dot"),
                          annotation_text=f"M-{k} {v:.2f}", annotation_position="top left",
                          annotation_font=dict(size=9, color="#94a3b8"))
    proj = res["projection"]
    for lbl, v in (("Proj low", proj["lower"]), ("Proj high", proj["upper"])):
        fig.add_hline(y=v, row=1, col=1, line=dict(color=C["accent"], width=1.2, dash="dash"),
                      annotation_text=f"{lbl} {v:.2f}", annotation_position="bottom left",
                      annotation_font=dict(size=9, color=C["accent"]))

    # --- volume profile (shares the price axis)
    if overlays.get("vp") and vp:
        colors = ["rgba(56,189,248,0.55)" if vp["val"] <= p <= vp["vah"] else "rgba(100,116,139,0.35)"
                  for p in vp["prices"]]
        fig.add_trace(go.Bar(x=vp["volumes"], y=vp["prices"], orientation="h", marker_color=colors,
                             name="Volume profile" + (" (TPO)" if vp["tpo_mode"] else ""),
                             hovertemplate="%{y:.2f}<extra></extra>", showlegend=False), row=1, col=2)
        for lbl, v, col in (("POC", vp["poc"], "#f472b6"), ("VAH", vp["vah"], "#38bdf8"),
                            ("VAL", vp["val"], "#38bdf8")):
            fig.add_hline(y=v, row=1, col=1, line=dict(color=col, width=1, dash="dashdot"),
                          annotation_text=f"{lbl} {v:.2f}", annotation_position="top right",
                          annotation_font=dict(size=9, color=col))
        fig.update_yaxes(matches="y", showticklabels=False, row=1, col=2)
        fig.update_xaxes(showticklabels=False, showgrid=False, row=1, col=2)

    # --- RSI
    fig.add_trace(go.Scatter(x=df.index, y=df["rsi"], name="RSI 14", mode="lines",
                             line=dict(color="#a78bfa", width=1.2)), row=2, col=1)
    for lvl, col in ((70, C["down"]), (30, C["up"])):
        fig.add_hline(y=lvl, row=2, col=1, line=dict(color=col, width=0.8, dash="dot"))
    fig.update_yaxes(range=[10, 90], row=2, col=1, title_text="RSI")

    # --- FII vs DII cash (aligned by date) + FII index-futures long/short ratio
    if fii is not None and not fii.empty:
        f = fii[fii.index >= df.index[0].normalize()].copy()
        # Intraday axes hide non-market hours, so pin each daily bar to mid-session.
        fx = f.index + (pd.Timedelta(hours=12, minutes=30) if intraday else pd.Timedelta(0))
        if "fii_cash_net" in f:
            fig.add_trace(go.Bar(x=fx, y=f["fii_cash_net"], name="FII cash net (₹cr)",
                                 marker_color=C["fii"]), row=3, col=1)
        if "dii_cash_net" in f:
            fig.add_trace(go.Bar(x=fx, y=f["dii_cash_net"], name="DII cash net (₹cr)",
                                 marker_color=C["dii"]), row=3, col=1)
        if "fii_index_futures_long_short_ratio" in f:
            fig.add_trace(go.Scatter(x=fx, y=f["fii_index_futures_long_short_ratio"], mode="lines+markers",
                                     name="FII idx-fut L/S", line=dict(color="#e879f9", width=1.5),
                                     marker=dict(size=3)), row=3, col=1, secondary_y=True)
        fig.update_yaxes(title_text="₹ cr", row=3, col=1, secondary_y=False)
        fig.update_yaxes(title_text="L/S", row=3, col=1, secondary_y=True, showgrid=False)

    # --- x-axis gaps: weekends, NSE holidays, and (intraday) non-market hours
    present = pd.DatetimeIndex(df.index.normalize().unique())
    bdays = pd.bdate_range(present.min(), present.max())
    holidays = [d.strftime("%Y-%m-%d") for d in bdays.difference(present)]
    breaks = [dict(bounds=["sat", "mon"]), dict(values=holidays)]
    if intraday:
        breaks.append(dict(bounds=[15.5, 9.25], pattern="hour"))
    fig.update_xaxes(rangebreaks=breaks, col=1, gridcolor=C["border"], showspikes=True,
                     spikemode="across", spikethickness=1, spikecolor="#475569")
    fig.update_xaxes(rangeslider_visible=False)

    # Initial view: last ~120 bars, y fitted to them plus the projection band.
    view = df.tail(120 if not intraday else 200)
    lo = min(view["low"].min(), proj["lower"]) * 0.995
    hi = max(view["high"].max(), proj["upper"]) * 1.005
    # Shared x-axes are linked, so the range must be set on every column-1 axis.
    fig.update_xaxes(range=[view.index[0], df.index[-1] + (df.index[-1] - df.index[-2]) * 3], col=1)
    fig.update_yaxes(range=[lo, hi], row=1, col=1, side="right", gridcolor=C["border"])

    fig.update_layout(
        template="plotly_dark", paper_bgcolor=C["bg"], plot_bgcolor=C["bg"],
        margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified", barmode="group",
        legend=dict(orientation="h", y=1.03, x=0, font=dict(size=10), bgcolor="rgba(0,0,0,0)"),
        title=dict(text=f"{res['symbol']} · {res['timeframe']} · last {res['close']:.2f}",
                   font=dict(size=13, color=C["muted"]), x=0.005, y=0.995),
        dragmode="pan", font=dict(color=C["text"]),
    )
    return fig


# ------------------------------------------------------------------ widgets
def card(title: str) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setProperty("class", "card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(12, 10, 12, 12)
    lay.setSpacing(6)
    t = QLabel(title.upper())
    t.setProperty("class", "h2")
    lay.addWidget(t)
    return f, lay


def wrap_label(text: str = "", cls: str | None = None) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if cls:
        lbl.setProperty("class", cls)
    return lbl


def bullets(items: list[str], color: str = C["text"]) -> str:
    return "".join(f"<div style='margin:2px 0;color:{color}'>• {html.escape(i)}</div>" for i in items)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NSE Quant Terminal — Index & ETF Swing Desk")
        # Fit the available screen (never larger than it), centred.
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1680, int(avail.width() * 0.95)), min(980, int(avail.height() * 0.92)))
        self.move(avail.center() - self.rect().center())
        self.pool = QThreadPool.globalInstance()
        self._tasks: set[Task] = set()   # keep tasks (and their signal objects) alive until finished
        self.result: dict | None = None
        self.fii = pd.DataFrame()
        self._chart_seq = 0
        self.svc = DataService()

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        self.split = split
        outer.addWidget(split)
        self.sidebar = self._build_sidebar()
        split.addWidget(self.sidebar)

        self.web = QWebEngineView()
        s = self.web.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.web.page().setBackgroundColor(Qt.GlobalColor.black)
        self.web.setHtml(f"<body style='background:{C['bg']};color:{C['muted']};"
                         "font-family:sans-serif;padding:40px'>Loading market data…</body>")
        self.live = LiveDashboard()
        self.live.open_symbol.connect(self.open_symbol)
        self.live.refresh_requested.connect(self.poll_live)
        self.live.levels_requested.connect(lambda: self.compute_levels(force=True))
        self.live.sync_requested.connect(self.sync)
        self.live.interval_changed.connect(lambda s: self.live_timer.setInterval(s * 1000))
        self.tabs = QTabWidget()
        self.tabs.addTab(self.live, "● Live — all indices")
        self.tabs.addTab(self.web, "Chart")
        self.tabs.addTab(self._build_scanner(), "Scanner")
        self.opts = OptionsDashboard()
        self.opts.refresh_requested.connect(self.load_options)
        self.tabs.addTab(self.opts, "Options — NIFTY / BANKNIFTY")
        self._opt_busy = False
        self._opt_engine: dict[str, tuple[float, dict, pd.DataFrame]] = {}   # underlying -> (ts, analysis, daily)
        self.opt_timer = QTimer(self)
        self.opt_timer.setInterval(config.OPTIONS_REFRESH_SECONDS * 1000)
        self.opt_timer.timeout.connect(lambda: self.tabs.currentWidget() is self.opts and self.opts.request())
        self.opt_timer.start()
        self.tabs.currentChanged.connect(self._tab_changed)
        split.addWidget(self.tabs)
        self.panel = self._build_panel()
        split.addWidget(self.panel)
        split.setSizes([230, 1030, 420])
        self.panel.setVisible(False)    # Live tab uses the full width…
        self.sidebar.setVisible(False)  # …its own header has refresh/sync controls

        self._live_busy = False
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(config.LIVE_REFRESH_SECONDS * 1000)
        self.live_timer.timeout.connect(self.poll_live)
        self.live_timer.start()
        split.setStretchFactor(1, 1)

        self.setStatusBar(QStatusBar())
        store = "MongoDB · nse_analytics" if self.svc.persistent else "IN-MEMORY (MongoDB unreachable)"
        self.db_lbl.setText(f"Store: {store}")
        self.db_lbl.setStyleSheet(f"color:{C['up'] if self.svc.persistent else C['down']}")
        self.run_analysis()
        self.compute_levels()
        self.poll_live()

    # ---------------------------------------------------------- live
    def _tab_changed(self, idx: int):
        page = self.tabs.widget(idx)
        self.panel.setVisible(page is self.web)
        self.sidebar.setVisible(page is not self.live and page is not self.opts)
        if page is self.web:
            # Sidebar ~15%, signal panel ~26% (min 340px), chart gets the rest.
            w = self.split.width()
            side, panel = max(200, int(w * 0.15)), max(340, int(w * 0.26))
            self.split.setSizes([side, max(w - side - panel, 300), panel])
        if self.tabs.widget(idx) is self.live:
            self.poll_live()
        if page is self.opts and self.opts.res is None:
            self.opts.request()

    # ---------------------------------------------------------- options
    def load_options(self, underlying: str, expiry: str):
        if self._opt_busy:
            self._opt_pending = (underlying, expiry)    # run the latest request once this one returns
            return
        self._opt_busy, self._opt_pending = True, None
        svc, cache = self.svc, self._opt_engine

        def job(progress):
            import time
            js, exp, _ = svc.option_chain(underlying, expiry or None)
            hit = cache.get(underlying)
            if hit is None or time.time() - hit[0] > 600:      # the EOD engine read changes slowly
                sym = UNDERLYINGS[underlying]["daily"]
                daily = svc.load_ohlcv(sym, "1D")
                engine = analyze(sym, "1D", daily, daily, svc.load_fii_dii(), svc.load_macro()) if len(daily) > 60 else {}
                hit = cache[underlying] = (time.time(), engine, daily)
            macro = svc.load_macro()
            vix = macro["indiavix"] if "indiavix" in macro else None
            return analyze_chain(underlying, js, exp, hit[2], vix, hit[1])

        task = Task(job)
        task.signals.done.connect(self._options_done)
        task.signals.failed.connect(self._options_failed)
        self._start(task)

    def _options_done(self, res: dict):
        self._opt_busy = False
        self.opts.set_result(res)
        self._options_next()

    def _options_failed(self, err: str):
        self._opt_busy = False
        self.opts.set_error(err.splitlines()[0] if err else "unknown error")
        self._options_next()

    def _options_next(self):
        if getattr(self, "_opt_pending", None):
            self.load_options(*self._opt_pending)

    def poll_live(self):
        """Fetch live quotes in the background; skipped while hidden or paused."""
        if self._live_busy or self.live.paused() or self.tabs.currentWidget() is not self.live:
            return
        self._live_busy = True
        task = Task(lambda _p: self.svc.live_snapshot())
        task.signals.done.connect(self._live_done)
        task.signals.failed.connect(self._live_failed)
        self._start(task)

    def _live_done(self, snap: dict):
        self._live_busy = False
        if not snap.get("quotes"):
            self.live.ts.setText("NSE live feed unavailable — retrying")
            return
        self.live.update_snapshot(snap)

    def _live_failed(self, err: str):
        self._live_busy = False
        log.warning("Live poll failed: %s", err.splitlines()[0])
        self.live.ts.setText("Live poll failed — retrying")

    def compute_levels(self, force: bool = False):
        """Load stored daily levels for the live tiles; analyse any index that has
        none yet (or all of them when `force`)."""
        svc = self.svc

        def job(progress):
            levels = {} if force else svc.latest_levels(config.INDEX_SYMBOLS)
            fii, macro = svc.load_fii_dii(), svc.load_macro()
            todo = [s for s in config.INDEX_SYMBOLS if s not in levels]
            for i, sym in enumerate(todo, 1):
                progress(f"Computing levels {i}/{len(todo)}: {config.SYMBOLS[sym]}")
                try:
                    daily = svc.load_ohlcv(sym, "1D")
                    svc.save_sr_levels(to_sr_document(analyze(sym, "1D", daily, daily, fii, macro)))
                except Exception as exc:
                    log.warning("Levels failed for %s: %s", sym, exc)
            last = fii.dropna(subset=["fii_cash_net"]).iloc[-1] if "fii_cash_net" in fii else None
            flows = (None if last is None else
                     (last.get("fii_cash_net"), last.get("dii_cash_net"), f"{last.name:%d-%b}"))
            # Last CHART_DAYS daily bars per index for the live mini charts.
            history = {}
            for sym in config.INDEX_SYMBOLS:
                d = svc.load_ohlcv(sym, "1D")
                if d is not None and not d.empty:
                    d = d.tail(CHART_DAYS)
                    history[sym] = [(ts.date(), float(o), float(h), float(l), float(c)) for ts, o, h, l, c in
                                    zip(d.index, d["open"], d["high"], d["low"], d["close"])]
            return svc.latest_levels(config.INDEX_SYMBOLS), flows, history

        self.live.lvl_btn.setEnabled(False)
        self.live.sync_btn.setEnabled(False)
        task = Task(job)
        task.signals.progress.connect(self._status)
        task.signals.done.connect(self._levels_done)
        task.signals.failed.connect(self._levels_failed)
        self._start(task)

    def _levels_done(self, payload):
        levels, flows, history = payload
        self.live.set_levels(levels)
        self.live.set_history(history)
        if flows:
            self.live.set_flows(*flows)
        self.live.lvl_btn.setEnabled(True)
        self.live.sync_btn.setEnabled(True)
        self._status(f"Live levels ready for {len(levels)}/{len(config.INDEX_SYMBOLS)} indices")

    def _levels_failed(self, err: str):
        self.live.lvl_btn.setEnabled(True)
        self.live.sync_btn.setEnabled(True)
        self._failed(err)

    def _start(self, task: Task):
        self._tasks.add(task)
        for sig in (task.signals.done, task.signals.failed):
            sig.connect(lambda *_a, t=task: self._tasks.discard(t))
        self.pool.start(task)

    def open_symbol(self, sym: str):
        self.tabs.setCurrentWidget(self.web)
        idx = self.sym.findData(sym)
        if idx == self.sym.currentIndex():
            self.run_analysis()
        else:
            self.sym.setCurrentIndex(idx)

    # ---------------------------------------------------------- sidebar
    def _build_sidebar(self) -> QFrame:
        side = QFrame(objectName="sidebar")
        side.setMinimumWidth(210)
        side.setMaximumWidth(280)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 16, 14, 14)
        lay.setSpacing(10)
        t = QLabel("NSE QUANT TERMINAL")
        t.setProperty("class", "h1")
        lay.addWidget(t)
        lay.addWidget(wrap_label("Buy on dips · Sell on rallies", "muted"))

        lay.addWidget(self._h2("Instrument"))
        self.sym = QComboBox()
        self.sym.setMaxVisibleItems(32)
        groups: dict[str, list] = {}
        for inst in config.INSTRUMENTS.values():
            groups.setdefault(inst.group, []).append(inst)
        for group, items in groups.items():
            self.sym.addItem(f"──  {group.upper()}  ──")
            self.sym.model().item(self.sym.count() - 1).setEnabled(False)
            for inst in items:
                label = f"{inst.symbol.removesuffix('.NS')}  ·  tracks {config.SYMBOLS[inst.underlying]}" \
                    if inst.kind == "etf" else inst.name
                self.sym.addItem(label, inst.symbol)
        self.sym.setCurrentIndex(self.sym.findData(config.DEFAULT_SYMBOL))
        self.sym.currentIndexChanged.connect(self.run_analysis)
        lay.addWidget(self.sym)

        lay.addWidget(self._h2("Timeframe"))
        row = QHBoxLayout()
        self.tf_group = QButtonGroup(self)
        for i, tf in enumerate(config.TIMEFRAMES):
            b = QPushButton(tf)
            b.setCheckable(True)
            b.setChecked(i == 0)
            self.tf_group.addButton(b)
            row.addWidget(b)
        self.tf_group.buttonClicked.connect(lambda _: self.run_analysis())
        lay.addLayout(row)

        lay.addWidget(self._h2("Overlays"))
        self.ov = {}
        for key, label in (("zones", "Support / resistance zones"), ("ema", "EMA 20 / 50 / 200"),
                           ("pivots", "Monthly pivots"), ("vp", "Volume profile")):
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.toggled.connect(self.render_chart)
            self.ov[key] = cb
            lay.addWidget(cb)

        lay.addSpacing(8)
        self.sync_btn = QPushButton("⟳  Sync Latest EOD Data", objectName="primary")
        self.sync_btn.clicked.connect(self.sync)
        lay.addWidget(self.sync_btn)
        self.sync_all = QCheckBox("Sync all indices && ETFs")
        self.sync_all.setChecked(True)
        lay.addWidget(self.sync_all)
        self.refresh_btn = QPushButton("Re-run analysis")
        self.refresh_btn.clicked.connect(self.run_analysis)
        lay.addWidget(self.refresh_btn)
        imp = QPushButton("Import FII/DII history (CSV)…")
        imp.clicked.connect(self.import_csv)
        lay.addWidget(imp)

        lay.addStretch(1)
        self.db_lbl = wrap_label("", "muted")
        lay.addWidget(self.db_lbl)
        self.status_lbl = wrap_label("", "muted")
        lay.addWidget(self.status_lbl)
        lay.addWidget(wrap_label("<span style='font-size:10px'>Educational analytics only — "
                                 "not investment advice.</span>", "muted"))
        return side

    @staticmethod
    def _h2(text: str) -> QLabel:
        lbl = QLabel(text.upper())
        lbl.setProperty("class", "h2")
        return lbl

    # ---------------------------------------------------------- right panel
    def _build_panel(self) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(360)
        body = QWidget(objectName="root")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        f, l = card("Signals & decision")
        self.rec_lbl = QLabel("—")
        self.rec_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rec_lbl.setMinimumHeight(54)
        l.addWidget(self.rec_lbl)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.kv = {}
        for i, (k, label) in enumerate((("regime", "Market regime"), ("range", "Next-month range"),
                                        ("scores", "Buy / sell score"), ("rsi", "RSI · ATR"),
                                        ("equiv", "ETF / index equivalent"))):
            kl = QLabel(label)
            kl.setProperty("class", "muted")
            vl = wrap_label("—")
            grid.addWidget(kl, i, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(vl, i, 1)
            self.kv[k] = vl
        l.addLayout(grid)
        self.plan_lbl = wrap_label()
        l.addWidget(self.plan_lbl)
        l.addWidget(self._h2("Key drivers / reasoning"))
        self.drivers_lbl = wrap_label()
        l.addWidget(self.drivers_lbl)
        lay.addWidget(f)

        f, l = card("Institutional flow (FII / DII)")
        self.inst_lbl = wrap_label()
        l.addWidget(self.inst_lbl)
        lay.addWidget(f)

        f, l = card("Key levels")
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Zone", "Range", "Str", "Touch", "Confluence"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        hh = self.table.horizontalHeader()
        for i in range(4):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(240)
        l.addWidget(self.table)
        self.piv_lbl = wrap_label("", "muted")
        l.addWidget(self.piv_lbl)
        lay.addWidget(f)

        f, l = card("Catalysts & context")
        self.cat_lbl = wrap_label()
        l.addWidget(self.cat_lbl)
        lay.addWidget(f)
        lay.addStretch(1)
        scroll.setWidget(body)
        return scroll

    # ---------------------------------------------------------- scanner
    SCAN_COLS = ["Instrument", "Last", "1D %", "20D %", "Regime", "Signal", "Buy", "Sell", "RSI",
                 "Proj low", "Proj high", "Support", "To S %", "Resist", "To R %", "Flow thesis"]

    def _build_scanner(self) -> QWidget:
        w = QWidget(objectName="root")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 10, 10, 10)
        top = QHBoxLayout()
        self.scan_btn = QPushButton("Scan all indices && ETFs (1D)", objectName="primary")
        self.scan_btn.clicked.connect(self.scan)
        top.addWidget(self.scan_btn)
        top.addWidget(wrap_label("Daily-timeframe analysis for every instrument. Click a column to sort; "
                                 "double-click a row to open its chart.", "muted"), 1)
        lay.addLayout(top)
        self.scan_table = QTableWidget(0, len(self.SCAN_COLS))
        self.scan_table.setHorizontalHeaderLabels(self.SCAN_COLS)
        self.scan_table.verticalHeader().setVisible(False)
        self.scan_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.scan_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.scan_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.scan_table.horizontalHeader().setStretchLastSection(True)
        self.scan_table.cellDoubleClicked.connect(self._open_from_scan)
        lay.addWidget(self.scan_table)
        return w

    def scan(self):
        self._busy(True, "Scanning all instruments…")
        svc = self.svc

        def job(progress):
            fii, macro = svc.load_fii_dii(), svc.load_macro()
            rows = []
            for i, sym in enumerate(config.INSTRUMENTS, 1):
                progress(f"Scanning {i}/{len(config.INSTRUMENTS)}: {config.SYMBOLS[sym]}")
                try:
                    daily = svc.load_ohlcv(sym, "1D")
                    res = analyze(sym, "1D", daily, daily, fii, macro)
                    svc.save_sr_levels(to_sr_document(res))
                    res.pop("frame")
                    c = daily["close"]
                    res["chg_1d"] = float((c.iloc[-1] / c.iloc[-2] - 1) * 100)
                    res["chg_20d"] = float((c.iloc[-1] / c.iloc[-21] - 1) * 100) if len(c) > 21 else None
                    rows.append(res)
                except Exception as exc:
                    log.warning("Scan failed for %s: %s", sym, exc)
                    rows.append({"symbol": sym, "error": str(exc)})
            return rows

        task = Task(job)
        task.signals.progress.connect(self._status)
        task.signals.done.connect(self._scan_done)
        task.signals.failed.connect(self._failed)
        self._start(task)

    def _scan_done(self, rows: list[dict]):
        t = self.scan_table
        t.setSortingEnabled(False)
        t.setRowCount(len(rows))
        for i, r in enumerate(rows):
            inst = config.INSTRUMENTS[r["symbol"]]
            name = QTableWidgetItem(inst.name if inst.kind == "index" else inst.symbol.removesuffix(".NS"))
            name.setData(Qt.ItemDataRole.UserRole + 1, r["symbol"])
            name.setToolTip(f"{inst.name} · {inst.group}")
            t.setItem(i, 0, name)
            if "error" in r:
                t.setItem(i, 1, QTableWidgetItem(f"no data: {r['error'][:60]}"))
                continue
            sig, proj = r["signal"], r["projection"]
            s0 = r["support_zones"][0]["mid"] if r["support_zones"] else None
            r0 = r["resistance_zones"][0]["mid"] if r["resistance_zones"] else None
            c = r["close"]

            def pct(v):
                return None if v is None else (v / c - 1) * 100
            cells = [
                NumItem(f"{c:,.2f}", c),
                NumItem(f"{r['chg_1d']:+.2f}", r["chg_1d"]),
                NumItem("—" if r["chg_20d"] is None else f"{r['chg_20d']:+.2f}", r["chg_20d"]),
                QTableWidgetItem(r["regime"]["trend"]),
                QTableWidgetItem(sig["recommendation"]),
                NumItem(str(sig["buy_score"]), sig["buy_score"]),
                NumItem(str(sig["sell_score"]), sig["sell_score"]),
                NumItem(f"{sig['rsi']:.0f}", sig["rsi"]),
                NumItem(f"{proj['lower']:,.2f}", proj["lower"]),
                NumItem(f"{proj['upper']:,.2f}", proj["upper"]),
                NumItem("—" if s0 is None else f"{s0:,.2f}", s0),
                NumItem("—" if s0 is None else f"{pct(s0):+.1f}", pct(s0)),
                NumItem("—" if r0 is None else f"{r0:,.2f}", r0),
                NumItem("—" if r0 is None else f"{pct(r0):+.1f}", pct(r0)),
                QTableWidgetItem(r["institutional"]["thesis"]),
            ]
            for j, it in enumerate(cells, 1):
                t.setItem(i, j, it)
            for j, v in ((2, r["chg_1d"]), (3, r["chg_20d"])):
                if v is not None:
                    t.item(i, j).setForeground(QColor(C["up"] if v >= 0 else C["down"]))
            t.item(i, 4).setForeground(QColor({"Bullish": C["up"], "Bearish": C["down"]}
                                              .get(r["regime"]["trend"], "#eab308")))
            t.item(i, 5).setForeground(QColor(sig["color"]))
        t.setSortingEnabled(True)
        ok = sum(1 for r in rows if "error" not in r)
        acc = sum(1 for r in rows if r.get("signal", {}).get("recommendation") == "ACCUMULATE ZONE")
        book = sum(1 for r in rows if r.get("signal", {}).get("recommendation") == "PROFIT BOOKING ZONE")
        self._busy(False, f"Scanned {ok}/{len(rows)} · {acc} in accumulate zone · {book} in profit-booking zone")
        self.live.set_levels(self.svc.latest_levels(config.INDEX_SYMBOLS))

    def _open_from_scan(self, row: int, _col: int):
        self.open_symbol(self.scan_table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1))

    # ---------------------------------------------------------- actions
    def _busy(self, busy: bool, msg: str = ""):
        for w in (self.sync_btn, self.live.sync_btn, self.refresh_btn, self.sym, self.scan_btn,
                  *self.tf_group.buttons()):
            w.setEnabled(not busy)
        if not busy:   # indices without a Yahoo feed have daily data only
            intraday = config.supports_intraday(self.sym.currentData())
            for b in self.tf_group.buttons():
                b.setEnabled(b.text() == "1D" or intraday)
        self._status(msg)

    def _status(self, msg: str):
        self.status_lbl.setText(msg)
        self.statusBar().showMessage(msg)

    def current(self) -> tuple[str, str]:
        symbol = self.sym.currentData()
        if not config.supports_intraday(symbol) and self.tf_group.checkedButton().text() != "1D":
            self.tf_group.buttons()[0].setChecked(True)
        return symbol, self.tf_group.checkedButton().text()

    def sync(self):
        self._busy(True, "Syncing…")
        symbol = self.sym.currentData()
        symbols = None if self.sync_all.isChecked() else [s for s in (symbol, config.pair_of(symbol)) if s]
        task = Task(lambda p: self.svc.sync(symbols=symbols, progress=p))
        task.signals.progress.connect(self._status)
        task.signals.done.connect(self._sync_done)
        task.signals.failed.connect(self._failed)
        self._start(task)

    def _sync_done(self, summary: dict):
        errs = summary.get("errors") or []
        cash = summary.get("fii_cash") or {}
        msg = (f"Synced · OI days +{summary.get('participant_oi_days', 0)}"
               f" · cash days {summary.get('fii_cash_days', 0)}"
               + (f" · FII {cash.get('fii_cash_net', 0):+,.0f} / DII {cash.get('dii_cash_net', 0):+,.0f} cr "
                  f"({pd.Timestamp(cash['date']):%d-%b})" if cash else ""))
        if errs:
            msg += f" · {len(errs)} warning(s)"
            log.warning("Sync warnings: %s", errs)
        self._busy(False, msg)
        self.run_analysis()
        self.compute_levels()   # refresh live tiles' charts, zones and EOD signals

    def run_analysis(self):
        symbol, tf = self.current()
        self._busy(True, f"Analysing {symbol} {tf}…")
        svc = self.svc

        def job(progress):
            daily = svc.load_ohlcv(symbol, "1D")
            frame = daily if tf == "1D" else svc.load_ohlcv(symbol, tf)
            fii, macro = svc.load_fii_dii(), svc.load_macro()
            res = analyze(symbol, tf, frame, daily, fii, macro)
            svc.save_sr_levels(to_sr_document(res))
            res["pair"] = pair_ratio(svc, symbol, daily)
            return res, fii

        task = Task(job)
        task.signals.done.connect(self._analysis_done)
        task.signals.failed.connect(self._failed)
        self._start(task)

    def _failed(self, err: str):
        self._busy(False, "Error — see details")
        QMessageBox.warning(self, "Operation failed", err)

    def _analysis_done(self, payload):
        self.result, self.fii = payload
        self.render_chart()
        self.render_panel()
        r = self.result
        self._busy(False, f"{r['symbol']} {r['timeframe']} as of "
                          f"{pd.Timestamp(r['as_of']).tz_convert(config.IST):%d-%b-%Y %H:%M} IST")

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "FII/DII history CSV", str(Path.home()), "CSV (*.csv)")
        if not path:
            return
        try:
            n = self.svc.store.upsert_fii_dii(import_fii_dii_csv(path))
            self._status(f"Imported {n} FII/DII rows")
            self.run_analysis()
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))

    # ---------------------------------------------------------- rendering
    def render_chart(self):
        if not self.result:
            return
        overlays = {k: cb.isChecked() for k, cb in self.ov.items()}
        fig = build_figure(self.result, self.fii, overlays)
        self._chart_seq += 1
        path = config.CHART_DIR / f"chart_{self._chart_seq}.html"
        # 'directory' writes plotly.min.js once beside the page: fully offline, and
        # avoids QWebEngine's 2 MB setHtml limit.
        fig.write_html(path, include_plotlyjs="directory", full_html=True,
                       config={"scrollZoom": True, "displaylogo": False, "responsive": True},
                       default_height="100%", default_width="100%")
        html_txt = path.read_text(encoding="utf-8").replace(
            "<head>", f"<head><style>html,body{{margin:0;height:100%;background:{C['bg']}}}</style>", 1)
        path.write_text(html_txt, encoding="utf-8")
        self.web.load(QUrl.fromLocalFile(str(path)))
        for old in config.CHART_DIR.glob("chart_*.html"):
            if old != path:
                old.unlink(missing_ok=True)

    def render_panel(self):
        r = self.result
        sig, reg, proj, inst = r["signal"], r["regime"], r["projection"], r["institutional"]
        col = sig["color"]
        self.rec_lbl.setText(sig["recommendation"])
        self.rec_lbl.setStyleSheet(
            f"font-size:18px;font-weight:800;color:{col};border:1px solid {col};border-radius:8px;"
            f"background:rgba(255,255,255,0.03);padding:8px")
        tcol = {"Bullish": C["up"], "Bearish": C["down"]}.get(reg["trend"], "#eab308")
        self.kv["regime"].setText(f"<b style='color:{tcol}'>{reg['trend']}</b> "
                                  f"<span style='color:{C['muted']}'>ADX {reg['adx']} · "
                                  f"{'above' if reg['above_ema200'] else 'below'} 200 EMA</span>")
        self.kv["range"].setText(f"<b>{proj['lower']:,.2f}</b> — <b>{proj['upper']:,.2f}</b>"
                                 f"<br><span style='color:{C['muted']}'>±1σ month = {proj['sigma_month_pct']}%"
                                 f" · M-pivot {proj['monthly_pivot']}</span>")
        self.kv["scores"].setText(f"<span style='color:{C['up']}'>{sig['buy_score']}/4</span> · "
                                  f"<span style='color:{C['down']}'>{sig['sell_score']}/4</span>")
        self.kv["rsi"].setText(f"{sig['rsi']} · {sig['atr']}")
        pair = r.get("pair")
        if pair:
            k = pair["ratio"]
            tag = pair["symbol"].removesuffix(".NS")
            if pair["kind"] == "etf":
                self.kv["equiv"].setText(f"{tag} ≈ ₹{proj['lower'] * k:,.2f} — ₹{proj['upper'] * k:,.2f} "
                                         f"<span style='color:{C['muted']}'>(ratio {k:.6f})</span>")
            else:
                self.kv["equiv"].setText(f"{pair['name']} ≈ {proj['lower'] * k:,.0f} — {proj['upper'] * k:,.0f}")
        else:
            self.kv["equiv"].setText(f"<span style='color:{C['muted']}'>No tracking ETF mapped</span>")

        p = sig.get("plan") or {}
        if p:
            rr = f" · R:R {p['risk_reward']}" if p.get("risk_reward") else ""
            self.plan_lbl.setText(
                f"<div style='background:{C['panel2']};border-radius:6px;padding:6px'>"
                f"<b>Trade plan</b> · entry {p['entry_zone'][0]:,.2f}–{p['entry_zone'][1]:,.2f} · "
                f"<span style='color:{C['down']}'>SL {p['stop_loss']:,.2f}</span> · "
                f"<span style='color:{C['up']}'>T {' / '.join(f'{t:,.2f}' for t in p['targets'])}</span>{rr}</div>")
        else:
            self.plan_lbl.setText("")
        self.drivers_lbl.setText(bullets(r["drivers"]))

        # institutional
        if inst.get("available"):
            def fmt(v, suffix=""):
                return "n/a" if v is None else f"{v:,.2f}{suffix}" if abs(v) < 100 else f"{v:,.0f}{suffix}"
            corr = ", ".join(f"{k.replace('_', ' ')} {v:+.2f}" for k, v in inst.get("correlation", {}).items())
            self.inst_lbl.setText(
                f"<b>{html.escape(inst['thesis'])}</b>"
                f"<table width='100%' style='margin-top:6px'>"
                f"<tr><td style='color:{C['fii']}'>FII last / 5d</td><td align='right'>{fmt(inst['fii_cash_last'])} / "
                f"{fmt(inst['fii_cash_5d'])} cr</td></tr>"
                f"<tr><td style='color:{C['dii']}'>DII last / 5d</td><td align='right'>{fmt(inst['dii_cash_last'])} / "
                f"{fmt(inst['dii_cash_5d'])} cr</td></tr>"
                f"<tr><td>FII idx-fut L/S</td><td align='right'>{fmt(inst['fii_ls'])}"
                f" ({'n/a' if inst['fii_ls_change_5d'] is None else f'{inst['fii_ls_change_5d']:+.2f}'} 5d)</td></tr>"
                f"<tr><td>Client L/S</td><td align='right'>{fmt(inst['client_ls'])}</td></tr>"
                f"<tr><td>Index PCR (OI)</td><td align='right'>{fmt(inst['pcr'])}</td></tr>"
                f"<tr><td>Institutional bias</td><td align='right'>{inst['bias']:+d}</td></tr></table>"
                + (f"<div style='color:{C['muted']};margin-top:4px'>Correlation: {corr}</div>" if corr else "")
                + bullets(inst["drivers"], C["muted"]))
        else:
            self.inst_lbl.setText("No FII/DII data yet — click <b>Sync Latest EOD Data</b>.")

        # levels table
        rows = [("R", z) for z in reversed(r["resistance_zones"][:6])] + [("S", z) for z in r["support_zones"][:6]]
        self.table.setRowCount(len(rows) + 1)
        idx = 0
        n_res = sum(1 for k, _ in rows if k == "R")
        for kind, z in rows:
            if idx == n_res:   # price marker row between resistances and supports
                self._table_row(idx, ["▶ LTP", f"{r['close']:,.2f}", "", "", ""], C["accent"])
                idx += 1
            color = C["down"] if kind == "R" else C["up"]
            self._table_row(idx, [f"{'Resist' if kind == 'R' else 'Support'}",
                                  f"{z['low']:,.2f} – {z['high']:,.2f}", f"{z['strength']:.1f}",
                                  str(z["touches"]), ", ".join(z["sources"][:4])], color)
            idx += 1
        if n_res == len(rows):
            self._table_row(idx, ["▶ LTP", f"{r['close']:,.2f}", "", "", ""], C["accent"])
        mp = r["monthly_pivots"]
        if mp:
            s, fib, cam = mp["standard"], mp["fibonacci"], mp["camarilla"]
            self.piv_lbl.setText(
                f"Monthly pivots (from {mp['based_on'][:7]}): Std P {s['P']:,.2f} · S1 {s['S1']:,.2f} · "
                f"R1 {s['R1']:,.2f} · Fib S1 {fib['S1']:,.2f} / R1 {fib['R1']:,.2f} · "
                f"Cam S3 {cam['S3']:,.2f} / R3 {cam['R3']:,.2f}")

        # catalysts
        cat = r["catalysts"]
        parts = []
        if cat["macro"]:
            parts.append("<b>Macro drivers</b>")
            for m in cat["macro"]:
                c = C["down"] if m["impact"] == "headwind" else C["up"]
                star = " ⚑" if m["significant"] else ""
                parts.append(f"<div>• {m['label']} <b>{m['last']:,.2f}</b> "
                             f"<span style='color:{c}'>{m['chg_5d']:+.1f}% 5d / {m['chg_20d']:+.1f}% 20d{star}</span>"
                             f" <span style='color:{C['muted']}'>ρ {m['corr_with_nifty']}</span></div>")
        if cat["attribution"]:
            parts.append("<br><b>Why the big moves happened (last 60 sessions)</b>")
            for a in cat["attribution"]:
                c = C["up"] if a["return_pct"] > 0 else C["down"]
                parts.append(f"<div>• {a['date']} <span style='color:{c}'>{a['return_pct']:+.2f}%</span> "
                             f"{a['type']}: <span style='color:{C['muted']}'>"
                             f"{html.escape('; '.join(a['reasons']))}</span></div>")
        if cat["upcoming"]:
            parts.append("<br><b>Upcoming event risk (45d)</b>")
            parts += [f"<div>• {e['date']:%d-%b} — {e['event']} <span style='color:{C['muted']}'>"
                      f"[{e['category']}]</span></div>" for e in cat["upcoming"]]
        self.cat_lbl.setText("".join(parts) or "Sync to load macro series.")

    def _table_row(self, i: int, vals: list[str], color: str):
        for j, v in enumerate(vals):
            it = QTableWidgetItem(v)
            it.setForeground(QColor(color if j == 0 else C["text"] if j < 4 else C["muted"]))
            self.table.setItem(i, j, it)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.FileHandler(config.LOG_FILE), logging.StreamHandler()])
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setApplicationName("NSE Quant Terminal")
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)   # SF on macOS, Segoe UI on Windows
    font.setPointSize(12)
    app.setFont(font)
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
