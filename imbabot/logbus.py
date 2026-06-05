"""Tiny logging helper shared by the CLI and GUI.

Every line is timestamped and appended to a rotating-ish log file (the guide's
"the app logs everything … save the log and send it"). An optional sink callback
lets the GUI mirror lines into its log panel. Secrets are never passed here.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from .config import log_path

Sink = Callable[[str, str], None]  # (formatted_line, level)


class Logger:
    def __init__(self, sink: Optional[Sink] = None, to_file: bool = True) -> None:
        self.sink = sink
        self._path: Optional[Path] = log_path() if to_file else None
        self.lines: List[str] = []

    def __call__(self, msg: str, level: str = "info") -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {level.upper():5s} {msg}"
        self.lines.append(line)
        if self._path is not None:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except Exception:
                pass
        if self.sink:
            try:
                self.sink(line, level)
            except Exception:
                pass

    def save_copy(self, dest: Path) -> Path:
        dest.write_text("\n".join(self.lines) + "\n", encoding="utf-8")
        return dest
