# DUE-E: install + test on Windows.
# Run from the due-e folder:  powershell -ExecutionPolicy Bypass -File .\test-due-e.ps1
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$results = @()
function Step($n, $title) { Write-Host "`n== $n. $title" -ForegroundColor Cyan }
function Record($name) {
    $ok = ($LASTEXITCODE -eq 0)
    $script:results += [pscustomobject]@{ Step = $name; Result = $(if ($ok) { "PASS" } else { "FAIL (exit $LASTEXITCODE)" }) }
    if ($ok) { Write-Host "   PASS" -ForegroundColor Green } else { Write-Host "   FAIL" -ForegroundColor Red }
}

Step 1 "Python"
$py = $null
foreach ($cand in @("py -3", "python")) {
    $exe, $arg = $cand.Split(" ")
    if (Get-Command $exe -ErrorAction SilentlyContinue) {
        $v = & $exe $arg -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($v -and [version]$v -ge [version]"3.10") { $py = $cand; Write-Host "   using $cand (Python $v)"; break }
    }
}
if (-not $py) { Write-Host "   Need Python 3.10+ - install from python.org (tick 'Add to PATH')." -ForegroundColor Red; exit 1 }

Step 2 "Virtual environment + install"
if (-not (Test-Path .venv)) { $exe, $arg = $py.Split(" "); & $exe $arg -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -e ".[dev]"
Record "install"

Step 3 "Offline test suite"
& .\.venv\Scripts\python.exe -m pytest -q
Record "pytest"

Step 4 "Calendar feeds (Canvas, your schedule, MASA, AIBG)"
& .\.venv\Scripts\due-e.exe check --feeds-only
Record "feeds"

Step 5 "Preview this morning's post (printed here, not sent)"
& .\.venv\Scripts\due-e.exe preview
Record "preview"

Step 6 "Send a test message to Discord"
& .\.venv\Scripts\due-e.exe post --kind test
Record "discord"

Write-Host "`n== Summary" -ForegroundColor Cyan
$results | Format-Table -AutoSize
if ($results.Result -contains "PASS" -and -not ($results.Result | Where-Object { $_ -like "FAIL*" })) {
    Write-Host "All good. Start DUE-E with run-due-e.bat (or: .\.venv\Scripts\due-e.exe run), then open http://127.0.0.1:8787" -ForegroundColor Green
} else {
    Write-Host "Something failed - copy the output above into the chat." -ForegroundColor Yellow
}
