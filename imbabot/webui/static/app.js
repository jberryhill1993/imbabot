/* Imbabot glass dashboard — Phase 1: static shell with placeholder data.
   Phase 2 wires window.pywebview.api (get_state poll + action calls) with the
   same element IDs, so this file's render targets are already final. */

"use strict";

/* ---------------------------------------------------------------- tabs */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".tabpane").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById("pane-" + tab.dataset.tab).classList.add("active");
  });
});

/* --------------------------------------------------- segmented controls */
document.querySelectorAll(".segment").forEach((seg) => {
  seg.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      seg.querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
    });
  });
});

/* ------------------------------------------------------- collapsible log */
const logCard = document.getElementById("log-card");
document.getElementById("log-head").addEventListener("click", (e) => {
  if (e.target.id === "btn-savelog") return;
  logCard.classList.toggle("open");
});

/* ------------------------------------------------ placeholder live-ness */
/* Simulated countdown so the shell FEELS real during the visual review. */
let tminus = 23 * 3600 + 59 * 60 + 51;
const elT = document.getElementById("stat-tminus");
setInterval(() => {
  tminus = (tminus - 1 + 86400) % 86400;
  const h = String(Math.floor(tminus / 3600)).padStart(2, "0");
  const m = String(Math.floor((tminus % 3600) / 60)).padStart(2, "0");
  const s = String(tminus % 60).padStart(2, "0");
  elT.textContent = `${h}:${m}:${s}`;
}, 1000);

/* Placeholder log lines (styled like the real Logger output). */
const LOG = [
  ["10:02:19", "info", "Imbabot 0.2.3-dev ready. Config: %APPDATA%\\imbabot-dev"],
  ["10:02:20", "info", "Authenticated. Token acquired."],
  ["10:02:20", "info", "Account: PRAC-V2-575271 (canTrade=True)"],
  ["10:02:21", "info", "Contract: NQU6 (CON.F.US.ENQ.U26) tick=0.25 $5.0/tick"],
  ["10:02:24", "info", "Morning Plan 2026-07-13: TRADE/MODERATE spike ~21pt -> 4ct"],
  ["10:03:01", "warn", "placeholder: this is the static shell — no engine attached"],
];
const logBody = document.getElementById("log-body");
LOG.forEach(([t, lvl, msg]) => {
  const div = document.createElement("div");
  div.className = "log-line " + lvl;
  div.innerHTML = `<span class="t">[${t}]</span> ${msg}`;
  logBody.appendChild(div);
});

/* ------------------------------------------ placeholder action feedback */
/* Buttons only pulse visually in Phase 1 — no engine calls until approval. */
["btn-arm", "btn-flatten", "btn-stop", "btn-recalc", "btn-savelog"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener("click", () => {
    el.style.filter = "brightness(1.35)";
    setTimeout(() => (el.style.filter = ""), 180);
  });
});
