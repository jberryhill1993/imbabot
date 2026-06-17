"""Settings + credential storage.

Settings (non-secret) live in a JSON file under the OS config dir. The API key is
a secret and is kept out of that file: it goes to the OS keychain via ``keyring``
when available, otherwise to a 0600 file you control. The key is never logged.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from datetime import time as dtime
from pathlib import Path
from typing import Optional

APP_NAME = "imbabot"
KEYRING_SERVICE = "imbabot-projectx"


def config_dir() -> Path:
    """Per-user config directory, created if missing.

    Honors ``IMBABOT_CONFIG_DIR`` so tests (and power users) can redirect state.
    """
    override = os.environ.get("IMBABOT_CONFIG_DIR")
    if override:
        path = Path(override)
        path.mkdir(parents=True, exist_ok=True)
        return path
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        path = Path(base) / APP_NAME
    elif os.sys.platform == "darwin":  # type: ignore[attr-defined]
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path() -> Path:
    return config_dir() / "settings.json"


def log_path() -> Path:
    return config_dir() / "imbabot.log"


@dataclass
class Settings:
    # --- connection ---
    username: str = ""
    base_url: str = "https://api.topstepx.com"
    account_id: Optional[int] = None
    account_name: str = ""

    # --- instrument / strategy ---
    contract_symbol: str = "MNQ"     # micro Nasdaq by default (small tick value)
    entry_points: float = 12.0
    stop_loss_points: float = 12.0
    take_profit_points: float = 12.0
    contracts: int = 2
    trade_mode: str = "semi_auto"    # "semi_auto" | "one_trade"

    # --- timing ---
    market_tz: str = "America/New_York"
    open_hour: int = 9
    open_minute: int = 30
    capture_offset_seconds: int = 3   # capture price 3s before the open

    # --- test mode (fire at a custom local time to verify it works) ---
    test_mode: bool = False           # if True, fire at test_fire_time instead of the 09:30 open
    test_fire_time: str = ""          # "HH:MM" or "HH:MM:SS" in YOUR local time

    # --- production daily schedule (recurring, weekday-only) ---
    # If set, the bot fires at this local wall-clock time every weekday (Mon–Fri)
    # and re-arms itself after each fire. Empty = use the 09:30 open default.
    strategy_fire_time: str = ""      # "HH:MM:SS" in YOUR local time, or "" to disable

    # --- backend selection ---
    backend: str = "api"              # "api" (ProjectX REST) | "browser" (automation)
    browser_driver: str = "selenium"  # "selenium" (bundles into the .exe/.app, drives installed Chrome) | "playwright"
    browser_platform: str = "projectx"  # "projectx" | "tradesea" (selector pack to use)
    browser_url_override: str = ""    # override the pack's URL if needed
    browser_tick_size: float = 0.25   # tick size for price math in browser mode (NQ/MNQ=0.25)
    browser_headless: bool = False    # MUST be False for manual login
    chrome_channel: str = "chrome"    # "chrome" (your installed Google Chrome) | "chromium" (bundled)

    # --- data / safety ---
    use_live_data: bool = False       # False = sim data subscription
    dry_run: bool = True              # True = compute & log, DO NOT send orders
    max_contracts: int = 5            # hard cap; orders above this are refused
    max_trades_per_day: int = 1       # client-side guard (mirror it platform-side too)
    display_timezone: str = "America/New_York"

    def open_time(self) -> dtime:
        return dtime(hour=self.open_hour, minute=self.open_minute, second=0)

    # ---- persistence ----
    def save(self, path: Optional[Path] = None) -> Path:
        path = path or settings_path()
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        path = path or settings_path()
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})


# --------------------------------------------------------------------- secrets
def _secret_file() -> Path:
    return config_dir() / "credentials"


def _keyring():
    try:
        import keyring  # type: ignore

        # Some headless backends raise on use; probe lazily where used.
        return keyring
    except Exception:
        return None


def store_api_key(username: str, api_key: str) -> str:
    """Persist the API key. Returns the backend used ('keyring' or 'file')."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(KEYRING_SERVICE, username, api_key)
            return "keyring"
        except Exception:
            pass
    path = _secret_file()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[username] = api_key
    path.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except Exception:
        pass
    return "file"


def load_api_key(username: str) -> Optional[str]:
    """Look up the API key: env var > keyring > local file."""
    env = os.environ.get("IMBABOT_API_KEY")
    if env:
        return env
    kr = _keyring()
    if kr is not None:
        try:
            val = kr.get_password(KEYRING_SERVICE, username)
            if val:
                return val
        except Exception:
            pass
    path = _secret_file()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get(username)
        except Exception:
            return None
    return None


def clear_api_key(username: str) -> None:
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, username)
        except Exception:
            pass
    path = _secret_file()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data.pop(username, None)
            path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
