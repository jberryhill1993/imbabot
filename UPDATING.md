# Pushing an update (OneDrive replace-in-place)

You share Imbabot as a single `Imbabot-Download.zip` on OneDrive. To ship a new version,
you rebuild that zip **in place** so the share link you already sent stays the same — people
just re-download it.

## One command

From the project folder, in PowerShell:

```powershell
# Rebuild + repackage, keep the current version number:
./publish_update.ps1

# Or bump the version first (shows in the app's title bar), then build:
./publish_update.ps1 -NewVersion 0.3.0
```

This runs the self-test, rebuilds `Imbabot.exe`, and **overwrites**
`…\OneDrive\Desktop\Imbabot-Download.zip` with the new build (keeping `READ ME FIRST.txt`
inside it).

## Two things to know

1. **Wait for OneDrive to sync.** After the script finishes, the new zip needs to upload.
   In File Explorer the file icon goes from the blue sync arrows to a **green check** when it's
   done. Don't tell people to download until you see the check.
2. **It's a manual re-download for them.** Nothing is pushed to anyone's computer. People who
   already have the old version get the update only when they open the same link again and
   re-download. They'll re-see the one-time Windows prompt (**More info → Run anyway**) because
   the app is unsigned — that's expected.

## Tip: version numbers

Bumping the version with `-NewVersion` updates the number shown in the app's title bar
(`Imbabot 0.3.0`). That's the easiest way for you and your users to confirm they're running the
newest build, since there's no automatic "update available" popup in this setup.
