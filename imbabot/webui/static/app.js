/* Imbabot glass dashboard — Phase 2: wired to the Python engine via pywebview js_api.
   Every action mirrors the classic Tkinter GUI's handler; confirm() dialogs replace
   the messagebox prompts. The API key is write-only: sent on Connect, never echoed. */

"use strict";

const $ = (id) => document.getElementById(id);
let API = null;          // window.pywebview.api once ready
let logCursor = 0;
let backend = "api";     // segmented control state
let tdvEnv = "demo";     // Tradovate environment segment state
let sltpMode = "points"; // SL/TP entry mode: "points" | "dollars" (per position)
let dollarsPerPoint = null;  // $/pt per contract, from the bridge (contract-aware)

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
    // In $ mode the two inputs hold dollars-per-position; the BRIDGE converts
    // to tick-floored points (single conversion implementation, in Python).
    sl_tp_entry_mode: sltpMode,
    stop_loss_points: sltpMode === "points" ? parseFloat($("f-sl").value) : undefined,
    take_profit_points: sltpMode === "points" ? parseFloat($("f-tp").value) : undefined,
    stop_loss_dollars: sltpMode === "dollars" ? parseFloat($("f-sl").value) : 0,
    take_profit_dollars: sltpMode === "dollars" ? parseFloat($("f-tp").value) : 0,
    contracts: parseInt($("f-contracts").value, 10),
    bot_stop_loss: $("f-botsl").checked,
    bot_take_profit: $("f-bottp").checked,
    trade_mode: $("f-oco").checked ? "one_trade" : "semi_auto",
    entry_order_type: $("f-stoplimit").checked ? "stop_limit" : "stop",
    entry_limit_offset_ticks: parseInt($("f-limitoff").value, 10),
    use_live_data: $("f-livedata").checked,
    dry_run: $("f-dry").checked,
    tdv_environment: tdvEnv,
    tdv_username: $("f-tdv-user").value.trim(),
    tdv_price_source: $("f-tdv-price").value,
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
function refreshBackendUI() {
  $("btn-connect").textContent = backend === "browser" ? "Launch Browser" : "Connect";
  $("fields-api").hidden = backend === "tradovate";
  $("fields-tdv").hidden = backend !== "tradovate";
}
document.querySelectorAll("#seg-backend button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#seg-backend button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    backend = b.dataset.val;
    refreshBackendUI();
  });
});

/* Tradovate environment segment (live is source-gated; the backend refuses it
   until safety.py LIVE_TRADING is flipped — surfaced here as a warning) */
document.querySelectorAll("#seg-tdvenv button").forEach((b) => {
  b.addEventListener("click", () => {
    if (b.dataset.val === "live" &&
        !confirm("LIVE Tradovate endpoint?\n\nThis build ships with LIVE_TRADING " +
                 "disabled in safety.py, so connecting will be refused until you " +
                 "deliberately enable it in source AFTER the demo check passes.\n\nSelect live anyway?")) {
      return;
    }
    document.querySelectorAll("#seg-tdvenv button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    tdvEnv = b.dataset.val;
  });
});

/* SL/TP entry mode: points vs $ per position (TopStep Position-Brackets UX).
   The bridge does the authoritative $->points conversion; hints here preview it. */
function sltpHint(el, hintEl) {
  if (sltpMode !== "dollars") { hintEl.textContent = ""; return; }
  const usd = parseFloat(el.value);
  const ct = parseInt($("f-contracts").value, 10);
  if (!usd || !ct || !dollarsPerPoint) { hintEl.textContent = ""; return; }
  const raw = usd / (ct * dollarsPerPoint);
  const pts = Math.max(0.25, Math.floor(raw / 0.25) * 0.25);  // floor to tick
  hintEl.textContent = `$${usd} at ${ct}ct ≈ ${pts} pts (${dollarsPerPoint}$/pt)`;
}
function refreshSltpUI() {
  const dollars = sltpMode === "dollars";
  $("lbl-sl").innerHTML = dollars ? 'Stop-loss $ <span class="hint">(per position)</span>'
                                  : 'Stop-loss points <span class="hint">(bot-managed)</span>';
  $("lbl-tp").innerHTML = dollars ? 'Take-profit $ <span class="hint">(per position)</span>'
                                  : 'Take-profit points <span class="hint">(bot-managed)</span>';
  $("f-sl").step = dollars ? "10" : "0.25";
  $("f-tp").step = dollars ? "10" : "0.25";
  sltpHint($("f-sl"), $("hint-sl"));
  sltpHint($("f-tp"), $("hint-tp"));
}
document.querySelectorAll("#seg-sltp button").forEach((b) => {
  b.addEventListener("click", () => {
    if (b.dataset.val === sltpMode) return;
    document.querySelectorAll("#seg-sltp button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    const ct = parseInt($("f-contracts").value, 10) || 1;
    // convert the displayed values so the switch never silently changes risk
    const sl = parseFloat($("f-sl").value), tp = parseFloat($("f-tp").value);
    if (dollarsPerPoint && b.dataset.val === "dollars") {
      if (sl) $("f-sl").value = Math.round(sl * ct * dollarsPerPoint);
      if (tp) $("f-tp").value = Math.round(tp * ct * dollarsPerPoint);
    } else if (dollarsPerPoint && b.dataset.val === "points") {
      if (sl) $("f-sl").value = Math.max(0.25, Math.floor(sl / (ct * dollarsPerPoint) / 0.25) * 0.25);
      if (tp) $("f-tp").value = Math.max(0.25, Math.floor(tp / (ct * dollarsPerPoint) / 0.25) * 0.25);
    }
    sltpMode = b.dataset.val;
    refreshSltpUI();
  });
});
["f-sl", "f-tp", "f-contracts"].forEach((id) =>
  $(id).addEventListener("input", refreshSltpUI));

/* one-click self-update */
$("btn-update").addEventListener("click", () => withApi(async () => {
  if (!confirm("Download and install the latest version now?\n\nThe bot will "
      + "verify the download, then close and reopen on the new version.")) return;
  $("btn-update").disabled = true;
  $("btn-update").textContent = "Updating…";
  const r = await API.apply_update();
  if (!r.ok) { $("btn-update").disabled = false; alert("Update failed: " + r.error); return; }
  if (r.restarting) $("btn-update").textContent = "Restarting…";
}));

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
  const payload = collectSettings();
  if (backend === "tradovate") {
    // Secrets ride a dedicated key the bridge strips BEFORE settings handling.
    payload.tdv_secrets = {
      password: $("f-tdv-pass").value,
      cid: $("f-tdv-cid").value.trim(),
      sec: $("f-tdv-sec").value,
    };
  }
  const r = await API.connect(payload, backend === "tradovate" ? "" : $("f-key").value,
                              $("f-remember").checked);
  $("btn-connect").disabled = false;
  if (!r.ok) { $("conn-status").textContent = "connect failed"; alert(r.error); return; }
  $("f-key").value = "";                       // never keep secrets in the DOM
  $("f-key").placeholder = "stored on this device";
  $("f-tdv-pass").value = ""; $("f-tdv-sec").value = ""; $("f-tdv-cid").value = "";
  if (r.env && $("f-remember").checked) {
    $("f-tdv-pass").placeholder = "stored on this device";
    $("f-tdv-sec").placeholder = "stored on this device";
  }
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
  // Never present the crude fallback (0.75*VIX) as advice: when the trained
  // model isn't loaded, say so loudly and show NOTHING actionable.
  if (!mp.calibrated) {
    $("mp-status").textContent = "⛔ MODEL NOT LOADED — no advice";
    $("mp-status").style.color = "var(--red)";
    $("mp-headline").textContent = "Morning Plan data missing — relaunch the bot (it self-installs); "
      + "if this persists, reinstall from the latest download.";
    $("mp-headline").classList.add("notrade");
    $("mp-headline").style.display = "";
    $("mp-cells").style.display = "none";
    $("mp-sizing").textContent = "";
    $("mp-alert").style.display = "none";
    $("mp-detail").textContent = "The spike predictor could not load its trained model, so it "
      + "cannot advise today. (It was NOT showing a real NO-TRADE call.)";
    return;
  }
  $("mp-status").style.color = "";
  const tag = mp.decision === "TRADE" ? "✅ TRADE" : "⛔ NO-TRADE";
  const sess = new Date(mp.session_date + "T12:00:00")
    .toLocaleDateString("en-US", { weekday: "short", month: "short", day: "2-digit" });
  const banner = mp.market_closed_today ? "  ·  ⚠ MARKET CLOSED TODAY — plan is for the NEXT session" : "";
  const cal = mp.calibrated ? "" : "  ·  UNCALIBRATED";
  $("mp-status").textContent =
    `${tag} · ${mp.conviction} · Session: ${sess} · Vol ${mp.volatility} · ` +
    `spike ~${Math.round(mp.predicted_spike)}pt · P(30+)=${Math.round(mp.p_big * 100)}%${banner}${cal}`;

  const p = mp.plan;
  // Fixed-bracket recommendation (2026-07-22 sweep-validated: symmetric ~8pt TP/SL, entry ±X
  // by the VIX rule). Shown for TRADE and NO-TRADE alike — the verdict stays the advice.
  const recCap = mp.rec_tp_dollars < p.target_dollars - 1;  // target needed >max contracts
  $("mp-headline").style.display = "";
  $("mp-cells").style.display = "";
  $("mp-ct").textContent = mp.rec_contracts;
  $("mp-entry").textContent = "±" + Math.round(mp.rec_entry_spread);
  $("mp-tp").textContent = money(mp.rec_tp_dollars);
  $("mp-sl").textContent = money(mp.rec_sl_dollars);
  if (mp.decision === "TRADE" && p.feasible) {
    $("mp-headline").textContent = "➡ ENTER IN TOPSTEP";
    $("mp-headline").classList.remove("notrade");
  } else {
    $("mp-headline").textContent = "➡ NO-TRADE — sit out (sizing below only if you trade anyway)";
    $("mp-headline").classList.add("notrade");
  }
  const sized = recCap
    ? `Your ${money(p.target_dollars)} target exceeds the ${p.max_contracts}-contract max — sized to the max instead`
    : `Sized to your ${money(p.target_dollars)} target`;
  $("mp-sizing").innerHTML =
    `Entry ±${Math.round(mp.rec_entry_spread)} — ${mp.rec_entry_reason} (±14 only when prior ` +
    `VIX ≥ 18; widening below that hurts). Enter the spread in the bot manually — advisory.<br>` +
    `${sized} at the validated ~8pt symmetric bracket (${mp.rec_contracts} × $160/contract).`;
  if (recCap) {
    $("mp-alert").textContent =
      `⚠ ${money(p.target_dollars)} needs more than ${p.max_contracts} contracts — capped. ` +
      `Today's max: TP ${money(mp.rec_tp_dollars)} / SL ${money(mp.rec_sl_dollars)} — use that.`;
    $("mp-alert").style.display = "";
  } else {
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

  // update banner: a newer published build is available
  const upd = $("btn-update");
  if (st.update && st.update.version) {
    upd.hidden = false;
    upd.textContent = `⬆ Update to v${st.update.version}`;
    upd.title = st.update.notes || "A newer version is available";
  } else {
    upd.hidden = true;
  }
  setPill("pill-conn", st.connected ? "ok" : "", st.connected ? "CONNECTED" : "OFFLINE",
          st.connected ? "green" : "gray");
  const venue = $("pill-venue");
  if (st.backend === "tradovate") {
    venue.hidden = false;
    setPill("pill-venue", st.tdv_env === "live" ? "bad" : "ok",
            st.tdv_env === "live" ? "TDV LIVE" : "TDV DEMO",
            st.tdv_env === "live" ? "red" : "green");
  } else {
    venue.hidden = true;
  }
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
  dollarsPerPoint = s.dollars_per_point || null;
  sltpMode = s.sl_tp_entry_mode || "points";
  document.querySelectorAll("#seg-sltp button").forEach((b) =>
    b.classList.toggle("active", b.dataset.val === sltpMode));
  if (sltpMode === "dollars" && s.stop_loss_dollars > 0) {
    $("f-sl").value = s.stop_loss_dollars;
    $("f-tp").value = s.take_profit_dollars || "";
  } else {
    $("f-sl").value = s.stop_loss_points;
    $("f-tp").value = s.take_profit_points;
  }
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
  $("f-tdv-user").value = s.tdv_username || "";
  $("f-tdv-price").value = s.tdv_price_source || "topstep";
  tdvEnv = s.tdv_environment || "demo";
  document.querySelectorAll("#seg-tdvenv button").forEach((b) =>
    b.classList.toggle("active", b.dataset.val === tdvEnv));
  if (s.has_tdv_credentials) {
    $("f-tdv-pass").placeholder = "stored on this device";
    $("f-tdv-sec").placeholder = "stored on this device";
  }
  backend = s.backend || "api";
  document.querySelectorAll("#seg-backend button").forEach((b) =>
    b.classList.toggle("active", b.dataset.val === backend));
  refreshBackendUI();
  refreshSltpUI();
  poll();
}

if (window.pywebview && window.pywebview.api) init();
else window.addEventListener("pywebviewready", init);
