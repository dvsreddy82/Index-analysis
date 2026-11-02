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
