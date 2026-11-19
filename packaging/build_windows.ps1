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
  if ($LASTEXITCODE -ne 0) { throw "Building the native scanners failed" }
  New-Item -ItemType Directory -Force "$root\hoard\bin" | Out-Null
  foreach ($b in "hoard-mft", "hoard-walk") {
    Copy-Item "$root\native\target\release\$b.exe" "$root\hoard\bin\$b.exe" -Force
    $extra += @("--add-binary", "$root\hoard\bin\$b.exe;.")
  }
} else {
  Write-Warning "Rust isn't installed, so the native scanners are left out of this build (Hoard falls back to Python)."
}

python -m PyInstaller --noconfirm --clean --onefile --windowed --name Hoard `
  --icon "$root\packaging\icons\hoard.ico" `
  @extra `
  --add-data "$root\hoard\ui.html;." `
  --add-data "$root\hoard\assets;assets" `
  --paths "$root\hoard" `
  --distpath "$root\dist" --workpath "$root\build\work" --specpath "$root\build" `
  --log-level WARN `
  "$root\hoard\Hoard.pyw"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# Code signing, only when a certificate is provided (e.g. GitHub secrets WINDOWS_CERT_PFX as base64 + WINDOWS_CERT_PASSWORD).
# Signed builds don't show "Windows protected your PC".
if ($env:WINDOWS_CERT_PFX -and $env:WINDOWS_CERT_PASSWORD) {
  $pfx = Join-Path $env:TEMP "hoard-sign.pfx"
  [IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($env:WINDOWS_CERT_PFX))
  $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" | Sort-Object FullName -Descending | Select-Object -First 1
  & $signtool.FullName sign /f $pfx /p $env:WINDOWS_CERT_PASSWORD /fd sha256 /tr http://timestamp.digicert.com /td sha256 "$root\dist\Hoard.exe"
  Remove-Item $pfx -Force
  if ($LASTEXITCODE -ne 0) { throw "Signing failed" }
  Write-Host "Signed $root\dist\Hoard.exe"
}
Write-Host "Built $root\dist\Hoard.exe"
