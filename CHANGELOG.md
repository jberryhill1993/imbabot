# Changelog

All notable changes to Imbabot are recorded here. This file lives on the development
branch (`v0.2.1-dev`); the stable, shipped build is **0.2.0** on `main`.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Versions use the number shown in the app's title bar (`Imbabot <version>`).

## [0.2.4.1] - 2026-07-22 (update-loop live-fire test; no functional changes)

## [0.2.4] - 2026-07-22 (released to `main`, tagged `v0.2.4`)

Auto-updating bot. **No trading-logic changes** — engine/strategy/projectx are
byte-identical to 0.2.3. (The Tradovate second-broker work stays on `v0.2.5-dev`,
gated on its demo forward-test; it is NOT in this release.)

### Added
- **Bundled model + self-install.** The calibrated 264-day Morning-Plan model
  (+ VIX/NQF dailies) ships inside the app (`imbabot/analysis/data/model/`) and
  self-installs into `%APPDATA%\imbabot\analysis` on launch
  (`analysis/bootstrap.py`). A fresh or second machine is calibrated with no
  `setup-data.bat` — root-cause fix for the 7/21 "NO-TRADE · UNCALIBRATED"
  panel (which was the crude 0.75×VIX fallback, not a real model call).
- **Loud MODEL-NOT-LOADED state.** If the model still can't load, both UIs show
  a red "⛔ MODEL NOT LOADED — no advice" instead of a fake NO-TRADE/spike, and
  the log records the resolved model path.
- **Auto-update from GitHub Releases** (`imbabot/updater.py`, recipe in
  `UPDATING.md`): silent model/data sync on launch (weekly retrains propagate
  to every machine zero-touch) + notify-and-one-click code updates (header
  ⬆ Update button → checksum-verified download → exe swap → relaunch). All
  downloads HTTPS + SHA-256 verified against the release's checksums.txt.
  Replaces the manual Drive/OneDrive reupload-redownload loop.
- Web UI session-start line in the file log (sessions are never invisible).

## [0.2.3] - 2026-07-15 (released to `main`, tagged `v0.2.3`)

First stable release since 0.2.0.1. Promotes the tick-data Morning Plan line and the new
glass web UI together. Trading logic (engine / strategy / projectx / OCO / scheduler /
config formats) is unchanged from the 0.2.1 line that passed a live forward-test week — this
release adds analysis + presentation, not order behavior.

### Added
- **Morning Plan analyzer** (advisory; never auto-applies) rebuilt on **Databento tbbo tick
  data**: a k-NN opening-spike predictor fit on 264 sessions and gated by expanding-window
  **walk-forward** (spike SIZE predictable, corr ~+0.53; win/loss is not, so TRADE/NO-TRADE
  routes through predicted size ≥ 20 pt). Outputs the TopStep block sized to a $ target
  (contracts · entry ±10 · **TP $** · **SL $**), a 5-contract cap alert + recommended TP,
  and refreshes VIX/NQ dailies on every Recalculate.
- **Overnight-gap whipsaw filter** — small-gap opens (≤ 40 pt) churn (30% clean vs ~49%),
  downgraded to NO-TRADE, with a freshness guard (only applies when measured within 60 min
  of the open; earlier recalcs show a caveat).
- **Glass web UI** (pywebview + HTML/CSS/JS) as the default window: navy glassmorphic
  dashboard, live tickers, ARM/FLATTEN/EMERGENCY-STOP action bar, Connect/Strategy/Test
  tabs, Morning Plan card, fully responsive layout. Driven by a thin **js_api bridge** over
  the existing engine ops (no trading-logic changes). The classic Tkinter GUI is retained —
  launch with `Imbabot --classic`.
- **Daily auto-fire scheduler** with self-re-arming that skips weekends **and US market
  holidays** (`market_calendar`).

### Changed
- **Pre-open reference capture moved 3 s → 1 s** before the open (fixes early-trigger losses:
  orders resting ~2 s pre-open were tripped by last-seconds churn). Settings migrate the old
  default automatically.

## [0.2.1] - superseded by 0.2.3

New work and the decisions behind it go here as we build them. Nothing in this section
ships until 0.2.1 is released (merged to `main`, `-dev` suffix dropped, tagged `v0.2.1`).

### Added
- **Morning Plan analyzer** (advisory; never auto-applies): a backtest-calibrated model
  that recommends, from pre-open VIX / overnight range / gap / ATR / economic-event
  features, the day's **entry spread**, **stop distance**, a **conviction** rating, and a
  **TRADE / SKIP** call. Shown on the Strategy tab with "Recalculate now" + "Run 12-month
  calibration" buttons. CLI: `calibrate-morning`, `morning`.
- **Profit-target sizing calculator** — type a $ target; get suggested contracts, the
  TopStep $ TP/SL brackets to set (so the point-stop holds at that size), the symmetric
  downside, and EV. Honest by design: size scales the outcome, not the odds.
- **2-D backtest** sweeping entry spread × stop distance with **exact second-level whipsaw
  detection** when fed 1-second data.
- **Databento 1-second CSV ingester** (`ingest-history --format databento`) — resolves the
  intrabar high/low sequence the open whipsaws on. FirstRate 1-minute path retained.
- **Data-feed auto-detect** — price capture tries the live feed then falls back to sim,
  logging which it used. Mixed eval/funded accounts work without a manual toggle.
- History-depth probe (`probe-history`) + Yahoo 12-mo daily VIX/NQ + US econ calendar.

### Changed
- **Strategy tab Mode** now exposes only **One-Trade (auto OCO)** (Semi-Auto / Two-Trade
  remain in the engine but are hidden); the **Use live data feed** checkbox is hidden
  (auto-detected).

### Fixed
- _(nothing yet)_

---

## [0.2.0] - Stable (shipped)

The current downloadable build. Frozen on `main`; distributed as
`Imbabot-Download.zip` via OneDrive.

### Highlights
- **Naked-entry opening-range straddle** — places exactly two stop-market entries
  (one buy stop above, one sell stop below the reference price) a few seconds before
  the 09:30 ET cash open. SL/TP attach from TopStep Position Brackets on fill.
- **Trade modes** — semi-auto, one-trade (OCO: opposite entry auto-cancels on fill),
  and two-trade (both sides allowed to fill).
- **Optional bot-managed brackets** — per-leg stop-loss / take-profit toggles for users
  who run TopStep Auto OCO instead of Position Brackets.
- **Weekday auto-fire schedule** — arm once; the bot self-re-arms and fires Mon–Fri at a
  chosen local time. Plus a one-shot test-fire time on the Test tab.
- **GUI** — required fields highlighted, optional SL/TP grayed behind enable toggles,
  collapsible activity log, fit-to-content window, live NQ/MNQ price + VIX readout.
- **Packaging** — single-file Windows `Imbabot.exe` (PyInstaller); `publish_update.ps1`
  rebuilds + repackages the OneDrive download in place (see `UPDATING.md`).
- **Self-test** — 62 offline checks (`Imbabot.exe cli selftest`).
