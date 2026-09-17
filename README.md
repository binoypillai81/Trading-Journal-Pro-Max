# Trade Time Machine

A local trading laboratory for **blind, chronological trade reconstruction**. You review every
historical trade in entry-time order, standing at the moment of entry. You see only the chart up
to that instant, then write down what you believed and lock it. Only after that do you reveal
what happened and compare it with your thesis.

The app is built around the decision process and hindsight bias, not P&L.

---

## Run it

```bash
git clone <this repository> && cd Trading-Journal-Pro-Max
./run.sh
```

Open <http://127.0.0.1:8765>. The first run creates `.venv` and installs `requirements.txt`.

* Journal database: `data/journal.sqlite3` (override with `TTM_DB_PATH=/path/file.sqlite3 ./run.sh`)
* Port: `PORT=9000 ./run.sh`
* Tests: `.venv/bin/python -m pytest -q tests`

Everything is local. The chart library (TradingView Lightweight Charts 4.2.3) is bundled in
`app/static/vendor`, so no internet is needed.

### Trying it with sample data

`sample_data/` holds synthetic index bars and sample trades in shuffled order (not real data).
Import `market_nifty_15m.csv` and `market_nifty_1m.csv` as `NIFTY` (timezone Asia/Kolkata), then
`sample_trades_random_order.csv` as round-trip trades. Regenerate with
`.venv/bin/python sample_data/make_sample.py`.

---

## Workflow

```
Import market data → Import trades → Confirm chronological order → Start session
→ blind chart at entry → questionnaire → LOCK THESIS → REVEAL → thesis vs reality
→ reflection → COMPLETE → next trade unlocks → analytics on completed trades only
```

### 1. Import market data (Import → 2 · Market data)

The chart needs 15-minute OHLC data. 1-minute data is optional but strongly recommended. With it,
the app rebuilds the candle that was forming at your exact entry time and measures MFE/MAE
precisely.

| File | Instrument symbol | Timeframe | Purpose |
|---|---|---|---|
| index 15-minute OHLC CSV | e.g. `NIFTY`, `BANKNIFTY`, `FINNIFTY` | 15m | chart (required) |
| index 1-minute OHLC CSV | same symbol | 1m | exact forming candle and excursions (optional) |
| index daily OHLC CSV | same symbol | 1d | official closing values for expiry settlement (optional) |

Instrument symbols must match the underlying your trades map to (e.g. `NIFTY` for `NIFTY24JAN21500CE`).

Daily (`1d`) bars hold official closing values. They are used only for expiry settlement, never for
charts.

Use **Option B · local file path** for large files. Columns map as `timestamp, open, high, low,
close, volume`.

**Bar label matters.** The app stores bars by their *open* time: the 10:30 bar covers
10:30–10:45. If a file labels bars by close time, import it with "CLOSE". Otherwise the cutoff
shifts by one bar. The importer warns when sessions appear to start at 09:30.

Trades whose chart instrument has no imported bars show "No market data"; they can be reviewed
without a chart or excluded (see below).

**Broker historical data and corporate actions.** Some sources (e.g. Kite historical data) adjust
stock prices for later splits and bonuses, so old stock prices appear on today's share basis.
Index data is unaffected. Check the price level against your traded strikes before relying on
stock charts.

**Short sessions.** Muhurat, special Saturday and partial-data sessions with fewer than 20 15-minute
bars are still drawn on the chart, but never used as the prior session for pivots. The chart notes
when one was skipped.

### 2. Import trades (Import → 1 · Trades)

Two row formats are supported. The app suggests one based on your columns, and you confirm it.

* **One row per round-trip trade:** entry and exit on the same row.
* **One row per fill (broker tradebook):** round trips are rebuilt. For each exact symbol, fills
  are walked in time order:
  * A trade opens when the net position leaves zero and closes when it returns to zero.
  * Adds and partial exits stay inside the same trade.
  * A fill that flips the position closes the trade and opens a new one with the remainder.
  * Entry and exit prices are quantity-weighted averages. P&L is realised, before charges.
  * **Held to expiry:** a position that never returns to zero is settled at expiry at intrinsic value
    (`max(0, S − K)` for CE, `max(0, K − S)` for PE, `S` for futures). `S` is the official closing index
    value from imported daily bars. The expiry date comes from the weekly symbol, or for monthly
    contracts from NSE's expiry weekday rules, moved back if that day was a holiday. The trade is
    labelled **HELD TO EXPIRY**. Stock options are physically settled and are not modelled, so they
    stay open.
  * **First fill is a sell:** if a contract's first fill in the file is a sell, its trades are held out
    of the sequence until you decide under **Import → Timestamp issues**:
    * **Real short:** the trades are kept as paired.
    * **Opening buy missing:** the first sell is treated as closing a position opened before the file
      started. It is excluded and the rest are re-paired.

    Decisions are logged.
  * **Multi-entry:** a trade that added to the position again after a partial exit is labelled
    **MULTI-ENTRY** after reveal. It remains one review.
  * **Blind view:** entry price and quantity are those of your **first order** only. The average
    across later adds is shown after reveal.

  Example mapping for a Zerodha-style tradebook: `order_execution_time` → fill date+time,
  `symbol` → instrument, `underlying` → underlying, `opt_type` → option type, `trade_type` → side,
  `quantity`, `price` → fill price, `order_id` → order ID. Timezone for values without an offset:
  **Asia/Kolkata**.

  Properties of the pairing (verified on a real multi-year tradebook):
  * Every fill is used exactly once.
  * P&L equals sell value minus buy value, plus any settlement, on every trade.
  * An independent lot-by-lot FIFO calculation gives the same realised P&L on all contracts that end
    flat.
  * Expiry dates and closing values used for settlement matched NSE bhavcopy data.

No column names are assumed; you map every field. The mapping screen shows each column's
**shape** (e.g. `9999-99-99T99:99:99`) rather than raw values, so exits and P&L are not exposed
before review. A separate button shows raw rows, with a warning, and that action is logged.

After mapping, **Normalize & build chronological preview** does the following:

1. Parses and validates every entry timestamp.
2. Refuses to continue until you choose a timezone, if any values lack an offset.
3. Refuses to continue until you choose a date format, if any dates are ambiguous
   (03/04/2026).
4. Normalizes each timestamp to a single instant and shows the exchange-local time.
5. Flags duplicate entry timestamps and shows the tie-break rule.
6. Flags rows without a valid timestamp as **Chronology unresolved**. These are imported but
   kept out of the sequence.
7. Shows the order: position, instrument, original value, normalized time, timezone source,
   entry, direction and status.

Tick **"These trades will be reviewed in this chronological order."** and click **Confirm
Chronological Order**. Resolve flagged rows under **Import → Timestamp issues**. Each correction
is recorded with the original value.

### 3. Review

Go to **Sessions → Start a new chronological session**. The review screen shows:

* Trade *N of M*, a progress bar, and the previous trade's time. The next trade's time is hidden
  by default; you can enable it in Settings.
* A 15-minute chart cut at the entry, with EMA 15, classic pivots and an entry marker.
  Previous-day high/low/close and day open are optional toggles.
* **Day P&L before this entry:** the realised P&L of trades that closed earlier the same day, with wins/losses,
  the current losing streak, positions still open, and whether it's the first trade of the day. Trades
  still open at the entry are excluded, because their result wasn't known yet. P&L is as imported
  (before charges). It's stored with the locked thesis and used in psychology analytics.
* A 9-section questionnaire: the moment, setup, direction, reason, expectations, risk,
  confidence, psychology (including "Was today's P&L on your mind?") and process. Drafts autosave.
* **Mandatory original thesis:** a snapshot you must confirm. Vague answers ("I thought it
  would go up", "bad trade") trigger specific follow-up prompts. To lock anyway, you must tick
  "I have been as specific as I honestly can", which is recorded.
* **REVEAL WHAT HAPPENED:** the rest of the candles, exit, P&L, MFE/MAE, duration and a *thesis
  vs reality* table. Each row is labelled FACT or USER'S STATED BELIEF.
* A post-outcome reflection, including the hindsight question and psychology flags.
* **TRADE COMPLETED:** the next trade unlocks, and observations from trades already reviewed are
  shown, marked **OBSERVATION — NOT YET A RULE**.

### Excluding trades (e.g. no market data)

Some trades can't be reviewed meaningfully, for example stock options with no market data. You can
exclude them in two places:

* **During review:** **Exclude this trade from review…** at the bottom of the review screen. A trade
  with no chart data also shows a callout with an exclude link.
* **In bulk:** **Import → Data coverage & exclusions** lists trades without chart data by instrument.
  You can exclude one instrument's trades or all of them at once, and restore any excluded trade
  from the same page.

Excluded trades:
* leave the chronological sequence, queue and analytics;
* are not deleted, and can be restored any time;
* still count toward that day's P&L context, because you knew their result at the time.

Every exclusion needs a reason and is logged. Trades whose thesis is already locked can't be excluded.

### Resuming

Close the browser or stop the server at any time. **Sessions → Resume** reopens the earliest
incomplete trade at the exact stage you left it, with drafts intact. Starting a new session never
overwrites an old one. **Completed** lists finished trades for study, filtering and revisions.

### Administrative override

Enable it under **Settings → Administrative override**. On the review screen, click
**Chronological-review exception…**, give a reason of at least 10 characters and type `SKIP`. The
trade becomes `SKIPPED_WITH_OVERRIDE`, its outcome stays hidden, and the next trade unlocks. The
reason, time, trade, position and prior state go into the append-only event log. Turning the
setting on or off is logged too.

---

## How chronological order is determined

`app/chronology.py`:

1. Normalized entry instant: every value is converted to UTC epoch seconds, so +05:30, `Z` and
   assumed-IST values compare correctly.
2. Execution-sequence column, if mapped. Trades without one sort after those with one.
3. Trade ID, in natural order: numeric IDs as numbers, before text IDs.
4. Import batch, earlier first.
5. Original CSV row number.

Exit time, P&L and import order never affect position. Instrument is deliberately **not** a
primary key: sorting by instrument first would review all NIFTY trades before all BANKNIFTY
trades and break the order of decisions. Unresolved timestamps get no position.

## How blind mode prevents leakage

The rules are enforced in the data layer, not by hiding things with CSS:

* **Hard cutoff in SQL.** Completed bars must satisfy `epoch + 900 <= entry`.
  * With 1-minute data, the forming bar is rebuilt from 1-minute bars with `epoch + 60 <= entry`.
  * Without 1-minute data, only the forming bar's **open** is selected.
  * If the traded price is on the chart's scale, the entry price is used as the last print.
* **Indicators.** EMA uses completed candles available at entry only. Pivots use the most recent
  *completed prior session*.
* **Blind trade projection.** Blind API responses select an explicit column list. Exit, P&L and
  raw CSV rows are never read for them.
* **Access control.** Any request for a trade later than the session's current trade returns
  HTTP 423. Outcome endpoints return 409 until the thesis is locked and revealed.
* **Immutability.** SQLite triggers make `thesis_snapshots`, `post_outcome` and `events`
  un-updatable and un-deletable. Later edits go to `journal_revisions`, so hindsight drift can be
  studied. Each snapshot also stores a SHA-256 hash.
* **Analytics scope.** The default is "completed before the current trade". It also **excludes
  earlier trades that were still open at the current entry**, because their outcome was not
  known yet. Full-history analytics require explicit acknowledgement.
* **Overlap caution.** If an earlier trade's reveal showed candles past the current entry,
  because you entered while it was still open, the review screen tells you.

## Options, futures and price basis

If the traded price is within 0.25% of the index level at entry, it's treated as being on the
chart's scale. Otherwise (options, most futures) the entry marker sits at the **index level at
entry**. MFE/MAE are then measured on the index in the direction the position needed:

| Position | Index direction needed |
|---|---|
| Long, or long CE | UP |
| Long PE | DOWN |
| Short CE | DOWN |
| Short PE | UP |

Stop, target and invalidation prices in the questionnaire are **chart (index) levels**.

---

## Files

```
app/
  main.py           FastAPI routes, error mapping, static serving
  db.py             SQLite schema, immutability triggers, settings, event log
  timeutil.py       timestamp parsing (offsets, UTC, IST, DMY/MDY, AM/PM, epochs, DST gaps/overlaps)
  importer.py       CSV upload, column profiles, mapping, round-trip & fills modes, preview, confirm, corrections
  chronology.py     sort key, tie-breaks, resequencing
  market.py         market import, blind chart builder (hard cutoff), EMA, pivots, path bars
  review.py         sessions, queue, state machine, access control, lock/reveal/complete, override, revisions
  questionnaire.py  question schema, validation, vague-answer prompts, thesis snapshot
  outcome.py        MFE/MAE, thesis window, thesis-vs-reality table
  analytics.py      scoped dataset, setups, calibration, psychology, why-analysis, consistency checks, patterns, observations
  samplegen.py      synthetic / sample data generation
  static/           index.html, styles.css, js/{app,lib,chart,importer,review,completed,analytics}.js, vendor/
tests/
  test_acceptance.py  the 18 acceptance tests plus workflow, fills, journal-quality and mapping-leak tests
  test_timeutil.py    timestamp parsing unit tests
sample_data/
  make_sample.py      builds sample market data (a real NIFTY slice) + shuffled sample trades
run.sh, requirements.txt
```

### Database tables

`settings`, `import_batches`, `trades` (original and normalized entry/exit, timezone source,
chrono_seq, import row, raw row), `timestamp_corrections`, `market_bars`, `review_sessions`,
`reviews` (status, stage timestamps, override, drafts, entry context), `thesis_snapshots`
(immutable), `post_outcome` (immutable, with outcome metrics as computed at completion),
`journal_revisions` (append-only) and `events` (append-only audit log).

---

## Known limitations

* **No tick data.** Prices inside the entry minute before the entry second are unknown.
  1-minute excursions can include a few seconds before entry. 15-minute excursions are
  approximate.
* **Market data coverage.** Only instruments with imported bars get charts. Without 1-minute data the
  forming candle shows only its open and excursions use 15-minute bars.
* **Option prices.** There is no option-premium chart. Excursions are measured on the underlying
  index.
* **Fills pairing** is per exact symbol and flat-to-flat. It can't know your intent when one
  position was really two ideas. Charges are not deducted.
* **Session times.** 15-minute bars are assumed to align to quarter hours (true for NSE's 09:15
  open).
* **Revealed candles.** A reveal necessarily shows candles after the entry. For overlapping
  trades, that is data after the *next* trade's entry. The app warns but cannot un-see it.
* **Large histories.** Full-history analytics rebuild context for every unreviewed trade and can
  take a while with ~10k trades. The queue and import preview show windows.
* **Not built yet (Phase 3).** LLM-assisted analysis, automatic reports, multi-session
  comparison and a dedicated hindsight-drift report. Revisions are already stored for the drift
  report.
* **VWAP** isn't offered, because the index series has no volume.

---

## Licence

MIT: see [LICENSE](LICENSE). The bundled TradingView Lightweight Charts library is licensed separately
under Apache-2.0 (`app/static/vendor/lightweight-charts.LICENSE`).

This is a journaling and review tool, not financial advice. It makes no trading recommendations, and
its calculations (P&L pairing, expiry settlement, excursions) should be checked against your broker's
records before you rely on them.
