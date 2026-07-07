# One-time setup for Windows (PowerShell): create a venv + install deps.
#   Right-click > Run with PowerShell, or:  powershell -ExecutionPolicy Bypass -File setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" }
      elseif (Get-Command python -ErrorAction SilentlyContinue) { "python" }
      else { $null }
if (-not $py) {
  Write-Error "Python 3 not found. Install from https://www.python.org/downloads/ (tick 'Add to PATH')."
  exit 1
}

& $py -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host ""
Write-Host "Setup complete. Next:"
Write-Host "  .\run.ps1 init     # create config.json + folders"
Write-Host "  .\run.ps1 serve    # open the dashboard (http://127.0.0.1:8042)"
