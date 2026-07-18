# Imbabot — opening-range breakout bot for TopstepX

A local desktop bot that places an **opening-range straddle** a few seconds before
the 09:30 ET cash open on your TopstepX / ProjectX futures account, then (in
One-Trade mode) cancels the opposite entry once one side fills.

This is a clean-room rebuild of the workflow in the setup guide. It has **two
backends**:

- **API (recommended)** — uses TopStep's official ProjectX/TopstepX API. More
  reliable, it's the path TopStep supports, and it removes the whole "the UI moved
  and the bot misclicked" class of failures.
- **Browser automation (fallback)** — drives the trading site in a real browser, like
  the original product, for when you don't want the API add-on. See
  [Browser backend](#browser-backend-fallback).

> ⚠️ **Read this first.** This software places **real orders with real money**. It
> is **not** financial advice. You are solely responsible for every order it
> places and any losses. See [Rules & risks](#rules--risks).

---

## What it does

1. A few seconds before 09:30 ET (default **09:29:57**), or at a custom time you
   schedule, it captures a reference price.
2. It places **exactly two** stop-entry orders — nothing else:
   - **BUY stop** `entry_points` above the reference (long breakout)
   - **SELL stop** `entry_points` below the reference (short breakout)

   By default these are **naked** entries: TopStep's **Position Brackets** attach your
   stop-loss / take-profit to the position the instant a leg fills. (You can opt into
   **bot-managed** SL/TP brackets per field on the Strategy tab — see
   [Strategy tab](#strategy-tab--what-to-fill-in).)
3. Mode controls what happens after a fill:
   - **Semi-Auto** — both orders are placed; *you* manage/cancel them.
   - **One-Trade** — whichever side fills first stays; the bot **cancels the other
     entry automatically** (one-cancels-the-other).
   - **Two-Trade** — both entries stay working (either/both can fill); set your
     platform **trade limit to 2/day**.
4. **Flatten** closes open positions but leaves working orders + any armed schedule
   intact; **Emergency Stop** cancels all working orders, flattens everything, and
   disarms.

Defaults: `MNQ` (Micro Nasdaq), ±12 points, 2 contracts, Semi-Auto, **dry-run ON**.

---

## Rules & risks

**Confirm these against your firm's current rules before trading live.** As of this
writing, for TopStep:

- ✅ Automated trading via the API is **allowed in the Trading Combine and Funded
  (evaluation) accounts**.
- ⛔ It is **prohibited in the Live Funded account.**
- 🖥️ **Local execution only** — no VPS, no cloud bots, no HFT/latency games. Running
  this `.exe` on your own machine is the intended use.
- 🧰 Third-party tools are **unsupported and used at your own risk.** TopStep won't
  help debug your automation, and a rules breach can void an account or payout.

This project ships **dry-run by default** and makes you deliberately turn it off.
It also caps contract size and trades-per-day on the client side — but those are a
*backup*. **Set the platform-side guards too** (see [Safety](#safety-net-do-this)).

Nothing here is trading or financial advice.

---

## Setup

### 1. Get a ProjectX API key
- In TopstepX, enable **API access** and create an **API key** (Topstep Help Center:
  "TopstepX API Access"). Note your **username** and **API key**.
- The API add-on is a paid subscription (~$29/mo; TopStep traders often get a
  discount code). It is **separate** from your evaluation.

### 2. Install Python deps (for running from source / building)
```bash
python3 -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows:
#   .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Sanity-check (no network, no account needed)
```bash
python -m imbabot.cli selftest
```
You should see `62 passed, 0 failed`.

### 4. Run the GUI
```bash
python run.py
# or:  python -m imbabot
```
Connect → pick account → set symbol & strategy → **leave dry-run ON** for your
first sessions and watch it work.

---

## Morning Plan — advisory spread / stop / sizing (0.2.1+)

The Strategy tab has a **Morning Plan** panel that, from a one-time backtest of historical
opening behavior, recommends the day's **entry spread**, **stop distance**, a **conviction**
rating, and a **TRADE / SKIP** call — plus a profit-target box that suggests **contracts** and
the **TopStep $ brackets** to set. It is **advisory only**: the bot never changes your settings;
you review the numbers and enter them yourself.

Setup (one time): obtain historical 1-second NQ bars (Databento `ohlcv-1s`, CSV) and ingest +
calibrate:
```
python -m imbabot.cli ingest-history "NQ_1s.csv" --symbol NQ --format databento
python -m imbabot.cli calibrate-morning --symbol NQ --tp-points 13.3
```
Each morning (10–30 min before the open) click **Recalculate now** (or `python -m imbabot.cli
morning --symbol NQ --target 1000`). Notes: the recommendation is a statistical estimate, **not a
prediction or financial advice**; it widens the entry / stop or says SKIP on high-whipsaw mornings.
Brackets are in **dollars**, so the point-stop shrinks as you add contracts — the sizing box gives
the dollar bracket to set so it holds.

## Daily routine (mirrors the guide)

1. Launch Imbabot; **Connect** (this resolves your contract automatically).
2. Pick the **account** and **symbol**. (Changing the symbol and clicking **Save
   settings** re-resolves and updates the contract label.)
3. Set **points / contracts / mode**, click **Save settings**. Match your TopstepX
   risk settings to your contract count.
4. Check the dashboard (last price, overnight range, countdown).
5. **Arm.** It captures price at 09:29:57 and places the two entries; a countdown
   shows the next fire time so it can't trigger early.
6. Flatten and Emergency Stop are always one click away.

### Strategy tab — what to fill in
The **required** settings are highlighted (green/✓): **Entry points**, **Contracts**,
and the **One-Trade** mode. **Stop-loss** and **Take-profit** are **grayed out** by
default because TopStep's Position Brackets handle them — tick the checkbox next to
either to let the **bot** manage that bracket instead (only if your TopStep account
is in *Auto OCO Brackets* mode, not Position Brackets).

### Scheduling (auto-fire)
- **Daily (production):** on the Strategy tab, enter a time in **`HH:MM:SS` 24-hour,
  your computer's local clock** (e.g. `08:31:00`) and click **Save & arm daily
  (Mon–Fri)**. The bot stays armed and fires at that time **every weekday**,
  re-arming itself after each fire, until you cancel/disarm. Weekends are skipped;
  **market holidays are not** — disarm on those days.
- **One-shot (test):** the **Test** tab fires once at a custom local time (or **Fire
  TEST now**), honoring dry-run. Use a SIM/practice account.

Both schedules use your **computer's clock** — TopStep's API exposes no server clock.

---

## Going live (after you've watched it in dry-run)

1. Uncheck **DRY-RUN** (you'll get a confirmation prompt).
2. The banner turns red: **● LIVE — REAL ORDERS**.
3. Arm. You'll confirm once more before live arming.

Start with **micros** (MNQ/MES) and **1 contract** while you build trust.

CLI equivalent:
```bash
python -m imbabot.cli login            # store + verify your API key
python -m imbabot.cli run              # dry-run, prints a live countdown
python -m imbabot.cli run --live-orders  # ⚠ sends real orders (asks you to confirm)
python -m imbabot.cli panic            # cancel everything + flatten
```

---

## Building the apps (Windows `.exe` + macOS `.app`)

PyInstaller builds for the OS it runs on — **you can't make a Windows `.exe` from a
Mac, or a Mac `.app` from Windows.** The spec handles both: on Windows it emits
`dist/Imbabot.exe`, on macOS `dist/Imbabot.app`.

### macOS (build natively on your Mac)
```bash
./build_macos.sh        # -> release/Imbabot-macOS-arm64.zip  (signed + verified)
```
A prebuilt, ad-hoc-signed `release/Imbabot-macOS-arm64.zip` is already included
(Apple Silicon). Unzip and drag `Imbabot.app` to Applications.

**First launch (unsigned/ad-hoc app):** macOS Gatekeeper will block it the first
time. Either **right-click the app → Open** (then confirm), or run:
```bash
xattr -dr com.apple.quarantine /path/to/Imbabot.app
```

### Windows — build on Windows (you can't from a Mac)
- **GitHub Actions (easiest):** push this repo; the workflow
  (`.github/workflows/build-exe.yml`) builds **both** the Windows `.exe` and the
  macOS `.app` on real runners and uploads them as artifacts. No Windows PC needed.
- **On a Windows PC:** `./build_windows.ps1` → `dist\Imbabot.exe`.

---

## Sharing it (download links for other people)

> **Sharing via OneDrive instead?** If you hand out a `Imbabot-Download.zip` link rather than a
> GitHub Release, see [UPDATING.md](UPDATING.md) for the one-command `publish_update.ps1` flow that
> rebuilds and overwrites that zip in place (same link, no re-share).

Builds are published as **GitHub Release** assets, which give permanent links anyone
can download:

1. Create a repo and push this folder:
   ```bash
   git init && git add . && git commit -m "Imbabot"
   git branch -M main
   git remote add origin https://github.com/<you>/imbabot.git
   git push -u origin main
   ```
2. Tag a version — that triggers `.github/workflows/release.yml`, which builds the
   Windows `.exe` and macOS `.app` on real runners and attaches them to a Release:
   ```bash
   git tag v0.1.0 && git push origin v0.1.0
   ```
3. Share the links from the repo's **Releases** page, e.g.
   `https://github.com/<you>/imbabot/releases/download/v0.1.0/Imbabot.exe`.

**These builds are unsigned**, so the first launch on someone else's machine shows a
security prompt. Tell recipients:
- **macOS:** right-click the app → **Open** → Open (once), or
  `xattr -dr com.apple.quarantine /path/to/Imbabot.app`.
- **Windows:** "Windows protected your PC" → **More info → Run anyway**.

To remove those prompts entirely you'd need paid code-signing (Apple Developer
$99/yr for notarization; a Windows signing cert). Not required — just nicer.

---

## Tradovate backend (0.2.5+, second broker)

A second, parallel execution venue next to TopstepX: a direct Tradovate API
connection (REST + WebSocket). Same strategy, same engine, same OCO monitor —
only the execution layer differs. Pick **Tradovate** in the Connect tab's
backend selector.

**Endpoints & safety.** DEMO (`demo.tradovateapi.com`) is the default and the
only endpoint this build will connect to: the LIVE endpoint is hard-gated by
`imbabot/tradovate/safety.py` (`LIVE_TRADING = False`, a source-level constant —
not a setting). **Guard parity with TopStep:** the Tradovate path runs under the
exact same guards as the TopStep path — the `max_contracts` setting, RiskGuard's
trades-per-day limit, and `dry_run` (ON by default) — so the same 4–5 contract
strategy executes unchanged on demo. Optional venue caps (`MAX_POSITION_SIZE`,
`MAX_DAILY_LOSS` kill switch) exist in `safety.py` but ship **disabled** (None);
set values there to re-enable them. Configure Tradovate's own account-level risk
settings as the platform-side backstop.

**Brackets are native.** On Tradovate the bot places entry stops as server-side
OSO orders (bracket SL/TP live at the venue and survive a crash or disconnect).
There is no TopStep-style "Position Brackets" backstop, so keep the bot
SL/TP toggles ON for Tradovate — a naked entry there is truly naked (the bot
warns loudly). Also set Tradovate's own account risk settings as the
platform-side backstop.

**Data.** Fills/orders/positions arrive over Tradovate's user-sync WebSocket;
quotes over the market-data WebSocket (requires a CME market-data subscription
valid for API usage). The overnight-range panel and history probes are not
supported on this backend yet (0.2.5 limitation — the Morning Plan analyzer
stays Databento-backed and works regardless of backend).

### Tradovate onboarding (one-time, before the demo check)
1. Tradovate account that can log in at trader.tradovate.com (demo is fine).
2. Buy the **API Access** add-on (Application Settings → API Access, ~$25/mo).
3. Generate an API key there: record the **cid** and **secret**; register the
   app name (default `Imbabot`).
4. Add the **CME market-data subscription** for API usage (quotes won't flow
   without it).
5. Log in once via the Tradovate website from the computer that will run the
   bot (satisfies device/captcha verification so API logins aren't challenged).
6. In Imbabot's Connect tab pick **Tradovate**, enter username / password /
   cid / secret yourself. "Remember on this device" stores them in the Windows
   Credential Manager (never in files, never in the log). Headless alternative:
   `IMBABOT_TDV_USER/PASSWORD/CID/SEC` environment variables.

### Demo integration check (required before anything else)
```
python scripts/tdv_demo_check.py            # read-only-ish: far-from-market order, then cancel
python scripts/tdv_demo_check.py --fill     # optionally also fill + liquidate 1 lot on demo
```
All steps must PASS (auth, sockets, contract, quote, OSO place/modify/cancel,
forced-reconnect resync). Then rehearse the engine itself on demo: Test tab →
auto-fire at a quiet time, 1 contract, dry-run first, then a real demo fire.

### Going live on Tradovate (deliberate, later)
Only after the demo check and an engine rehearsal pass: edit
`imbabot/tradovate/safety.py` → `LIVE_TRADING = True`, **decide the venue caps**
(`MAX_POSITION_SIZE` / `MAX_DAILY_LOSS` are None for demo parity — on a personal
account there is no prop-firm daily-loss net above you, so consider setting
real values), re-run `python -m imbabot.cli selftest`, switch Environment to
**live** in the Connect tab, and confirm the red **TDV LIVE** badge + startup
banner. The account roles are: **TopStep PRAC = testing**, **Tradovate = live
capital** — never flip live on a whim; the gate is in source on purpose.

## Browser backend (fallback)

If you don't want the API add-on, Imbabot can drive the trading site in a real
browser instead — same opening-range strategy, executed by clicking the chart.
This mirrors the original product (a dedicated browser per platform; you log in
yourself, then Arm).

**Honest trade-offs vs the API:** browser automation is slower, more fragile (a UI
change can break it), and — as a third-party tool placing orders — exactly the kind
of automation prop firms scrutinize. Prefer the API where you can. Browser mode is
the fallback.

### How it works
- **Selector packs.** Each site is described by a JSON "selector pack"
  (`imbabot/browser/selectors/*.json`): where the price is, where your net position
  is, and the click/type steps to place a stop, cancel one, cancel all, and flatten.
  One generic adapter runs any pack, so the framework is tested against a mock page
  and you calibrate JSON for the real site — no code changes.
- **Drivers.** The default driver is **Selenium**, which drives your **installed
  Google Chrome** and *bundles into the packaged `.exe`/`.app`* — so the Launch
  button works for people who just download the app (nothing to install but Chrome).
  Playwright is an alternative driver for running from source (`browser_driver`
  setting). Both present the same interface, so the selector packs are identical.
- **One dedicated thread** owns the browser, so the UI stays responsive and orders
  fire tick-accurately at the open.
- A persistent, **isolated browser profile per platform** keeps you logged in
  between runs.

### Setup
- **Packaged app:** nothing to install except **Google Chrome**. On first launch
  the bundled `selenium-manager` downloads a matching `chromedriver` (needs internet
  once). Verify it can drive Chrome with:
  `Imbabot.app/Contents/MacOS/Imbabot selenium-smoke` (macOS) — expect `SELENIUM_SMOKE_OK`.
- **From source:** `pip install selenium` (or `pip install playwright && playwright
  install chromium` to use the Playwright driver instead).

### Run it
- **GUI:** pick **Browser automation** in the Backend panel, choose the platform,
  click **Launch Browser**, log in, then **Arm**.
- **CLI:**
  ```bash
  python -m imbabot.cli browser-run --platform projectx              # dry-run
  python -m imbabot.cli browser-run --platform projectx --live-orders # ⚠ real orders
  ```

### Calibrating a selector pack (required for real sites)
**`projectx.json` (TopstepX) ships pre-calibrated and live-tested** (2026-06-04, against
a practice account): price is read from the page title, orders are placed by real
keystrokes into the order card + clicking BUY/SELL (verified to submit at the typed
price with no confirmation dialog). Prereqs on the chart: set Order Type to **"Stop
Market"** and configure your **Position Bracket (SL/TP)** in TopStep — there are no
per-order SL/TP fields. **Use Semi-Auto** until you sim-verify One-Trade (its
fill-detection isn't confirmed yet). `tradesea.json` is still placeholder.

To (re)calibrate any site — e.g. if TopStep changes its UI:

**Easy — the point-and-click recorder (recommended):**
```bash
python -m imbabot.cli browser-calibrate projectx
```
Chrome opens; log into TopStep (a **sim/eval** account, ideally while flat) and open
your chart + order ticket + positions. The recorder highlights elements; click the
one it asks for (price, position, Buy, Sell, qty, submit, …). **Clicks are blocked
while picking, so nothing gets ordered.** It writes the calibrated pack to your
config dir (overrides the bundled one — no rebuild needed). Re-run anytime TopStep
changes its UI.

**Manual:** `python -m imbabot.cli browser-inspect projectx`, find selectors with
DevTools (Cmd/Ctrl+Shift+C), and edit `imbabot/browser/selectors/projectx.json` —
replace each `CALIBRATE: …`. Order-step variables: `$trigger_price $size $sl_points
$tp_points $sl_ticks $tp_ticks $side`.

Either way: verify price/position read correctly, then test in **dry-run** → **sim**
→ 1 micro contract. The `cancel_by_price` step (One-Trade OCO) may need a manual
touch — **Semi-Auto** works without it.

> For browser mode, **Semi-Auto is the safe default** (you manage the orders). The
> One-Trade auto-cancel needs the `cancel_by_price` step calibrated so it cancels
> only the opposite *entry*, not your protective stop. The mock page proves the
> mechanic (`python tests/test_browser_mock.py`).

### Packaging note
Browser mode **works in the packaged app**: the Selenium driver + its
`selenium-manager` are bundled (the `.app` is ~34 MB). It drives the user's own
Chrome, so no browser binary is shipped. Requirements for the downloaded app: Google
Chrome installed, and internet on first run (to fetch `chromedriver`). Playwright is
*not* bundled — it's only for the from-source driver option.

---

## Configuration reference

Settings live in a JSON file in your OS config dir (the GUI writes it for you):

| Key | Default | Meaning |
|-----|---------|---------|
| `contract_symbol` | `MNQ` | Instrument to trade |
| `entry_points` | `12` | Distance above/below reference for the two entries |
| `stop_loss_points` | `12` | Stop distance — used **only** when `bot_stop_loss` is on (else TopStep owns the SL) |
| `take_profit_points` | `12` | Target distance — used **only** when `bot_take_profit` is on (else TopStep owns the TP) |
| `bot_stop_loss` | `false` | `true` = bot attaches its own stop bracket (needs Auto OCO Brackets on TopStep) |
| `bot_take_profit` | `false` | `true` = bot attaches its own target bracket (needs Auto OCO Brackets on TopStep) |
| `contracts` | `2` | Size per leg |
| `trade_mode` | `semi_auto` | `semi_auto`, `one_trade`, or `two_trade` |
| `open_hour` / `open_minute` | `9` / `30` | Cash open (ET) — used when no `strategy_fire_time` is set |
| `capture_offset_seconds` | `3` | Capture price this many seconds before the open |
| `strategy_fire_time` | `""` | Daily weekday fire time (`HH:MM:SS`, local clock). Empty = use the 09:30 open |
| `test_mode` / `test_fire_time` | `false` / `""` | One-shot custom-time fire (Test tab), local clock |
| `use_live_data` | `false` | `false` = sim data subscription |
| `dry_run` | `true` | **`true` = never sends orders** |
| `max_contracts` | `5` | Hard client-side size cap |
| `max_trades_per_day` | `1` | Client-side daily trade guard |

Your **API key is never stored in this file** — it goes to the OS keychain
(`keyring`) or a `0600` `credentials` file, and is never logged.

---

## TopStep account setup (API backend)

For the **API backend** (the recommended path), set these up on TopstepX once:

- **API access enabled + an API key** — the paid ProjectX add-on. Stored in your
  OS keychain, never in the settings file.
- **"Position Brackets" mode enabled** (the default, recommended path), with your
  **Stop-Loss / Take-Profit set on TopStep**. The bot places only the two naked entry
  stops (one BUY above, one SELL below); when a leg fills, TopStep attaches your
  configured SL/TP to the position. In this mode, do **not** also enable the bot's own
  brackets (`bot_stop_loss` / `bot_take_profit`) — a naked-entry fill is protected by
  TopStep. *(Only if you instead enable the bot's brackets on the Strategy tab do you
  switch TopStep to **Auto OCO Brackets** mode; mixing bot brackets with Position
  Brackets is rejected, and earlier builds that always attached brackets produced four
  resting orders.)*
- A **tradable account** (`canTrade = true`) that **allows automation** — Trading
  Combine, Practice, or Funded-eval. Automation is **prohibited on a Live Funded**
  account.
- **Risk settings matched to your contract count.** Your *Personal Daily Loss Limit*
  (PDLL) and *Daily Profit Target* (PDPT) are account-level daily backstops, separate
  from the per-trade Position Bracket — the bot never touches either.

Sizing the Position Bracket in dollars — **NQ** is **$20 / point** ($5 / tick) per
contract; **MNQ** is **$2 / point** ($0.50 / tick). So on NQ, a **$500 take-profit =
25 points (100 ticks)** and a **$300 stop = 15 points (60 ticks)** per contract
(divide by your contract count if TopStep sizes the bracket per position rather than
per contract).

**No TopStep screens need to be open.** Orders are placed server-side over the API —
you don't need the chart, order ticket, or any TopStep window open for the bot to
fire. (A chart is optional, just to watch.) This is the main advantage over the
browser backend, which *does* require the chart open and the stop selected.

**Trade modes:** *Semi-Auto* (you manage both orders), *One-Trade* (auto-cancels the
opposite entry on a fill), and *Two-Trade* (leaves both working — set your platform
**trade limit to 2/day** so both can fill).

## Safety net (do this)

The bot's guards are a backup. Also set, **on the TopstepX platform**:
- **Daily loss limit** = your intended risk, with **liquidate** enabled.
- **Trade limit** = **1/day** (One-Trade/Semi-Auto) or **2/day** (Two-Trade) to
  prevent an accidental extra entry.
- A manual stop on platforms where the bracket may not display.

---

## How it's built

```
imbabot/
  models.py      enums, dataclasses, tick math      (no deps — unit-tested)
  strategy.py    builds the straddle plan            (no deps — unit-tested)
  scheduler.py   next-fire timing + countdown
  config.py      settings + secure credential storage
  risk.py        client-side guardrails
  logbus.py      shared timestamped logger
  projectx.py    ProjectX/TopstepX REST client       ┐ API backend
  engine.py      orchestration: connect/arm/fire/OCO  ┘
  browser/                                            ┐ Browser backend (fallback)
    base.py      pack-driven adapter + action runner  │
    drivers.py   Selenium (bundled) / Playwright shim │
    engine.py    BrowserEngine + threaded Controller  │
    selectors/   per-site JSON selector packs         ┘
  cli.py         headless control + `selftest` + browser-run/inspect
  gui.py         Tkinter dashboard  →  the .exe
  _fake.py       in-memory broker for offline tests
tests/           run_all.py + API-client, browser-mock, browser-controller suites
```

Run everything with `python tests/run_all.py`. Coverage (95 checks):
- `imbabot.cli selftest` — 62 offline engine checks (strategy, naked + bot-managed
  brackets, OCO, weekday/test scheduling, risk, panic, flatten).
- `tests/test_projectx_client.py` — 4 HTTP request-shaping checks (API layer).
- `tests/test_browser_mock.py` — 13 checks: the real browser adapter+engine driving
  a headless Chromium through capture→place→fill→cancel→flatten.
- `tests/test_browser_controller.py` — 6 checks: the threaded controller launches,
  polls, arms, fires on schedule, and shuts down cleanly.
- `tests/test_browser_selenium.py` — 10 checks: the same flow on the **Selenium**
  driver in real Chrome (the path that bundles into the .exe/.app).

---

## Troubleshooting

- **"No time zone found" on Windows** → `pip install tzdata` (already in
  requirements; the spec bundles it into the exe).
- **401 / token expired** → tokens last ~24h; click Connect again.
- **Position/fill not detected in One-Trade mode** → field names can vary slightly
  between ProjectX firms; run once in sim and check the log. TopStep's Position
  Bracket still protects the filled side even if the opposite-entry cancel is delayed.
- **Orders rejected** → check `canTrade` on the account, contract size vs your risk
  settings, and that the market is open.

Logs are written to the config dir and can be saved from the GUI (**Save log…**).
