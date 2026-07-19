# Changelog

All notable changes to Imbabot are recorded here. This file lives on the development
branch (`v0.2.1-dev`); the stable, shipped build is **0.2.0** on `main`.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Versions use the number shown in the app's title bar (`Imbabot <version>`).

## [0.2.5-dev] - unreleased (branch `v0.2.5-dev`)

### Added — Tradovate second broker (parallel to TopstepX)
- **`imbabot/tradovate/` package**: direct Tradovate API integration.
  - `auth.py` — access-token lifecycle: acquire (`accesstokenrequest` with
    name/password/appId/appVersion/deviceId/cid/sec), proactive renewal at
    T-10min (`renewaccesstoken`, GET with POST fallback), p-ticket penalty
    backoff, p-captcha surfaced as a clear user action.
  - `client.py` — full BrokerAdapter surface. Bracketed entries ride native
    server-side **OSO** orders (absolute tick-snapped prices — survive crash/
    disconnect); flatten stays engine-driven; `liquidateposition` reserved for
    the kill switch. Contract resolution picks the front month and
    sanity-checks tick math. All order bodies `isAutomated: true`.
  - `ws.py` — user-sync + market-data WebSockets (`websocket-client`, daemon
    threads): 2.5s client heartbeat, exponential-backoff reconnect with
    fresh-token authorize → `user/syncrequest` resync → quote re-subscribe;
    order/position caches feed the unchanged 0.5s OCO poll (REST fallback if
    the socket is unhealthy).
  - `safety.py` — **hard-coded** environment gate: `LIVE_TRADING=False` (live
    endpoint un-constructable until edited in source). **Guard parity with
    TopStep** (user directive 2026-07-18): the optional venue caps
    (`MAX_POSITION_SIZE`, `MAX_DAILY_LOSS` kill switch) ship **disabled**
    (None) so the identical 4–5 contract strategy runs on demo under the same
    guards as TopStep (max_contracts + RiskGuard + dry_run); set values in
    safety.py to re-enable (revisit at the live flip). The kill-switch
    machinery remains built and selftested. Startup banner states
    env/endpoint/gates on every connect.
- **`imbabot/broker.py`** — BrokerAdapter Protocol codifying the duck-typed
  engine⇄client contract; selftest pins ProjectX/Tradovate/fakes conformance.
- **Both UIs**: Tradovate backend selector, credential form (username/password/
  cid/secret → Windows Credential Manager via "Remember", or session-only),
  demo/live selector with live-lock warning, TDV DEMO/LIVE badge.
- **`scripts/tdv_demo_check.py`** — gated demo integration probe (auth,
  sockets, contract, quote, OSO place/modify/cancel, forced-reconnect resync,
  optional fill+liquidate), all 1-contract, demo-only.
- Engine: single constructor branch routes `backend="tradovate"`; everything
  else (strategy, risk, OCO monitor, flatten, dry-run gate) unchanged and
  shared across brokers.
- Config: `tdv_*` settings (non-secret) + `store/load/clear_tradovate_credentials`
  (keyring → 0600-file fallback, `IMBABOT_TDV_*` env override).
- Dependency: `websocket-client>=1.7`. Selftest: 121 → **215** checks, all
  offline.

### Added — pluggable reference-price source (skip the $290/mo CME fee)
- Verified: real-time CME data over the Tradovate API needs CME sub-vendor
  registration (~$290/mo to CME; the $39 retail bundle does NOT cover API) —
  but ORDER ROUTING needs no data license. New `tdv_price_source` setting:
  **"topstep"** (default — reference price from the existing ProjectX feed via
  the stored TopStep key, public NQ quote fallback; `tradovate/pricefeed.py`),
  "tradovate" (MD WebSocket, for licensed accounts), "public". The MD socket
  only opens for "tradovate"; the user-sync socket always runs. Both UIs gain
  a Price source selector; demo check is source-aware.

### Added — $-based SL/TP entry (TopStep Position-Brackets UX)
- New "SL/TP entered as: points | $ per position" mode in both UIs. Dollar
  amounts convert to per-contract points via contracts × $/point (resolved
  contract math when connected; NQ/MNQ/ES/MES fallback table), FLOORED to the
  tick grid (a $-entered stop never risks more than typed). Points remain the
  single engine source of truth; typed dollars persist for the form.
  Needed because Tradovate UI bracket presets do not apply to API orders —
  the bot's OSO brackets are the only brackets there.

### Notes
- `session_range`/`retrieve_bars` are not yet supported on the Tradovate
  backend (dashboard shows “—”; the Morning Plan is Databento-backed and
  unaffected).
- Account roles: TopStep PRAC = testing venue; Tradovate = eventual live
  (locked until the demo check passes and `safety.py` is deliberately edited).

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
