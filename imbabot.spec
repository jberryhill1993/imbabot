# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — builds Imbabot for the platform it runs on.

  • Windows  ->  dist/Imbabot.exe        (single-file, double-clickable)
  • macOS    ->  dist/Imbabot.app        (proper onedir .app bundle)

PyInstaller cannot cross-compile, so build each on its own OS (or use the
GitHub Actions matrix in .github/workflows/build-exe.yml, which builds both).

Build:
    pip install -r requirements.txt pyinstaller
    pyinstaller imbabot.spec --noconfirm

Notes
- collect_all('tzdata') bundles the IANA tz DB so the 09:30 ET scheduling works on
  a clean machine (otherwise zoneinfo raises "No time zone found").
- keyring's backends load dynamically -> pulled in as hiddenimports.
- Playwright is excluded to keep the binary lean; browser mode runs from source.
- Drop an icon at assets/imbabot.ico (Windows) / assets/imbabot.icns (macOS).
"""
import importlib.util
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# tzdata: timezone DB for 09:30 ET scheduling (always required on a clean machine).
# selenium: the OPTIONAL browser backend that drives the user's installed Chrome — only
#   collected if installed, since the API-only straddle bot doesn't need it. When present,
#   its bundled selenium-manager binary (a data file) that resolves chromedriver is included.
_bundle = ["tzdata"]
if importlib.util.find_spec("selenium") is not None:
    _bundle.append("selenium")
for pkg in _bundle:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += collect_submodules("keyring")
hiddenimports += ["imbabot.gui", "imbabot.engine", "imbabot.projectx", "imbabot.cli"]

# Browser selector packs are JSON data files PyInstaller won't auto-collect — bundle
# them so the (calibrated) packs ship inside the .exe/.app for downloaders.
datas += [("imbabot/browser/selectors", "imbabot/browser/selectors")]

# Analyzer data (economic-event calendar) — bundled so calendar.py resolves it under
# sys._MEIPASS in the frozen app (0.2.1+ Morning Plan).
datas += [("imbabot/analysis/data", "imbabot/analysis/data")]

_is_mac = sys.platform == "darwin"
# Platform-correct icon format: macOS EXE/.app uses .icns, Windows .exe uses .ico.
if _is_mac:
    _icon = "assets/imbabot.icns" if os.path.exists("assets/imbabot.icns") else None
else:
    _icon = "assets/imbabot.ico" if os.path.exists("assets/imbabot.ico") else None

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Keep it lean: browser mode (Playwright + Chromium) runs from source.
    excludes=["numpy", "pandas", "matplotlib", "PyQt5", "PySide2", "playwright"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if _is_mac:
    # onedir + .app bundle (the supported macOS GUI layout)
    exe = EXE(
        pyz, a.scripts, [], exclude_binaries=True,
        name="Imbabot", console=False, strip=False, upx=False, icon=_icon,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Imbabot")
    app = BUNDLE(
        coll,
        name="Imbabot.app",
        icon=_icon,
        bundle_identifier="com.imbabot.app",
        info_plist={
            "CFBundleName": "Imbabot",
            "CFBundleDisplayName": "Imbabot",
            "CFBundleShortVersionString": "0.2.0",
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.finance",
        },
    )
else:
    # Windows / Linux: single-file executable
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="Imbabot", debug=False, bootloader_ignore_signals=False, strip=False,
        upx=True, runtime_tmpdir=None, console=False,
        disable_windowed_traceback=False, icon=_icon,
    )
