# Builds dist\Hoard.exe: a single self-contained Windows executable.
# Usage (from anywhere):  powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $root

python -m pip install --quiet --upgrade pyinstaller

# Fast-scan helper (Rust). Optional: without it Hoard just uses the normal scanner.
$extra = @()
$cargo = (Get-Command cargo -ErrorAction SilentlyContinue).Source
if (-not $cargo) { $c = Join-Path $env:USERPROFILE ".cargo\bin\cargo.exe"; if (Test-Path $c) { $cargo = $c } }
if ($cargo) {
  & $cargo build --release --manifest-path "$root\native\Cargo.toml"
  if ($LASTEXITCODE -ne 0) { throw "Building hoard-mft failed" }
  New-Item -ItemType Directory -Force "$root\hoard\bin" | Out-Null
  Copy-Item "$root\native\target\release\hoard-mft.exe" "$root\hoard\bin\hoard-mft.exe" -Force
  $extra = @("--add-binary", "$root\hoard\bin\hoard-mft.exe;.")
} else {
  Write-Warning "Rust isn't installed, so the fast NTFS scanner is left out of this build."
}

python -m PyInstaller --noconfirm --clean --onefile --windowed --name Hoard `
  --icon "$root\packaging\icons\hoard.ico" `
  @extra `
  --add-data "$root\hoard\ui.html;." `
  --paths "$root\hoard" `
  --distpath "$root\dist" --workpath "$root\build\work" --specpath "$root\build" `
  --log-level WARN `
  "$root\hoard\Hoard.pyw"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Write-Host "Built $root\dist\Hoard.exe"
