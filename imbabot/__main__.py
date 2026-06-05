"""`python -m imbabot` launches the GUI. Use `python -m imbabot.cli ...` for CLI."""
from __future__ import annotations

import sys


def main() -> int:
    # Allow `python -m imbabot cli ...` as a convenience passthrough.
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        from .cli import main as cli_main

        return cli_main(sys.argv[2:])
    from .gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
