"""Self-update from GitHub Releases — one published version, every machine pulls it.

Replaces the manual "rebuild → upload to Drive → redownload on the other PC" loop.
Two layers, split by safety:

- **Data/model (silent):** `sync_model()` fetches the release's `analysis-data.zip`
  when newer and drops it into `config_dir()/analysis/` — data only, never
  executed. This also delivers the weekly model retrains with zero touch.
- **Program/code (notify + one-click):** `check_for_update()` compares the
  running `__version__` to the latest release tag; the UI shows a banner and,
  on click, `download_app()` + `apply_app_update()` swap the frozen build and
  relaunch.

Safety: every download is HTTPS and its SHA-256 is verified against the
release's `checksums.txt` BEFORE anything is extracted or executed. The source
is the user's own public repo. Nothing runs on a checksum mismatch.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import __version__
from .config import config_dir

REPO = "jberryhill1993/imbabot"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
APP_ASSET_PREFIX = "Imbabot-"      # Imbabot-<ver>.zip
DATA_ASSET = "analysis-data.zip"
CHECKSUMS_ASSET = "checksums.txt"
_DATA_STAMP = ".data_version"      # in config_dir()/analysis


# ------------------------------------------------------------------ versions
def parse_version(s: str) -> Tuple[int, ...]:
    """'v0.2.6', '0.2.6-dev', '0.2.6' -> (0,2,6). Non-numeric parts dropped."""
    s = (s or "").strip().lstrip("vV").split("-")[0].split("+")[0]
    parts = []
    for p in s.split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


# --------------------------------------------------------------- HTTP (injectable)
def _http_get_json(url: str, timeout: float) -> dict:
    import requests
    r = requests.get(url, timeout=timeout,
                     headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return r.json()


def _http_get_bytes(url: str, timeout: float) -> bytes:
    import requests
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


# ------------------------------------------------------------------ model
@dataclass
class UpdateInfo:
    version: str
    notes: str
    app_url: Optional[str] = None
    data_url: Optional[str] = None
    checksums: Optional[Dict[str, str]] = None   # filename -> sha256
    code_update_available: bool = False           # a newer app build exists


def _parse_checksums(text: str) -> Dict[str, str]:
    """`<sha256>␣␣<filename>` per line (sha256sum format)."""
    out: Dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            out[parts[-1]] = parts[0].lower()
    return out


def check_for_update(current: str = __version__, *, timeout: float = 6.0,
                     get_json: Callable[[str, float], dict] = _http_get_json,
                     get_bytes: Callable[[str, float], bytes] = _http_get_bytes
                     ) -> Optional[UpdateInfo]:
    """Fetch the latest release; return UpdateInfo (or None on any failure —
    the network being off must never block launch)."""
    try:
        rel = get_json(LATEST_URL, timeout)
        tag = str(rel.get("tag_name") or rel.get("name") or "")
        if not tag:
            return None
        assets = {a.get("name"): a.get("browser_download_url")
                  for a in rel.get("assets", []) if a.get("name")}
        checksums = None
        if CHECKSUMS_ASSET in assets:
            try:
                checksums = _parse_checksums(
                    get_bytes(assets[CHECKSUMS_ASSET], timeout).decode("utf-8", "replace"))
            except Exception:
                checksums = None
        app_url = next((u for n, u in assets.items()
                        if n.startswith(APP_ASSET_PREFIX) and n.endswith(".zip")), None)
        return UpdateInfo(
            version=tag.lstrip("vV"),
            notes=str(rel.get("body") or "").strip(),
            app_url=app_url,
            data_url=assets.get(DATA_ASSET),
            checksums=checksums,
            code_update_available=is_newer(tag, current),
        )
    except Exception:
        return None


# ------------------------------------------------------------- verification
def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify(data: bytes, name: str, info: UpdateInfo) -> None:
    if info.checksums and name in info.checksums:
        got = _sha256(data)
        if got != info.checksums[name]:
            raise ValueError(f"checksum mismatch for {name}: {got} != {info.checksums[name]}")
    # No checksum listed -> refuse (never execute/extract unverified content).
    elif info.checksums is not None:
        raise ValueError(f"no checksum published for {name} — refusing to use it")


# ---------------------------------------------------------- silent data sync
def _local_data_version() -> str:
    p = config_dir() / "analysis" / _DATA_STAMP
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def sync_model(info: Optional[UpdateInfo] = None, *, log: Optional[Callable[..., None]] = None,
               get_bytes: Callable[[str, float], bytes] = _http_get_bytes,
               timeout: float = 20.0) -> bool:
    """Silently install the release's analysis-data.zip when newer. Data only —
    verified by checksum, extracted into config_dir()/analysis. Returns True if
    updated. Never raises."""
    try:
        info = info or check_for_update()
        if not info or not info.data_url:
            return False
        if _local_data_version() == info.version:
            return False            # already have this release's data
        blob = get_bytes(info.data_url, timeout)
        _verify(blob, DATA_ASSET, info)
        dst = config_dir() / "analysis"
        dst.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(__import__("io").BytesIO(blob)) as z:
            _safe_extract(z, dst)
        (dst / _DATA_STAMP).write_text(info.version, encoding="utf-8")
        if log:
            log(f"Morning-Plan data updated to {info.version} (auto).")
        return True
    except Exception as exc:
        if log:
            log(f"Data auto-update skipped: {exc}", "warn")
        return False


def _safe_extract(z: zipfile.ZipFile, dst: Path) -> None:
    """Extract with a zip-slip guard (no member escapes dst)."""
    dst = dst.resolve()
    for member in z.namelist():
        target = (dst / member).resolve()
        if not str(target).startswith(str(dst)):
            raise ValueError(f"unsafe zip member {member}")
    z.extractall(dst)


# --------------------------------------------------- program (code) update
def download_app(info: UpdateInfo, *, log: Optional[Callable[..., None]] = None,
                 get_bytes: Callable[[str, float], bytes] = _http_get_bytes,
                 timeout: float = 60.0) -> Optional[Path]:
    """Download + checksum-verify the app zip to a temp dir. Returns the path."""
    if not info.app_url:
        return None
    name = info.app_url.rsplit("/", 1)[-1]
    blob = get_bytes(info.app_url, timeout)
    _verify(blob, name, info)              # raises on mismatch -> no bad swap
    out = Path(tempfile.gettempdir()) / name
    out.write_bytes(blob)
    if log:
        log(f"Downloaded {name} ({len(blob) // 1024} KB), verified.")
    return out


def apply_app_update(zip_path: Path, *, log: Optional[Callable[..., None]] = None) -> bool:
    """Swap the FROZEN install with the downloaded zip and relaunch.

    Only meaningful for the packaged .exe (`sys.frozen`). Source/dev runs are
    git-managed — returns False without doing anything. Windows can't overwrite
    a running exe, so a detached .bat waits for this process to exit, replaces
    the install dir, and relaunches.
    """
    if not getattr(sys, "frozen", False):
        if log:
            log("Code auto-update is for the packaged app only; this is a source "
                "run (update via git).", "warn")
        return False
    try:
        install_dir = Path(sys.executable).parent
        staging = Path(tempfile.mkdtemp(prefix="imbabot-update-"))
        with zipfile.ZipFile(zip_path) as z:
            _safe_extract(z, staging)
        # The zip contains a top folder like Imbabot-<ver>/ — use it if present.
        roots = [p for p in staging.iterdir() if p.is_dir()]
        new_root = roots[0] if len(roots) == 1 else staging
        exe_name = Path(sys.executable).name
        bat = staging / "_apply_update.bat"
        bat.write_text(
            "@echo off\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'xcopy /E /I /Y "{new_root}\\*" "{install_dir}" >nul\r\n'
            f'start "" "{install_dir}\\{exe_name}"\r\n'
            f'rmdir /S /Q "{staging}"\r\n',
            encoding="utf-8")
        import subprocess
        DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(["cmd", "/c", str(bat)], creationflags=DETACHED, close_fds=True)
        if log:
            log(f"Updater launched — the app will restart on the new version.")
        return True
    except Exception as exc:
        if log:
            log(f"Code update failed to apply: {exc}", "error")
        return False
