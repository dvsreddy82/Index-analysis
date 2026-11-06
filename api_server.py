"""Read-only JSON API for the Android companion app.

The desktop app stays the single owner of the Shoonya session, MongoDB and all
analysis; this server only exposes what it has already computed (refreshing it
on demand).  Python standard library only.

Security: every request needs `Authorization: Bearer <key>`; the key is random,
created once and kept in ~/.nse_analytics/api.json (owner-only).  Binds to all
interfaces on PHONE_API_PORT (default 8787) so a phone on the same Wi-Fi — or on
Tailscale — can reach it.  GET only; nothing can place orders or change data.

Endpoints (all GET, JSON):
  /api/health      server, Shoonya and market status
  /api/ticker      BANKNIFTY / NIFTY / VIX, open P&L, alert counts, setup flag   (poll every 2-5 s)
  /api/overview    every index Shoonya streams, VIX, FII/DII headline
  /api/banknifty   BANKNIFTY + constituents: weights, prices, contributions, breadth, alerts
  /api/chain?und=NIFTY|BANKNIFTY&n=15   option-chain summary + rows around ATM
  /api/risk        writer risk monitor for both indices
  /api/fii         FII/DII read, evening setup, strike zones, signals, recent sessions
  /api/positions   open positions with management alerts, today's P&L, margin
  /api/alerts      every active alert with a stable id (the phone notifies on new ids)
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import socket
import subprocess
import threading
import time
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
from PySide6.QtCore import QObject, Signal

import config

log = logging.getLogger(__name__)

API_FILE = config.APP_HOME / "api.json"
PORT = config.PHONE_API_PORT
VERSION = 1


def api_key() -> str:
    try:
        d = json.loads(API_FILE.read_text())
        if d.get("key"):
            return d["key"]
    except (OSError, ValueError):
        pass
    key = secrets.token_urlsafe(24)
    API_FILE.write_text(json.dumps({"key": key}))
    os.chmod(API_FILE, 0o600)
    return key


def local_addresses() -> list[str]:
    """IPv4 addresses a phone could use: Wi-Fi / Ethernet, and Tailscale (100.x) if present."""
    out = []
    try:
        txt = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        for line in txt.splitlines():
            line = line.strip()
            if line.startswith("inet ") and not line.startswith("inet 127."):
                out.append(line.split()[1])
    except Exception:
        pass
    if not out:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            out.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return sorted(set(out), key=lambda ip: (not ip.startswith(("192.168.", "10.", "172.")), ip))


def _clean(o):
    """JSON-safe: numpy / pandas scalars, timestamps, NaN → None, tuples → lists."""
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_clean(v) for v in o]
    if isinstance(o, (pd.Timestamp, datetime, date)):
        return o.isoformat()
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return None if math.isnan(v) or math.isinf(v) else round(v, 4)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, pd.DataFrame):
        return None
    return o


def _id(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


class ApiServer(QObject):
    """Runs the HTTP server on a background thread. `need` asks the GUI thread to refresh
    something (queued signal, so handlers never touch Qt objects directly)."""
    need = Signal(str)

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.key = api_key()
        self.httpd: ThreadingHTTPServer | None = None
        self.started_at = time.time()
        self._chain_locks = {"NIFTY": threading.Lock(), "BANKNIFTY": threading.Lock()}
        self.error: str | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):          # quiet: no per-request log lines
                pass

            def do_GET(self):
                server._handle(self)

        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
            self.httpd.daemon_threads = True
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
            log.info("Phone API listening on port %s", PORT)
        except OSError as exc:
            self.error = str(exc)
            log.warning("Phone API not started: %s", exc)

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd = None

    def urls(self) -> list[str]:
        return [f"http://{ip}:{PORT}" for ip in local_addresses()]

    # ---------------------------------------------------------------- dispatch
    def _handle(self, h: BaseHTTPRequestHandler):
        url = urlparse(h.path)
        if h.headers.get("Authorization", "") != f"Bearer {self.key}":
            return self._send(h, 401, {"error": "missing or wrong API key"})
        route = {"/api/health": self.health, "/api/ticker": self.ticker, "/api/overview": self.overview,
                 "/api/banknifty": self.banknifty, "/api/chain": self.chain, "/api/risk": self.risk,
                 "/api/fii": self.fii, "/api/positions": self.positions, "/api/alerts": self.alerts}.get(url.path)
        if route is None:
            return self._send(h, 404, {"error": "unknown endpoint"})
        try:
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            self._send(h, 200, _clean(route(q)))
        except Exception as exc:
            log.exception("Phone API %s failed", url.path)
            self._send(h, 500, {"error": str(exc)})

    @staticmethod
    def _send(h, code: int, body):
        data = json.dumps(body, ensure_ascii=False, default=str).encode()
        h.send_response(code)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        h.wfile.write(data)

    # ---------------------------------------------------------------- endpoints
    def health(self, _q):
        from sync_service import market_open, now_ist
        kind, text, _c = self.win.broker.state()
        return {"version": VERSION, "app": "NSE Quant Terminal", "server_time": now_ist(), "market_open": market_open(),
                "shoonya": {"state": kind, "text": text}, "mongodb": self.win.svc.persistent,
                "uptime_s": int(time.time() - self.started_at)}

    def ticker(self, _q):
        quotes, extra = getattr(self.win, "_ticker_state", ({}, {}))
        names = {"NIFTY BANK": "BANKNIFTY", "^NSEI": "NIFTY 50", "INDIA VIX": "India VIX"}
        out = []
        for sym in ("NIFTY BANK", "^NSEI", "INDIA VIX"):
            q = quotes.get(sym) or {}
            if q.get("last"):
                ch = (q["last"] - q["prev"]) if q.get("prev") else None
                out.append({"symbol": sym, "name": names[sym], "last": q["last"], "prev": q.get("prev"),
                            "chg": ch, "pct": ch / q["prev"] * 100 if ch is not None else None,
                            "high": q.get("high"), "low": q.get("low")})
        return {"quotes": out, **extra, "time": datetime.now()}

    def overview(self, _q):
        from shoonya_feed import INDEX_TOKENS, VIX_TOKEN
        feed = self.win.broker.feed
        snap = (self.win._last_snap or {}).get("quotes") or {}
        names = {"^NSEI": "NIFTY 50", "NIFTY BANK": "BANKNIFTY", "NIFTY NEXT 50": "NIFTY Next 50",
                 "NIFTY FINANCIAL SERVICES": "FINNIFTY", "NIFTY MIDCAP SELECT": "MIDCPNIFTY"}
        rows = []
        for sym, key in list(INDEX_TOKENS.items()) + [("INDIA VIX", VIX_TOKEN)]:
            t = feed.tick(key) if feed.live else None
            if t and feed._fresh(t):
                last, prev, hi, lo = (_f(t.get("lp")), _f(t.get("c")), _f(t.get("h")), _f(t.get("l")))
                live = True
            else:
                s = snap.get(sym) or ((self.win._last_snap or {}).get("vix") if sym == "INDIA VIX" else {}) or {}
                last, prev, hi, lo, live = s.get("last"), s.get("prev"), s.get("high"), s.get("low"), False
            if not last:
                continue
            ch = last - prev if prev else None
            rows.append({"symbol": sym, "name": names.get(sym, "India VIX"), "last": last, "prev": prev, "chg": ch,
                         "pct": ch / prev * 100 if ch is not None and prev else None, "high": hi, "low": lo,
                         "live": live})
        fr = self.win.fii_view.res or {}
        head = {}
        if fr.get("available"):
            f = fr["frame"].iloc[-1]
            head = {"as_of": fr["as_of"], "fii_cash": f.get("fii_cash"), "dii_cash": f.get("dii_cash"),
                    "score": fr["score"], "bias": fr["bias"]}
        return {"indices": rows, "fii": head, "source": "Shoonya real-time" if feed.live else "NSE delayed"}

    def banknifty(self, _q):
        bnf = self.win.bnf_svc
        if not bnf.symbols or time.time() - self.win._bnf_at > (120 if self.win.broker.live else 60):
            self.need.emit("banknifty")
        if not bnf.symbols:
            return {"loading": True}
        s = bnf.live_snapshot()
        return {"index": s["index"], "rows": [{k: v for k, v in r.items() if k != "spark"} | {"spark": r["spark"][-60:]}
                                              for r in s["rows"]],
                "explained": s["explained"], "up_weight": s["up_weight"], "adv": s["adv"], "dec": s["dec"],
                "alerts": [{"level": lv, "text": t} for lv, t in s["alerts"]], "fit": s["fit"], "live": s["live"]}

    def chain(self, q):
        und = (q.get("und") or "NIFTY").upper()
        if und not in self._chain_locks:
            raise ValueError("und must be NIFTY or BANKNIFTY")
        n = int(q.get("n") or 15)
        opts = self.win.options
        res = opts.fresh(und, 20 if self.win.broker.live else 90)
        if res is None:
            with self._chain_locks[und]:
                res = opts.fresh(und, 20 if self.win.broker.live else 90) or opts.analyse(und)
        rows = res.get("chain") or []
        i = min(range(len(rows)), key=lambda j: abs(rows[j]["strike"] - res["spot"])) if rows else 0
        keep = ("strike", "pcr", "ce_oi", "ce_chg_oi", "ce_ltp", "ce_chg_pct", "ce_iv", "ce_delta", "ce_buildup",
                "pe_oi", "pe_chg_oi", "pe_ltp", "pe_chg_pct", "pe_iv", "pe_delta", "pe_buildup")
        return {"underlying": und, "expiry": res["expiry"], "dte": res["dte"], "spot": res["spot"], "atm": res["atm"],
                "atm_iv": res.get("atm_iv"), "sigma_move": res.get("sigma_move"), "straddle": res.get("straddle"),
                "lot": res.get("lot"), "vix": res.get("vix"), "skew": res.get("skew"), "source": res.get("source"),
                "timestamp": res.get("timestamp"), "summary": res.get("chain_summary"),
                "verdict": res.get("verdict"), "score": res.get("score"),
                "rows": [{k: r.get(k) for k in keep} for r in rows[max(i - n, 0): i + n + 1]]}

    def risk(self, _q):
        if time.time() - self.win._risk_at > 60:
            self.need.emit("risk")
        out = {}
        for und, r in self.win.risk_view.results.items():
            out[und] = {k: r[k] for k in ("spot", "expiry", "dte", "regime", "bias", "danger_up", "danger_down",
                                          "alerts", "prohibitions", "safe", "positions", "call_wall", "put_wall",
                                          "atm_iv", "vix", "sigma", "window_min", "source", "timestamp")}
            out[und]["gex"] = {"net_cr": r["gex"]["net"] / 1e7, "flip": r["gex"].get("flip")}
        return {"indices": out, "updated": self.win._risk_at}

    def fii(self, _q):
        from fii_analysis import strike_zones
        r = self.win.fii_view.res or {}
        if not r.get("available"):
            return {"available": False}
        f = r["frame"].tail(15).iloc[::-1]
        recent = [{"date": ts, "fii_cash": x.get("fii_cash"), "dii_cash": x.get("dii_cash"),
                   "fii_long_pct": x.get("fii_long_pct"), "fii_fut_net": x.get("fii_fut_net"), "pcr": x.get("pcr"),
                   "score": x.get("score"), "nifty": x.get("nifty")} for ts, x in f.iterrows()]
        s = r.get("setup") or {}
        setup = {"date": s.get("date"), "direction": s.get("direction"), "lean": s.get("lean"),
                 "conditions": [{"name": a, "value": b, "ok": c} for a, b, c in s.get("conditions") or []],
                 "record": s.get("record")}
        zones = {u: strike_zones(r["bias"], self.win.fii_view.zone_res.get(u)) for u in ("NIFTY", "BANKNIFTY")}
        return {"available": True, "as_of": r["as_of"], "sessions": r["sessions"], "score": r["score"], "bias": r["bias"],
                "conviction": r["conviction"], "agreement": r.get("agreement"), "playbook": r["playbook"],
                "warnings": r["warnings"], "components": r["components"], "evidence": r.get("evidence"),
                "setup": setup, "zones": zones, "recent": recent}

    def positions(self, _q):
        d = self.win.trades.data or {}
        if not d:
            self.need.emit("trades")
            return {"loading": True}
        groups = []
        for g in d.get("positions") or []:
            groups.append({"und": g["und"], "expiry": g["expiry"], "dte": g.get("dte"), "structure": g["structure"],
                           "defined_risk": g["defined_risk"], "credit": g["credit"], "open_pnl": g["open_pnl"],
                           "mtm": g["mtm"], "captured": g.get("captured"), "spot": g.get("spot"),
                           "alerts": [{"level": a, "text": t} for a, t in g["alerts"]],
                           "legs": [{"tsym": l.get("tsym"), "qty": l.get("qty"), "avg": l.get("avg"), "ltp": l.get("lp"),
                                     "mtm": l.get("mtm"), "kind": l.get("kind"), "strike": l.get("strike")}
                                    for l in g["legs"]]})
        at = self.win.trades_svc.last_book.get("at")
        return {"positions": groups, "today": d.get("today"), "limits": d.get("limits"), "status": d.get("status"),
                "synced_at": at, "metrics": {k: v for k, v in (d.get("metrics") or {}).items()
                                             if k not in ("best", "worst")}}

    def alerts(self, _q):
        """Every active alert with a stable id; the phone raises a notification for unseen ids."""
        items = []
        for und, r in self.win.risk_view.results.items():
            for a in r["alerts"]:
                if a["level"] in ("RED", "AMBER"):
                    items.append({"id": _id("risk", und, a["level"], a["title"]), "level": a["level"],
                                  "source": "Writer risk monitor", "title": f"{und}: {a['title']}",
                                  "text": " · ".join(a["signals"][:2]) + f" → {a['action']}"})
        sync = self.win.sync_svc.last.get("setup_alert") or {}
        if getattr(self.win, "_setup_active", None) and sync.get("key"):
            items.append({"id": _id("setup", sync["key"]), "level": "SETUP", "source": "Evening setup",
                          "title": "Squeeze-loading setup " + ("▲ UP" if self.win._setup_active == "up" else "▼ DOWN"),
                          "text": sync.get("message", "")})
        if self.win.bnf_svc.symbols:
            for lv, t in self.win.bnf_svc.live_snapshot()["alerts"]:
                if lv == "red":
                    items.append({"id": _id("bnf", t.split(" ")[0], t[:40]), "level": "RED",
                                  "source": "BANKNIFTY monitor", "title": "BANKNIFTY breadth", "text": t})
        for g in (self.win.trades.data or {}).get("positions") or []:
            for a, t in g["alerts"]:
                if a == "stop":
                    items.append({"id": _id("pos", g["und"], g["expiry"], t[:50]), "level": "RED",
                                  "source": "My positions", "title": f"EXIT · {g['und']} {g['structure']}", "text": t})
        return {"alerts": items, "time": datetime.now()}


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None
