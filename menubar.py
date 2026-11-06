"""Live index ticker in the macOS menu bar (status bar), e.g.

    BNF 57,936.30 ▲ +342.10 (+0.59%)

The text is coloured green / red by the day's change and updates every second
from Shoonya ticks (NSE's snapshot when Shoonya isn't connected).  Clicking it
opens a menu with BANKNIFTY / NIFTY / VIX details, your book's open P&L, active
risk alerts, a chooser for what the bar shows, and "Open NSE Quant Terminal".

Uses AppKit's NSStatusItem through PyObjC (Qt's tray icon can only draw a small
square icon).  Everything runs on the GUI (main) thread, which Qt shares with
Cocoa.  On other platforms, or without PyObjC, it falls back to a Qt tray icon
whose tooltip carries the same text.
"""
from __future__ import annotations

import json
import logging
import sys

import config

log = logging.getLogger(__name__)

SETTINGS = config.APP_HOME / "menubar.json"
CHOICES = {"BANKNIFTY": ["NIFTY BANK"], "NIFTY": ["^NSEI"], "Both": ["NIFTY BANK", "^NSEI"]}
SHORT = {"NIFTY BANK": "BNF", "^NSEI": "NIFTY", "INDIA VIX": "VIX"}

try:  # macOS only
    import objc
    from AppKit import (NSAttributedString, NSColor, NSFont, NSFontAttributeName, NSForegroundColorAttributeName,
                        NSMenu, NSMenuItem, NSMutableAttributedString, NSStatusBar, NSVariableStatusItemLength)
    from Foundation import NSObject
    HAVE_APPKIT = sys.platform == "darwin"
except Exception:          # pragma: no cover - non-mac or PyObjC missing
    HAVE_APPKIT = False


if HAVE_APPKIT:
    class _MenuTarget(NSObject):
        """Receives menu clicks and forwards them to Python callbacks."""

        def initWithOwner_(self, owner):
            self = objc.super(_MenuTarget, self).init()
            if self is None:
                return None
            self.owner = owner
            return self

        def openApp_(self, sender):
            self.owner._on_open()

        def choose_(self, sender):
            self.owner._choose(str(sender.representedObject()))

        def toggleHide_(self, sender):
            self.owner._toggle_hidden()

        def menuNeedsUpdate_(self, menu):          # NSMenuDelegate: fill the menu as it opens
            self.owner._menu_rebuild()


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


class MenuBarTicker:
    def __init__(self, on_open=None):
        self.on_open = on_open
        self.choice = "BANKNIFTY"
        self.hidden = False
        try:
            s = json.loads(SETTINGS.read_text())
            self.choice = s.get("choice", self.choice) if s.get("choice") in CHOICES else self.choice
            self.hidden = bool(s.get("hidden", False))
        except (OSError, ValueError):
            pass
        self.quotes: dict[str, dict] = {}
        self.extra: dict = {}
        self._qt_tray = None
        self.item = None
        if HAVE_APPKIT:
            try:
                self._build_appkit()
            except Exception as exc:
                log.warning("Menu bar ticker unavailable (%s); using a tray icon", exc)
                self.item = None
        if self.item is None:
            self._build_qt()

    # ---------------------------------------------------------------- setup
    def _build_appkit(self):
        self.target = _MenuTarget.alloc().initWithOwner_(self)
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.item.button().setToolTip_("NSE Quant Terminal — live index ticker")
        self.menu = NSMenu.alloc().init()
        self.menu.setAutoenablesItems_(False)
        self.menu.setDelegate_(self.target)
        self.item.setMenu_(self.menu)
        self._title("Loading…", None)

    def _build_qt(self):
        from PySide6.QtGui import QColor, QIcon, QPixmap
        from PySide6.QtWidgets import QMenu, QSystemTrayIcon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        pm = QPixmap(16, 16)
        pm.fill(QColor("#38bdf8"))
        self._qt_tray = QSystemTrayIcon(QIcon(pm))
        m = QMenu()
        m.addAction("Open NSE Quant Terminal", self._on_open)
        self._qt_tray.setContextMenu(m)
        self._qt_menu = m
        self._qt_tray.show()

    def _save(self):
        try:
            SETTINGS.write_text(json.dumps({"choice": self.choice, "hidden": self.hidden}))
        except OSError:
            pass

    # ---------------------------------------------------------------- inputs
    def update(self, quotes: dict[str, dict], extra: dict | None = None):
        """quotes: symbol -> {last, prev, high, low, live(bool)}; extra: {book_pnl, book_at, red, amber, source}."""
        self.quotes = {k: v for k, v in quotes.items() if v and v.get("last")}
        self.extra = extra or {}
        self._render()

    def close(self):
        if self.item is not None:
            NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
            self.item = None
        if self._qt_tray is not None:
            self._qt_tray.hide()

    # ---------------------------------------------------------------- actions
    def _on_open(self):
        if self.on_open:
            self.on_open()

    def _choose(self, choice: str):
        if choice in CHOICES:
            self.choice = choice
            self.hidden = False
            self._save()
            self._render()

    def _toggle_hidden(self):
        self.hidden = not self.hidden
        self._save()
        self._render()

    # ---------------------------------------------------------------- drawing
    def _segments(self, sym: str):
        q = self.quotes.get(sym)
        if not q:
            return [(f"{SHORT.get(sym, sym)} —", None)]
        last, prev = q["last"], q.get("prev")
        segs = [(f"{SHORT.get(sym, sym)} {_fmt(last)}", None)]
        if prev:
            ch = last - prev
            arrow = "▲" if ch >= 0 else "▼"
            segs.append((f" {arrow}{abs(ch):,.1f} ({ch / prev * 100:+.2f}%)", ch >= 0))
        return segs

    def _render(self):
        syms = CHOICES[self.choice]
        if self.hidden:
            segs = [("₹", None)]
        else:
            segs = []
            for i, s in enumerate(syms):
                if i:
                    segs.append(("   ", None))
                segs += self._segments(s)
            if self.extra.get("setup"):
                segs.append(("  ⚑▲" if self.extra["setup"] == "up" else "  ⚑▼", self.extra["setup"] == "up"))
            if self.extra.get("red"):
                segs.append((f"  ⚠{self.extra['red']}", False))
        plain = "".join(t for t, _ in segs)
        if self.item is not None:
            self._title_segments(segs)
        elif self._qt_tray is not None:
            self._qt_tray.setToolTip(plain + self._detail_text())

    def _title(self, text, up):
        self._title_segments([(text, up)])

    def _title_segments(self, segs):
        s = NSMutableAttributedString.alloc().init()
        font = NSFont.monospacedDigitSystemFontOfSize_weight_(12.5, 0.3)
        for text, up in segs:
            attrs = {NSFontAttributeName: font}
            if up is True:
                attrs[NSForegroundColorAttributeName] = NSColor.systemGreenColor()
            elif up is False:
                attrs[NSForegroundColorAttributeName] = NSColor.systemRedColor()
            s.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(text, attrs))
        self.item.button().setAttributedTitle_(s)

    def _detail_lines(self) -> list[tuple[str, bool | None]]:
        out = []
        for sym, name in (("NIFTY BANK", "BANKNIFTY"), ("^NSEI", "NIFTY 50"), ("INDIA VIX", "India VIX")):
            q = self.quotes.get(sym)
            if not q:
                continue
            line = f"{name:<10} {_fmt(q['last'])}"
            up = None
            if q.get("prev"):
                ch = q["last"] - q["prev"]
                up = ch >= 0 if sym != "INDIA VIX" else ch < 0
                line += f"   {ch:+,.2f} ({ch / q['prev'] * 100:+.2f}%)"
            if q.get("high") and q.get("low"):
                line += f"   H {_fmt(q['high'])}  L {_fmt(q['low'])}"
            out.append((line, up))
        e = self.extra
        if e.get("book_pnl") is not None:
            out.append((f"My open P&L  ₹{e['book_pnl']:+,.0f}" + (f"   (as of {e['book_at']})" if e.get("book_at")
                                                                   else ""), e["book_pnl"] >= 0))
        if e.get("setup"):
            out.append((f"Evening setup: {'▲ UP' if e['setup'] == 'up' else '▼ DOWN'} squeeze loading — short "
                        f"{'calls' if e['setup'] == 'up' else 'puts'} exposed", e["setup"] == "up"))
        if e.get("red") or e.get("amber"):
            out.append((f"Risk monitor: {e.get('red', 0)} RED · {e.get('amber', 0)} AMBER", False if e.get("red")
                        else None))
        out.append((e.get("source") or "", None))
        return [x for x in out if x[0]]

    def _detail_text(self) -> str:
        return "\n" + "\n".join(t for t, _ in self._detail_lines())

    def _menu_rebuild(self):
        m = self.menu
        m.removeAllItems()
        font = NSFont.monospacedDigitSystemFontOfSize_weight_(12.5, 0.0)
        for text, up in self._detail_lines():
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(text, None, "")
            attrs = {NSFontAttributeName: font}
            if up is True:
                attrs[NSForegroundColorAttributeName] = NSColor.systemGreenColor()
            elif up is False:
                attrs[NSForegroundColorAttributeName] = NSColor.systemRedColor()
            it.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(text, attrs))
            it.setEnabled_(True)
            m.addItem_(it)
        m.addItem_(NSMenuItem.separatorItem())
        head = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Show in menu bar", None, "")
        head.setEnabled_(False)
        m.addItem_(head)
        for choice in CHOICES:
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(f"   {choice}", "choose:", "")
            it.setTarget_(self.target)
            it.setRepresentedObject_(choice)
            it.setState_(1 if choice == self.choice and not self.hidden else 0)
            m.addItem_(it)
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "   Hide price (show ₹ only)" if not self.hidden else "   Show price again", "toggleHide:", "")
        it.setTarget_(self.target)
        m.addItem_(it)
        m.addItem_(NSMenuItem.separatorItem())
        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Open NSE Quant Terminal", "openApp:", "")
        it.setTarget_(self.target)
        m.addItem_(it)
