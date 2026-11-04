"""One visual language for every screen: colour tokens, the application
stylesheet, and the small building blocks pages share (page header, cards,
status pills, labels).  Spacing follows a 4-px grid (4 / 8 / 12 / 16)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

# ------------------------------------------------------------------ colour tokens
C = {
    "bg": "#0b0f17", "panel": "#111827", "panel2": "#0f172a", "border": "#1f2937", "hover": "#1e293b",
    "text": "#e5e7eb", "muted": "#94a3b8", "faint": "#64748b", "accent": "#38bdf8",
    "up": "#22c55e", "down": "#ef4444", "warn": "#f59e0b", "info": "#38bdf8",
    "fii": "#3b82f6", "dii": "#f59e0b",
    "ema20": "#facc15", "ema50": "#38bdf8", "ema200": "#c084fc",
}
SPACE = {"xs": 4, "s": 8, "m": 12, "l": 16}

QSS = f"""
* {{ font-size: 12px; color: {C['text']}; }}
QMainWindow, QWidget#root {{ background: {C['bg']}; }}
QFrame#sidebar {{ background: {C['panel2']}; border-right: 1px solid {C['border']}; }}
QFrame#appbar {{ background: {C['panel2']}; border-bottom: 1px solid {C['border']}; }}
QFrame#nav {{ background: {C['panel2']}; border-right: 1px solid {C['border']}; }}
QFrame.card {{ background: {C['panel']}; border: 1px solid {C['border']}; border-radius: 8px; }}
QLabel.h1 {{ font-size: 16px; font-weight: 700; color: {C['accent']}; }}
QLabel.h2 {{ font-size: 11px; font-weight: 700; color: {C['muted']}; letter-spacing: 1px; }}
QLabel.muted {{ color: {C['muted']}; }}
QComboBox, QPushButton {{ background: {C['panel']}; border: 1px solid {C['border']};
    border-radius: 6px; padding: 5px 10px; }}
QComboBox QAbstractItemView {{ background: {C['panel']}; selection-background-color: {C['hover']}; }}
QPushButton:hover {{ border-color: {C['accent']}; }}
QPushButton:checked {{ background: #0c4a6e; border-color: {C['accent']}; }}
QPushButton#primary {{ background: #0369a1; border-color: #0284c7; font-weight: 600; }}
QPushButton#primary:hover {{ background: #0284c7; }}
QPushButton:disabled {{ color: #475569; }}
QPushButton::menu-indicator {{ width: 0; }}
QMenu {{ background: {C['panel']}; border: 1px solid {C['border']}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {C['hover']}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; }}
QLineEdit {{ background: {C['panel2']}; border: 1px solid {C['border']}; border-radius: 5px; padding: 5px 8px; }}
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
QListWidget#navlist {{ background: transparent; border: none; outline: none; }}
QListWidget#navlist::item {{ padding: 8px 12px; border-radius: 6px; margin: 1px 6px; color: {C['muted']}; }}
QListWidget#navlist::item:hover {{ background: {C['hover']}; color: {C['text']}; }}
QListWidget#navlist::item:selected {{ background: #0c4a6e; color: #ffffff; }}
"""


# ------------------------------------------------------------------ building blocks
def label(text: str = "", size: int | None = None, muted: bool = False) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    style = "background:transparent;border:none;"
    if size:
        style += f"font-size:{size}px;"
    if muted:
        style += f"color:{C['muted']};"
    lbl.setStyleSheet(style)
    return lbl


def card(title: str = "") -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setProperty("class", "card")
    f.setStyleSheet(f"QFrame {{ background:{C['panel']}; border:1px solid {C['border']}; border-radius:8px; }}"
                    "QLabel { border:none; background:transparent; }")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(SPACE["m"], SPACE["s"] + 2, SPACE["m"], SPACE["m"])
    lay.setSpacing(SPACE["s"])
    if title:
        t = QLabel(title.upper())
        t.setStyleSheet(f"font-size:11px;font-weight:700;color:{C['muted']};letter-spacing:1px")
        lay.addWidget(t)
    return f, lay


def toolbar() -> tuple[QFrame, QHBoxLayout]:
    """The strip of page-specific controls under a page header."""
    f = QFrame()
    f.setStyleSheet(f"QFrame {{ background:{C['panel']}; border:1px solid {C['border']}; border-radius:8px; }}"
                    "QLabel, QCheckBox { border:none; background:transparent; }")
    lay = QHBoxLayout(f)
    lay.setContentsMargins(SPACE["m"], 6, SPACE["m"], 6)
    lay.setSpacing(SPACE["s"])
    return f, lay


def pill(text: str, color: str = C["muted"]) -> str:
    """Inline status pill (rich text)."""
    return (f"<span style='color:{color};background:rgba(148,163,184,0.10);border-radius:8px;"
            f"padding:2px 8px'>● {text}</span>")


class PageHeader(QFrame):
    """Title + one-line purpose on the left, page actions on the right.
    Every screen starts with one, so the eye always finds the same anchor."""

    def __init__(self, title: str, subtitle: str = ""):
        super().__init__()
        self.setStyleSheet("QFrame { background:transparent; border:none; } QLabel { background:transparent; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SPACE["xs"], 0, SPACE["xs"], 0)
        lay.setSpacing(SPACE["s"])
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = QLabel(title)
        self.title.setStyleSheet(f"font-size:17px;font-weight:700;color:{C['text']}")
        col.addWidget(self.title)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setTextFormat(Qt.TextFormat.RichText)
        self.subtitle.setStyleSheet(f"font-size:11px;color:{C['muted']}")
        col.addWidget(self.subtitle)
        lay.addLayout(col, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(SPACE["s"])
        lay.addLayout(self.actions)

    def add(self, w: QWidget):
        self.actions.addWidget(w)
        return w


def page(title: str, subtitle: str, body: QWidget) -> tuple[QWidget, PageHeader]:
    """Wrap a screen with the standard header and margins."""
    w = QWidget(objectName="root")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(SPACE["l"], SPACE["m"], SPACE["l"], SPACE["s"])
    lay.setSpacing(SPACE["s"])
    head = PageHeader(title, subtitle)
    lay.addWidget(head)
    lay.addWidget(body, 1)
    return w, head
