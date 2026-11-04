# NSE Quant Terminal — Index & ETF Swing Desk

A desktop analytics terminal (PySide6 + MongoDB + Plotly) that ingests NSE data,
computes monthly support/resistance zones, correlates FII/DII institutional
positioning with price, and generates "Buy on Dips, Sell on Rallies" swing
signals. It covers **34 NSE indices (NIFTY 50, 17 broad-market and 16 sectoral indices)**,
plus the ETFs that track them.

## Instrument universe

| Group | Index | Tracking ETF (trade proxy) | Intraday (1H/15m) |
|---|---|---|---|
| Large cap | NIFTY 50 | NIFTYBEES | ✓ |
| Large cap | NIFTY Next 50 | JUNIORBEES | ✓ |
| Large cap | NIFTY 100 | NIF100BEES | ✓ |
| Large cap | NIFTY 200 | — | ✓ |
| Broad market | NIFTY 500 | MONIFTY500 | ✓ |
| Broad market | NIFTY Total Market | — | ✓ |
| Broad market | NIFTY500 Multicap 50:25:25 | MULTICAP | ✓ |
| Broad market | NIFTY LargeMidcap 250 | — | ✓ |
| Broad market | NIFTY MidSmallcap 400 | — | ✓ |
| Mid cap | NIFTY Midcap 50 | — | ✓ |
| Mid cap | NIFTY Midcap 100 | MOM100 | ✓ |
| Mid cap | NIFTY Midcap 150 | MID150BEES | ✓ |
| Mid cap | NIFTY Midcap Select | MIDSELIETF | daily only |
| Small cap | NIFTY Smallcap 50 | — | ✓ |
| Small cap | NIFTY Smallcap 100 | — | ✓ |
| Small cap | NIFTY Smallcap 250 | HDFCSML250 | ✓ |
| Small cap | NIFTY Smallcap 500 | — | daily only |
| Small cap | NIFTY Microcap 250 | — | ✓ |
| Sectoral | NIFTY Bank | BANKBEES | ✓ |
| Sectoral | NIFTY Private Bank | PVTBANIETF | ✓ |
| Sectoral | NIFTY PSU Bank | PSUBNKBEES | ✓ |
| Sectoral | NIFTY Financial Services (FINNIFTY) | BFSI | ✓ |
| Sectoral | NIFTY Financial Services 25/50 | — | daily only |
| Sectoral | NIFTY Auto | AUTOBEES | ✓ |
| Sectoral | NIFTY IT | ITBEES | ✓ |
| Sectoral | NIFTY FMCG | FMCGIETF | ✓ |
| Sectoral | NIFTY Metal | METALIETF | ✓ |
| Sectoral | NIFTY Pharma | PHARMABEES | ✓ |
| Sectoral | NIFTY Healthcare | HEALTHY | ✓ |
| Sectoral | NIFTY Realty | MOREALTY | ✓ |
| Sectoral | NIFTY Media | — | ✓ |
| Sectoral | NIFTY Oil & Gas | OILIETF | ✓ |
| Sectoral | NIFTY Consumer Durables | — | ✓ |
| Sectoral | NIFTY Chemicals | GROWWCHEM | ✓ |

* **Where the data comes from:** daily OHLCV for every index except NIFTY 50 comes from NSE's official EOD archive (`ind_close_all_DDMMYYYY.csv`). One file per day covers all the indices, so the first sync back-fills about 2 years in roughly 30 seconds. Yahoo's daily history for most of these indices is missing or cut short. Intraday bars and all ETF prices come from Yahoo.
* **Today's bar:** NSE publishes the archive after the close. Until then, today's daily bar is built from the stored 1H candles. The two daily-only indices show the previous close until the evening sync.
* **ETF mapping:** I checked each ETF's fund name on Yahoo before mapping it. Indices without a liquid tracking ETF are analysed as index-only, and the card shows "No tracking ETF mapped". To add one, set `etf=` in `config._INDICES` and add the ETF's name to `_ETF_NAMES`.
* **FII/DII data is market-wide** and comes mostly from NIFTY index futures and options. It is a useful sentiment backdrop for mid and small caps, but it says less about them than about NIFTY 50.

> This is an analytics tool for education and research. It is not investment advice.
> Signals come from rules and can be wrong. Size positions and manage risk yourself.

---

## 1. Architecture

```
                 ┌──────────────────────── External sources ─────────────────────────┐
                 │ Yahoo Finance (yfinance)            NSE (nseindia / nsearchives)  │
                 │  ^NSEI, NIFTYBEES.NS 1D/1H/15m       fiidiiTradeReact  (cash)     │
                 │  Brent, USD/INR, US10Y, VIX, DXY     fao_participant_oi_*.csv     │
                 └───────────────┬──────────────────────────────┬────────────────────┘
                                 │                              │
                        ┌────────▼──────────────────────────────▼────────┐
                        │ data_fetcher.py                                 │
                        │  fetch_ohlcv · fetch_macro · NSEClient          │
                        │  (session warm-up, retries, CSV parsing)        │
                        │  DataService.sync()  ← "Sync Latest EOD Data"   │
                        └────────┬───────────────────────────────▲────────┘
                       upserts   │                               │ loads
                        ┌────────▼───────────────────────────────┴────────┐
                        │ db_handler.py  →  MongoDB `nse_analytics`       │
                        │  index_ohlcv · fii_dii_activity · sr_levels     │
                        │  macro_series   (validators + unique indexes)   │
                        │  MemoryStore fallback when Mongo is down        │
                        └────────┬────────────────────────────────────────┘
                                 │ DataFrames
                        ┌────────▼────────────────────────────────────────┐
                        │ indicators.py  (pure pandas/numpy, no I/O)      │
                        │  EMA/RSI/ATR/ADX → Pivots (Std/Fib/Cam, M & W)  │
                        │  Volume Profile (POC/VAH/VAL/HVN) → Swings      │
                        │  → Zone clustering + touch counts               │
                        │  Institutional thesis · Catalyst attribution    │
                        │  Projection (±1σ snapped to zones) · Signal     │
                        └────────┬────────────────────────────────────────┘
                                 │ result dict  (+ saved to sr_levels)
                        ┌────────▼────────────────────────────────────────┐
                        │ app.py  (PySide6, QThreadPool workers)          │
                        │  Sidebar │ Plotly chart in QWebEngineView │ Cards│
                        └─────────────────────────────────────────────────┘
```

| File | Responsibility |
|---|---|
| `config.py` | All tunables (Mongo URI, symbols, thresholds); each can be overridden by an environment variable |
| `db_handler.py` | MongoDB connection, JSON-schema validators, compound/unique indexes, idempotent bulk upserts |
| `data_fetcher.py` | yfinance OHLCV + macro, NSE FII/DII cash, NiftyTrader/Moneycontrol cash history, participant-wise OI parser, CSV back-fill, `DataService` facade |
| `indicators.py` | Full quant engine; `python indicators.py NIFTYBEES.NS 1D` prints the analysis as JSON |
| `app.py` | Desktop GUI: live tab, chart tab, scanner tab, decision panels |
| `live_view.py` | Live one-screen dashboard (tiles, range bars, auto-refresh) |

### MongoDB collections

| Collection | Key / index | Main fields |
|---|---|---|
| `index_ohlcv` | unique `(symbol, timeframe, timestamp)` + `(symbol, timestamp desc)` | open, high, low, close, volume |
| `fii_dii_activity` | unique `date` | fii_cash_net, dii_cash_net, fii_index_futures_long_short_ratio, client_ratio, pcr, fii_index_fut_net_oi, … |
| `sr_levels` | unique `(symbol, timeframe, calculated_date)` | support_zones[], resistance_zones[], poc_level, vah, val, pivots, projection, signal |
| `macro_series` | unique `(series, date)` | close |
| `broker_fills`, `broker_orders`, `pnl_daily`, `trade_journal` | unique `fill_id` / `(order_no, date)` / `date` / `strategy_id` | Trades & P&L tab data |
| `ai_reviews` | unique `review_id` | time, model, preset, question, answer, tokens |

Every write is an `UpdateOne(..., upsert=True)` keyed on the natural key, so running a sync again never duplicates data. FII cash and participant-OI records for the same date are merged with `$set`.

---

## 2. Installation

### Prerequisites
* Python 3.11+ (tested on 3.14)
* MongoDB Community 7+ running locally

### MongoDB
```bash
brew tap mongodb/brew
```
```bash
brew install mongodb-community
```
```bash
brew services start mongodb-community
```
On Linux: install `mongodb-org` from the MongoDB repo, then run `sudo systemctl enable --now mongod`.
On Windows: use the MSI installer and run it as a service.
The app creates the `nse_analytics` database, its collections and its indexes on first run.
To use a different server, set `MONGO_URI` (for example `export MONGO_URI=mongodb://user:pass@host:27017`).

### Python environment
```bash
python3 -m venv .venv
```
```bash
source .venv/bin/activate
```
```bash
pip install -r requirements.txt
```

### First run
```bash
python data_fetcher.py
```
This is the initial sync. It fetches about 2 years of daily bars (from the NSE archive for the 33 archive indices, from Yahoo for NIFTY 50 and the ETFs), 180 days of 1H bars and 60 days of 15m bars for all 56 instruments, the last ~90 sessions of FII/DII cash figures, about 90 sessions (135 days) of participant OI, and the macro series. It takes about 1–2 minutes.
```bash
python app.py
```
After that, click **Sync Latest EOD Data** once a day, after about 6 pm IST, when NSE has published the provisional FII/DII figures and the participant OI file.

Other useful commands:
```bash
python db_handler.py
```
Shows collection counts and indexes.
```bash
python indicators.py ^NSEI 1D
```
Prints the full analysis as JSON (no GUI).

### Data notes
* **FII/DII cash history:** NSE's API only returns the **latest** session, so every sync also reads the last ~90 sessions of net figures from NiftyTrader's FII/DII page, plus buy/sell/net for the last ~30 sessions from Moneycontrol. Both carry the same NSE provisional figures. Older days then build up in MongoDB with each sync. Days older than ~30 sessions have net values only, without buy/sell. Skip a sync for more than ~4 months and you get a gap. To back-fill further, use the **Import FII/DII history (CSV)…** button. Any CSV with a date column and columns containing `FII…Net` and `DII…Net` works (for example a Moneycontrol or NSDL export).
* **Participant OI** (FII index-futures long/short, client long/short, index PCR) comes from NSE's daily archive CSVs, so this history is back-filled automatically.
* If MongoDB is unreachable, the app still runs on an in-memory store (shown in red in the sidebar), but nothing is saved between runs.
* Event dates in the catalyst module are partly approximate (RBI MPC uses typical windows). The FOMC dates are listed in `indicators._FOMC`, so extend that list each year.

---

## 3. How the engine works

### Support & resistance zones
Every candidate level gets a weight:

| Source | Weight |
|---|---|
| Volume-profile POC | 2.0 |
| VAH / VAL | 1.5 |
| HVN | 0.8 |
| Monthly Standard pivots | 1.5 |
| Monthly Fibonacci pivots | 1.0 |
| Monthly Camarilla pivots | 0.8 |
| Weekly pivots | half of the monthly weight |
| 200 EMA | 1.5 |
| 50 EMA | 1.2 |
| 20 EMA | 0.7 |
| Fractal swing high/low | 1.0 (0.6 if older than the 60-day window) |

Candidates within `±0.35 × ATR` of each other are clustered into a zone. Overlapping zones are merged or split, and **touch counts** are added: a touch is a distinct visit where price later left the zone again. The final zone strength is the sum of the weights plus 0.5 × touches. Zones below the last price are support (green) and zones above it are resistance (red). Stronger zones are drawn more opaque.

### Pivots
Monthly and weekly Standard, Fibonacci and Camarilla pivots are computed from the **last completed** month or week. In early November, the monthly pivots are built from October's high, low and close, which makes them the reference levels for November.

### Next-month projected range
The range starts at ±1σ of monthly volatility: `close × exp(±σ_daily × √21)`, using the last 60 sessions. Each end then snaps to the strongest confluence zone within 1.2% of that ±1σ price, weighted by distance. The card shows what each end snapped to.

### Institutional thesis
| Condition | Thesis |
|---|---|
| 5-day price ↑, and FII cash > 0 or FII long/short ratio rising | **Institution-driven up-move** |
| 5-day price ↑, FII index futures heavily short or the ratio falling, client long/short > 1.2 | **RETAIL TRAP divergence** |
| 5-day price ↓, FII selling with DIIs absorbing ≥ 80% of it | Base building |
| 5-day price ↓, FII selling | Institutional distribution |

"Heavily short" is judged both in absolute terms (long/short < 0.6 and falling) and against the ratio's own 60-day percentile (≤ 20th). The second test matters because FIIs have stayed structurally net short in index futures for long stretches.
The PCR (index options put OI / call OI) and the client long/short ratio are read as contrarian gauges.

### Signal engine (four checks per side)

| # | Buy / Accumulate | Sell / Book profit |
|---|---|---|
| 1 Location | Price inside or within 0.6 ATR of a support zone, VAL, 50 EMA, or monthly S1/S2 | Within 0.6 ATR of a resistance zone, VAH, or monthly R1/R2 |
| 2 Momentum | Bullish RSI divergence, or RSI ≤ 40 | RSI > 70, or bearish divergence |
| 3 Flows | DII or FII 5-day net > 0, or positive institutional bias | FII heavily short in index futures, or long/short ratio falling sharply |
| 4 Structure | Above 200 EMA and not in a bearish regime | Price more than 2 ATR above the 20 EMA |

| Recommendation | Rule |
|---|---|
| **ACCUMULATE ZONE** | Buy ≥ 3 and the location check passes, and the market is not in a Bearish regime below the 200 EMA |
| WATCH — COUNTER-TREND BASE | The same buy setup, but in a Bearish regime below the 200 EMA (buy-on-dips only works with the primary trend) |
| **PROFIT BOOKING ZONE** | Sell ≥ 3 and the location check passes |
| WATCH — NEAR ACCUMULATION / TIGHTEN STOPS | Score of 2 |
| HOLD — MIXED SIGNALS | Both sides ≥ 3 |
| AVOID — WAIT FOR BASE | Bearish regime below the 200 EMA |
| HOLD | Anything else |

The trade plan uses the active support zone (or the nearest one below price) as the entry. The stop loss sits 0.5 ATR below the zone. Targets are the next resistance zones that are at least 1 ATR away.

---

## 4. Walkthrough — reading the screen and running the NIFTYBEES swing

### Reading the chart
1. **Green and red bands** are demand and supply zones. The label `S 263.70 · 4t` means a support zone centred at 263.70 that price has visited 4 times. More opaque bands are stronger.
2. **Dashed cyan lines (Proj low / Proj high)** mark the projected trading range for the month. Think of them as the outer bands of the month: you accumulate near the lower one and book profits near the upper one.
3. **Dotted grey lines (M-S2 … M-R2)** are the monthly Standard pivots. Price above M-P means the month has a bullish tilt.
4. **Volume profile (right strip):** the blue bins form the value area. The pink **POC** line is the price with the most volume, and price tends to be drawn back to it. Above **VAH** means the market is accepting higher prices; below **VAL** means weakness.
5. **RSI pane:** dotted lines at 70 and 30. Look for divergences at support zones.
6. **Flow pane:** blue bars are FII cash, orange bars are DII cash, and the magenta line is the FII index-futures long/short ratio (right axis). A rising magenta line along with rising price confirms the move. Price rising while the line falls is the retail-trap warning.

### Live tab (one screen, auto-refreshing)
The app opens on **● Live — all indices**: one tile for each of the 34 indices. The sidebar is hidden on this tab so the grid uses the full window, and tiles widen to fill each row. At 1900×1000 all 34 fit without scrolling (9 per row); smaller windows scroll. The header also has a **Sync EOD** button, which refreshes the charts and levels when it finishes. Every 15 seconds (5–60 s is selectable; you can also Pause or click Refresh now) it pulls live quotes from NSE in two requests: `/api/allIndices` for the indices and `/api/etf` for the ETFs. It polls only while this tab is showing.

**Header strip:** market open/closed, NSE timestamp, NIFTY and BANKNIFTY, India VIX, NIFTY 500 advances/declines, and the latest FII/DII cash figures.

**Each tile:**
* **A 30-day chart.** This shows the last 30 daily candles, and today's candle updates live from the quote. The green and red bands are the index's two strongest support and two strongest resistance zones, the dotted line is the live price, and the high and low of the 30 days are labelled on the right. The % change over those 30 days is shown next to the price.
* **Name, live price and day % change.** The background tint grows stronger with the size of the day's move (green or red), and the coloured left stripe shows the group.
* **Range bar.** The bar spans the projected monthly range. The green third is the dip/accumulate band and the red third is the rally/profit band. Grey shows today's high–low and the white tick is the live price (orange when it is outside the range).
* **Live tag**, in priority order:
  1. BELOW / ABOVE MONTHLY RANGE
  2. AT KEY SUPPORT / AT KEY RESISTANCE (inside one of the 2 strongest zones on that side)
  3. DIP BAND (lower third of the range) / RALLY BAND (upper third)
  4. MID-RANGE

  Next to it is the last end-of-day signal (`EOD …`).
* **Nearest support and resistance** below and above the live price, with the % distance to each.
* **Tracking ETF** live price and % change, when one is mapped.
* **Hover** for open/high/low/previous close, the 52-week range, and constituent advances/declines. **Click** a tile to open its chart.

The zones, ranges and EOD signals come from the latest daily analysis stored in `sr_levels`. They are computed automatically for any index that doesn't have them yet. Use **Recompute levels** after the evening sync to refresh them all.

Reading it during the session: a tile that reads **AT KEY SUPPORT** or **DIP BAND**, with an EOD signal of ACCUM or WATCH, is a dip-buy candidate, so open the chart and time the entry on 1H. **AT KEY RESISTANCE** or **RALLY BAND** with RSI stretched is where you book profits. **BELOW MONTHLY RANGE** means the month's support has broken, so wait for a base before buying.

### Realtime chart tab (NIFTY / BANKNIFTY)
A TradingView-style intraday chart for NIFTY 50 and BANKNIFTY, built on TradingView's open-source [Lightweight Charts](https://github.com/tradingview/lightweight-charts) v5 (Apache-2.0, bundled in `vendor/`, so it also works offline). The page is `chart_web/chart.html`; `rt_chart.py` feeds it.
* **Data:**
  * **With Shoonya connected:** 1-minute candles come from Shoonya's `TPSeries` (real-time, about 9 days back). The forming candle updates every second from the websocket ticks, and history is re-pulled every 2 minutes.
  * **Without Shoonya:** Yahoo 1-minute bars (delayed) refresh every 30 seconds.
  * **1D:** uses the stored daily bars, with today's candle built live.
  * The header shows real-time or delayed, and a countdown to the candle close.
* **Timeframes:** 1, 3, 5, 10, 15 and 30 min, 1H (anchored at 09:15) and 1D. Keys 1–8 switch between them. You can show 1, 2 or 5 days, or everything loaded.
* **Chart types:** candles, Heikin Ashi, OHLC bars, line, area.
* **Indicators:**
  * Overlays (lengths editable): EMA ×2, SMA, Bollinger Bands, Supertrend, and a session VWAP. Index candles carry no volume, so the VWAP is equal-weighted.
  * Separate, resizable panes: RSI with 70/50/30 lines, MACD 12/26/9 with histogram, and ATR.
* **Levels:**
  * Previous day high, low and close; CPR (pivot, BC, TC); classic pivots R1–R3 / S1–S3; Camarilla H3/H4/L3/L4.
  * Previous week high and low; today's high and low; the opening range (5, 15 or 30 min).
  * From the app:
    * daily support and resistance zones from `sr_levels`;
    * option OI walls (top call and put strikes) and max pain from the Options analysis, refreshed every 15 minutes;
    * your open option strikes from the Trades tab, e.g. "Short 23,700 CE ×2".
* **Tools:**
  * **Drawing:** horizontal line (H), trend line (T), measure (M: points, %, bars and time between two clicks), undo (Ctrl+Z) and clear.
  * **Price alerts:** press A and click a price, or type one in the Alerts menu. An alert fires once when the live price crosses it, with an in-chart banner, a sound, a status-bar message and a dock bounce. Alerts keep working while you're on another tab, as long as Shoonya is connected.
* **Layout:**
  * Side by side, stacked, or a single index.
  * **Sync** links the crosshair and scrolling of the two charts. **Compare** overlays the other index's % move on a single chart.
  * **Fit**, **Latest**, and 📷 to save a PNG. The legend shows OHLC, change and every indicator value at the crosshair.
* **Saved state:** settings, drawings and alerts are kept in `~/.nse_analytics/chart_state.json`.

### Scanner tab
**Scanner — all indices → Scan** runs the daily analysis for all 56 instruments (34 indices and 22 ETFs) and saves each result to `sr_levels`. The table shows:

* last price, 1-day and 20-day % change
* regime and signal
* buy and sell scores, and RSI
* the projected range
* the nearest support and resistance, with the % distance to each
* the institutional thesis

Click a column header to sort. For example, sort by *To S %* to find the indices sitting closest to support. Double-click a row to open that instrument's chart.

A practical routine:
1. Run the scan.
2. Shortlist the indices showing ACCUMULATE, or WATCH with *To S %* close to 0, in a Range-bound or Bullish regime.
3. Open each one on 1D, then trade its tracking ETF using the "ETF / index equivalent" row.

**Comparing across indices:** on breadth, if large caps are Range-bound while mid and small caps are Bearish, the large-cap ETFs (NIFTYBEES, JUNIORBEES, NIF100BEES, BANKBEES) are the safer dip-buys. Only rotate into mid or small caps once their regime flips out of Bearish, or they reclaim the 200 EMA.

### Real-time data from Shoonya (optional, read-only)

Without a broker connection, live prices come from NSE's website snapshots, which run about 1–2 minutes behind for the option chain. With a Shoonya (Finvasia) account, the app streams real-time ticks instead.

**One-time setup:** on **trade.shoonya.com → Profile → API Key**, note your **Client ID** and **Secret code**, and set the redirect URL to `http://127.0.0.1:8765/shoonya/callback`.

**Credentials in `.env` (optional):** copy `.env.example` to `.env` and fill in `SHOONYA_USER_ID`, `SHOONYA_CLIENT_ID`, `SHOONYA_SECRET` and, optionally, `SHOONYA_TOTP_KEY`. `.env` is git-ignored, and `config.py` loads it at startup.
* With these set, **Connect** skips the details dialog and goes straight to Shoonya's sign-in page.
* The waiting window shows the current 6-digit TOTP, with a Copy button, for you to paste into Shoonya's page.
* Values in `.env` are never copied into `~/.nse_analytics/shoonya.json`.

**To connect:**
1. Click **Connect Shoonya (real-time)**. It's in the Live and Options tab headers and in the status bar.
2. Enter your user ID, client ID and secret, then click **Sign in with Shoonya**.
3. Shoonya's own login page opens in your browser. Sign in there with your password and TOTP; the app never sees them.
4. Shoonya redirects back to `127.0.0.1:8765`, where the app catches the one-time code and exchanges it for the day's access token.
5. If the browser shows an error page instead, paste its address into the waiting dialog.

With "Remember" ticked, the user ID, client ID, secret and the day's token are saved to `~/.nse_analytics/shoonya.json`. That file is owner-only and lives outside the repo. Reconnecting the same day skips the browser step.

What you get once connected:
* **Live tab:** NIFTY 50, NIFTY Bank, India VIX and the tracking ETFs update every second. The other indices stay on NSE snapshots.
* **Options tab:** the analysis runs on Shoonya's live option chain (±40 strikes around ATM) every 5 seconds, labelled **● Shoonya real-time**. PCR covers those strikes only.
* **Fallback:** if Shoonya drops or a lookup fails, the app falls back to NSE automatically and says why.

**About the API:** Shoonya retired its old QuickAuth login (`/NorenWClientTP/`, now 502) in April 2026 and hasn't published updated docs or an updated library. The client in `shoonya_feed.py` follows OpenAlgo's maintained Shoonya integration:
* **Token exchange:** `GenAcsTok`, with checksum `SHA-256(client_id + secret + code)`.
* **REST:** `/NorenWClientAPI/` with a `Bearer` token, falling back to `jKey` for routes that need it.
* **Websocket:** `wss://api.shoonya.com/NorenWSAPI/`.

**No orders:** the client is read-only. It never calls an order route.

### Trades & P&L tab (positions, orders, journal, "why I lost")

The tab reads your Shoonya account through read-only calls (PositionBook, OrderBook, TradeBook, Limits). While connected, it syncs every 30 seconds on this tab and about every 2 minutes elsewhere. Everything is stored in MongoDB, so the history builds up for later analysis.

* **Open positions:** live legs grouped by underlying and expiry, with leg and strategy P&L, matching Shoonya's MTM. The management-plan checks flag:
  * **BOOK** at ~50% of the credit captured.
  * **EXIT** at a loss of 1.5× credit, or when spot moves through the outermost short strike.
  * **WATCH** within 0.25σ of that strike, or one day before expiry.
* **Orders (today) and Fills:** Shoonya's order book (with rejection reasons) and every stored fill.
* **Market context on each fill:** every new fill is stamped with the Options-tab market read at that moment (IV vs realised vol, environment score, VIX, trend/ADX, OI walls, events). Imported fills get a context reconstructed from daily data instead, which has no option IV.
* **Trade history:**
  * **Grouping:** legs of the same underlying and expiry opened on the same day form one strategy. The structure is named (iron condor, iron butterfly, strangle, credit spread…), with "+ adjustments" when there are extra legs.
  * **Matching:** fills are FIFO-matched into round trips. Options held to expiry are settled at intrinsic value from the index close.
  * **Net P&L:** shown after estimated statutory charges, which are set in `config.CHARGES`.
  * **Details:** double-click a strategy for its legs, the market at entry, what went against the plan, and your journal entry.
* **Performance:** equity curve, daily P&L, win rate, payoff ratio, break-even win rate, expectancy, profit factor, drawdown, losing streak. P&L is broken down by structure, underlying, DTE at entry, selling environment at entry, IV vs realised vol, risk type, held-to-expiry, weekday and entry time.
* **Why I lost:** rule-based findings ranked by rupees lost, each with a comparison to your other trades and a fix. The rules cover:
  * selling in an unfavourable environment or into cheap IV;
  * strikes inside 0.75σ, entries under 5 DTE, and entries in the first 15 minutes;
  * events, neutral structures in trends, and spreads against the directional read;
  * losses past 2× credit, undefined risk, and holding to expiry;
  * revenge trades, overtrading, and single losses above 2% of capital;
  * small wins with big losses, winners booked too early, and charges eating profits.
* **Journal:** tags (no stop, FOMO, revenge, oversized…), a rating and notes per strategy, plus P&L by tag.
* **Carried-forward positions:** Shoonya only lists today's trades. A position carried from an earlier session is recorded once at Shoonya's average price, marked "carry-forward" because its real entry date is unknown, so it reaches the history when you close it. Use **Import CSV…** for older trades from a broker export (date, symbol, buy/sell, qty, price).
* **Settings:** `TRADING_CAPITAL` (0 = use Shoonya's cash limit) and `TRADES_SYNC_SECONDS` are in `config.py`.
* **🤖 AI coach tab (local LLM chat):** a top-level tab where you chat with a local model about your trades. It sends the numbers this tab computed (metrics, breakdowns, the *Why I lost* findings, open positions with alerts, and your last 30 strategies with market context at entry, mistakes and journal notes) to a model running locally in [Ollama](https://ollama.com). Nothing leaves your machine. One-click reviews: *Full review*, *Why am I losing?*, *Open positions*, *Last trade post-mortem* and *Personal pre-trade checklist*. You can also type your own questions; follow-ups continue the conversation, and **New chat** starts fresh. Your trades load automatically when you open the tab; connect Shoonya so the coach sees live P&L. Answers stream in as markdown and are saved to `ai_reviews`, listed on the left as *Past answers*; you can open one and carry on the conversation. The model is told to quote your numbers, flag thin samples, check open positions against your management plan, and not forecast the market. It is an automated review for self-study, not investment advice.
  * **Setup:** `brew install ollama` (or the app from ollama.com), then `ollama pull qwen3:14b`. The app starts `ollama serve` itself if it isn't running.
  * **Model choice:** `qwen3:14b` (~9.3 GB) is the default. It reasons well over numbers and fits in 24 GB of memory alongside the app. `gpt-oss:20b` (~14 GB) is a slightly stronger, slower alternative; `qwen3:8b` (~5 GB) suits 16 GB machines. Finance-specific fine-tunes are generally weaker than these general reasoning models at this kind of analysis. Set `AI_MODEL` / `OLLAMA_HOST` in `.env` to change the default or the server. *Deep thinking* lets the model reason before answering: slower, but more careful.

### Option chain tab (NIFTY / BANKNIFTY)
Pick NIFTY or BANKNIFTY and an expiry. With Shoonya connected the chain is real-time and refreshes every 5 seconds; otherwise it uses NSE's snapshot. The tab has three sub-tabs.
* **Option chain:** the standard mirrored layout: calls on the left, the strike with its PCR in the middle, puts on the right.
  * **Columns:** OI with a bar, change in OI with a bar, LTP with % change, IV, delta, volume and build-up. Gamma/theta/vega and bid/ask can be switched on; wide layouts scroll sideways.
  * **Highlighting:** in-the-money cells are tinted, the ATM row is blue, a SPOT divider row (with the ±1σ move to expiry) sits between the strikes either side of spot, and the top-3 OI strikes on each side are bold.
  * **Range:** ±10, ±15, ±20 or ±30 strikes, or all. **Centre on ATM** scrolls back.
  * **Hover** any cell for the full contract: bid/ask, OI change %, every Greek, intrinsic and time value, and build-up.
  * **Build-up:** price↑ OI↑ = long build-up, price↓ OI↑ = short build-up (writers adding), price↑ OI↓ = short covering, price↓ OI↓ = long unwinding.
  * **Summary strip:**
    * spot, ATM, ATM IV, ATM straddle (points and ₹ per lot) and the ±1σ expected range;
    * PCR on OI and on change in OI, max pain, and the OI support (largest put OI) and resistance (largest call OI);
    * total call/put OI and change, VIX, days left and lot size.
  * **One-line read:** the OI range, where fresh writing is happening today, the PCR mood, and the put–call skew.
* **OI · change in OI · IV smile:** strike charts with spot, max pain and the ±1σ band.
* **Selling analysis & strategies:** the existing option-selling screen, below.

### Options tab (NIFTY / BANKNIFTY option selling)

Reads NSE's live option chain (`api/option-chain-v3`) and screens **positional** premium-selling structures: an iron condor, a short strangle, and a bull-put or bear-call spread when the directional read is one-sided. It is a screening tool, not advice. Every probability is a model estimate.

* **Default expiry:** the first one at least 7 days out. Below that, gamma dominates and the trade stops being positional. You can pick another expiry from the drop-down.
* **Option-chain read:** ATM IV against 10/20-session realised vol, the 1σ expected move and ATM straddle, PCR (total and today's OI change), max pain, the three biggest call and put OI walls, put–call skew, India VIX percentile, and trend/ADX from the main engine.
* **Selling environment:** a checklist covering IV rich or cheap versus realised vol, vol expansion, days to expiry, VIX regime, trend strength, the OI-wall band and events before expiry. It adds up to FAVOURABLE, MIXED or UNFAVOURABLE.
* **Strike selection:**
  * Short strikes sit near 0.16 delta, about 1σ out of the money (0.28 delta for one-sided spreads).
  * Among strikes within ±0.03 delta of that target, the one with the most OI is chosen.
  * A short strike is nudged beyond a nearby OI wall or S/R zone.
  * Wings are about 0.5σ wide.
  * Only strikes with OI, volume today, a two-sided quote and a spread of ≤10% are used.
  * Sells are priced at the bid and buys at the ask.
* **Per structure:**
  * credit, max loss, breakevens;
  * win probability at IV and at realised vol;
  * **expectancy at realised vol**, the simulated average P&L if the index keeps moving like its last 20 sessions (positive means the premium is rich);
  * theta and vega;
  * average loss in the worst 5% of outcomes.
  * Each comes with why it can work, what can go wrong, and a management plan: book profit at ~50% of credit, cut at ~1.5× credit or on a daily close through a short strike, close or roll 1–2 sessions before expiry.
* **Badges:** a structure with negative expectancy is labelled **NEGATIVE EXPECTANCY — NO EDGE** whatever its win rate.
* **Settings:** lot sizes (`NIFTY_LOT`, `BANKNIFTY_LOT`) and the refresh interval (`OPTIONS_REFRESH_SECONDS`) are in `config.py`. NSE revises lot sizes periodically.

### Using the decision card
* Do your **monthly planning on the 1D timeframe**, because that is where the zones, projection and pivots apply. Use **1H or 15m only to time entries** inside a daily zone that is already flagged.
* Check **NIFTY (`^NSEI`)** for the main market picture. The card's *NIFTYBEES equivalent* row converts the NIFTY range into NIFTYBEES prices using the live price ratio. Then switch to `NIFTYBEES.NS` for the exact prices to trade.

### A monthly playbook
1. **Start of the month (first 1–2 sessions):** sync, then note the regime, the projected range and the top two support and resistance zones on 1D.
2. **Accumulate in tranches.** When the card shows **ACCUMULATE ZONE**, or WATCH with price inside a green zone:
   * Buy 1/3 at the top of the support zone.
   * Buy 1/3 at the zone's midpoint or VAL, ideally after a bullish RSI divergence or a 1H close back above the 20 EMA.
   * Buy the last 1/3 only if flows confirm, meaning DII buying continues or the FII long/short ratio turns up.
   * Use the card's stop loss for the whole position. A daily close below the zone low minus 0.5 ATR means the setup is wrong.
3. **Hold** while price stays between the zones, the regime is not Bearish, and the institutional thesis is not "Retail trap" or "Distribution".
4. **Book profits in tranches.** When the card moves to **TIGHTEN STOPS** or **PROFIT BOOKING ZONE** (near a red zone, VAH or monthly R1, with RSI above 70 and/or FII shorting):
   * Book 50% at the first target.
   * Trail the rest under the most recent 1H swing low, and exit it at the second target or at the projected high.
5. **Stand aside** when the card says **AVOID — WAIT FOR BASE**, or when a RETAIL TRAP thesis shows up during a rally. Also stand aside before high-impact events in the upcoming-event list (RBI, FOMC, Budget, monthly expiry), unless price is already deep inside a strong support zone.

### Worked example (live output on 2 Nov 2026, for illustration only)

**NIFTY 50** (last 23,320)

| Item | Value |
|---|---|
| Regime | Range-bound (ADX 30.8, below the 200 EMA) |
| Projected November range | 22,404 – 23,838 |
| Support zone | 23,032 – 23,231 |
| Monthly R1 / VAH | ≈ 23,426 / 23,256 |
| Card | WATCH — NEAR ACCUMULATION |

**NIFTYBEES** (last 265.70)

| Item | Value |
|---|---|
| Projected range | 257.0 – 273.5 |
| Support zone | 262.3 – 264.9 |
| Stop loss | 260.3 |
| Targets | 272.1 / 276.3 |
| Risk : reward | ≈ 1.5 |

**Flows:** FII cash was +₹3,599 cr on 30 Oct. FIIs are still heavily net short index futures (long/short 0.13), clients are crowded long (4.1), and the PCR is 1.37 (put-heavy, a contrarian bullish reading).

**How to read this:** price sits just above a support zone and just below a resistance cluster, and the stock is still below its 200 EMA. That combination is a *wait for a pullback* setup, not a chase.
* Tranches go in near 263–264, with a deeper add near the 257–258 projected low or VAL.
* Profits get booked into 272–274.
* A daily close below about 260 invalidates the dip-buy.

Re-sync daily: the card updates as soon as price, flows or RSI change.

---

## 5. Tuning
All thresholds are in `config.py`, and each can be overridden with an environment variable:

| Setting | Default |
|---|---|
| `ANALYSIS_WINDOW_DAYS` | 60 |
| `SWING_WINDOW` | 3 |
| `ZONE_ATR_MULT` | 0.35 |
| `PROXIMITY_ATR_MULT` | 0.6 |
| `FII_LS_BEARISH` | 0.60 |
| `FII_LS_BULLISH` | 1.20 |
| `VP_BINS` | 48 |

Logs are written to `~/.nse_analytics/app.log`.
