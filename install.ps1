# Installs codex-dashboard and cx into %USERPROFILE%\.codex-dashboard, puts `codex-dashboard` on the user PATH,
# and installs the codex-workers skill into %USERPROFILE%\.claude\skills.
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

# The Claude Code skill that teaches supervisors to use cx (versioned here, installed per user).
$skill = Join-Path $env:USERPROFILE '.claude\skills\codex-workers'
New-Item -ItemType Directory -Force -Path $skill | Out-Null
Copy-Item (Join-Path $src 'skills\codex-workers\SKILL.md') $skill -Force

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
