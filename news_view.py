"""Market news screen: headlines that can move NIFTY / BANKNIFTY, newest first, with
impact, tags, headline tone and sources; filters and search; double-click opens the
article.  Data comes from news_service.py; this file only draws."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QHeaderView, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from theme import C, label, toolbar

IMPACT_STYLE = {"HIGH": (C["down"], "#450a0a"), "MEDIUM": (C["warn"], "#3b2306"), "LOW": (C["muted"], C["panel"])}
TONE = {"positive": ("▲ positive", C["up"]), "negative": ("▼ negative", C["down"]), "neutral": ("• neutral", C["muted"])}
FILTERS = [("All", None), ("High impact", "HIGH"), ("BANKNIFTY", "BANKNIFTY"), ("NIFTY", "NIFTY"), ("Macro", "MACRO"),
           ("RBI", "RBI"), ("Bank filings", "FILING")]


class NewsDashboard(QWidget):
    refresh_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.items: list[dict] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        bar, hl = toolbar()
        self.group = QButtonGroup(self)
        for i, (name, _key) in enumerate(FILTERS):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setChecked(i == 0)
            self.group.addButton(b, i)
            hl.addWidget(b)
        self.group.idClicked.connect(lambda _i: self.render())
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search headlines…")
        self.search.textChanged.connect(lambda _t: self.render())
        hl.addWidget(self.search, 1)
        b = QPushButton("Check now", objectName="primary")
        b.clicked.connect(self.refresh_requested.emit)
        hl.addWidget(b)
        lay.addWidget(bar)
        self.status = label("Checking news sources…")
        lay.addWidget(self.status)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Time (IST)", "Impact", "Tags", "Headline", "Tone", "Source"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        for i in range(6):
            hh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._open)
        lay.addWidget(self.table, 1)
        lay.addWidget(label(f"<span style='color:{C['muted']}'>Sources: RBI press releases &amp; notifications, NSE "
                            "corporate announcements (BANKNIFTY banks), Economic Times, Livemint, Business Standard, "
                            "Google News. Checked every 2 min from 8:30 am to 4 pm IST, every 10 min otherwise. "
                            "Impact and tone are keyword rules on the headline — a reading aid, not a forecast. "
                            "HIGH items alert you (Mac notification + phone), at most one per story and per 20 min. "
                            "Double-click a headline to open it.</span>", 11))

    def set_status(self, text: str):
        self.status.setText(text)

    def set_items(self, items: list[dict]):
        self.items = items
        self.render()

    def _open(self, row: int, _col: int):
        it = self.table.item(row, 3)
        link = it.data(Qt.ItemDataRole.UserRole) if it else None
        if link:
            QDesktopServices.openUrl(QUrl(link))

    def render(self):
        key = FILTERS[self.group.checkedId()][1]
        q = self.search.text().strip().lower()
        rows = []
        for d in self.items:
            if key == "HIGH" and d.get("impact") != "HIGH":
                continue
            if key and key != "HIGH" and key not in d.get("tags", []):
                continue
            if q and q not in d["title"].lower() and q not in (d.get("summary") or "").lower():
                continue
            rows.append(d)
        t = self.table
        t.setRowCount(len(rows))
        bold = QFont(self.font())
        bold.setBold(True)
        for r, d in enumerate(rows):
            p = pd.Timestamp(d["published"]).tz_convert("Asia/Kolkata")
            today = p.date() == datetime.now().date()
            cells = [f"{p:%H:%M}" if today else f"{p:%d %b %H:%M}", d.get("impact", ""), " · ".join(d.get("tags", [])),
                     d["title"], TONE.get(d.get("tone"), ("", C["muted"]))[0],
                     ", ".join(d.get("sources") or [d.get("source", "")])]
            for c, v in enumerate(cells):
                it = QTableWidgetItem(v)
                if c == 1:
                    fg, bg = IMPACT_STYLE.get(v, (C["muted"], C["panel"]))
                    it.setForeground(QColor(fg))
                    it.setBackground(QColor(bg))
                    it.setFont(bold)
                    it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c == 3:
                    it.setData(Qt.ItemDataRole.UserRole, d.get("link"))
                    it.setToolTip((d.get("summary") or d["title"])[:400])
                    if d.get("impact") == "HIGH":
                        it.setFont(bold)
                    if d.get("alerted"):
                        it.setText("🔔 " + v)
                if c == 4:
                    it.setForeground(QColor(TONE.get(d.get("tone"), ("", C["muted"]))[1]))
                if c in (0, 2, 5):
                    it.setForeground(QColor(C["muted"]))
                t.setItem(r, c, it)
