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

1. A few seconds before 09:30 ET (default **09:29:57**) it captures a reference price.
2. It places two **stop-entry** orders:
   - **BUY stop** `entry_points` above the reference (long breakout)
   - **SELL stop** `entry_points` below the reference (short breakout)
   - each with an attached **stop-loss** and **take-profit** bracket.
3. Mode controls what happens next:
   - **Semi-Auto** — both orders are placed; *you* manage/cancel them.
   - **One-Trade** — whichever side fills first stays; the bot **cancels the other
     entry automatically** (one-cancels-the-other).
4. An **Emergency Stop** cancels all working orders and flattens all positions.

Defaults: `MNQ` (Micro Nasdaq), ±12 points, 2 contracts, **dry-run ON**.

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
You should see `28 passed, 0 failed`.

### 4. Run the GUI
```bash
python run.py
# or:  python -m imbabot
```
Connect → pick account → set symbol & strategy → **leave dry-run ON** for your
first sessions and watch it work.

---

## Daily routine (mirrors the guide)

1. Launch Imbabot; **Connect**.
2. Pick the **account** and **symbol**; click **Resolve** to confirm the contract.
3. Set **points / contracts / mode**, click **Save**. Match your TopstepX risk
   settings to your contract count.
4. Check the dashboard (last price, overnight range, countdown).
5. **Arm.** It captures price at 09:29:57 and places orders; a countdown shows the
   next fire time so it can't trigger early.
6. Emergency Stop is always one click away.

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
| `stop_loss_points` | `12` | Protective stop distance from fill |
| `take_profit_points` | `12` | Target distance from fill |
| `contracts` | `2` | Size per leg |
| `trade_mode` | `semi_auto` | `semi_auto` or `one_trade` |
| `open_hour` / `open_minute` | `9` / `30` | Cash open (ET) |
| `capture_offset_seconds` | `3` | Capture price this many seconds before the open |
| `use_live_data` | `false` | `false` = sim data subscription |
| `dry_run` | `true` | **`true` = never sends orders** |
| `max_contracts` | `5` | Hard client-side size cap |
| `max_trades_per_day` | `1` | Client-side daily trade guard |

Your **API key is never stored in this file** — it goes to the OS keychain
(`keyring`) or a `0600` `credentials` file, and is never logged.

---

## Safety net (do this)

The bot's guards are a backup. Also set, **on the TopstepX platform**:
- **Daily loss limit** = your intended risk, with **liquidate** enabled.
- **Trade limit = 1/day** to prevent an accidental second entry.
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

Run everything with `python tests/run_all.py`. Coverage (61 checks):
- `imbabot.cli selftest` — 28 offline engine checks (strategy, OCO, risk, panic).
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
  between ProjectX firms; run once in sim and check the log. The per-leg brackets
  still bound risk even if the opposite-entry cancel is delayed.
- **Orders rejected** → check `canTrade` on the account, contract size vs your risk
  settings, and that the market is open.

Logs are written to the config dir and can be saved from the GUI (**Save log…**).
