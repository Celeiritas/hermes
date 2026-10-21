#Requires -Version 5
<#
install-sendto.ps1 - the Hermes entries in Explorer's right-click > Send To menu, with the Hermes icon.
  install-sendto.ps1              makes (or refreshes) the five Send To entries
  install-sendto.ps1 -Desktop     also "Hermes · Magnet" and "Hermes · Collect" on the Desktop
  install-sendto.ps1 -Remove      moves every entry this script made to %APPDATA%\Hermes\.set-aside-<date>\ (nothing is deleted)
  install-sendto.ps1 -Icon <file.ico>   use another icon

Each entry runs hermes.ps1 from this folder in its own window (-Window: it pauses before closing, and stays open
after a failure). The icon: hermes.ico next to this script if there is one, else one is made from hermes.png
(the Hermes medallion from argus-ui's Leafy Realm theme), else PowerShell's own.
#>
param([string]$Icon, [switch]$Desktop, [switch]$Remove)
$ErrorActionPreference = 'Stop'

$Script = Join-Path $PSScriptRoot 'hermes.ps1'
if (-not (Test-Path -LiteralPath $Script)) { throw "hermes.ps1 is not next to this script ($PSScriptRoot)" }
$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$SendTo = [Environment]::GetFolderPath('SendTo')
if (-not $SendTo) { $SendTo = Join-Path $env:APPDATA 'Microsoft\Windows\SendTo' }
$DesktopDir = [Environment]::GetFolderPath('Desktop')

# name -> hermes.ps1 command. Send To appends the selected files after these arguments.
$Entries = [ordered]@{
    'Hermes · Mail'    = 'send servermail'
    'Hermes · Torrent' = 'torrent'
    'Hermes · Magnet'  = 'magnet'
    'Hermes · Lesson'  = 'send lesson'
    'Hermes · ROMs'    = 'send roms'
}
$DesktopEntries = [ordered]@{
    'Hermes · Magnet'  = 'magnet'
    'Hermes · Collect' = 'collect'
}

function Get-Icon {
    if ($Icon) { if (Test-Path -LiteralPath $Icon) { return (Resolve-Path -LiteralPath $Icon).Path }; throw "icon not found: $Icon" }
    $ico = Join-Path $PSScriptRoot 'hermes.ico'
    if (Test-Path -LiteralPath $ico) { return $ico }
    $png = Join-Path $PSScriptRoot 'hermes.png'
    if (Test-Path -LiteralPath $png) {
        try {
            Add-Type -AssemblyName System.Drawing
            $bmp = New-Object System.Drawing.Bitmap $png
            $handle = $bmp.GetHicon()
            $i = [System.Drawing.Icon]::FromHandle($handle)
            $fs = [IO.File]::Create($ico)
            try { $i.Save($fs) } finally { $fs.Close() }
            $i.Dispose(); $bmp.Dispose()
            Write-Host "Made $ico from hermes.png"
            return $ico
        } catch { Write-Host "Could not make an icon from hermes.png ($($_.Exception.Message)); using PowerShell's" -ForegroundColor Yellow }
    }
    return $PowerShell
}

function Set-Shortcut($folder, $name, $command, $iconPath) {
    $path = Join-Path $folder ($name + '.lnk')
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $PowerShell
    $lnk.Arguments = ('-NoProfile -ExecutionPolicy Bypass -File "{0}" -Window {1}' -f $Script, $command)
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.IconLocation = $iconPath + ',0'
    $lnk.Description = 'Hermes: ' + $command
    $lnk.Save()
    Write-Host ("  {0}  ->  hermes.ps1 {1}" -f $path, $command)
}

if ($Remove) {
    $aside = Join-Path (Join-Path $env:APPDATA 'Hermes') ('.set-aside-' + (Get-Date -Format 'yyyyMMdd-HHmm'))
    $n = 0
    foreach ($pair in @(@($SendTo, $Entries.Keys), @($DesktopDir, $DesktopEntries.Keys))) {
        foreach ($name in $pair[1]) {
            $path = Join-Path $pair[0] ($name + '.lnk')
            if (Test-Path -LiteralPath $path) {
                New-Item -ItemType Directory -Force -Path $aside | Out-Null
                Move-Item -LiteralPath $path -Destination (Join-Path $aside ((Split-Path $pair[0] -Leaf) + '-' + $name + '.lnk'))
                $n++
            }
        }
    }
    if ($n) { Write-Host "Moved $n shortcut(s) to $aside (nothing deleted)." } else { Write-Host 'No Hermes shortcuts to remove.' }
    exit 0
}

$iconPath = Get-Icon
Write-Host "Send To entries ($SendTo):"
foreach ($name in $Entries.Keys) { Set-Shortcut $SendTo $name $Entries[$name] $iconPath }
if ($Desktop) {
    Write-Host "Desktop ($DesktopDir):"
    foreach ($name in $DesktopEntries.Keys) { Set-Shortcut $DesktopDir $name $DesktopEntries[$name] $iconPath }
}
Write-Host 'Done. Right-click a file > Send To > Hermes · Mail (or Torrent, Magnet, Lesson, ROMs).' -ForegroundColor Green
