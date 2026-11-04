"""AI coach sub-tab of Trades & P&L: ask a local Ollama model to review your own trades.

The brief sent to the model is built by ai_coach.build_brief from the numbers the
Trades tab already computed.  Answers stream in as markdown and are saved to the
`ai_reviews` collection so past reviews can be re-read and compared over time.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QPushButton, QSplitter, QTextBrowser, QVBoxLayout, QWidget)

import ai_coach as ai
from theme import C


class _Bridge(QObject):
    chunk = Signal(str, str)        # kind, text
    done = Signal(dict)
    failed = Signal(str)
    status = Signal(bool, list, str)  # server up, models, message


WELCOME = """### 🤖 AI coach — chat about your trades
Runs **qwen3:14b locally** through Ollama — your trades never leave this Mac.

**Start with a one-click review above**, or type a question below, for example:
- *Why am I losing money on BANKNIFTY?*
- *Should I be selling strangles or iron condors given my results?*
- *Which of my habits cost me the most this month?*
- *Review my open NIFTY position against my plan.*

Follow-up questions continue the same conversation. **New chat** starts fresh with the latest numbers.
The coach sees what the Trades & P&L tab computed for the selected period: metrics, breakdowns, the
*Why I lost* findings, open positions and alerts, and your recent strategies with market context, mistakes and
journal notes.

*Automated review of your own data — not investment advice.*"""


class AICoachPanel(QWidget):
    data_needed = Signal()          # ask the app to load / sync trades

    def __init__(self, data_provider, store, period_provider=lambda: ""):
        super().__init__()
        self.setObjectName("root")
        self.data_provider, self.store, self.period_provider = data_provider, store, period_provider
        self.bridge = _Bridge()
        self.bridge.chunk.connect(self._on_chunk)
        self.bridge.done.connect(self._on_done)
        self.bridge.failed.connect(self._on_failed)
        self.bridge.status.connect(self._on_status)
        self.stop = threading.Event()
        self.running = False
        self.thread_msgs: list[dict] = []       # follow-up conversation for the current review
        self.brief: dict | None = None
        self.text, self.think_chars, self.current = "", 0, {}
        self.chat: list[tuple[str, str]] = []    # (role, markdown) shown as the conversation
        self._pending: tuple | None = None       # question waiting for trades / the model list to load
        self._checked = False
        self.repaint_timer = QTimer(self)
        self.repaint_timer.setInterval(150)
        self.repaint_timer.timeout.connect(self._repaint)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)

        top = QFrame()
        top.setStyleSheet(f"QFrame {{ background:{C['panel']}; border:1px solid {C['border']}; border-radius:8px; }}"
                          "QLabel, QCheckBox { border:none; background:transparent; }")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(10, 6, 10, 6)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Local model</b>"))
        self.model = QComboBox()
        self.model.setMinimumWidth(170)
        row.addWidget(self.model)
        b = QPushButton("↻")
        b.setToolTip("Re-check Ollama and the installed models")
        b.setFixedWidth(32)
        b.clicked.connect(self.check_server)
        row.addWidget(b)
        self.think = QCheckBox("Deep thinking")
        self.think.setChecked(False)
        self.think.setToolTip("Let the model reason step by step before answering: more careful, about twice as slow")
        row.addWidget(self.think)
        self.server = QLabel("Checking Ollama…")
        self.server.setTextFormat(Qt.TextFormat.RichText)
        self.server.setStyleSheet("font-size:11px")
        row.addWidget(self.server, 1)
        tl.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Quick reviews</b>"))
        self.preset_btns = []
        for name in ai.PRESETS:
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, n=name: self.ask(ai.PRESETS[n], preset=n, new=True))
            row.addWidget(b)
            self.preset_btns.append(b)
        row.addStretch(1)
        tl.addLayout(row)
        lay.addWidget(top)

        bottom = QFrame()
        bottom.setStyleSheet(f"QFrame {{ background:{C['panel']}; border:1px solid {C['border']}; border-radius:8px; }}")
        row = QHBoxLayout(bottom)
        row.setContentsMargins(10, 6, 10, 6)
        self.q = QLineEdit()
        self.q.setPlaceholderText("Type a question about your trades and press Enter…  e.g. \"Why am I losing on "
                                  "BANKNIFTY?\"")
        self.q.setStyleSheet(f"QLineEdit {{ background:{C['panel2']}; color:{C['text']}; border:1px solid "
                             f"{C['border']}; border-radius:5px; padding:5px 8px; }}")
        self.q.returnPressed.connect(self._ask_free)
        row.addWidget(self.q, 1)
        self.ask_btn = QPushButton("Ask", objectName="primary")
        self.ask_btn.clicked.connect(self._ask_free)
        self.new_btn = QPushButton("New chat")
        self.new_btn.clicked.connect(self._new)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop.set)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.text))
        for w in (self.ask_btn, self.new_btn, self.stop_btn, self.copy_btn):
            row.addWidget(w)

        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget(objectName="root")
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(QLabel(f"<span style='color:{C['muted']}'>Past answers</span>"))
        self.saved = QListWidget()
        self.saved.setStyleSheet(f"QListWidget {{ background:{C['panel2']}; color:{C['text']}; border:1px solid "
                                 f"{C['border']}; border-radius:6px; font-size:11px; }}"
                                 f"QListWidget::item {{ padding:5px; border-bottom:1px solid {C['border']}; }}"
                                 f"QListWidget::item:selected {{ background:{C['border']}; }}")
        self.saved.setWordWrap(True)
        self.saved.itemClicked.connect(self._open_saved)
        ll.addWidget(self.saved, 1)
        split.addWidget(left)
        right = QWidget(objectName="root")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.state = QLabel("")
        self.state.setTextFormat(Qt.TextFormat.RichText)
        self.state.setStyleSheet(f"font-size:11px;color:{C['muted']}")
        rl.addWidget(self.state)
        self.out = QTextBrowser()
        self.out.setOpenExternalLinks(False)
        self.out.setStyleSheet(f"QTextBrowser {{ background:{C['panel2']}; border:1px solid {C['border']};"
                               "border-radius:6px; padding:10px; font-size:13px; }")
        self.out.setMarkdown(WELCOME)
        rl.addWidget(self.out, 1)
        split.addWidget(right)
        split.setSizes([220, 900])
        lay.addWidget(split, 1)
        lay.addWidget(bottom)

    # ------------------------------------------------------------ server / models
    def check_server(self):
        self.server.setText("Checking Ollama…")

        def job():
            up = ai.ensure_server()
            try:
                names = ai.models() if up else []
                msg = ""
            except Exception as exc:
                names, msg = [], str(exc)
            self.bridge.status.emit(up, names, msg)

        threading.Thread(target=job, daemon=True).start()

    def _on_status(self, up: bool, names: list, msg: str):
        cur = self.model.currentText() or ai.DEFAULT_MODEL
        self.model.clear()
        self.model.addItems(names)
        if names:
            self.model.setCurrentIndex(max(self.model.findText(cur), self.model.findText(ai.DEFAULT_MODEL), 0))
        if not up:
            self.server.setText(f"<span style='color:{C['down']}'>● Ollama not running</span> — install from "
                                "ollama.com or run <code>ollama serve</code>, then ↻")
        elif ai.DEFAULT_MODEL not in names:
            self.server.setText(f"<span style='color:#f59e0b'>● Ollama up</span> — recommended model missing: run "
                                f"<code>ollama pull {ai.DEFAULT_MODEL}</code>, then ↻" + (f" ({msg})" if msg else ""))
        else:
            self.server.setText(f"<span style='color:{C['up']}'>● Ollama running locally</span> · "
                                f"{len(names)} model(s)")
        self._busy(self.running)
        self._checked = True
        if names:
            self.data_ready()

    def showEvent(self, e):
        super().showEvent(e)
        if not self.model.count():
            self.check_server()
        self.load_saved()

    # ------------------------------------------------------------ asking
    def _ask_free(self):
        q = self.q.text().strip()
        if q and not self.running:
            self.ask(q, preset="Question", new=not self.thread_msgs)
            self.q.clear()

    def _new(self):
        self.thread_msgs, self.brief, self.chat, self.text = [], None, [], ""
        self.out.setMarkdown(WELCOME)
        self.state.setText("New chat — the next question starts fresh with the latest data.")

    def data_ready(self):
        """Trades finished loading: run a question that was waiting for them."""
        if self._pending and not self.running:
            args, self._pending = self._pending, None
            self.ask(*args)

    def _render(self, partial: str | None = None, note: str = ""):
        parts = []
        for role, md in self.chat:
            parts.append(f"#### 🧑 You\n{md}" if role == "user" else f"#### 🤖 Coach\n{md}")
        if partial is not None:
            parts.append(f"#### 🤖 Coach\n{partial or note}")
        sb = self.out.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 40
        self.out.setMarkdown("\n\n---\n\n".join(parts) if parts else WELCOME)
        if at_end or partial is not None and not partial:
            sb.setValue(sb.maximum())

    def ask(self, question: str, preset: str, new: bool):
        if self.running:
            return
        model = self.model.currentText()
        if not model:
            if not self._checked:
                self._pending = (question, preset, new)       # model list still loading
                self.state.setText("Starting the local model…")
                return
            self.state.setText(f"<span style='color:{C['down']}'>No local model available — see the status above.</span>")
            return
        d = self.data_provider() or {}
        if not d:
            self._pending = (question, preset, new)
            self.state.setText("Loading your trades first…")
            self.data_needed.emit()
            return
        if not d.get("strategies") and not d.get("positions"):
            self.state.setText(f"<span style='color:{C['down']}'>No trades found for this period. Connect Shoonya "
                               "and sync, import a CSV, or pick a longer period on the Trades & P&L tab.</span>")
            return
        if new:
            self.thread_msgs, self.chat = [], []
        if new or self.brief is None:
            self.brief = ai.build_brief(d, self.period_provider())
        self.chat.append(("user", f"**{preset}**" if preset != "Question" else question))
        msgs = ai.messages_for(self.brief, question, self.thread_msgs)
        self.current = {"review_id": uuid.uuid4().hex[:12], "time": datetime.now(), "model": model,
                        "preset": preset, "question": question, "period": self.period_provider()}
        self.text, self.think_chars = "", 0
        self.stop.clear()
        self._busy(True)
        self._render("", "*Reading your trades…*")
        self.state.setText(f"Sending {len(self.brief.get('recent_closed_strategies', []))} closed and "
                           f"{len(self.brief.get('open_positions', []))} open position(s) to {model} (local)…")
        self._t0 = time.time()
        self.repaint_timer.start()
        think = self.think.isChecked()

        def job():
            try:
                res = ai.chat_stream(model, msgs, lambda k, t: self.bridge.chunk.emit(k, t), self.stop, think)
                self.bridge.done.emit(res)
            except Exception as exc:
                self.bridge.failed.emit(str(exc))

        threading.Thread(target=job, daemon=True).start()

    def _on_chunk(self, kind: str, text: str):
        if kind == "think":
            self.think_chars += len(text)
        else:
            self.text += text

    def _clean(self, s: str) -> str:
        return re.sub(r"<think>.*?</think>", "", s, flags=re.S).strip()

    def _repaint(self):
        if self.text:
            self._render(self._clean(self.text))
            self.state.setText(f"Writing… {len(self.text):,} chars")
        elif self.think_chars:
            self.state.setText(f"Thinking… ({self.think_chars:,} chars of reasoning)")
        else:
            self.state.setText(f"Reading your trades… {int(time.time() - self._t0)}s "
                               "(the first answer takes longer while the model loads)")

    def _on_done(self, res: dict):
        self.repaint_timer.stop()
        answer = self._clean(res["content"]) or "*(no answer)*"
        self.text = answer
        self.chat.append(("coach", answer))
        self._render()
        stopped = self.stop.is_set()
        self.state.setText(f"{'Stopped' if stopped else 'Done'} in {res['seconds']:.0f}s · "
                           f"{res['tokens']:,} tokens · {self.current['model']} (local) · saved to ai_reviews")
        self.thread_msgs += [{"role": "user", "content": self.current["question"]},
                             {"role": "assistant", "content": answer}]
        try:
            self.store.upsert_docs("ai_reviews", [{**self.current, "answer": answer, "tokens": res["tokens"],
                                                   "seconds": round(res["seconds"], 1), "stopped": stopped}])
        except Exception as exc:
            self.state.setText(self.state.text() + f" · save failed: {exc}")
        self._busy(False)
        self.load_saved()

    def _on_failed(self, err: str):
        self.repaint_timer.stop()
        if self.chat and self.chat[-1][0] == "user":
            self.chat.pop()
        self._render()
        self._busy(False)
        self.state.setText(f"<span style='color:{C['down']}'>Failed: {err[:300]}</span>")

    def _busy(self, on: bool):
        self.running = on
        has_model = self.model.count() > 0
        for b in self.preset_btns + [self.ask_btn, self.new_btn]:
            b.setEnabled(not on and has_model)
        self.stop_btn.setEnabled(on)

    # ------------------------------------------------------------ saved reviews
    def load_saved(self):
        try:
            docs = self.store.find_docs("ai_reviews")
        except Exception:
            return
        docs.sort(key=lambda d: d.get("time") or datetime.min, reverse=True)
        self.saved.clear()
        for d in docs[:200]:
            t = d.get("time")
            label = f"{t:%d %b %H:%M}" if isinstance(t, datetime) else str(t)[:16]
            it = QListWidgetItem(f"{label} · {d.get('preset')}\n{(d.get('question') or '')[:60]}")
            it.setData(Qt.ItemDataRole.UserRole, d)
            it.setToolTip(f"{d.get('model')} · period {d.get('period')}")
            self.saved.addItem(it)

    def _open_saved(self, it: QListWidgetItem):
        if self.running:
            return
        d = it.data(Qt.ItemDataRole.UserRole)
        self.text = d.get("answer", "")
        q = d.get("question", "") if d.get("preset") == "Question" else f"**{d.get('preset')}**"
        self.chat = [("user", q), ("coach", self.text)]
        self._render()
        self.state.setText(f"Past answer from {d.get('model')} · period {d.get('period')}. Follow-ups continue it "
                           "with today's data.")
        self.thread_msgs = [{"role": "user", "content": d.get("question", "")},
                            {"role": "assistant", "content": self.text}]
        self.brief = None
