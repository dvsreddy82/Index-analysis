"""The one place the app talks to Shoonya about the connection.

`Broker` owns the single ShoonyaFeed, the OAuth sign-in (browser + local
redirect catcher), today's saved session, and the connection state.  Screens
never connect on their own: they read `broker.feed` and listen to
`broker.changed` (any state change), `broker.connected` and
`broker.disconnected`.  Read-only: no order route is ever called.

Shoonya is used only in market hours (weekdays 9:00 am – 3:40 pm IST, pre-open included):
today's saved session connects by itself when the window opens, and the connection is
closed after the window (once the closing candles are stored).  Outside the window every
screen uses NSE / Yahoo and the data stored in MongoDB.  A manual connect after hours is
still possible (e.g. to pull positions) and is left alone.  In market hours a connection
whose index ticks stop arriving is reported as degraded (`healthy` is False), and the
screens fall back to NSE until ticks resume.
"""
from __future__ import annotations

import html
import logging
from datetime import date

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLineEdit, QMessageBox, QPushButton, QVBoxLayout)

from shoonya_feed import (REDIRECT_URL, OAuthCatcher, ShoonyaFeed, code_from_text, from_env, load_settings,
                          save_settings, totp_now)
from market_hours import SESSION, in_session, market_open, now_ist  # noqa: F401
from tasks import Runner
from theme import C, label

log = logging.getLogger(__name__)



class Broker(QObject):
    changed = Signal()            # state or status text changed
    connected = Signal(str)       # connection message
    disconnected = Signal()
    session_closing = Signal()    # the market-hours window ended: store the closing data, then end_session()

    def __init__(self, runner: Runner, parent=None):
        super().__init__(parent)
        self.runner = runner
        self.feed = ShoonyaFeed()
        self.busy = False
        self._last = None
        self._watch = QTimer(self)                 # websocket reconnects change the status text
        self._watch.timeout.connect(self._poll_state)
        self._watch.start(2000)
        self._manual_after_hours = False          # connected by hand outside the window: leave it
        self._user_off: date | None = None        # disconnected by hand today: no auto-reconnect
        self._closing = False
        self._session = QTimer(self)
        self._session.timeout.connect(self._session_tick)
        self._session.start(60_000)

    # ---------------------------------------------------------------- state
    @property
    def live(self) -> bool:
        return self.feed.live

    @property
    def healthy(self) -> bool:
        """Connected and actually streaming: an index tick in the last 30 s (in market hours)."""
        f = self.feed
        if not f.live:
            return False
        if not market_open():
            return True
        from shoonya_feed import INDEX_TOKENS
        return any(f._fresh(f.tick(INDEX_TOKENS[s])) for s in ("NIFTY BANK", "^NSEI"))

    def state(self) -> tuple[str, str, str]:
        """(kind, short text, colour): kind is live / connecting / degraded / closed / off."""
        f = self.feed
        if self.busy:
            return "connecting", "Shoonya: connecting…", C["warn"]
        if f.live and not self.healthy:
            return "degraded", "Shoonya: no ticks — using NSE until they resume", C["warn"]
        if f.live:
            return "live", "Shoonya real-time", C["up"]
        if f.logged_in:
            return "degraded", f"Shoonya: {f.status} — using NSE", C["warn"]
        if not in_session():
            return "closed", "Market closed — NSE / stored data (Shoonya is used 9:00–15:40)", C["muted"]
        return "off", "Shoonya: not connected — using NSE", C["muted"]

    # ---------------------------------------------------------------- market-hours window
    def _session_tick(self):
        if in_session():
            self._closing = False
            self._manual_after_hours = False
            if not (self.busy or self.feed.live or self.feed.logged_in) and self._user_off != date.today():
                self.auto_connect()
        elif (self.feed.live or self.feed.logged_in) and not self._manual_after_hours and not self._closing:
            self._closing = True
            log.info("Market-hours window over: storing closing data, then closing Shoonya")
            self.session_closing.emit()
            QTimer.singleShot(5 * 60_000, self.end_session)           # in case the closing jobs never report

    def end_session(self):
        if (self.feed.live or self.feed.logged_in) and not in_session() and not self._manual_after_hours:
            self.feed.close()
            self.disconnected.emit()
            self._poll_state()

    def _poll_state(self):
        s = self.state()
        if s != self._last:
            self._last = s
            self.changed.emit()

    # ---------------------------------------------------------------- actions
    def toggle(self, parent):
        if self.feed.live or self.feed.logged_in:
            if QMessageBox.question(parent, "Shoonya", "Disconnect from Shoonya real-time data?") \
                    == QMessageBox.StandardButton.Yes:
                self.disconnect()
            return
        self.connect(parent)

    def disconnect(self):
        self._user_off = date.today()
        self.feed.close()
        self.disconnected.emit()
        self._poll_state()

    def auto_connect(self):
        """Reuse today's saved session silently (no browser, no dialogs) — market hours only."""
        if not in_session():
            log.info("Outside market hours: Shoonya not connected (NSE / stored data in use)")
            return
        s = load_settings()
        if s.get("access_token") and s.get("token_date") == date.today().isoformat():
            self._run(s, token=s["access_token"], remember=True, parent=None, silent=True)

    def connect(self, parent):
        if self.busy:
            return
        if not in_session():
            if QMessageBox.question(parent, "Shoonya", "The market is closed, so prices come from NSE and the data "
                                    "stored in MongoDB — Shoonya isn't needed.\n\nConnect anyway (for example to "
                                    "pull your positions)?") != QMessageBox.StandardButton.Yes:
                return
            self._manual_after_hours = True
        self._user_off = None
        s = load_settings()
        if s.get("access_token") and s.get("token_date") == date.today().isoformat():
            self._run(s, token=s["access_token"], remember=True, parent=parent, retry_signin=True)
            return
        self._signin(s, parent)

    def _signin(self, settings: dict, parent):
        if from_env():                       # credentials come from .env: go straight to the browser
            v, remember = {k: settings[k] for k in ("user_id", "client_id", "secret")}, True
        else:
            dlg = ShoonyaDialog(settings, REDIRECT_URL, parent)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            v, remember = dlg.values(), dlg.remember.isChecked()
        try:
            catcher = OAuthCatcher()
        except OSError as exc:
            QMessageBox.warning(parent, "Shoonya", f"Could not listen on {REDIRECT_URL} ({exc}). "
                                "Close whatever uses that port, or paste the redirected address when asked.")
            catcher = None
        url = ShoonyaFeed.authorize_url(v)
        QDesktopServices.openUrl(QUrl(url))
        wait = ShoonyaWaitDialog(url, (lambda: catcher.code) if catcher else (lambda: None), parent)
        ok = wait.exec() == QDialog.DialogCode.Accepted
        if catcher:
            catcher.close()
        if ok and wait.code:
            self._run(v, code=wait.code, remember=remember, parent=parent)

    def _run(self, settings: dict, token: str | None = None, code: str | None = None, remember: bool = True,
             parent=None, retry_signin: bool = False, silent: bool = False):
        self.busy = True
        self._poll_state()
        feed = self.feed

        def job(_progress):
            tok = token or feed.exchange_code(settings, code)
            return {"msg": feed.connect(settings, tok), "token": tok}

        def done(out):
            self.busy = False
            if remember:
                save_settings(settings, out["token"])
            self._poll_state()
            self.connected.emit(out["msg"])

        def failed(err):
            self.busy = False
            self._poll_state()
            if silent:
                log.info("Saved Shoonya session not usable: %s", err.splitlines()[0])
            elif retry_signin:             # today's saved session no longer works: sign in again
                self._signin(settings, parent)
            else:
                QMessageBox.warning(parent, "Shoonya connection failed", err.split("\n\n")[0])

        self.runner.start(job, done, failed)


# ---------------------------------------------------------------------- dialogs
class ShoonyaDialog(QDialog):
    """Client ID / secret for Shoonya's OAuth login. Password and TOTP are typed on
    Shoonya's own page in the browser, never in this app."""

    def __init__(self, settings: dict, redirect_url: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect Shoonya — real-time data (read-only)")
        self.setMinimumWidth(500)
        lay = QVBoxLayout(self)
        lay.addWidget(label(
            f"<span style='color:{C['muted']}'>Used only for live prices, option chains and your positions — "
            "this app never places orders. On <b>trade.shoonya.com → Profile → API Key</b>, copy your "
            "<b>Client ID</b> and <b>Secret code</b>, and set the redirect URL to:</span>"))
        url = QLineEdit(redirect_url)
        url.setReadOnly(True)
        lay.addWidget(url)
        lay.addWidget(label(f"<span style='color:{C['muted']}'>You then sign in with your password and TOTP on "
                            "Shoonya's own page in your browser — this app never sees them.</span>"))
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.f: dict[str, QLineEdit] = {}
        for key, text, secret in (("user_id", "User ID", False), ("client_id", "Client ID (API key)", False),
                                  ("secret", "Secret code", True)):
            e = QLineEdit(settings.get(key, ""))
            if secret:
                e.setEchoMode(QLineEdit.EchoMode.Password)
            self.f[key] = e
            form.addRow(text, e)
        lay.addLayout(form)
        self.remember = QCheckBox("Remember these and today's session on this computer")
        self.remember.setChecked(True)
        lay.addWidget(self.remember)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Sign in with Shoonya")
        btns.accepted.connect(self._ok)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _ok(self):
        if all(self.f[k].text().strip() for k in self.f):
            self.accept()

    def values(self) -> dict:
        return {k: e.text().strip() for k, e in self.f.items()}


class ShoonyaWaitDialog(QDialog):
    """Waits for the browser redirect; the user can also paste the redirected URL."""

    def __init__(self, auth_url: str, poll, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Signing in to Shoonya…")
        self.setMinimumWidth(520)
        self.code: str | None = None
        self._poll = poll
        lay = QVBoxLayout(self)
        lay.addWidget(label("Finish signing in on Shoonya's page that just opened in your browser. "
                            "This window closes by itself when Shoonya sends you back."))
        link = label(f"<a href='{html.escape(auth_url)}' style='color:{C['accent']}'>Open the sign-in page again</a>")
        link.setOpenExternalLinks(True)
        lay.addWidget(link)
        if totp_now():
            row = QHBoxLayout()
            self.code_lbl = label()
            row.addWidget(self.code_lbl, 1)
            copy = QPushButton("Copy code")
            copy.clicked.connect(lambda: QApplication.clipboard().setText((totp_now() or ("",))[0]))
            row.addWidget(copy)
            lay.addLayout(row)
            self._tick_code()
            t = QTimer(self)
            t.timeout.connect(self._tick_code)
            t.start(1000)
        lay.addWidget(label(f"<span style='color:{C['muted']}'>If the browser shows an error page after "
                            "signing in, copy its full address and paste it here:</span>"))
        self.paste = QLineEdit()
        self.paste.setPlaceholderText("http://127.0.0.1:8765/shoonya/callback?code=…")
        lay.addWidget(self.paste)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.button(QDialogButtonBox.StandardButton.Ok).setText("Use pasted address")
        btns.accepted.connect(self._use_paste)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._check)
        self.timer.start(300)

    def _tick_code(self):
        now = totp_now()
        if now:
            code, left = now
            self.code_lbl.setText(f"TOTP for Shoonya's page: <b style='font-size:18px;letter-spacing:2px'>"
                                  f"{code[:3]} {code[3:]}</b> <span style='color:{C['muted']}'>· {left}s left</span>")

    def _check(self):
        code = self._poll()
        if code:
            self.code = code
            self.accept()

    def _use_paste(self):
        code = code_from_text(self.paste.text())
        if code:
            self.code = code
            self.accept()
