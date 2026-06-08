<#
.SYNOPSIS
    Rebuild the Collections/ tree from a fresh Zotero export, then commit and push.

.DESCRIPTION
    Run this AFTER re-exporting your Zotero library (Format: BibLaTeX, with the
    "Export Files" option enabled) INTO THIS FOLDER, overwriting the .bib and
    regenerating the files/ folder.

    Place this script, update.sh and zotero_sync.py at the ROOT of your library
    repository (next to the .bib and files/).

.PARAMETER ZoteroDb
    Path to zotero.sqlite. Default: %USERPROFILE%\Zotero\zotero.sqlite

.PARAMETER NoPush
    Commit locally without pushing.

.EXAMPLE
    pwsh ./update.ps1
.EXAMPLE
    pwsh ./update.ps1 -ZoteroDb "D:\Zotero\zotero.sqlite"
#>
param(
    [string]$ZoteroDb = "$env:USERPROFILE\Zotero\zotero.sqlite",
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
Set-Location $repo

if (-not (Test-Path (Join-Path $repo "files"))) {
    Write-Error "The 'files/' folder is missing. Re-export your Zotero library (with 'Export Files' enabled) into this folder, then run this script again."
    exit 1
}
if (-not (Test-Path $ZoteroDb)) {
    Write-Error "Zotero database not found: $ZoteroDb  (use -ZoteroDb <path> if it is elsewhere)"
    exit 1
}
$python = (Get-Command python -ErrorAction SilentlyContinue) ?? (Get-Command py -ErrorAction SilentlyContinue)
if (-not $python) { Write-Error "Python not found in PATH."; exit 1 }

# 1. Copy the database (works even while Zotero is running)
$tmp = Join-Path $env:TEMP ("zot_sync_{0}.sqlite" -f ([guid]::NewGuid().ToString('N')))
Copy-Item $ZoteroDb $tmp -Force
Write-Host "Zotero database copied."

try {
    # 2. Rebuild the collection tree
    & $python.Source (Join-Path $repo "zotero_sync.py") --repo $repo --db $tmp
    if ($LASTEXITCODE -ne 0) { throw "zotero_sync.py failed (exit code $LASTEXITCODE)." }
} finally {
    Remove-Item $tmp -ErrorAction SilentlyContinue
}

# 3. Commit and push
git config core.longpaths true | Out-Null   # long nested paths on Windows
git add -A
$staged = (git diff --cached --name-only | Measure-Object -Line).Lines
if ($staged -eq 0) {
    Write-Host "Nothing to commit."
    exit 0
}
$stamp = Get-Date -Format "yyyy-MM-dd HH:mm"
git commit -m "Update Zotero library ($stamp)" | Out-Null
Write-Host "Committed ($staged files changed)."

if ($NoPush) {
    Write-Host "Local commit only (-NoPush). Run 'git push' later."
} else {
    git push
    Write-Host "Pushed to GitHub."
}
