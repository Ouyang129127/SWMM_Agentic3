$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"
Set-Location $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (!(Test-Path -LiteralPath $taskPython)) { $taskPython = "python" }
& $taskPython -m uvicorn web_app:app --host 127.0.0.1 --port 8001
exit $LASTEXITCODE
