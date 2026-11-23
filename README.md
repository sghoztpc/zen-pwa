# Zen PWA

Chromium-style "Install as app" for [Zen Browser](https://zen-browser.app) on Windows.
A WebExtension plus a small local Python helper (Native Messaging host).

## What it does
- Works on any website: reads the site's Web App Manifest (`name`, `start_url`, `icons`, `theme_color`), or falls back to page metadata.
- Creates an isolated Zen profile per app (`%LOCALAPPDATA%\ZenPWA\profiles\<app-id>`) with a `userChrome.css` that hides the browser UI, so the site looks like a normal app window (with close / minimize / maximize).
- Copies the login cookies for that site (and its sign-in domains, e.g. Google for YouTube Music) from your main Zen profile, so you stay signed in.
- Converts the site icon to a Windows `.ico` and creates Start Menu (`Zen Apps`) and Desktop shortcuts.
- Builds a tiny launcher `.exe` per app (and one for Zen itself, `Zen Browser` in the Start Menu) so launchers like Raycast treat each app as a separate program.

## Requirements
- Windows 10/11, Zen Browser (Gecko 142+)
- Python 3.11+ on `PATH` with Pillow: `pip install Pillow`
- .NET Framework 4 `csc.exe` (built into Windows) for the launchers

## Setup
1. Register the native helper (writes `zen_pwa_host.bat` / `.json` for your machine and a registry key under `HKCU`):
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\native-host\register_host.ps1
   ```
2. Install the extension in Zen:
   - **Quick test:** open `about:debugging#/runtime/this-firefox`, click **Load Temporary Add-on...**, pick `extension/manifest.json`. It is removed when Zen restarts.
   - **Permanent:** get it signed (see below) and open the signed `.xpi` in Zen.
3. On any website, click the Zen PWA toolbar button and choose **Install as App**.

## Updating the extension
Zen only keeps signed extensions permanently.
1. Raise `version` in `extension/manifest.json`.
2. Build the package: `python .\package_xpi.py` → `dist/zen-pwa.xpi`.
3. Go to https://addons.mozilla.org/developers/addons, pick the add-on, upload a new version, choose **On your own** (unlisted).
4. After approval: **Manage Status & Versions** → the new version → click the file under **Files** while in Zen, then **Add**.

## Uninstall
- Remove apps from the extension popup (deletes profile, launcher, and shortcuts).
- Unregister the helper: `powershell -ExecutionPolicy Bypass -File .\native-host\unregister_host.ps1`

## Security notes
- The helper only accepts messages from this extension's ID.
- URLs are validated (http/https only, no backslashes); app IDs are restricted to `[a-z0-9-]`.
- Launchers are compiled with `csc.exe /noconfig` inside a private temp folder and moved into place atomically.

## Layout
```
extension/      WebExtension (popup, background, content script)
native-host/    Python helper + register/unregister scripts
package_xpi.py  Builds dist/zen-pwa.xpi
```
