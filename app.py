"""NSE Quant Terminal — PySide6 desktop app.

Layout
------
 ┌ App bar: market status · data source · FII/DII + EOD freshness · MongoDB · Sync ▾ · Shoonya ┐
 ├ Navigation ──┬ Page (title + purpose, then the screen) ─────────────────────────────────────┤
 │ MARKETS      │ Live overview · Intraday chart · Option chain · FII / DII flows              │
 │ RESEARCH     │ Index analysis · Scanner                                                     │
 │ MY TRADING   │ Trades & P&L · AI coach                                                      │
 └──────────────┴──────────────────────────────────────────────────────────────────────────────┘

Data flows through services, not screens:
  broker.Broker            the single Shoonya connection (read-only), sign-in, state
  market_data.MarketData   Shoonya-first candles/quotes, stored in MongoDB for offline use
  options_service          option-chain analysis + snapshots
  trades_service           broker books → MongoDB → journal analytics
  sync_service             scheduled sync: FII/DII as published, EOD, intraday, chain snapshots
  fii_analysis             FII/DII positioning → option-writing read

Run:  python app.py
"""
from __future__ import annotations

import html
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QFont, QFontDatabase, QKeySequence, QShortcut
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QScrollArea,
                               QSplitter, QStackedWidget, QStatusBar, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

import config
import fii_analysis
from ai_view import AICoachPanel
from broker import Broker
from data_fetcher import DataService, import_fii_dii_csv
from fii_view import FIIDashboard
from indicators import analyze, to_sr_document
from live_view import CHART_DAYS, LiveDashboard
from market_data import MarketData
from options_service import OptionsService
from options_view import OptionsDashboard
from rt_chart import SYMBOLS as RT_SYMBOLS, RealtimeCharts
from risk_view import RiskDashboard
import risk_engine
import trades_engine as te
from shoonya_feed import INDEX_TOKENS
from sync_service import SyncService, market_open, now_ist
from tasks import Runner, Task
from theme import C, QSS, page
from trades_service import TradesService
from trades_view import TradesDashboard

IST = ZoneInfo(config.IST)
log = logging.getLogger("app")


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
    """Shell: app bar, navigation rail and pages. Data work lives in the services
    (broker, market data, options, trades, sync); this class wires them to screens."""

    NAV = [("MARKETS", [("live", "◉  Live overview"), ("intraday", "◔  Intraday chart"),
                        ("chain", "▦  Option chain"), ("risk", "⚠  Writer risk monitor"),
                        ("fii", "⇅  FII / DII flows")]),
           ("RESEARCH", [("analysis", "◇  Index analysis"), ("scanner", "☰  Scanner")]),
           ("MY TRADING", [("trades", "₹  Trades & P&L"), ("ai", "✦  AI coach")])]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("NSE Quant Terminal — Index & ETF desk")
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1680, int(avail.width() * 0.95)), min(980, int(avail.height() * 0.92)))
        self.move(avail.center() - self.rect().center())
        # ---- services
        self.runner = Runner()
        self.pool = self.runner.pool
        self.svc = DataService()
        self.broker = Broker(self.runner, self)
        self.shoonya = self.broker.feed                    # read-only handle for screens
        self.market = MarketData(self.svc, self.broker)
        self.options = OptionsService(self.svc, self.broker, self.market)
        self.trades_svc = TradesService(self.svc, self.broker, self.options)
        self.sync_svc = SyncService(self.svc, self.broker, self.market, self.runner, self)
        self.result: dict | None = None
        self.fii = pd.DataFrame()
        self._chart_seq = 0
        self._last_snap: dict | None = None
        self._flags = {"live": False, "opt": False, "trades": False, "rtc": False, "fii": False}
        self._pending: dict[str, object] = {}
        self._rtc_loaded = 0.0
        self._rtc_need_fit = False

        # ---- layout: app bar / (nav | pages)
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_appbar())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        v.addLayout(body, 1)
        body.addWidget(self._build_nav())
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        self.pages: dict[str, QWidget] = {}
        self._build_pages()
        self.setStatusBar(QStatusBar())

        # ---- wiring
        self.broker.changed.connect(self._broker_state)
        self.broker.connected.connect(self._broker_connected)
        self.broker.disconnected.connect(self._broker_disconnected)
        self.sync_svc.status_changed.connect(self._sync_state)
        self.sync_svc.fii_updated.connect(lambda _o: self.load_fii())
        self.sync_svc.eod_updated.connect(self._eod_done)
        self.sync_svc.chains_due.connect(self._chains_due)
        self.sync_svc.progress.connect(self._status)

        self.live_timer = QTimer(self)
        self.live_timer.setInterval(config.LIVE_REFRESH_SECONDS * 1000)
        self.live_timer.timeout.connect(self.poll_live)
        self.live_timer.start()
        self.rt_timer = QTimer(self)               # 1 s: live tiles, intraday ticks, clock
        self.rt_timer.setInterval(1000)
        self.rt_timer.timeout.connect(self._second)
        self.rt_timer.start()
        self.trades_timer = QTimer(self)
        self.trades_timer.setInterval(config.TRADES_SYNC_SECONDS * 1000)
        self.trades_timer.timeout.connect(self._trades_tick)
        self.trades_timer.start()
        self._trades_ticks = 0
        self.opt_timer = QTimer(self)
        self.opt_timer.setInterval(config.OPTIONS_REFRESH_SECONDS * 1000)
        self.opt_timer.timeout.connect(lambda: self.current_page() == "chain" and self.opts.request())
        self.opt_timer.start()
        self._risk_at = 0.0
        self._red_seen: set[str] = set()
        self.risk_timer = QTimer(self)
        self.risk_timer.setInterval(15_000)
        self.risk_timer.timeout.connect(self.load_risk)
        self.risk_timer.start()

        self._broker_state()
        self._sync_state()
        self.nav.setCurrentRow(1)                  # Live overview
        self.run_analysis()
        self.compute_levels()
        self.load_fii()
        self.broker.auto_connect()                 # today's saved Shoonya session, silently
        self.sync_svc.start()

    # ================================================================== shell
    def _build_appbar(self) -> QFrame:
        bar = QFrame(objectName="appbar")
        bar.setFixedHeight(46)
        h = QHBoxLayout(bar)
        h.setContentsMargins(16, 0, 12, 0)
        h.setSpacing(10)
        t = QLabel("NSE QUANT TERMINAL")
        t.setStyleSheet(f"font-size:14px;font-weight:800;color:{C['accent']};letter-spacing:1px")
        h.addWidget(t)
        self.clock_lbl = QLabel("")
        self.clock_lbl.setTextFormat(Qt.TextFormat.RichText)
        h.addWidget(self.clock_lbl)
        h.addStretch(1)
        self.fresh_lbl = QLabel("")
        self.fresh_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.fresh_lbl.setStyleSheet("font-size:11px")
        h.addWidget(self.fresh_lbl)
        self.store_lbl = QLabel("")
        self.store_lbl.setTextFormat(Qt.TextFormat.RichText)
        self.store_lbl.setStyleSheet("font-size:11px")
        h.addWidget(self.store_lbl)
        self.sync_btn = QPushButton("⟳ Sync")
        menu = QMenu(self.sync_btn)
        menu.addAction("Sync everything now (EOD prices, FII/DII, macro)", self.sync)
        menu.addAction("Check for new FII/DII data", lambda: self.sync_svc.run_fii("manual"))
        menu.addAction("Back-fill intraday candles from Shoonya", lambda: self.sync_svc.run_intraday(full=True))
        menu.addSeparator()
        menu.addAction("Import FII/DII history (CSV)…", self.import_csv)
        self.sync_btn.setMenu(menu)
        h.addWidget(self.sync_btn)
        self.broker_btn = QPushButton("Connect Shoonya")
        self.broker_btn.setToolTip("Read-only real-time prices, option chains and your positions from Shoonya")
        self.broker_btn.clicked.connect(lambda: self.broker.toggle(self))
        h.addWidget(self.broker_btn)
        return bar

    def _build_nav(self) -> QFrame:
        nav = QFrame(objectName="nav")
        nav.setFixedWidth(196)
        lay = QVBoxLayout(nav)
        lay.setContentsMargins(0, 10, 0, 10)
        self.nav = QListWidget(objectName="navlist")
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._nav_keys: list[str | None] = []
        for section, items in self.NAV:
            head = QListWidgetItem(section)
            head.setFlags(Qt.ItemFlag.NoItemFlags)
            f = QFont(self.font())
            f.setBold(True)
            f.setPointSizeF(max(f.pointSizeF() - 2, 9))
            head.setFont(f)
            head.setForeground(QColor(C["faint"]))
            self.nav.addItem(head)
            self._nav_keys.append(None)
            for key, text in items:
                self.nav.addItem(QListWidgetItem(text))
                self._nav_keys.append(key)
        self.nav.currentRowChanged.connect(self._nav_changed)
        lay.addWidget(self.nav, 1)
        lay.addWidget(wrap_label(f"<span style='font-size:10px;color:{C['faint']}'>Read-only analytics. "
                                 "Not investment advice.</span>", "muted"))
        for i, key in enumerate(k for k in self._nav_keys if k):
            sc = QShortcut(QKeySequence(f"Ctrl+{i + 1}"), self)
            sc.activated.connect(lambda k=key: self.show_page(k))
        return nav

    def _build_pages(self):
        # Markets
        self.live = LiveDashboard()
        self.live.open_symbol.connect(self.open_symbol)
        self.live.refresh_requested.connect(self.poll_live)
        self.live.levels_requested.connect(lambda: self.compute_levels(force=True))
        self.live.interval_changed.connect(lambda s: self.live_timer.setInterval(s * 1000))
        self._add("live", "Live overview", "Every index and ETF on one screen: live price, today's move, key levels "
                  "and the EOD signal · real-time from Shoonya where it streams, NSE (≈15 s) for the rest", self.live)
        self.rtc = RealtimeCharts()
        self.rtc.reload_requested.connect(lambda: self.load_rt_chart(force=True))
        self.rtc.alert_fired.connect(self._chart_alert)
        self._add("intraday", "Intraday chart", "NIFTY 50 and BANKNIFTY candles with indicators, levels, your strikes, "
                  "drawings and price alerts · candles are stored in MongoDB for offline use", self.rtc)
        self.opts = OptionsDashboard()
        self.opts.refresh_requested.connect(self.load_options)
        self._add("chain", "Option chain", "NIFTY / BANKNIFTY chain, OI and IV charts, and the option-selling "
                  "analysis · snapshots every 5 min are kept in MongoDB", self.opts)
        self.risk_view = RiskDashboard()
        self.risk_view.refresh_requested.connect(lambda: self.load_risk(force=True))
        self._add("risk", "Writer risk monitor", "Squeeze, put-flush, institutional-divergence and negative-gamma "
                  "checks for NIFTY / BANKNIFTY writers, with prohibitions, safe zones and guidance for your legs",
                  self.risk_view)
        self.fii_view = FIIDashboard()
        self.fii_view.sync_requested.connect(lambda: self.sync_svc.run_fii("manual"))
        self.fii_view.import_requested.connect(self._import_fii_path)
        self.fii_view.zones_requested.connect(lambda: self._chains_due(force=True))
        self._add("fii", "FII / DII flows", "Institutional cash flows and F&O positioning → a direction for option "
                  "writing · synced automatically as NSE publishes (≈5–9 pm IST)", self.fii_view)
        # Research
        analysis = QWidget(objectName="root")
        al = QHBoxLayout(analysis)
        al.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        self.split = split
        self.sidebar = self._build_sidebar()
        split.addWidget(self.sidebar)
        self.web = QWebEngineView()
        s = self.web.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.web.page().setBackgroundColor(Qt.GlobalColor.black)
        self.web.setHtml(f"<body style='background:{C['bg']};color:{C['muted']};"
                         "font-family:sans-serif;padding:40px'>Loading market data…</body>")
        split.addWidget(self.web)
        self.panel = self._build_panel()
        split.addWidget(self.panel)
        split.setStretchFactor(1, 1)
        al.addWidget(split)
        self._add("analysis", "Index analysis", "Daily / hourly / 15-min analysis of any index or ETF: zones, pivots, "
                  "volume profile, institutional flow and the decision card", analysis)
        self._add("scanner", "Scanner", "Daily analysis of all 56 indices and ETFs in one sortable table",
                  self._build_scanner())
        # My trading
        self.trades = TradesDashboard()
        self.trades.sync_requested.connect(lambda: self.sync_trades(pull=True))
        self.trades.recompute_requested.connect(lambda: self.sync_trades(pull=False))
        self.trades.import_requested.connect(self.import_trades)
        self.trades.journal_saved.connect(self.save_journal)
        self._add("trades", "Trades & P&L", "Positions with management alerts, orders, fills, history, performance, "
                  "why you lost, and your journal · synced from Shoonya into MongoDB", self.trades)
        self.ai = AICoachPanel(lambda: {**self.trades.data, "fii_read": self._fii_brief(),
                                        "risk_monitor": self._risk_brief()} if self.trades.data else {},
                               self.svc.store, lambda: self.trades.period.currentText())
        self.ai.data_needed.connect(lambda: self.sync_trades(pull=self.broker.live))
        self._add("ai", "AI coach", "Chat with a local model (Ollama) about your own trades — nothing leaves this Mac",
                  self.ai)

    def _add(self, key: str, title: str, subtitle: str, widget: QWidget):
        w, _head = page(title, subtitle, widget)
        self.pages[key] = w
        self.stack.addWidget(w)

    def show_page(self, key: str):
        self.nav.setCurrentRow(self._nav_keys.index(key))

    def current_page(self) -> str | None:
        w = self.stack.currentWidget()
        return next((k for k, p in self.pages.items() if p is w), None)

    def _nav_changed(self, row: int):
        key = self._nav_keys[row] if 0 <= row < len(self._nav_keys) else None
        if key is None:
            return
        self.stack.setCurrentWidget(self.pages[key])
        if key == "analysis":
            w = self.split.width() or self.width() - 200
            side, panel = max(210, int(w * 0.17)), max(340, int(w * 0.28))
            self.split.setSizes([side, max(w - side - panel, 300), panel])
        elif key == "live":
            self.poll_live()
        elif key == "intraday":
            self.load_rt_chart()
            self._chains_due()
            if self._rtc_need_fit:
                self._rtc_need_fit = False
                QTimer.singleShot(80, self.rtc.refit)
        elif key == "chain" and self.opts.res is None:
            self.opts.request()
        elif key in ("trades", "ai") and not self.trades.data:
            self.sync_trades(pull=self.broker.live)
        elif key == "risk":
            self.load_risk(force=time.time() - self._risk_at > 30)
        elif key == "fii" and len(self.fii_view.zone_res) < 2:
            self._chains_due(force=True)

    # ================================================================== status
    def _second(self):
        t = now_ist()
        open_ = market_open(t)
        self.clock_lbl.setText(
            f"<span style='color:{C['up'] if open_ else C['muted']}'>● {'Market open' if open_ else 'Market closed'}"
            f"</span> <span style='color:{C['faint']}'>{t:%a %d %b · %H:%M:%S} IST</span>")
        if self.current_page() == "intraday":
            self.load_rt_chart()                   # no-op until the reload interval has passed
        if self.broker.live:
            if self._last_snap and self.current_page() == "live":
                self._show_snapshot()
            self._rtc_tick()

    def _broker_state(self):
        kind, text, color = self.broker.state()
        self.broker_btn.setText({"live": "● Shoonya live", "connecting": "… Connecting",
                                 "degraded": "◐ Shoonya", "off": "Connect Shoonya"}[kind])
        self.broker_btn.setToolTip(text + (" — click to disconnect" if kind in ("live", "degraded") else
                                           " — read-only prices, chains and positions"))
        self.broker_btn.setStyleSheet(f"color:{color};font-weight:600" if kind != "off" else "")
        self.broker_btn.setEnabled(kind != "connecting")
        self._sync_state()

    def _sync_state(self):
        s = self.sync_svc.summary()

        def day(v):
            try:
                return pd.Timestamp(v).strftime("%d %b") if v and v != "None" else "—"
            except ValueError:
                return "—"

        def ago(sec):
            if sec is None:
                return "never"
            return f"{sec / 60:.0f} min ago" if sec < 3600 else f"{sec / 3600:.0f} h ago" if sec < 86400 \
                else f"{sec / 86400:.0f} d ago"
        busy = s["busy"]
        kind, text, color = self.broker.state()
        src = (f"<span style='color:{C['up']}'>● real-time</span>" if kind == "live" else
               f"<span style='color:{C['warn']}'>● NSE delayed</span>")
        self.fresh_lbl.setText(
            f"{src} &nbsp; <span style='color:{C['muted']}'>FII/DII <b style='color:{C['text']}'>{day(s['fii_cash'])}"
            f"</b> · OI {day(s['fii_oi'])} · EOD {ago(s['eod_age'])}"
            + (f" · <span style='color:{C['warn']}'>syncing {', '.join(busy)}…</span>" if busy else "") + "</span>")
        store = "MongoDB" if self.svc.persistent else "in-memory (MongoDB unreachable)"
        self.store_lbl.setText(f"<span style='color:{C['up'] if self.svc.persistent else C['down']}'>● {store}</span>")
        self.fii_view.set_freshness(
            f"Cash data to <b>{day(s['fii_cash'])}</b> · participant OI to <b>{day(s['fii_oi'])}</b> · "
            f"<span style='color:{C['muted']}'>NSE publishes ≈5–9 pm IST; checked every 15 min from 4:30 pm until "
            "today's data is in, and at start-up</span>", "fii_dii" in busy)

    def _status(self, msg: str):
        self.status_lbl.setText(msg)
        self.statusBar().showMessage(msg)

    def _broker_connected(self, msg: str):
        self._status(f"Shoonya: {msg} — real-time data on")
        self.opt_timer.setInterval(5000)                   # live chain: refresh often
        if self.current_page() == "chain":
            self.opts.request()
        self.sync_trades(pull=True)
        self.load_rt_chart(force=True)
        self.poll_live()

    def _broker_disconnected(self):
        self.opt_timer.setInterval(config.OPTIONS_REFRESH_SECONDS * 1000)
        self._status("Shoonya disconnected — using NSE delayed data and stored history")

    def closeEvent(self, event):
        self.broker.feed.close()
        super().closeEvent(event)

    def _start(self, task):
        """Run a pre-built Task (scanner) through the shared runner."""
        self.runner._live.add(task)
        for sig in (task.signals.done, task.signals.failed):
            sig.connect(lambda *_a, t=task: self.runner._live.discard(t))
        self.pool.start(task)

    def _once(self, key: str, fn, done, failed=None, *args):
        """Run `fn` in the background unless one is already running; remember the
        latest request and replay it when the running one finishes."""
        if self._flags.get(key):
            self._pending[key] = args
            return
        self._flags[key] = True

        def fin(cb):
            def inner(x):
                self._flags[key] = False
                if cb:
                    cb(x)
                if key in self._pending:
                    nxt = self._pending.pop(key)
                    getattr(self, {"opt": "load_options", "trades": "sync_trades"}[key])(*nxt) \
                        if key in ("opt", "trades") else None
            return inner
        self.runner.start(lambda _p: fn(*args), fin(done), fin(failed or (lambda e: log.warning("%s: %s", key, e))))

    # ================================================================== live overview
    def poll_live(self):
        if self.live.paused() or self.current_page() != "live" or self._flags["live"]:
            return
        self._flags["live"] = True
        self.runner.start(lambda _p: self.svc.live_snapshot(), self._live_done, self._live_failed)

    def _live_done(self, snap: dict):
        self._flags["live"] = False
        if not snap.get("quotes"):
            self.live.ts.setText("NSE live feed unavailable — retrying")
            return
        self._last_snap = snap
        self._show_snapshot()

    def _live_failed(self, err: str):
        self._flags["live"] = False
        log.warning("Live poll failed: %s", err.splitlines()[0])
        self.live.ts.setText("Live poll failed — retrying")

    def _show_snapshot(self):
        snap = self.broker.feed.overlay_snapshot(self._last_snap)
        self.live.update_snapshot(snap)
        if snap.get("source"):
            self.live.ts.setText(self.live.ts.text() + f" · <span style='color:{C['up']}'>● {snap['source']}</span>")

    def compute_levels(self, force: bool = False):
        """Stored daily levels for the live tiles; analyse any index that has none yet
        (or all of them when `force`)."""
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
            history = {}
            for sym in config.INDEX_SYMBOLS:
                d = svc.load_ohlcv(sym, "1D")
                if d is not None and not d.empty:
                    d = d.tail(CHART_DAYS)
                    history[sym] = [(ts.date(), float(o), float(h), float(l), float(c)) for ts, o, h, l, c in
                                    zip(d.index, d["open"], d["high"], d["low"], d["close"])]
            return svc.latest_levels(config.INDEX_SYMBOLS), flows, history

        self.live.lvl_btn.setEnabled(False)
        self.runner.start(job, self._levels_done, self._levels_failed, self._status)

    def _levels_done(self, payload):
        levels, flows, history = payload
        self.live.set_levels(levels)
        self.rtc.set_levels(levels)
        self.live.set_history(history)
        if flows:
            self.live.set_flows(*flows)
        self.live.lvl_btn.setEnabled(True)
        self._status(f"Live levels ready for {len(levels)}/{len(config.INDEX_SYMBOLS)} indices")

    def _levels_failed(self, err: str):
        self.live.lvl_btn.setEnabled(True)
        self._failed(err)

    def open_symbol(self, sym: str):
        self.show_page("analysis")
        idx = self.sym.findData(sym)
        if idx == self.sym.currentIndex():
            self.run_analysis()
        else:
            self.sym.setCurrentIndex(idx)

    # ================================================================== intraday chart
    RT_RELOAD_LIVE, RT_RELOAD_DELAYED = 120, 30

    def load_rt_chart(self, force: bool = False):
        every = self.RT_RELOAD_LIVE if self.broker.live else self.RT_RELOAD_DELAYED
        if self._flags["rtc"] or (not force and time.time() - self._rtc_loaded < every):
            return
        self._flags["rtc"] = True
        market, svc = self.market, self.svc

        def job(_p):
            data, src = market.candles_1m([s for s, _ in RT_SYMBOLS])
            daily = {}
            for sym, _ in RT_SYMBOLS:
                try:
                    daily[sym] = svc.load_ohlcv(sym, "1D")
                except Exception as exc:
                    log.warning("daily bars for chart: %s", exc)
            return data, src, daily

        def failed(err):
            self._flags["rtc"] = False
            self._rtc_loaded = time.time()
            self.rtc.set_source(f"chart data failed: {err.splitlines()[0][:80]}")

        self.runner.start(job, self._rtc_done, failed)

    def _rtc_done(self, payload):
        data, src, daily = payload
        self._flags["rtc"] = False
        self._rtc_loaded = time.time()
        for sym, df in daily.items():
            self.rtc.set_daily(sym, df)
        snap = self._last_snap or {}
        for sym, _ in RT_SYMBOLS:
            self.rtc.set_prev_close(sym, ((snap.get("quotes") or {}).get(sym) or {}).get("prev"))
        self.rtc.set_history(data, src)
        if self.current_page() != "intraday":
            self._rtc_need_fit = True               # drawn while hidden: re-fit when first shown

    def _rtc_tick(self):
        if not self._rtc_loaded:
            return                                 # ticks flow on every page so chart alerts fire anywhere
        now = datetime.now(IST).replace(tzinfo=None)
        feed = self.broker.feed
        for sym, _ in RT_SYMBOLS:
            t = feed.tick(INDEX_TOKENS[sym])
            if feed._fresh(t):
                if t.get("c"):
                    self.rtc.set_prev_close(sym, _num(t.get("c")))
                self.rtc.push_tick(sym, _num(t.get("lp")), now)

    def _chart_alert(self, msg: str):
        self._status(f"⏰ Chart alert: {msg}")
        QApplication.beep()
        QApplication.alert(self)

    # ================================================================== option chain
    def load_options(self, underlying: str, expiry: str):
        self._once("opt", self.options.analyse, self._options_done, self._options_failed, underlying, expiry)

    def _options_done(self, res: dict):
        self.opts.set_result(res)
        self._chain_result(res)

    def _options_failed(self, err: str):
        self.opts.set_error(err.splitlines()[0] if err else "unknown error")

    def _chain_result(self, res: dict):
        """Every fresh chain analysis feeds the chart levels and the FII/DII strike zones."""
        self.rtc.set_option_levels(res["underlying"], res)
        self.fii_view.set_zone_results({res["underlying"]: res})

    def _chains_due(self, force: bool = False):
        """Analyse both underlyings in the background (stores snapshots); refresh
        the chart levels and FII/DII zones without touching the chain screen."""
        for und in ("NIFTY", "BANKNIFTY"):
            res = self.options.fresh(und, 300 if not force else 60)
            if res:
                self._chain_result(res)
                continue
            key = f"chain_{und}"
            if self._flags.get(key):
                continue
            self._flags[key] = True

            def done(r, k=key):
                self._flags[k] = False
                self._chain_result(r)

            def failed(e, k=key):
                self._flags[k] = False
                log.warning("Background chain failed: %s", e.splitlines()[0])

            self.runner.start(lambda _p, u=und: self.options.analyse(u, None), done, failed)

    # ================================================================== writer risk monitor
    def load_risk(self, force: bool = False):
        """Every 30 s on the screen; every 3 min in the background while Shoonya is live in
        market hours (so a new RED alert can be raised from any page)."""
        if self._flags.get("risk"):
            return
        visible = self.current_page() == "risk"
        if not force:
            if not self.risk_view.auto.isChecked():
                return
            every = 30 if visible else (180 if self.broker.live and market_open() else None)
            if every is None or time.time() - self._risk_at < every:
                return
        self._flags["risk"] = True
        self._risk_at = time.time()
        opts, svc, trades = self.options, self.svc, self.trades_svc

        def job(_p):
            fii = svc.load_fii_dii()
            frame = fii_analysis.build_frame(fii, svc.load_ohlcv("^NSEI", "1D")) if len(fii) else None
            book = trades.last_book.get("positions")
            if book is None:
                docs = svc.store.find_docs("broker_positions", sort="date", desc=True, limit=1)
                book = docs[0].get("rows") if docs else []
            out = {}
            for und, sym in (("NIFTY", "^NSEI"), ("BANKNIFTY", "NIFTY BANK")):
                try:
                    res = opts.fresh(und, 45) or opts.analyse(und)
                    hist = opts.intraday_history(und)
                    prev = risk_engine.pick_previous(hist, res["expiry"])
                    c = svc.store.load_ohlcv(sym, "1m", start=pd.Timestamp.now(tz=config.IST) - pd.Timedelta(minutes=90))
                    path = list(zip(c.index, c["close"])) if c is not None and len(c) else []
                    if path and market_open():
                        path.append((pd.Timestamp.now(tz=config.IST), res["spot"]))
                    groups = te.manage_positions(book or [], {und: res["spot"]}, {und: res.get("sigma_move")})
                    legs = [l for g in groups if g["und"] == und for l in g["legs"]]
                    chains = {}
                    for e in {pd.Timestamp(l["expiry"]).strftime("%d-%b-%Y") for l in legs
                              if l.get("expiry") is not None and l.get("qty")} - {res["expiry"]}:
                        try:
                            chains[e] = opts.analyse(und, e, record=False)
                        except Exception as exc:
                            log.warning("risk: %s %s chain failed: %s", und, e, exc)
                    out[und] = risk_engine.assess(res, prev, path, frame, fii, legs, chains)
                except Exception as exc:
                    out[und] = {"error": str(exc)}
            return out

        def done(out):
            self._flags["risk"] = False
            reds = []
            for und, r in out.items():
                if "error" in r:
                    self.risk_view.set_error(und, r["error"])
                    continue
                self.risk_view.set_result(und, r)
                self._chain_result_light(und)
                reds += [f"{und}: {a['title']}" for a in r["alerts"] if a["level"] == "RED"]
            new = set(reds) - self._red_seen
            self._red_seen = set(reds)
            item = self.nav.item(self._nav_keys.index("risk"))
            item.setText("⚠  Writer risk monitor" + (f"  ({len(reds)} RED)" if reds else ""))
            item.setForeground(QColor(C["down"] if reds else C["muted"]))
            self.risk_view.set_status(
                f"Updated {datetime.now():%H:%M:%S}" + (f" · <b style='color:{C['down']}'>{len(reds)} RED alert(s)"
                                                       f"</b>" if reds else " · no RED alerts"))
            if new and self.risk_view.notify.isChecked():
                self._status("🚨 " + " · ".join(sorted(new)))
                QApplication.beep()
                QApplication.alert(self)

        def failed(err):
            self._flags["risk"] = False
            self.risk_view.set_status(f"<span style='color:{C['down']}'>Risk check failed: "
                                      f"{html.escape(err.splitlines()[0])}</span>")

        self.runner.start(job, done, failed)

    def _chain_result_light(self, und: str):
        res = self.options.fresh(und, 120)
        if res:
            self._chain_result(res)

    def _risk_brief(self) -> dict:
        """Latest risk-monitor output for the AI coach's Risk desk mode."""
        out = {}
        for und, r in self.risk_view.results.items():
            out[und] = {k: r[k] for k in ("spot", "expiry", "dte", "regime", "bias", "danger_up", "danger_down",
                                          "alerts", "prohibitions", "safe", "positions", "call_wall", "put_wall",
                                          "atm_iv", "vix", "sigma", "window_min")}
            out[und]["gex_net_cr_per_1pct"] = round(r["gex"]["net"] / 1e7, 2)
            out[und]["gamma_flip"] = r["gex"].get("flip")
        return out

    # ================================================================== FII / DII
    def load_fii(self):
        svc = self.svc

        def job(_p):
            return fii_analysis.analyse(svc.load_fii_dii(), svc.load_ohlcv("^NSEI", "1D"))

        self.runner.start(job, self._fii_done, lambda e: log.warning("FII analysis failed: %s", e.splitlines()[0]))

    def _fii_done(self, res: dict):
        self.fii_view.set_result(res)
        if res.get("available"):
            self._status(f"FII/DII read {res['bias']} ({res['score']:+.0f}) as of {res['as_of']:%d %b}")

    def _fii_brief(self) -> dict:
        """Compact FII/DII read for the AI coach."""
        r = self.fii_view.res or {}
        if not r.get("available"):
            return {}
        return {"as_of": f"{r['as_of']:%Y-%m-%d}", "score": round(r["score"]), "bias": r["bias"],
                "conviction": r["conviction"], "writing_side": r["playbook"]["side"],
                "signals": {c["label"]: f"{c['value']} ({c['reading']}, score {c['score']:+.1f})"
                            for c in r["components"]},
                "warnings": r["warnings"]}

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "FII/DII history CSV", str(Path.home()), "CSV (*.csv)")
        if path:
            self._import_fii_path(path)

    def _import_fii_path(self, path: str):
        try:
            n = self.svc.store.upsert_fii_dii(import_fii_dii_csv(path))
            self._status(f"Imported {n} FII/DII rows")
            self.load_fii()
            self.run_analysis()
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))

    # ================================================================== trades
    def _trades_tick(self):
        """Pull broker books while connected: every tick on the Trades page, every
        4th tick (~2 min) elsewhere so fills are stamped with context promptly."""
        self._trades_ticks += 1
        if not (self.broker.live and self.trades.auto.isChecked()):
            return
        if self.current_page() == "trades" or self._trades_ticks % 4 == 0:
            self.sync_trades(pull=True)

    def sync_trades(self, pull: bool):
        if self._flags["trades"]:
            self._pending["trades"] = (bool((self._pending.get("trades") or (False,))[0]) or pull,)
            return
        self._once("trades", lambda p, d: self.trades_svc.refresh(p, d), self._trades_done, self._trades_failed,
                   pull, self.trades.period_days())

    def _trades_done(self, d: dict):
        self.trades.set_data(d)
        self.ai.data_ready()
        self.rtc.set_positions(d.get("positions") or [])

    def _trades_failed(self, err: str):
        self.trades.set_status(f"<span style='color:{C['down']}'>Trades sync failed: "
                               f"{html.escape(err.splitlines()[0] if err else '')}</span>")

    def import_trades(self, path: str):
        try:
            total, n = self.trades_svc.import_csv(path)
        except Exception as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        self.trades.set_status(f"Imported {total} fills ({n} new or updated) from {html.escape(path)}")
        self.sync_trades(pull=False)

    def save_journal(self, doc: dict):
        self.svc.store.upsert_docs("trade_journal", [doc])
        self.sync_trades(pull=False)

    # ================================================================== sync
    def sync(self):
        self.sync_svc.run_eod()

    def _eod_done(self, summary: dict):
        errs = summary.get("errors") or []
        cash = summary.get("fii_cash") or {}
        msg = (f"Synced · OI days +{summary.get('participant_oi_days', 0)}"
               f" · cash days {summary.get('fii_cash_days', 0)}"
               + (f" · FII {cash.get('fii_cash_net', 0):+,.0f} / DII {cash.get('dii_cash_net', 0):+,.0f} cr "
                  f"({pd.Timestamp(cash['date']):%d-%b})" if cash else ""))
        if errs:
            msg += f" · {len(errs)} warning(s)"
            log.warning("Sync warnings: %s", errs)
        self._status(msg)
        self.run_analysis()
        self.compute_levels()
        self.load_fii()

    # ================================================================== index analysis
    def _busy(self, busy: bool, msg: str = ""):
        for w in (self.refresh_btn, self.sym, self.scan_btn, *self.tf_group.buttons()):
            w.setEnabled(not busy)
        if not busy:
            intraday = config.supports_intraday(self.sym.currentData()) or bool(self.shoonya.key_for(self.sym.currentData()))
            for b in self.tf_group.buttons():
                b.setEnabled(b.text() == "1D" or intraday)
        self._status(msg)

    def current(self) -> tuple[str, str]:
        symbol = self.sym.currentData()
        ok_intraday = config.supports_intraday(symbol) or bool(self.shoonya.key_for(symbol))
        if not ok_intraday and self.tf_group.checkedButton().text() != "1D":
            self.tf_group.buttons()[0].setChecked(True)
        return symbol, self.tf_group.checkedButton().text()

    def run_analysis(self):
        symbol, tf = self.current()
        self._busy(True, f"Analysing {symbol} {tf}…")
        svc, market = self.svc, self.market

        def job(progress):
            src = None
            if tf != "1D" or market.shoonya_key(symbol):
                for t in ({tf, "1H"} if tf != "1D" else {"1H"}):     # 1H also builds today's live daily bar
                    last = svc.store.latest_timestamp(symbol, t)
                    stale = last is None or (pd.Timestamp.now(tz="UTC") - pd.Timestamp(last)).total_seconds() > 6 * 3600
                    if market.shoonya_key(symbol) or stale:
                        s = market.refresh_intraday(symbol, t)
                        src = src or (s if t == tf else None)
            daily = svc.load_ohlcv(symbol, "1D")
            frame = daily if tf == "1D" else svc.store.load_ohlcv(symbol, tf)
            if frame is None or frame.empty:
                raise RuntimeError(f"No {tf} candles stored for {symbol} yet — connect Shoonya (it streams this "
                                   "instrument) or pick 1D.")
            fii, macro = svc.load_fii_dii(), svc.load_macro()
            res = analyze(symbol, tf, frame, daily, fii, macro)
            svc.save_sr_levels(to_sr_document(res))
            res["pair"] = pair_ratio(svc, symbol, daily)
            res["data_source"] = src or ("NSE archive" if tf == "1D" else "stored")
            return res, fii

        self.runner.start(job, self._analysis_done, self._failed)

    def _failed(self, err: str):
        self._busy(False, "Error — see details")
        QMessageBox.warning(self, "Operation failed", err)

    def _analysis_done(self, payload):
        self.result, self.fii = payload
        self.render_chart()
        self.render_panel()
        r = self.result
        src = {"shoonya": "Shoonya real-time candles", "yahoo": "Yahoo candles", "offline": "stored candles (offline)"}
        self.src_lbl.setText(f"<span style='font-size:11px'>Data: {src.get(r.get('data_source'), r.get('data_source'))}"
                             "</span>")
        self._busy(False, f"{r['symbol']} {r['timeframe']} as of "
                          f"{pd.Timestamp(r['as_of']).tz_convert(config.IST):%d-%b-%Y %H:%M} IST")

    # ---------------------------------------------------------- sidebar
    def _build_sidebar(self) -> QFrame:
        side = QFrame(objectName="sidebar")
        side.setMinimumWidth(210)
        side.setMaximumWidth(280)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(14, 16, 14, 14)
        lay.setSpacing(10)
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
        self.refresh_btn = QPushButton("Re-run analysis", objectName="primary")
        self.refresh_btn.clicked.connect(self.run_analysis)
        lay.addWidget(self.refresh_btn)
        self.src_lbl = wrap_label("", "muted")
        lay.addWidget(self.src_lbl)

        lay.addStretch(1)
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


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


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
