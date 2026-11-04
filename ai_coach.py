"""Local-LLM trade review through Ollama (http://127.0.0.1:11434) — nothing leaves the machine.

The Trades & P&L engine already computes the numbers (metrics, breakdowns,
rule-based findings, open-position alerts).  This module condenses them, plus
the recent strategies with their market context at entry, mistakes and journal
notes, into a compact JSON brief and asks a local model to coach on process:
what is costing money, what is working, and concrete rules to adopt.

The model only sees the trader's own data; it is instructed to quote the numbers,
flag thin samples, and not to forecast the market.  Output is an automated
review for self-study, not investment advice.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import date, datetime

import requests

import config
import trades_engine as te

OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA.startswith("http"):
    OLLAMA = "http://" + OLLAMA
DEFAULT_MODEL = os.environ.get("AI_MODEL", "qwen3:14b")
NUM_CTX = 20480          # ~4 GB of KV cache for a 14B model; brief + reasoning fit comfortably
RECENT_STRATEGIES = 30

SYSTEM = """You are a disciplined, numbers-first performance coach for an Indian index-options seller.
The trader sells NIFTY and BANKNIFTY options positionally (multi-day holds, no scalping).
You are reviewing the trader's OWN trade journal, exported as JSON by their analytics app.

Rules:
- Use only the data provided. Quote the actual numbers (₹ amounts, win rates, counts) behind every claim.
- If a pattern rests on fewer than ~5 trades, say the sample is thin and treat it as a hypothesis.
- Focus on process the trader controls: entry selection (environment score, IV vs realised vol, DTE, events,
  trend vs structure), position sizing, defined vs undefined risk, adjustments, exits and discipline.
- The trader's own management plan: book around 50% of credit, exit when the loss reaches 1.5x credit or when
  spot moves through the outermost short strike, avoid holding short options into expiry-day gamma.
  For open positions, compare each against this plan and lay out the options with their trade-offs;
  the decision is the trader's.
- Do not predict market direction, do not promise profits, do not invent data.
- Write in concise markdown: short headings, bullets, a small table where it helps. Amounts in ₹ with Indian
  digit grouping; index levels and strikes are points, not ₹. No preamble.
- End with one italic line: "Automated review of your own data — not investment advice."
"""

PRESETS: dict[str, str] = {
    "Full review": (
        "Give me a complete review of my trading:\n"
        "1. Scorecard: net P&L, win rate, payoff, profit factor, expectancy, drawdown — one line of verdict.\n"
        "2. The 3 biggest leaks, each with the ₹ it cost, the evidence and the fix.\n"
        "3. What is working (keep doing) with evidence.\n"
        "4. Open positions vs my management plan.\n"
        "5. Five concrete, measurable rules for the next 2 weeks."),
    "Why am I losing?": (
        "Find the root causes of my losses. Rank them by ₹ lost. For each: the pattern in my data, why it loses "
        "money for an option seller, and the single rule that would have prevented most of it. Then estimate how "
        "my net P&L would have looked if I had followed those rules (show the arithmetic from the data)."),
    "Open positions": (
        "Review each open position against my management plan: credit, open P&L, % captured, distance of spot "
        "from the outermost short strikes, DTE and alerts. For each, list the realistic choices (hold, book, "
        "exit, roll/adjust) with their risk and trade-offs, and which one my own plan points to."),
    "Last trade post-mortem": (
        "Do a post-mortem of my most recently closed strategy: setup and market context at entry, how it was "
        "managed, what went right or wrong versus my plan, and the lesson to carry forward."),
    "Personal pre-trade checklist": (
        "From my own winning and losing patterns, write a short pre-trade checklist (yes/no questions) I should "
        "run before every new position, and a sizing rule. Each item must cite the data that justifies it."),
}


# ------------------------------------------------------------------ server
def server_up(timeout: float = 1.5) -> bool:
    try:
        return requests.get(f"{OLLAMA}/api/tags", timeout=timeout).ok
    except requests.RequestException:
        return False


def ensure_server(wait: float = 8.0) -> bool:
    """Start `ollama serve` in the background if it is installed but not running."""
    if server_up():
        return True
    exe = shutil.which("ollama") or next((p for p in ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama")
                                          if os.path.exists(p)), None)
    if not exe:
        return False
    subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    end = time.time() + wait
    while time.time() < end:
        if server_up():
            return True
        time.sleep(0.4)
    return False


def models() -> list[str]:
    r = requests.get(f"{OLLAMA}/api/tags", timeout=5)
    r.raise_for_status()
    return sorted(m["name"] for m in r.json().get("models", []))


# ------------------------------------------------------------------ brief
def _r(v, nd=0):
    if v is None:
        return None
    if isinstance(v, float):
        return round(v, nd) if nd else round(v)
    return v


def _t(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d %H:%M") if isinstance(v, datetime) else v.isoformat()
    return str(v)


CTX_KEYS = ("verdict", "score", "vix", "atm_iv", "hv20", "vrp", "trend", "bias", "sigma_move", "events", "source")


def _ctx(c: dict) -> dict:
    out = {}
    for k in CTX_KEYS:
        v = c.get(k)
        if v not in (None, "", [], {}):
            out[k] = _r(v, 2) if isinstance(v, float) else v
    return out


def _strategy(s: dict, journal: dict | None) -> dict:
    flags = [label for _fid, label in te._flags(s)]
    out = {
        "id": s["id"], "und": s["und"], "structure": s["structure"], "defined_risk": s["defined_risk"],
        "status": s["status"], "opened": _t(s["entry_time"]), "closed": _t(s["exit_time"]),
        "expiry": _t(s["expiry"]), "dte_at_entry": s["dte_entry"], "lots": _r(s["lots"], 1),
        "credit": _r(s["credit"]), "net_pnl": _r(s["net"]), "charges": _r(s["charges"]),
        "pct_credit_kept": _r(s["captured"] * 100) if s["captured"] is not None else None,
        "loss_x_credit": _r(s["loss_x_credit"], 2) or None, "held_to_expiry": s["held_to_expiry"],
        "days_held": _r(s["holding_days"], 1),
        "legs": [f"{l['side']} {l['qty']} {l['kind']} {format(l['strike'], 'g') if l['strike'] is not None else ''} @ {_r(l['entry_px'], 2)}"
                 + (f" -> {_r(l['exit_px'], 2)} ({l['exit_reason']})" if l.get("exit_px") is not None else "")
                 for l in s["legs"]],
        "market_at_entry": _ctx(s.get("ctx") or {}), "mistakes": flags,
    }
    if journal:
        out["journal"] = {k: journal[k] for k in ("tags", "rating", "notes") if journal.get(k)}
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def build_brief(d: dict, period: str = "") -> dict:
    """Compact JSON-able summary of everything the Trades tab computed."""
    m = dict(d.get("metrics") or {})
    for k in ("best", "worst"):
        if m.get(k):
            m[k] = {"id": m[k]["id"], "net": _r(m[k]["net"])}
    m = {k: (_r(v, 2) if isinstance(v, float) else v) for k, v in m.items()}
    journal = d.get("journal") or {}
    strats = d.get("strategies") or []
    closed = sorted([s for s in strats if s["status"] == "closed"], key=lambda s: s["exit_time"], reverse=True)
    findings = [{"issue": f["title"], "trades": f["trades"], "share_pct": _r(f["share"]), "net": _r(f["net"]),
                 "lost": _r(f["lost"]), "win_rate": _r(f["win_rate"]),
                 "win_rate_without": _r(f["win_rate_others"]), "avg": _r(f["avg"]),
                 "avg_without": _r(f["avg_others"]), "why": f["why"], "suggested_fix": f["fix"]}
                for f in (d.get("findings") or [])]
    positions = [{"und": g["und"], "expiry": _t(g["expiry"]), "dte": g["dte"], "structure": g["structure"],
                  "defined_risk": g["defined_risk"], "spot": _r(g.get("spot"), 1), "credit": _r(g["credit"]),
                  "open_pnl": _r(g["open_pnl"]), "realised_today": _r(g["realised"]),
                  "pct_credit_captured": _r(g["captured"] * 100) if g["captured"] is not None else None,
                  "legs": [f"{l['tsym']} qty {int(l['qty']):+d} avg {_r(l['avg'], 2)} ltp {_r(l['lp'], 2)}"
                           for l in g["legs"] if l.get("qty")],
                  "alerts": [f"{kind.upper()}: {msg}" for kind, msg in g["alerts"]]}
                 for g in (d.get("positions") or [])]
    lim = d.get("limits") or {}
    return {
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"), "period": period,
        "trader": {"style": "positional NIFTY/BANKNIFTY option seller, no scalping",
                   "lot_sizes": {"NIFTY": config.NIFTY_LOT, "BANKNIFTY": config.BANKNIFTY_LOT},
                   "capital": _r(config.TRADING_CAPITAL or lim.get("available_total")),
                   "margin_used": _r(lim.get("margin_used"))},
        "metrics": m,
        "breakdowns": {k: [{"bucket": r["bucket"], "trades": r["trades"], "win_rate": _r(r["win_rate"]),
                            "net": _r(r["net"]), "avg": _r(r["avg"])} for r in rows]
                       for k, rows in (d.get("breakdowns") or {}).items()},
        "rule_based_findings": findings,
        "open_positions": positions,
        "recent_closed_strategies": [_strategy(s, journal.get(s["id"])) for s in closed[:RECENT_STRATEGIES]],
        "open_strategies": [_strategy(s, journal.get(s["id"])) for s in strats if s["status"] == "open"],
    }


def messages_for(brief: dict, question: str, history: list[dict] | None = None) -> list[dict]:
    data = json.dumps(brief, ensure_ascii=False, default=str, separators=(",", ":"))
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"My trading data (JSON):\n```json\n{data}\n```"},
            {"role": "assistant", "content": "Got it — I have your data. What would you like reviewed?"}]
    msgs += history or []
    msgs.append({"role": "user", "content": question})
    return msgs


# ------------------------------------------------------------------ chat
def chat_stream(model: str, msgs: list[dict], on_chunk, stop=None, think: bool = True) -> dict:
    """Stream a chat completion. on_chunk(kind, text) with kind 'think' or 'text'.
    Returns {"content", "thinking", "tokens", "seconds"}."""
    body = {"model": model, "messages": msgs, "stream": True, "think": think,
            "options": {"num_ctx": NUM_CTX, "temperature": 0.3}}
    t0, content, thinking, tokens = time.time(), [], [], 0
    with requests.post(f"{OLLAMA}/api/chat", json=body, stream=True, timeout=(5, 600)) as r:
        if r.status_code == 400 and think:          # model without a thinking mode
            return chat_stream(model, msgs, on_chunk, stop, think=False)
        if not r.ok:
            raise RuntimeError(f"Ollama {r.status_code}: {r.text[:200]}")
        for line in r.iter_lines():
            if stop is not None and stop.is_set():
                break
            if not line:
                continue
            j = json.loads(line)
            if j.get("error"):
                raise RuntimeError(f"Ollama: {j['error']}")
            msg = j.get("message") or {}
            if msg.get("thinking"):
                thinking.append(msg["thinking"])
                on_chunk("think", msg["thinking"])
            if msg.get("content"):
                content.append(msg["content"])
                on_chunk("text", msg["content"])
            if j.get("done"):
                tokens = j.get("eval_count") or 0
                break
    return {"content": "".join(content), "thinking": "".join(thinking), "tokens": tokens,
            "seconds": time.time() - t0}
