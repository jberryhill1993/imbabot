# Changelog

All notable changes to Imbabot are recorded here. This file lives on the development
branch (`v0.2.1-dev`); the stable, shipped build is **0.2.0** on `main`.

The format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Versions use the number shown in the app's title bar (`Imbabot <version>`).

## [0.2.1] - Unreleased (in development on `v0.2.1-dev`)

New work and the decisions behind it go here as we build them. Nothing in this section
ships until 0.2.1 is released (merged to `main`, `-dev` suffix dropped, tagged `v0.2.1`).

### Added
- _(nothing yet)_

### Changed
- _(nothing yet)_

### Fixed
- _(nothing yet)_

---

## [0.2.0] - Stable (shipped)

The current downloadable build. Frozen on `main`; distributed as
`Imbabot-Download.zip` via OneDrive.

### Highlights
- **Naked-entry opening-range straddle** — places exactly two stop-market entries
  (one buy stop above, one sell stop below the reference price) a few seconds before
  the 09:30 ET cash open. SL/TP attach from TopStep Position Brackets on fill.
- **Trade modes** — semi-auto, one-trade (OCO: opposite entry auto-cancels on fill),
  and two-trade (both sides allowed to fill).
- **Optional bot-managed brackets** — per-leg stop-loss / take-profit toggles for users
  who run TopStep Auto OCO instead of Position Brackets.
- **Weekday auto-fire schedule** — arm once; the bot self-re-arms and fires Mon–Fri at a
  chosen local time. Plus a one-shot test-fire time on the Test tab.
- **GUI** — required fields highlighted, optional SL/TP grayed behind enable toggles,
  collapsible activity log, fit-to-content window, live NQ/MNQ price + VIX readout.
- **Packaging** — single-file Windows `Imbabot.exe` (PyInstaller); `publish_update.ps1`
  rebuilds + repackages the OneDrive download in place (see `UPDATING.md`).
- **Self-test** — 62 offline checks (`Imbabot.exe cli selftest`).
