"""Pack-driven browser adapters.

A *selector pack* (JSON) describes how to talk to one trading site's DOM: where the
live price is, where the net position shows, and the click/type *action sequences*
to place a stop order, cancel one, cancel all, and flatten. One generic
``ConfigurableAdapter`` interprets any pack, so the mock page (tested here) and the
real Project X / TradeSea sites run the *same* code path — only the JSON differs.

This means the brittle, site-specific part is data you can calibrate, not code you
have to rewrite. See `imbabot/browser/selectors/*.json` and the README's
calibration section.

All methods take a Playwright *sync* ``page`` and must be called on the thread that
created it (the BrowserController guarantees this).
"""
from __future__ import annotations

import json
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional


class AdapterError(RuntimeError):
    pass


_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def parse_number(text: str) -> float:
    """Pull the first number out of a DOM string like '$29,464.75' or '-2'."""
    if text is None:
        raise AdapterError("no text to parse a number from")
    m = _NUM_RE.search(text.replace(" ", " "))
    if not m:
        raise AdapterError(f"no number found in {text!r}")
    return float(m.group(0).replace(",", ""))


def _decimals_for_tick(tick_size: float) -> int:
    d = Decimal(str(tick_size)).normalize()
    exp = d.as_tuple().exponent
    return max(0, -int(exp))


@dataclass
class SelectorPack:
    name: str
    url: str = ""
    logged_in: str = ""        # selector that exists only once the chart is ready
    price: str = ""            # selector whose text holds the live price
    price_js: str = ""         # OR a JS expression returning the price (wins if set)
    position_size: str = ""    # selector whose text is the signed net position
    order_method: str = "dom"  # "dom" (action sequences) — informational
    actions: Dict[str, List[dict]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SelectorPack":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def from_file(cls, path: Path) -> "SelectorPack":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


class ActionRunner:
    """Interprets a list of action dicts against a Playwright page.

    Supported actions (each a dict with an ``action`` key):
      {"action":"click","selector":"..."}
      {"action":"fill","selector":"...","value":"$trigger_price"}
      {"action":"set_value","selector":"...","value":"$size"}   # via JS, fires input event
      {"action":"select","selector":"...","value":"Stop"}        # by label or value
      {"action":"press","selector":"...","key":"Enter"}          # selector optional
      {"action":"check","selector":"..."}
      {"action":"wait","selector":"...","state":"visible","timeout":8000}
      {"action":"eval","js":"..."}

    ``$name`` tokens in ``selector``/``value``/``js`` are substituted from ``ctx``.
    """

    def __init__(self, default_timeout: int = 8000) -> None:
        self.default_timeout = default_timeout

    @staticmethod
    def _subst(text: Optional[str], ctx: Dict[str, str]) -> Optional[str]:
        if text is None:
            return None
        for key, val in ctx.items():
            text = text.replace("$" + key, str(val))
        return text

    def run(self, page: Any, steps: List[dict], ctx: Dict[str, str]) -> None:
        for raw in steps:
            step = dict(raw)
            action = step.get("action")
            sel = self._subst(step.get("selector"), ctx)
            timeout = int(step.get("timeout", self.default_timeout))
            try:
                if action in ("comment", "note"):
                    continue  # self-documentation in a pack; does nothing
                if action == "click":
                    page.locator(sel).click(timeout=timeout)
                elif action == "fill":
                    page.locator(sel).fill(self._subst(step.get("value", ""), ctx), timeout=timeout)
                elif action == "set_value":
                    value = self._subst(step.get("value", ""), ctx)
                    # React-safe: use the native value setter so controlled inputs
                    # (e.g. TopstepX) actually update their state, then fire events.
                    page.locator(sel).evaluate(
                        "(el, v) => { const proto = el.tagName === 'TEXTAREA' ? "
                        "window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype; "
                        "const setter = Object.getOwnPropertyDescriptor(proto, 'value').set; "
                        "setter.call(el, v); "
                        "el.dispatchEvent(new Event('input', {bubbles:true})); "
                        "el.dispatchEvent(new Event('change', {bubbles:true})); }",
                        value,
                    )
                elif action == "select":
                    value = self._subst(step.get("value", ""), ctx)
                    loc = page.locator(sel)
                    try:
                        loc.select_option(label=value, timeout=timeout)
                    except Exception:
                        loc.select_option(value=value, timeout=timeout)
                elif action == "press":
                    key = step.get("key", "Enter")
                    if sel:
                        page.locator(sel).press(key, timeout=timeout)
                    else:
                        page.keyboard.press(key)
                elif action == "check":
                    page.locator(sel).check(timeout=timeout)
                elif action == "wait":
                    state = step.get("state", "visible")
                    page.locator(sel).wait_for(state=state, timeout=timeout)
                elif action == "eval":
                    page.evaluate(self._subst(step.get("js", ""), ctx))
                else:
                    raise AdapterError(f"unknown action {action!r}")
            except AdapterError:
                raise
            except Exception as exc:
                raise AdapterError(f"action {action} on {sel!r} failed: {exc}") from exc


class PlatformAdapter(ABC):
    """Minimal surface the BrowserEngine needs from a trading site."""

    name: str = "base"

    @abstractmethod
    def url(self) -> str: ...

    @abstractmethod
    def is_logged_in(self, page: Any) -> bool: ...

    @abstractmethod
    def read_price(self, page: Any) -> float: ...

    @abstractmethod
    def read_net_position(self, page: Any) -> int: ...

    @abstractmethod
    def place_stop_entry(self, page: Any, *, side: str, trigger_price: float, size: int,
                         sl_points: float, tp_points: float, tick_size: float) -> str: ...

    @abstractmethod
    def cancel_entry(self, page: Any, handle: str) -> None: ...

    @abstractmethod
    def cancel_all_orders(self, page: Any) -> None: ...

    @abstractmethod
    def flatten_all(self, page: Any) -> None: ...


class ConfigurableAdapter(PlatformAdapter):
    """A PlatformAdapter fully described by a SelectorPack."""

    def __init__(self, pack: SelectorPack, url_override: str = "") -> None:
        self.pack = pack
        self.name = pack.name
        self._url_override = url_override
        self._runner = ActionRunner()

    def url(self) -> str:
        return self._url_override or self.pack.url

    def is_logged_in(self, page: Any) -> bool:
        if not self.pack.logged_in:
            return True  # no indicator configured -> assume the user handles it
        try:
            return page.locator(self.pack.logged_in).count() > 0
        except Exception:
            return False

    def read_price(self, page: Any) -> float:
        if self.pack.price_js:
            return float(page.evaluate(self.pack.price_js))
        if not self.pack.price:
            raise AdapterError(f"{self.name}: no price selector configured")
        text = page.locator(self.pack.price).first.inner_text(timeout=8000)
        return parse_number(text)

    def read_net_position(self, page: Any) -> int:
        if not self.pack.position_size:
            return 0
        try:
            if page.locator(self.pack.position_size).count() == 0:
                return 0
            text = page.locator(self.pack.position_size).first.inner_text(timeout=4000)
            return int(round(parse_number(text)))
        except AdapterError:
            return 0

    def _ctx(self, *, side: str, trigger_price: float, size: int,
             sl_points: float, tp_points: float, tick_size: float) -> Dict[str, str]:
        dp = _decimals_for_tick(tick_size)
        sl_ticks = max(1, int(round(sl_points / tick_size))) if tick_size else int(sl_points)
        tp_ticks = max(1, int(round(tp_points / tick_size))) if tick_size else int(tp_points)
        return {
            "side": side,
            "trigger_price": f"{trigger_price:.{dp}f}",
            "trigger_price_raw": repr(trigger_price),
            "size": str(size),
            "sl_points": repr(sl_points),
            "tp_points": repr(tp_points),
            "sl_ticks": str(sl_ticks),
            "tp_ticks": str(tp_ticks),
        }

    def place_stop_entry(self, page: Any, *, side: str, trigger_price: float, size: int,
                         sl_points: float, tp_points: float, tick_size: float) -> str:
        key = "buy" if side == "buy" else "sell"
        steps = self.pack.actions.get(key)
        if not steps:
            raise AdapterError(f"{self.name}: no '{key}' action sequence in pack")
        ctx = self._ctx(side=side, trigger_price=trigger_price, size=size,
                        sl_points=sl_points, tp_points=tp_points, tick_size=tick_size)
        self._runner.run(page, steps, ctx)
        return ctx["trigger_price"]  # handle = the formatted trigger price

    def cancel_entry(self, page: Any, handle: str) -> None:
        steps = self.pack.actions.get("cancel_by_price")
        if not steps:
            raise AdapterError(f"{self.name}: no 'cancel_by_price' action sequence in pack")
        self._runner.run(page, steps, {"price": handle})

    def cancel_all_orders(self, page: Any) -> None:
        steps = self.pack.actions.get("cancel_all")
        if steps:
            self._runner.run(page, steps, {})

    def flatten_all(self, page: Any) -> None:
        steps = self.pack.actions.get("flatten")
        if steps:
            self._runner.run(page, steps, {})


def _pack_dir() -> Path:
    """Where the bundled selector packs live (frozen .exe/.app vs. from source)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "imbabot" / "browser" / "selectors"
    return Path(__file__).parent / "selectors"


_PACK_DIR = _pack_dir()


def user_pack_path(platform: str):
    """Per-user override location (lets you re-calibrate without rebuilding the app)."""
    from ..config import config_dir

    return config_dir() / "selectors" / f"{platform}.json"


def load_pack(platform: str) -> SelectorPack:
    """Load a selector pack. A user override in the config dir wins over the bundled one."""
    try:
        override = user_pack_path(platform)
        if override.exists():
            return SelectorPack.from_file(override)
    except Exception:
        pass
    path = _PACK_DIR / f"{platform}.json"
    if not path.exists():
        raise AdapterError(f"no selector pack for platform {platform!r} ({path})")
    return SelectorPack.from_file(path)


def make_adapter(platform: str, url_override: str = "") -> ConfigurableAdapter:
    return ConfigurableAdapter(load_pack(platform), url_override=url_override)
