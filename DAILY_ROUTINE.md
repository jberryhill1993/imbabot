# Imbabot — Daily Routine (one-page cheat sheet)

## Which bot is which
- **`Imbabot.exe`** (title bar `0.2.0.1`) — your **stable** bot for **funded accounts**. Don't change it.
- **`Imbabot-Test.exe`** (title bar `0.2.1-dev`) — the **experiment**, for the **practice account only**.
  It keeps its own settings/login (separate from the stable bot), so the two never interfere.

> This routine is for the **Test bot**. Right now the goal is **not profit** — it's collecting real
> stop-limit fill data so we can learn whether the strategy can actually work after costs.

## What the bot does (plain English)
1. A few seconds before the open (08:29:57 CT) it **captures the current NQ price**.
2. It places **two orders**: a buy that many points **above** that price, and a sell that many points
   **below** it (your "entry points" / spread). Whichever way the open breaks, one triggers.
3. **The moment one fills, it cancels the other** — so you only ever get **one** trade that day.
4. **TopStep's bracket runs the exit** (your $ take-profit and stop-loss, attached automatically).
5. With **Stop-limit entries ON**, the entries won't fill more than ~1 point past their trigger —
   this caps the slippage that was bleeding the strategy.

## About the "Morning Plan" panel — read this once
The Morning Plan's daily TRADE/SKIP and spread numbers **did not hold up in honest testing** (no real
predictive edge). So **do not change your settings each morning based on it.** Glance at it only for
context (is today unusually choppy / a big event?). Changing settings daily would also ruin the
forward-test. **Keep your settings fixed.**

## Recommended FIXED settings (set once, then leave alone)
**Test bot — Strategy tab:**
- Entry points (±): **12**
- Contracts: **your choice (e.g. 3)** — must match the $ brackets below
- Mode: **One-Trade (auto OCO)**
- **Stop-limit entries: ON**, offset **4**
- Dry-run: **OFF** (it's the practice account)

**TopStepX — Risk Settings (Position Brackets, in dollars):**
- Take-profit: **$800**
- Stop-loss: **~$720** (widen from $500 — your old stop was too tight)
- Stop bracket type: **stop-market**
- Bracket mode: **Position Brackets** (NOT Auto OCO)

**Dollar ↔ point math:** $20 × contracts = $ per point. At **3 contracts**: $720 = 12 pts, $800 ≈ 13 pts.
If you change contracts, recompute the dollar amounts to keep the same point distance.

## Every morning — order of operations
1. **~20 min before 8:30 CT:** open **`Imbabot-Test.exe`** → **Connect** (practice account).
2. **Confirm the fixed settings above are still set.** Don't change them day to day.
3. **Confirm TopStep brackets:** TP $800 / SL ~$720, stop-market, Position Brackets, sized to contracts.
4. *(Optional)* Click **Recalculate now** — glance at it for context only. **Do not adjust settings.**
5. **Arm** (or rely on the daily 8:31 schedule).
6. **08:29:57:** the bot captures the price and places the two stop-limit entries.
7. **One fills → the other is cancelled automatically.** TopStep's bracket handles the exit. One trade.
8. **Weekly:** in TopStep, export your **orders** and send the file — we measure your real fill rate +
   slippage. That data tells us if the stop-limit edge is real.

## First-run check
On the **first** live fire, watch the activity log. If you see a **rejection** mentioning the order
type, the stop-limit order code may need a tweak — tell me and I'll fix it. Nothing dangerous happens;
a rejected order simply doesn't place.
