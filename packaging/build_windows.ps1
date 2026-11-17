# Builds dist\Hoard.exe: a single self-contained Windows executable.
# Usage (from anywhere):  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $root

python -m pip install --quiet --upgrade pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Hoard `
  --icon "$root\packaging\icons\hoard.ico" `
  --add-data "$root\hoard\ui.html;." `
  --paths "$root\hoard" `
  --distpath "$root\dist" --workpath "$root\build\work" --specpath "$root\build" `
  --log-level WARN `
  "$root\hoard\Hoard.pyw"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built $root\dist\Hoard.exe"
