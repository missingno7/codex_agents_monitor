# Installs codex-dashboard into %USERPROFILE%\.codex-dashboard and puts `codex-dashboard` on the user PATH.
# Re-run after editing the sources to update the installed copy.
$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot
$dst = Join-Path $env:USERPROFILE '.codex-dashboard'
$bin = Join-Path $dst 'bin'

New-Item -ItemType Directory -Force -Path $dst, $bin, (Join-Path $dst 'static') | Out-Null
Copy-Item (Join-Path $src 'codex_dashboard.py') $dst -Force
Copy-Item (Join-Path $src 'cx.py') $dst -Force
Copy-Item (Join-Path $src 'static\index.html') (Join-Path $dst 'static') -Force
Copy-Item (Join-Path $src 'README.md') $dst -Force

# cmd / PowerShell shim
Set-Content -Encoding ascii -Path (Join-Path $bin 'codex-dashboard.cmd') -Value @'
@echo off
python "%~dp0..\codex_dashboard.py" %*
'@
Set-Content -Encoding ascii -Path (Join-Path $bin 'cx.cmd') -Value @'
@echo off
python "%~dp0..\cx.py" %*
'@
# Git Bash / MSYS shim for the human command (extensionless, LF line endings).
# No such shim for cx on purpose: a shebang script breaks the Windows parent/child chain, so stopping
# the background task would no longer kill the Codex worker. Agents run `python ~/.codex-dashboard/cx.py`.
$sh = "#!/bin/sh`nexec python `"`$(dirname `"`$0`")/../codex_dashboard.py`" `"`$@`"`n"
[IO.File]::WriteAllText((Join-Path $bin 'codex-dashboard'), $sh)
Remove-Item (Join-Path $bin 'cx') -ErrorAction SilentlyContinue

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$parts = @($userPath -split ';' | Where-Object { $_ })
if ($parts -notcontains $bin) {
    [Environment]::SetEnvironmentVariable('Path', (($parts + $bin) -join ';'), 'User')
    Write-Host "Added $bin to the user PATH (open a new terminal to use it)."
}
Write-Host "Installed to $dst"
Write-Host "Run:  codex-dashboard      then open http://localhost:8765/"
