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
  /api/news        relevant headlines (?hours=&impact=HIGH&tag=BANKNIFTY&limit=)
  /api/global      world indices, US futures, crude, dollar, yields, rupee, US VIX + the read for India's open
  /api/signals     NIFTY / BANKNIFTY: latest 15-minute direction call, today's calls, key levels, level breaks
  /api/alerts      every active alert with a stable id (the phone notifies on new ids)
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from PySide6.QtCore import QObject, Signal

import config
from api_routes import ApiRoutes

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



class ApiServer(QObject):
    """Runs the HTTP server on a background thread. `need` asks the GUI thread to refresh
    something (queued signal, so handlers never touch Qt objects directly)."""
    need = Signal(str)

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.key = api_key()
        self.routes = ApiRoutes(win, self.need.emit)
        self._first_seen = self.routes._first_seen
        self.httpd: ThreadingHTTPServer | None = None
        self.started_at = time.time()
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
        if url.path not in self.routes.table:
            return self._send(h, 404, {"error": "unknown endpoint"})
        try:
            self._send(h, 200, self.routes.get(h.path))
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

