"""Interactive selector recorder — calibrate a real site's pack by pointing & clicking.

You log into the platform once; the recorder highlights elements as you hover and,
when you click the one it asks for, captures a robust CSS selector for it. Clicks
are BLOCKED while picking, so nothing gets ordered during calibration. It then
assembles the buy/sell/cancel/flatten action sequences and saves the pack to your
config dir (which overrides the bundled placeholder — no rebuild needed).

Run:  python -m imbabot.cli browser-calibrate projectx

Calibrate against a SIM / evaluation account, ideally while flat, and verify in
dry-run before trusting it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

# JS injected into the page: hover highlight + click-to-capture (click suppressed),
# a small toolbar with a Skip button, and a robust-ish selector generator.
_PICKER_JS = r"""
(function(){
  if (window.__imbaInstalled) { return 'already'; }
  window.__imbaInstalled = true;
  window.__imbaActive = false;
  window.__imbaPicked = null;
  var hl = document.createElement('div');
  hl.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;border:2px solid #4da3ff;background:rgba(77,163,255,0.15);display:none;border-radius:3px';
  document.documentElement.appendChild(hl);
  var bar = document.createElement('div');
  bar.id = '__imbaBar';
  bar.style.cssText = 'position:fixed;bottom:14px;left:14px;z-index:2147483647;background:#11151a;color:#e6e6e6;padding:8px 12px;border:1px solid #4da3ff;border-radius:8px;font:13px system-ui,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.4)';
  bar.innerHTML = '<b style="color:#4da3ff">Imbabot</b> <span id="__imbaMsg">calibration ready</span> <button id="__imbaSkip" style="margin-left:10px;cursor:pointer">Skip</button>';
  document.documentElement.appendChild(bar);
  bar.querySelector('#__imbaSkip').addEventListener('click', function(ev){
    ev.stopPropagation(); ev.preventDefault();
    if (window.__imbaActive) window.__imbaPicked = '__SKIP__';
  }, true);
  function esc(s){ return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^a-zA-Z0-9_-]/g,'\\$&'); }
  function uniq(sel){ try { return document.querySelectorAll(sel).length === 1; } catch(e){ return false; } }
  function cssPath(el){
    if (!(el instanceof Element)) return '';
    if (el.id && uniq('#'+esc(el.id))) return '#'+esc(el.id);
    var attrs = ['data-testid','data-test','data-qa','data-name','data-id','data-cy','name','aria-label','role'];
    for (var i=0;i<attrs.length;i++){
      var v = el.getAttribute(attrs[i]);
      if (v){ var s = el.tagName.toLowerCase()+'['+attrs[i]+'="'+String(v).replace(/"/g,'\\"')+'"]'; if (uniq(s)) return s; }
    }
    var parts = [], node = el, depth = 0;
    while (node && node.nodeType === 1 && depth < 6){
      if (node.id && uniq('#'+esc(node.id))){ parts.unshift('#'+esc(node.id)); break; }
      var sel = node.tagName.toLowerCase();
      var cls = (typeof node.className === 'string') ? node.className.trim().split(/\s+/).filter(function(c){
        return c && c.length < 25 && !/[0-9a-f]{6,}/i.test(c) && !/^[0-9]/.test(c);
      }) : [];
      if (cls.length) sel += '.' + cls.slice(0,2).map(esc).join('.');
      var p = node.parentNode;
      if (p && p.children){
        var same = Array.prototype.filter.call(p.children, function(c){ return c.tagName === node.tagName; });
        if (same.length > 1){ sel += ':nth-of-type(' + (Array.prototype.indexOf.call(same, node)+1) + ')'; }
      }
      parts.unshift(sel);
      node = node.parentElement; depth++;
    }
    return parts.join(' > ');
  }
  document.addEventListener('mousemove', function(e){
    if (!window.__imbaActive) { hl.style.display='none'; return; }
    if (e.target.closest && e.target.closest('#__imbaBar')) return;
    var r = e.target.getBoundingClientRect();
    hl.style.display='block'; hl.style.left=r.left+'px'; hl.style.top=r.top+'px';
    hl.style.width=r.width+'px'; hl.style.height=r.height+'px';
  }, true);
  document.addEventListener('click', function(e){
    if (!window.__imbaActive) return;
    if (e.target.closest && e.target.closest('#__imbaBar')) return;
    e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation();
    window.__imbaPicked = cssPath(e.target);
    return false;
  }, true);
  return 'installed';
})();
"""

# (key, prompt, required)
_TARGETS = [
    ("chart_ready", "an element that is ALWAYS visible once your chart is loaded (e.g. the chart toolbar)", True),
    ("price", "the LIVE PRICE number on the chart/quote", True),
    ("position", "your NET POSITION quantity (open a 1-lot first if it's blank when flat) — or Skip", False),
    ("buy", "the BUY button (or Buy tab)", True),
    ("sell", "the SELL button (or Sell tab)", True),
    ("order_type", "the ORDER-TYPE selector, set to Stop — or Skip if there isn't one", False),
    ("stop_price", "the STOP-PRICE input field — or Skip", False),
    ("quantity", "the QUANTITY / size input field", True),
    ("stop_loss", "the STOP-LOSS input — or Skip", False),
    ("take_profit", "the TAKE-PROFIT input — or Skip", False),
    ("submit", "the SUBMIT / Place-Order / Confirm button", True),
    ("cancel_all", "the CANCEL-ALL-ORDERS button — or Skip", False),
    ("flatten", "the FLATTEN / Close-All button — or Skip", False),
]


def _pick(driver, prompt: str, allow_skip: bool, timeout: float = 240.0) -> Optional[str]:
    driver.execute_script(
        "window.__imbaActive=true; window.__imbaPicked=null;"
        "var m=document.getElementById('__imbaMsg'); if(m){m.textContent=arguments[0];}",
        prompt,
    )
    print(f"\n  → Click: {prompt}" + ("   (or click 'Skip' in the toolbar)" if allow_skip else ""))
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        val = driver.execute_script("return window.__imbaPicked;")
        if val:
            driver.execute_script("window.__imbaActive=false;")
            if val == "__SKIP__":
                print("    · skipped")
                return None
            print(f"    · captured: {val}")
            return val
    driver.execute_script("window.__imbaActive=false;")
    print("    · timed out (skipped)")
    return None


def _assemble(platform: str, url: str, caught: Dict[str, str]) -> dict:
    def order_seq(first_click_sel: str) -> List[dict]:
        steps: List[dict] = [{"action": "click", "selector": first_click_sel}]
        if caught.get("order_type"):
            steps.append({"action": "select", "selector": caught["order_type"], "value": "Stop"})
        if caught.get("stop_price"):
            steps.append({"action": "set_value", "selector": caught["stop_price"], "value": "$trigger_price"})
        if caught.get("quantity"):
            steps.append({"action": "set_value", "selector": caught["quantity"], "value": "$size"})
        if caught.get("stop_loss"):
            steps.append({"action": "set_value", "selector": caught["stop_loss"], "value": "$sl_points"})
        if caught.get("take_profit"):
            steps.append({"action": "set_value", "selector": caught["take_profit"], "value": "$tp_points"})
        if caught.get("submit"):
            steps.append({"action": "click", "selector": caught["submit"]})
        return steps

    actions: Dict[str, List[dict]] = {}
    if caught.get("buy"):
        actions["buy"] = order_seq(caught["buy"])
    if caught.get("sell"):
        actions["sell"] = order_seq(caught["sell"])
    if caught.get("cancel_all"):
        actions["cancel_all"] = [{"action": "click", "selector": caught["cancel_all"]}]
    if caught.get("flatten"):
        actions["flatten"] = [{"action": "click", "selector": caught["flatten"]}]
    # cancel_by_price (One-Trade OCO) isn't auto-captured; leave a calibratable note.
    actions["cancel_by_price"] = [
        {"action": "comment", "note": "Set this to click the cancel button on the working-order "
         "row whose price is $price. Often an XPath like "
         "//*[contains(text(),'$price')]/ancestor::tr//button[contains(@class,'cancel')]"},
    ]

    return {
        "_comment": f"Calibrated by browser-calibrate for {platform}. Verify in dry-run before live.",
        "name": platform,
        "url": url,
        "logged_in": caught.get("chart_ready", ""),
        "price": caught.get("price", ""),
        "position_size": caught.get("position", ""),
        "order_method": "dom",
        "actions": actions,
    }


def _save(platform: str, pack: dict) -> List[Path]:
    from .base import _PACK_DIR, user_pack_path

    written: List[Path] = []
    # 1) user override (always; what the running app loads first)
    up = user_pack_path(platform)
    up.parent.mkdir(parents=True, exist_ok=True)
    up.write_text(json.dumps(pack, indent=2), encoding="utf-8")
    written.append(up)
    # 2) bundled source pack, if writable (so a rebuild ships the calibration)
    bp = _PACK_DIR / f"{platform}.json"
    try:
        if bp.exists():
            bp.write_text(json.dumps(pack, indent=2), encoding="utf-8")
            written.append(bp)
    except Exception:
        pass
    return written


def run_calibration(platform: str, url_override: str = "") -> int:
    from ..config import Settings, config_dir
    from .base import load_pack
    from .drivers import open_selenium

    settings = Settings.load()
    pack = load_pack(platform)
    url = url_override or settings.browser_url_override or pack.url
    profile = config_dir() / "browser" / platform
    profile.mkdir(parents=True, exist_ok=True)

    print(f"\nImbabot calibration — {platform}")
    print("=" * 40)
    print("Opening Chrome. Calibrate against a SIM/eval account, ideally while flat.")
    try:
        session = open_selenium(profile, headless=False)
    except Exception as exc:
        print(f"Could not launch Chrome: {exc}")
        return 1
    driver = session.page.driver
    try:
        if url:
            driver.get(url)
        input("\n1) Log into TopStep and open your chart + order ticket + positions.\n"
              "2) Press Enter here when ready to start picking elements… ")
        driver.execute_script(_PICKER_JS)

        caught: Dict[str, str] = {}
        for key, prompt, required in _TARGETS:
            sel = _pick(driver, prompt, allow_skip=not required)
            if sel:
                caught[key] = sel

        pack_dict = _assemble(platform, pack.url, caught)
        written = _save(platform, pack_dict)
        print("\nSaved calibrated pack to:")
        for p in written:
            print(f"  · {p}")

        # quick read-back test
        print("\nQuick check (read-only):")
        try:
            if caught.get("price"):
                px = driver.find_element("css selector", caught["price"]).text
                print(f"  price selector reads: {px!r}")
            if caught.get("position"):
                pos = driver.find_elements("css selector", caught["position"])
                print(f"  position selector matches: {len(pos)} element(s)")
        except Exception as exc:
            print(f"  read-back warning: {exc}")
        print("\nNext: test it in DRY-RUN (browser-run --platform "
              f"{platform}) before going live. cancel_by_price (One-Trade OCO) may "
              "still need a manual touch — Semi-Auto works without it.")
        return 0
    finally:
        input("\nPress Enter to close the calibration browser… ")
        session.close()


if __name__ == "__main__":
    import sys

    raise SystemExit(run_calibration(sys.argv[1] if len(sys.argv) > 1 else "projectx"))
