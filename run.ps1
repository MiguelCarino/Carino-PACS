# Wrapper for Windows. Passes all args to the CLI.
#   .\run.ps1 serve      .\run.ps1 receive      .\run.ps1 send
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
  Write-Error "No virtualenv found - run .\setup.ps1 first."
  exit 1
}
& .\.venv\Scripts\python.exe -m pacs @args
