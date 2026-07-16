/* Imbabot glass dashboard — Phase 2: wired to the Python engine via pywebview js_api.
   Every action mirrors the classic Tkinter GUI's handler; confirm() dialogs replace
   the messagebox prompts. The API key is write-only: sent on Connect, never echoed. */

"use strict";

const $ = (id) => document.getElementById(id);
let API = null;          // window.pywebview.api once ready
let logCursor = 0;
let backend = "api";     // segmented control state

/* ---------------------------------------------------------------- helpers */
function setChg(prefix, chg, pct, invert = false) {
  const good = invert ? chg < 0 : chg > 0;
  const cls = chg === 0 ? "flat" : good ? "up" : "down";
  const arrow = chg > 0 ? "▲" : chg < 0 ? "▼" : "■";
  const el = $(prefix + "-chg");
  el.classList.remove("up", "down", "flat");
  el.classList.add(cls);
  $(prefix + "-chg-pts").textContent = `${arrow} ${chg >= 0 ? "+" : ""}${chg.toFixed(2)}`;
  $(prefix + "-chg-pct").textContent = `(${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)`;
}

function setPill(id, kind, txt, dotCls) {
  const pill = $(id);
  pill.classList.remove("ok", "bad", "warn");
  if (kind) pill.classList.add(kind);
  $(id + "-txt").textContent = txt;
  const dot = pill.querySelector(".dot");
  dot.className = "dot " + dotCls;
}

function money(v) { return "$" + Math.round(v).toLocaleString("en-US"); }

/* collectSettings(): mirrors gui._collect_settings — keys match bridge._SETTINGS_FIELDS */
function collectSettings() {
  return {
    backend: backend,
    browser_platform: $("f-platform").value,
    test_mode: $("f-test-mode").checked,
    test_fire_time: $("f-test-time").value.trim(),
    strategy_fire_time: $("f-daily-time").value.trim(),
    base_url: $("f-base").value.trim(),
    username: $("f-user").value.trim(),
    contract_symbol: $("f-symbol").value.trim().toUpperCase(),
    entry_points: parseFloat($("f-entry").value),
    stop_loss_points: parseFloat($("f-sl").value),
    take_profit_points: parseFloat($("f-tp").value),
    contracts: parseInt($("f-contracts").value, 10),
    bot_stop_loss: $("f-botsl").checked,
    bot_take_profit: $("f-bottp").checked,
    trade_mode: $("f-oco").checked ? "one_trade" : "semi_auto",
    entry_order_type: $("f-stoplimit").checked ? "stop_limit" : "stop",
    entry_limit_offset_ticks: parseInt($("f-limitoff").value, 10),
    use_live_data: $("f-livedata").checked,
    dry_run: $("f-dry").checked,
  };
}

function liveSummary(p) {
  return `${p.contract_symbol}  ±${p.entry_points}pt  x${p.contracts}  mode=${p.trade_mode}`;
}

/* ---------------------------------------------------------------- tabs */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("pane-" + tab.dataset.tab).classList.add("active");
  });
});

/* backend segmented control */
document.querySelectorAll("#seg-backend button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#seg-backend button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    backend = b.dataset.val;
    $("btn-connect").textContent = backend === "browser" ? "Launch Browser" : "Connect";
  });
});

/* collapsible log */
$("log-head").addEventListener("click", (e) => {
  if (e.target.id === "btn-savelog") return;
  $("log-card").classList.toggle("open");
});

/* dry-run OFF needs a confirm (gui._on_dry_toggle parity) */
$("f-dry").addEventListener("change", () => {
  if (!$("f-dry").checked &&
      !confirm("Disable dry-run?\n\nThis allows the bot to place REAL orders on your account.\n\nProceed?")) {
    $("f-dry").checked = true;
  }
});

/* ---------------------------------------------------------------- actions */
async function withApi(fn) {
  if (!API) { alert("Engine bridge not ready yet — try again in a second."); return; }
  try { return await fn(); }
  catch (e) { alert("Error: " + e); }
}

$("btn-connect").addEventListener("click", () => withApi(async () => {
  $("conn-status").textContent = backend === "browser" ? "launching browser…" : "connecting…";
  $("btn-connect").disabled = true;
  const r = await API.connect(collectSettings(), $("f-key").value, $("f-remember").checked);
  $("btn-connect").disabled = false;
  if (!r.ok) { $("conn-status").textContent = "connect failed"; alert(r.error); return; }
  $("f-key").value = "";                       // never keep the key in the DOM
  $("f-key").placeholder = "stored on this device";
  if (r.browser) { $("conn-status").textContent = "browser launching — log in, then Arm"; return; }
  $("conn-status").textContent = "connected";
  $("conn-status").className = "up";
  const sel = $("f-account");
  sel.innerHTML = "";
  r.accounts.forEach((a) => {
    const o = document.createElement("option");
    o.value = a.id;
    o.textContent = `${a.name} (id=${a.id})${a.can_trade ? "" : " [locked]"}`;
    if (a.id === r.account_id) o.selected = true;
    sel.appendChild(o);
  });
  $("contract-info").textContent = r.contract;
}));

$("f-account").addEventListener("change", () => withApi(async () => {
  const id = parseInt($("f-account").value, 10);
  if (!Number.isNaN(id)) {
    const r = await API.pick_account(id);
    if (!r.ok) alert(r.error);
  }
}));

$("btn-save").addEventListener("click", () => withApi(async () => {
  const r = await API.save_settings(collectSettings());
  if (!r.ok) { alert(r.error); return; }
  if (r.contract) $("contract-info").textContent = r.contract;
}));

$("btn-arm").addEventListener("click", () => withApi(async () => {
  const p = collectSettings();
  const arming = $("btn-arm").textContent.trim() === "ARM";
  if (arming && !p.dry_run &&
      !confirm(`Arm in LIVE mode?\n\n${liveSummary(p)}\n\nReal orders will be sent at the open.`)) return;
  const r = await API.arm(p);
  if (!r.ok) alert(r.error);
}));

$("btn-flatten").addEventListener("click", () => withApi(async () => {
  const r = await API.flatten();
  if (!r.ok) alert(r.error);
}));

$("btn-stop").addEventListener("click", () => withApi(async () => {
  const r = await API.emergency_stop();
  if (!r.ok) alert(r.error);
}));

$("btn-daily").addEventListener("click", () => withApi(async () => {
  const p = collectSettings();
  const hms = $("f-daily-time").value.trim();
  const cancelling = $("btn-daily").textContent.includes("Cancel");
  if (!cancelling && !p.dry_run &&
      !confirm(`Fire LIVE every weekday at ${hms} (your computer's clock)?\n\n${liveSummary(p)}\n\n` +
               "Real orders will be sent automatically each weekday until you DISARM.")) return;
  const r = await API.schedule_daily(p, hms);
  if (!r.ok) { alert(r.error); return; }
  if (r.armed && r.first_fire) alert(`Daily schedule armed — first fire ${r.first_fire}, then every weekday at ${hms}.`);
}));

$("btn-autofire").addEventListener("click", () => withApi(async () => {
  const p = collectSettings();
  const hms = $("f-test-time").value.trim();
  const cancelling = $("btn-autofire").textContent.includes("Cancel");
  if (!cancelling) {
    const pv = await API.preview_test_time(hms);
    if (!pv.ok) { alert(pv.error); return; }
    if (pv.fires_tomorrow &&
        !confirm(`${hms} has already passed on your computer's clock (${pv.now}).\n\n` +
                 `This will fire TOMORROW — ${pv.first_fire}.\n\nSchedule it for tomorrow?`)) return;
    if (!p.dry_run &&
        !confirm(`Auto-fire in LIVE mode at ${hms} (your computer's clock)?\n\n${liveSummary(p)}\n\n` +
                 "Real orders will be sent at that time.")) return;
  }
  const r = await API.schedule_test(p, hms);
  if (!r.ok) alert(r.error);
}));

$("btn-fire-now").addEventListener("click", () => withApi(async () => {
  if (!confirm("Run the fire sequence RIGHT NOW (places the straddle)?\n\n" +
               "Use a SIM/practice account. If DRY-RUN is on it only logs the plan; " +
               "otherwise it places real orders. Cancel/flatten with Emergency Stop after.")) return;
  const r = await API.fire_test_now(collectSettings());
  if (!r.ok) alert(r.error);
}));

$("btn-savelog").addEventListener("click", () => withApi(async () => {
  const r = await API.save_log();
  if (!r.ok && r.error !== "cancelled") alert(r.error);
}));

/* ------------------------------------------------------------ Morning Plan */
$("btn-recalc").addEventListener("click", () => withApi(async () => {
  $("btn-recalc").disabled = true;
  $("mp-status").textContent = "… calculating …";
  const r = await API.recalc_morning(parseFloat($("f-mp-target").value) || 800);
  $("btn-recalc").disabled = false;
  if (!r.ok) { $("mp-status").textContent = "Morning Plan error: " + r.error; return; }
  renderPlan(r.plan);
}));

function renderPlan(mp) {
  /* mirrors gui._show_morning_plan content exactly */
  const tag = mp.decision === "TRADE" ? "✅ TRADE" : "⛔ NO-TRADE";
  const sess = new Date(mp.session_date + "T12:00:00")
    .toLocaleDateString("en-US", { weekday: "short", month: "short", day: "2-digit" });
  const banner = mp.market_closed_today ? "  ·  ⚠ MARKET CLOSED TODAY — plan is for the NEXT session" : "";
  const cal = mp.calibrated ? "" : "  ·  UNCALIBRATED";
  $("mp-status").textContent =
    `${tag} · ${mp.conviction} · Session: ${sess} · Vol ${mp.volatility} · ` +
    `spike ~${Math.round(mp.predicted_spike)}pt · P(30+)=${Math.round(mp.p_big * 100)}%${banner}${cal}`;

  const p = mp.plan;
  if (mp.decision === "TRADE" && p.feasible) {
    $("mp-headline").textContent = "➡ ENTER IN TOPSTEP";
    $("mp-headline").classList.remove("notrade");
    $("mp-headline").style.display = "";
    $("mp-cells").style.display = "";
    $("mp-ct").textContent = p.contracts;
    $("mp-entry").textContent = "±" + Math.round(p.entry_spread);
    $("mp-tp").textContent = money(p.achievable_dollars);
    $("mp-sl").textContent = money(p.sl_bracket_dollars);
    const sized = p.capped
      ? `Your ${money(p.target_dollars)} target exceeds the ${p.max_contracts}-contract max — sized to the max instead`
      : `Sized to your ${money(p.target_dollars)} target`;
    $("mp-sizing").innerHTML =
      `${sized} — TP ${Math.round(p.tp_distance_points)}pt, reachable inside the ~1-second opening spike.<br>` +
      `Today's MAX at ${p.max_contracts} contracts:  Take-Profit ${money(p.recommended_tp_dollars)}  ·  ` +
      `Stop-Loss ${money(p.recommended_sl_dollars)}.`;
    if (p.capped) {
      $("mp-alert").textContent =
        `⚠ Your ${money(p.target_dollars)} target needs ~${p.contracts_wanted} contracts — capped at ` +
        `${p.max_contracts}. The max at ${p.max_contracts} contracts today is ${money(p.recommended_tp_dollars)} — use that.`;
      $("mp-alert").style.display = "";
    } else {
      $("mp-alert").style.display = "none";
    }
  } else {
    $("mp-headline").textContent = "➡ NO-TRADE — sit out today";
    $("mp-headline").classList.add("notrade");
    $("mp-headline").style.display = "";
    $("mp-cells").style.display = "none";
    $("mp-sizing").textContent = "";
    $("mp-alert").style.display = "none";
  }
  const vix = mp.prior_vix ? `VIX ${mp.prior_vix.toFixed(1)} (prior close)` : "VIX n/a";
  const gap = mp.overnight_gap != null
    ? `Gap ${Math.round(mp.overnight_gap)}pt${mp.gap_fresh ? "" : " (early ⚠)"}` : "Gap n/a";
  $("mp-detail").innerHTML = `${vix}  ·  ${gap}  ·  News: ${mp.news_label}<br>${mp.rationale}`;
}

/* ------------------------------------------------------------ state poll */
const SWEEP_LEN = 188.5;
function applyState(st) {
  $("stat-tminus").textContent = st.countdown;
  $("stat-fire").textContent = st.next_fire;
  const secs = parseInt((st.countdown || "0:0:0").split(":").pop(), 10) || 0;
  $("ring-sweep").style.strokeDashoffset = String(SWEEP_LEN * (1 - secs / 60));
  if (st.nq) { $("nq-sym").textContent = st.nq.symbol; $("nq-px").textContent = st.nq.price.toLocaleString("en-US", {minimumFractionDigits: 2}); setChg("nq", st.nq.chg, st.nq.pct); }
  if (st.vix) { $("vix-px").textContent = st.vix.price.toFixed(2); setChg("vix", st.vix.chg, st.vix.pct, true); }
  $("stat-price").textContent = st.last_price != null
    ? st.last_price.toLocaleString("en-US", {minimumFractionDigits: 2}) : "—";
  $("stat-range").textContent = st.range
    ? `${st.range.low.toLocaleString("en-US", {minimumFractionDigits: 1})}–${st.range.high.toLocaleString("en-US", {minimumFractionDigits: 1})}` : "—";

  setPill("pill-conn", st.connected ? "ok" : "", st.connected ? "CONNECTED" : "OFFLINE",
          st.connected ? "green" : "gray");
  setPill("pill-live", st.dry_run ? "ok" : "bad", st.dry_run ? "DRY-RUN" : "LIVE",
          st.dry_run ? "green" : "red pulse");
  setPill("pill-armed", st.armed ? "warn" : "", st.armed ? "ARMED" : "DISARMED",
          st.armed ? "green pulse" : "gray");

  $("mode-note").textContent = st.dry_run ? "● DRY-RUN — no orders sent" : "● LIVE — REAL ORDERS";
  $("mode-note").style.color = st.dry_run ? "var(--green)" : "var(--red)";
  $("btn-arm").textContent = st.armed ? "DISARM" : "ARM";
  $("btn-daily").textContent = st.connected && st.armed
    ? "✖ Cancel daily schedule" : "💾 Save & arm daily (Mon–Fri)";
  $("btn-autofire").textContent = st.connected && st.armed
    ? "✖ Cancel auto-fire" : "💾 Save & schedule auto-fire";

  if (st.log && st.log.length) {
    const body = $("log-body");
    st.log.forEach((e) => {
      const div = document.createElement("div");
      div.className = "log-line " + e.level;
      div.innerHTML = `<span class="t">[${e.ts}]</span> ${e.msg.replace(/&/g, "&amp;").replace(/</g, "&lt;")}`;
      body.appendChild(div);
    });
    while (body.children.length > 800) body.removeChild(body.firstChild);
    body.scrollTop = body.scrollHeight;
    logCursor = st.seq;
  }
}

async function poll() {
  if (API) {
    try { applyState(await API.get_state(logCursor)); } catch (e) { /* transient */ }
  }
  setTimeout(poll, 1000);
}

/* ------------------------------------------------------------------ init */
async function init() {
  API = window.pywebview.api;
  const s = await API.get_settings();
  $("f-platform").value = s.browser_platform;
  $("f-base").value = s.base_url;
  $("f-user").value = s.username;
  $("f-key").placeholder = s.has_key ? "stored on this device" : "enter your API key";
  $("f-symbol").value = s.contract_symbol;
  $("f-entry").value = s.entry_points;
  $("f-sl").value = s.stop_loss_points;
  $("f-tp").value = s.take_profit_points;
  $("f-contracts").value = s.contracts;
  $("f-daily-time").value = s.strategy_fire_time;
  $("f-botsl").checked = s.bot_stop_loss;
  $("f-bottp").checked = s.bot_take_profit;
  $("f-oco").checked = s.trade_mode === "one_trade";
  $("f-dry").checked = s.dry_run;
  $("f-stoplimit").checked = s.entry_order_type === "stop_limit";
  $("f-limitoff").value = s.entry_limit_offset_ticks;
  $("f-livedata").checked = s.use_live_data;
  $("f-test-mode").checked = s.test_mode;
  $("f-test-time").value = s.test_fire_time;
  backend = s.backend || "api";
  document.querySelectorAll("#seg-backend button").forEach((b) =>
    b.classList.toggle("active", b.dataset.val === backend));
  $("btn-connect").textContent = backend === "browser" ? "Launch Browser" : "Connect";
  poll();
}

if (window.pywebview && window.pywebview.api) init();
else window.addEventListener("pywebviewready", init);
