# Run the Agentic World Simulator natively on Windows (Python 3.13 venv).
# PostgreSQL + Local Gemma keep running in Docker. Close this window (or Ctrl+C) to stop the app.
#   powershell -ExecutionPolicy Bypass -File .\start-native.ps1
Set-Location -LiteralPath $PSScriptRoot
$host.UI.RawUI.WindowTitle = 'Agentic World Simulator (native)'

if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    Write-Host 'Creating the Python 3.13 virtual environment...'
    py install -y 3.13
    py -V:3.13 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

# Docker prints progress on stderr; run it through cmd so PowerShell doesn't treat that as an error.
cmd /c "docker compose up -d db ollama ollama-pull >nul 2>&1"
cmd /c "docker compose stop app >nul 2>&1"   # the Docker app would hold port 8000
Write-Host 'Starting on http://localhost:8000 ...'
& .\.venv\Scripts\python.exe run.py
