"""Browser drivers behind one tiny page interface.

The adapter/ActionRunner only needs a handful of methods on a ``page``:
    page.locator(sel).{click,fill,select_option,press,check,wait_for,evaluate,
                       inner_text,count,first}
    page.keyboard.press(key)
    page.evaluate(js)
    page.goto(url)

Playwright's sync ``page`` already provides these. This module adds a thin
``SeleniumPage`` shim so the *same* selector packs + ConfigurableAdapter +
BrowserEngine run on Selenium too — which is what bundles cleanly into the frozen
app and drives the user's installed Google Chrome.

Selector convention (portable across both drivers): a selector starting with
``//`` or ``(//`` is treated as XPath; everything else is a CSS selector.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


# --------------------------------------------------------------- Selenium shim
def _is_xpath(sel: str) -> bool:
    s = sel.strip()
    return s.startswith("//") or s.startswith("(//") or s.startswith("xpath=")


def _by(sel: str):
    from selenium.webdriver.common.by import By

    if _is_xpath(sel):
        return By.XPATH, sel[6:] if sel.startswith("xpath=") else sel
    return By.CSS_SELECTOR, sel


_KEYMAP = None


def _key(name: str):
    global _KEYMAP
    from selenium.webdriver.common.keys import Keys

    if _KEYMAP is None:
        _KEYMAP = {
            "Enter": Keys.ENTER, "Tab": Keys.TAB, "Escape": Keys.ESCAPE,
            "Backspace": Keys.BACK_SPACE, "Delete": Keys.DELETE, "Space": " ",
            "ArrowUp": Keys.ARROW_UP, "ArrowDown": Keys.ARROW_DOWN,
        }
    return _KEYMAP.get(name, name)


class SeleniumLocator:
    def __init__(self, driver: Any, sel: str) -> None:
        self._d = driver
        self._sel = sel

    @property
    def first(self) -> "SeleniumLocator":
        return self  # Selenium's find_element already returns the first match

    def _wait(self, timeout: int, condition):
        from selenium.webdriver.support.ui import WebDriverWait

        return WebDriverWait(self._d, max(0.1, timeout / 1000.0)).until(condition)

    def _el(self, timeout: int = 8000):
        from selenium.webdriver.support import expected_conditions as EC

        return self._wait(timeout, EC.presence_of_element_located(_by(self._sel)))

    def click(self, timeout: int = 8000) -> None:
        from selenium.webdriver.support import expected_conditions as EC

        self._wait(timeout, EC.element_to_be_clickable(_by(self._sel))).click()

    def fill(self, value: str, timeout: int = 8000) -> None:
        # Real keystrokes (focus -> select-all -> delete -> type). Verified against
        # TopstepX: a JS value-set leaves the SUBMITTED order on the stale price; only
        # real typing commits. Follow with a Tab/blur step to commit on-blur inputs.
        import sys as _sys
        from selenium.webdriver.common.keys import Keys

        el = self._el(timeout)
        try:
            el.click()
        except Exception:
            pass
        mod = Keys.COMMAND if _sys.platform == "darwin" else Keys.CONTROL
        el.send_keys(mod, "a")
        el.send_keys(Keys.DELETE)
        el.send_keys(str(value))

    def evaluate(self, js_func: str, arg: Any = None) -> Any:
        el = self._el()
        return self._d.execute_script(
            "return (" + js_func + ")(arguments[0], arguments[1])", el, arg
        )

    def select_option(self, label: Optional[str] = None, value: Optional[str] = None,
                      timeout: int = 8000) -> None:
        from selenium.webdriver.support.ui import Select

        sel = Select(self._el(timeout))
        if label is not None:
            sel.select_by_visible_text(label)
        elif value is not None:
            sel.select_by_value(value)

    def press(self, key: str, timeout: int = 8000) -> None:
        self._el(timeout).send_keys(_key(key))

    def check(self, timeout: int = 8000) -> None:
        el = self._el(timeout)
        if not el.is_selected():
            el.click()

    def wait_for(self, state: str = "visible", timeout: int = 8000) -> None:
        from selenium.webdriver.support import expected_conditions as EC

        by = _by(self._sel)
        cond = {
            "visible": EC.visibility_of_element_located(by),
            "attached": EC.presence_of_element_located(by),
            "hidden": EC.invisibility_of_element_located(by),
        }.get(state, EC.presence_of_element_located(by))
        self._wait(timeout, cond)

    def inner_text(self, timeout: int = 8000) -> str:
        return self._el(timeout).text

    def count(self) -> int:
        by, q = _by(self._sel)
        return len(self._d.find_elements(by, q))


class _Keyboard:
    def __init__(self, driver: Any) -> None:
        self._d = driver

    def press(self, key: str) -> None:
        self._d.switch_to.active_element.send_keys(_key(key))


class SeleniumPage:
    """Playwright-page-shaped wrapper over a Selenium WebDriver."""

    def __init__(self, driver: Any) -> None:
        self._d = driver
        self.keyboard = _Keyboard(driver)

    @property
    def driver(self) -> Any:
        """Raw Selenium WebDriver (used by the calibration recorder)."""
        return self._d

    def locator(self, sel: str) -> SeleniumLocator:
        return SeleniumLocator(self._d, sel)

    def evaluate(self, js: str, arg: Any = None) -> Any:
        # Try as an expression first (Playwright-style), fall back to statements.
        try:
            return self._d.execute_script("return (" + js + ")", arg)
        except Exception:
            return self._d.execute_script(js, arg)

    def goto(self, url: str, wait_until: Optional[str] = None) -> None:
        self._d.get(url)


# ------------------------------------------------------------- driver sessions
class DriverSession:
    """Holds a live driver + its page; close() tears everything down."""

    def __init__(self, page: Any, closer) -> None:
        self.page = page
        self._closer = closer

    def close(self) -> None:
        try:
            self._closer()
        except Exception:
            pass


def open_selenium(user_dir: Path, headless: bool = False, channel: str = "chrome") -> DriverSession:
    """Launch the user's installed Google Chrome under Selenium, isolated profile."""
    from selenium import webdriver

    opts = webdriver.ChromeOptions()
    opts.add_argument(f"--user-data-dir={user_dir}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--start-maximized")
    if headless:
        opts.add_argument("--headless=new")
    # quiet the "Chrome is being controlled by automated test software" infobar
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    # Selenium Manager (bundled) resolves the matching chromedriver automatically.
    driver = webdriver.Chrome(options=opts)
    return DriverSession(SeleniumPage(driver), driver.quit)


def open_playwright(user_dir: Path, headless: bool = False, channel: str = "chrome") -> DriverSession:
    """Launch via Playwright (used when running from source with playwright installed)."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    kwargs = {"headless": headless, "args": ["--start-maximized"], "no_viewport": True}
    if channel == "chrome":
        kwargs["channel"] = "chrome"
    ctx = pw.chromium.launch_persistent_context(str(user_dir), **kwargs)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    def _close():
        ctx.close()
        pw.stop()

    return DriverSession(page, _close)


def open_driver(name: str, user_dir: Path, headless: bool, channel: str) -> DriverSession:
    if name == "playwright":
        return open_playwright(user_dir, headless, channel)
    return open_selenium(user_dir, headless, channel)
