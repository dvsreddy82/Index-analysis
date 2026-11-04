"""Background sync to MongoDB, so every screen also works offline.

One scheduler (a 60-second QTimer) runs these jobs; each records its outcome
in the `sync_log` collection and the app bar shows the freshness:

* fii_dii   — at start-up, then from 4:30 pm to 11 pm IST on weekdays every
              15 minutes until today's cash figures and participant-wise OI
              are both stored (NSE publishes them ~5-9 pm).
* eod       — once a trading day after 6:30 pm IST (and at start-up when the
              last run is older than a day): NSE index archive, intraday
              candles, macro series — the full `DataService.sync`.
* intraday  — every 5 min while Shoonya is connected: 1-minute candles for
              every instrument Shoonya streams (backfills ~9 days the first
              time), plus 15m / 1H candles once an hour.
* chains    — every 5 min in market hours while connected: NIFTY and
              BANKNIFTY option-chain snapshots (via the options service).
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, time as dtime

import pandas as pd
from PySide6.QtCore import QObject, QTimer, Signal

import config
from tasks import Runner

log = logging.getLogger(__name__)

IST_OPEN, IST_CLOSE = dtime(9, 15), dtime(15, 30)
FII_WINDOW = (dtime(16, 30), dtime(23, 0))
EOD_AFTER = dtime(18, 30)


def now_ist() -> datetime:
    return pd.Timestamp.now(tz=config.IST).to_pydatetime()


def market_open(t: datetime | None = None) -> bool:
    t = t or now_ist()
    return t.weekday() < 5 and IST_OPEN <= t.time() <= IST_CLOSE


class SyncService(QObject):
    status_changed = Signal()          # any job finished: refresh freshness displays
    fii_updated = Signal(dict)         # new FII/DII data stored
    eod_updated = Signal(dict)
    chains_due = Signal()              # ask the app to snapshot option chains now
    progress = Signal(str)

    def __init__(self, svc, broker, market, runner: Runner, parent=None):
        super().__init__(parent)
        self.svc, self.broker, self.market, self.runner = svc, broker, market, runner
        self.busy: set[str] = set()
        self.last: dict[str, dict] = {d["job"]: d for d in self._read_log()}
        self._attempt: dict[str, float] = {}
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(60_000)
        broker.connected.connect(lambda _m: self.run_intraday(full=True))

    # ---------------------------------------------------------------- log
    def _read_log(self) -> list[dict]:
        try:
            return self.svc.store.find_docs("sync_log")
        except Exception:
            return []

    def _record(self, job: str, ok: bool, msg: str, extra: dict | None = None):
        doc = {"job": job, "at": datetime.now().astimezone(), "ok": ok, "message": msg, **(extra or {})}
        self.last[job] = doc
        try:
            self.svc.store.upsert_docs("sync_log", [doc])
        except Exception as exc:
            log.warning("sync_log not written: %s", exc)
        self.status_changed.emit()

    def age(self, job: str) -> float | None:
        d = self.last.get(job)
        if not d or not d.get("at"):
            return None
        at = pd.Timestamp(d["at"])
        at = at if at.tzinfo else at.tz_localize("UTC")
        return (pd.Timestamp.now(tz="UTC") - at).total_seconds()

    # ---------------------------------------------------------------- schedule
    def start(self):
        """First pass shortly after launch (catch-up)."""
        QTimer.singleShot(4_000, lambda: self.run_fii(reason="start-up"))
        QTimer.singleShot(20_000, self.tick)

    def tick(self):
        t = now_ist()
        weekday = t.weekday() < 5
        # FII / DII: in the publishing window until both of today's datasets are in
        if weekday and FII_WINDOW[0] <= t.time() <= FII_WINDOW[1] and not self.fii_complete(t.date()):
            if time.time() - self._attempt.get("fii_dii", 0) > 15 * 60:
                self.run_fii(reason="publishing window")
        # EOD: once per trading day after 6:30 pm, or when the last run is over a day old
        age = self.age("eod")
        done_today = self._ran_today("eod") and (self.last.get("eod") or {}).get("after_close")
        if (weekday and t.time() >= EOD_AFTER and not done_today) or age is None or age > 26 * 3600:
            if time.time() - self._attempt.get("eod", 0) > 60 * 60:
                self.run_eod()
        # intraday candles while connected
        if self.broker.live and (market_open(t) or (self.age("intraday") or 1e9) > 3600):
            if time.time() - self._attempt.get("intraday", 0) > 5 * 60:
                self.run_intraday(full=(self.age("intraday_full") or 1e9) > 3600)
        if self.broker.live and market_open(t) and time.time() - self._attempt.get("chains", 0) > 5 * 60:
            self._attempt["chains"] = time.time()
            self.chains_due.emit()

    def _ran_today(self, job: str) -> bool:
        d = self.last.get(job)
        if not d or not d.get("at"):
            return False
        at = pd.Timestamp(d["at"])
        at = (at if at.tzinfo else at.tz_localize("UTC")).tz_convert(config.IST)
        return at.date() == now_ist().date()

    def fii_complete(self, day: date) -> bool:
        d = self.last.get("fii_dii") or {}
        return str(d.get("latest_cash")) == str(day) and str(d.get("latest_oi")) == str(day)

    # ---------------------------------------------------------------- jobs
    def run_fii(self, reason: str = "manual"):
        if "fii_dii" in self.busy:
            return
        self.busy.add("fii_dii")
        self._attempt["fii_dii"] = time.time()
        self.status_changed.emit()
        prev = self.last.get("fii_dii") or {}

        def done(out):
            self.busy.discard("fii_dii")
            new = (str(out.get("latest_cash")) != str(prev.get("latest_cash"))
                   or str(out.get("latest_oi")) != str(prev.get("latest_oi")) or bool(out.get("oi_days")))
            msg = (f"cash to {out.get('latest_cash'):%d-%b}" if out.get("latest_cash") else "no cash data") + \
                  (f" · participant OI to {out.get('latest_oi'):%d-%b}" if out.get("latest_oi") else "")
            self._record("fii_dii", not out.get("errors") or bool(out.get("latest_cash")), msg,
                         {"latest_cash": str(out.get("latest_cash")), "latest_oi": str(out.get("latest_oi")),
                          "reason": reason})
            if new or reason == "manual":
                self.fii_updated.emit(out)

        def failed(err):
            self.busy.discard("fii_dii")
            self._record("fii_dii", False, err.splitlines()[0][:200], {"latest_cash": prev.get("latest_cash"),
                                                                     "latest_oi": prev.get("latest_oi")})

        self.runner.start(lambda _p: self.svc.sync_fii_dii(), done, failed)

    def run_eod(self, symbols=None, progress=None):
        if "eod" in self.busy:
            return
        self.busy.add("eod")
        self._attempt["eod"] = time.time()
        self.status_changed.emit()
        after_close = now_ist().time() >= EOD_AFTER

        def done(summary):
            self.busy.discard("eod")
            errs = summary.get("errors") or []
            self._record("eod", len(errs) < 5, f"{summary.get('index_archive_days', 0)} archive days · "
                                               f"{len(errs)} warning(s)", {"after_close": after_close})
            self.eod_updated.emit(summary)

        def failed(err):
            self.busy.discard("eod")
            self._record("eod", False, err.splitlines()[0][:200], {"after_close": False})

        self.runner.start(lambda p: self.svc.sync(symbols=symbols, progress=p), done, failed,
                          progress or self.progress.emit)

    def run_intraday(self, full: bool = False):
        if "intraday" in self.busy or not self.broker.live:
            return
        self.busy.add("intraday")
        self._attempt["intraday"] = time.time()
        market = self.market
        symbols = sorted(s for s in list(config.INSTRUMENTS) + ["INDIA VIX"] if market.feed.key_for(s))

        def job(_p):
            n = 0
            for s in symbols:
                market.refresh_intraday(s, "1m")
                n += 1
                if full:
                    for tf in ("15m", "1H"):
                        market.refresh_intraday(s, tf)
            return n

        def done(n):
            self.busy.discard("intraday")
            self._record("intraday", True, f"{n} instruments · 1m" + (" + 15m/1H" if full else ""))
            if full:
                self._record("intraday_full", True, f"{n} instruments")

        def failed(err):
            self.busy.discard("intraday")
            self._record("intraday", False, err.splitlines()[0][:200])

        self.runner.start(job, done, failed)

    # ---------------------------------------------------------------- display
    def summary(self) -> dict:
        """Freshness for the app bar."""
        fd = self.last.get("fii_dii") or {}
        return {"fii_cash": fd.get("latest_cash"), "fii_oi": fd.get("latest_oi"),
                "eod_age": self.age("eod"), "intraday_age": self.age("intraday"), "busy": sorted(self.busy)}
