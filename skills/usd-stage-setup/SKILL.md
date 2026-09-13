---
name: "usd-stage-setup"
description: "Use when a user wants to set up the USD Stage for Blender CLI tool; locate the Blender executable and plugin install path, verify connectivity, and configure a shell alias. Also use to troubleshoot CLI errors like 'Blender not found' or 'Plugin not loaded'."
---

# USD Stage for Blender CLI setup

Find Blender and the installed add-on, check that the CLI can drive them, and
set up the `usdstage` alias the `usd-stage-cli` skill uses.

## Prerequisites

- Blender 5.2 or newer.
- The USD Stage for Blender add-on installed and enabled. The release zip,
  `usd-stage-for-blender-<version>.zip`, comes from the private
  `studiomeije/usd-stage-blender` repository (ask tom@studiomeije.com for
  access) and installs through **Edit ▸ Preferences ▸ Extensions ▸ Add-ons ▸
  Install from Disk…**. A development install symlinks `<repo>/Plugin` to
  `extensions/user_default/usd_stage`.
- Python 3 on the path (`python3`, or `py` on Windows).

## 1. Find Blender

Use the path the user gave, if any. Otherwise check the usual places:

```bash
# macOS
test -x /Applications/Blender.app/Contents/MacOS/Blender && echo found
# Linux
command -v blender
```

```powershell
# Windows
Get-ChildItem "C:\Program Files\Blender Foundation\*\blender.exe"
```

If nothing turns up, ask the user where Blender is.

## 2. Find the extension folder

Blender installs the add-on under its manifest id, `usd_stage`, and the CLI
lives at the top of that folder:

```bash
# macOS
find ~/Library/Application\ Support/Blender -path "*/extensions/*/usd_stage/cli/__main__.py"
# Linux
find ~/.config/blender -path "*/extensions/*/usd_stage/cli/__main__.py"
```

```powershell
# Windows
Get-ChildItem "$env:APPDATA\Blender Foundation\Blender\*\extensions\*\usd_stage\cli\__main__.py"
```

The extension folder is the `usd_stage` directory in the result. In a
repository checkout it is `<repo>/Plugin`.

## 3. Check the connection

```bash
python3 /path/to/usd_stage --blender /path/to/blender version
python3 /path/to/usd_stage --blender /path/to/blender settings list
```

`version` shows that Blender starts. `settings list` also shows that the
add-on loads. If a check fails:

| Message | Exit | Cause | Fix |
|---------|------|-------|-----|
| `Blender not found at '…'` | 2 | Wrong Blender path | Set `USDSTAGE_BLENDER` or pass `--blender` |
| `No output from Blender (exit code …)` | 1 | Blender crashed while starting | Re-run with `--verbose` and read Blender's error |
| `USD Stage for Blender add-on could not be loaded` or `Failed to import command registry` | 3 | The add-on is not enabled, or the path is not the extension folder | Enable the add-on in Blender's preferences and check the path from step 2 |

## 4. Set up the shell

macOS and Linux, in `~/.zshrc` or `~/.bashrc`:

```bash
export USDSTAGE_BLENDER="/path/to/blender"
alias usdstage="python3 /path/to/usd_stage"
```

Windows, in the PowerShell profile (`notepad $PROFILE`):

```powershell
$env:USDSTAGE_BLENDER = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
function usdstage { py "$env:APPDATA\Blender Foundation\Blender\5.2\extensions\user_default\usd_stage" @args }
```

Open a new terminal and run `usdstage version`.

## 5. Report

Tell the user the Blender path, the extension folder, whether the alias is
set up, and the output of `usdstage version`. For reporting an export
problem, the `usd-stage-cli` skill covers support bundles.
