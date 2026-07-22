# Publishing an Imbabot update (GitHub Releases → both machines auto-pull)

**As of 0.2.5, Imbabot self-updates from GitHub Releases** on the fork
(`github.com/jberryhill1993/imbabot`). Publish an update **once** and every
machine picks it up — no more Google Drive / OneDrive reupload-and-redownload.
(The old `publish_update.ps1` OneDrive flow is retired.)

Two layers:
- **Model/data → silent & automatic.** Each launch fetches `analysis-data.zip`
  when its version is newer and installs it into `%APPDATA%\imbabot\analysis`.
  This delivers weekly model retrains with zero user action.
- **Program/code → notify + one-click.** The bot compares its `__version__` to
  the latest release tag; a green **⬆ Update** button appears in the header and,
  on click, downloads + checksum-verifies the new build, swaps the exe, and
  relaunches.

Every download is HTTPS and its SHA-256 is verified against `checksums.txt`
before anything is extracted or run. Nothing runs on a checksum mismatch.

## Prerequisites (one time)
- The fork must be **public** (unauthenticated GitHub API + asset downloads).
- `gh` CLI authenticated with push access to the fork.

## Release recipe
Bump `imbabot/__init__.py` `__version__` (e.g. `0.2.6`), then:

```bash
VER=0.2.6

# 1) Build the app zip (PyInstaller) -> Imbabot-$VER.zip containing Imbabot.exe (+ bundled data).
#    The model is bundled in the exe, so a fresh install is calibrated even before the data sync.

# 2) Build the data bundle (latest calibrated model + dailies)
python - <<'PY'
import zipfile, os
base = os.path.join(os.environ["APPDATA"], "imbabot", "analysis")
with zipfile.ZipFile("analysis-data.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for n in ("spike_model.json", "VIX_daily.json", "NQF_daily.json"):
        z.write(os.path.join(base, n), n)
PY

# 3) Checksums (sha256sum format: "<hash>␣␣<name>")
sha256sum Imbabot-$VER.zip analysis-data.zip > checksums.txt

# 4) Publish
gh release create v$VER --repo jberryhill1993/imbabot \
   --title "Imbabot $VER" --notes "What changed…" \
   Imbabot-$VER.zip analysis-data.zip checksums.txt
```

Both machines will show the Update button on next launch (code) and silently
pull the new model (data).

## Weekly model refit (data-only, no code change)
After `analyze-ticks <zip> --fit`, refresh the committed bundle so fresh installs
ship the newest model, then publish a data-only release:
```bash
cp %APPDATA%\imbabot\analysis\{spike_model,VIX_daily,NQF_daily}.json imbabot/analysis/data/model/
git commit -am "weekly model refit" && git push fork <branch>
# rebuild analysis-data.zip + checksums.txt (steps 2-3) and gh release create v<VER>
```

## Notes
- Source runs (`python run.py`, the dev bot) do the silent **data** sync + show
  the banner, but the **code** swap is a no-op (update via git) — the exe swap
  only applies to the packaged app.
- Rolling back = publish an older build under a newer tag, or reinstall from a
  prior release's assets.
