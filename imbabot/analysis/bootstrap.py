"""Self-install the bundled Morning-Plan model into the config dir on launch.

A build ships with a calibrated `spike_model.json` (+ VIX/NQF dailies) under
`imbabot/analysis/data/model/`. On every launch we copy those into
`config_dir()/analysis/` when they're missing or older than the bundle. This
makes any install self-sufficient — no `setup-data.bat`, and no more silent
"UNCALIBRATED" fallback on a machine that never had the data (the 7/21 machine-#2
symptom). The tick cache is NOT bundled: prediction needs only the model +
dailies (`_recent_thrust` safely defaults without ticks).

Best-effort: never raises, so a packaging hiccup can't block startup.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable, List, Optional

from ..config import config_dir

# Files that make the Morning Plan calibrated. Order not significant.
_BUNDLED = ("spike_model.json", "VIX_daily.json", "NQF_daily.json")


def bundled_model_dir() -> Path:
    """The packaged model dir — PyInstaller `_MEIPASS` when frozen, else source."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "imbabot" / "analysis" / "data" / "model"  # type: ignore[attr-defined]
    return Path(__file__).parent / "data" / "model"


def install_bundled_analysis(log: Optional[Callable[..., None]] = None) -> List[str]:
    """Copy bundled model files into config_dir()/analysis when missing/newer.

    Returns the list of filenames installed (empty if all already current).
    """
    installed: List[str] = []
    try:
        src_dir = bundled_model_dir()
        if not src_dir.is_dir():
            return installed
        dst_dir = config_dir() / "analysis"
        dst_dir.mkdir(parents=True, exist_ok=True)
        for name in _BUNDLED:
            src = src_dir / name
            if not src.is_file():
                continue
            dst = dst_dir / name
            if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
                continue          # local copy is same-age-or-newer — keep it
            shutil.copy2(src, dst)
            installed.append(name)
        if installed and log:
            log(f"Installed calibrated Morning-Plan data into {dst_dir} "
                f"({', '.join(installed)}).")
    except Exception as exc:      # never block launch on a data-install hiccup
        if log:
            log(f"Bundled-model install skipped: {exc}", "warn")
    return installed
