# install_skill.ps1
# Install dsh-self-improve into DSH's skills directory.
# Usage: powershell -ExecutionPolicy Bypass -File .\install_skill.ps1
# (此脚本只安装 skill 本体; hooks 注册请用 deploy.ps1)

$ErrorActionPreference = "Stop"

# Source dir = this script's directory
$Src = $PSScriptRoot

# Target: prefer DSH_HOME env var, else default path
$DshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { "F:\DSH-Home\.dsh" }
$Dest = Join-Path $DshHome "skills\dsh-self-improve"

Write-Host "== Codex Self-Improve - Install =="
Write-Host "Source : $Src"
Write-Host "Target : $Dest"
Write-Host ""

if (-not (Test-Path (Join-Path $Src "SKILL.md"))) {
    Write-Host "ERROR: SKILL.md not found in source. Run this from the skill directory." -ForegroundColor Red
    exit 1
}

if (Test-Path $Dest) {
    Write-Host "Target exists. Updating (overwrite)." -ForegroundColor Yellow
}

try {
    # Preserve existing runtime learnings (do not clobber records added while Codex ran)
    $ExistingLearnings = Join-Path $Dest ".learnings"
    $HasExistingLearnings = Test-Path $ExistingLearnings
    $LearningsBackup = $null
    if ($HasExistingLearnings) {
        $LearningsBackup = Join-Path $env:TEMP ("si_learnings_" + [guid]::NewGuid().ToString("N"))
        Copy-Item $ExistingLearnings $LearningsBackup -Recurse -Force
        Write-Host "Existing learnings preserved (backup: $LearningsBackup)" -ForegroundColor Yellow
    }

    if (Test-Path $Dest) { Remove-Item $Dest -Recurse -Force }
    Copy-Item $Src $Dest -Recurse -Force

    if ($HasExistingLearnings -and $LearningsBackup) {
        Copy-Item $LearningsBackup $ExistingLearnings -Recurse -Force
        Remove-Item $LearningsBackup -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Runtime learnings restored." -ForegroundColor Yellow
    }
    Write-Host "[OK] Installed: $Dest" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "The skill will be active on DSH's next turn (no restart needed)." -ForegroundColor Cyan
Write-Host "Verify:"
Write-Host "  python `"$Dest\learn.py`" status"
Write-Host "To also register the auto hooks (SessionStart/Stop), run deploy.ps1."
