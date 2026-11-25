$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$PyInstaller = Join-Path $RepoRoot ".venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $PyInstaller)) {
    throw "PyInstaller not found at $PyInstaller. Create .venv and install requirements-dev.txt first."
}

$BuildRoot = Join-Path $env:TEMP "localdeck-build"
$WorkDir = Join-Path $BuildRoot "work"
$DistDir = Join-Path $BuildRoot "dist"
$SpecDir = $BuildRoot

if (Test-Path $BuildRoot) {
    Remove-Item -Recurse -Force $BuildRoot
}
New-Item -ItemType Directory -Force -Path $WorkDir, $DistDir | Out-Null

$WebSrc = Join-Path $RepoRoot "localdeck\web"
$IconPath = Join-Path $WebSrc "static\localdeck.ico"
$EntryScript = Join-Path $RepoRoot "localdeck\__main__.py"

& $PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name LocalDeck `
    --icon "$IconPath" `
    --add-data "$WebSrc;localdeck\web" `
    --collect-data webview `
    --hidden-import uvicorn.logging `
    --hidden-import uvicorn.loops.auto `
    --hidden-import uvicorn.protocols.http.auto `
    --hidden-import uvicorn.protocols.websockets.auto `
    --hidden-import uvicorn.lifespan.on `
    --hidden-import websockets.sync.client `
    --hidden-import pystray._win32 `
    --workpath $WorkDir `
    --distpath $DistDir `
    --specpath $SpecDir `
    $EntryScript

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE"
}

$BuiltAppDir = Join-Path $DistDir "LocalDeck"
$InstallDir = Join-Path $env:LOCALAPPDATA "LocalDeck\app"

if (Test-Path $InstallDir) {
    Remove-Item -Recurse -Force $InstallDir
}
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Recurse -Force (Join-Path $BuiltAppDir "*") $InstallDir

$ExePath = Join-Path $InstallDir "LocalDeck.exe"
if (-not (Test-Path $ExePath)) {
    throw "Expected executable not found at $ExePath"
}

$InstalledIconPath = Join-Path $InstallDir "localdeck.ico"
Copy-Item -Force $IconPath $InstalledIconPath

$WshShell = New-Object -ComObject WScript.Shell
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "LocalDeck.lnk"
$StartMenuShortcut = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\LocalDeck.lnk"

foreach ($ShortcutPath in @($DesktopShortcut, $StartMenuShortcut)) {
    $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $ExePath
    $Shortcut.WorkingDirectory = $InstallDir
    $Shortcut.IconLocation = "$InstalledIconPath,0"
    $Shortcut.Description = "LocalDeck Wireless Stream Deck"
    $Shortcut.Save()
}

# Refresh Windows Explorer icon cache so the new icon shows immediately
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class LocalDeckShellRefresh {
    [DllImport("shell32.dll")]
    public static extern void SHChangeNotify(int wEventId, int uFlags, IntPtr dwItem1, IntPtr dwItem2);
}
"@ -ErrorAction SilentlyContinue
    [LocalDeckShellRefresh]::SHChangeNotify(0x08000000, 0x0000, [IntPtr]::Zero, [IntPtr]::Zero)
} catch {}
try { & ie4uinit.exe -show 2>$null } catch {}

Write-Host ""
Write-Host "LocalDeck installed cleanly:"
Write-Host "  App:        $ExePath"
Write-Host "  Desktop:    $DesktopShortcut"
Write-Host "  Start Menu: $StartMenuShortcut"
Write-Host ""
Write-Host "Not: Telefondan yerel Wi-Fi uzerinden baglanabilmek icin Windows Guvenlik Duvari uyarisinda 'Ozel Aglar' iznini onaylayin."
