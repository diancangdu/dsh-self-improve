# deploy.ps1 - dsh-self-improve one-click deploy (lazy install)
# Does: 1) install skill 2) copy hooks 3) register hooks in managed_config.toml 4) add to [skills] enabled
# Idempotent: safe to run multiple times. Backs up config before modifying.
# Usage: powershell -ExecutionPolicy Bypass -File .\deploy.ps1
$ErrorActionPreference = "Stop"

$Src = $PSScriptRoot
$DshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { "F:\DSH-Home\.dsh" }
$SkillName = "dsh-self-improve"
$SkillDest = Join-Path $DshHome "skills\$SkillName"
$HooksDir = Join-Path $DshHome "hooks"
$ManagedCfg = Join-Path $DshHome "managed_config.toml"

# ---- 0a. Self-reference guard (added 2026-09-25) ----
# On this machine the skill is deployed IN PLACE: this script lives inside the
# skill directory, so $Src and $SkillDest are the same folder. Without this
# guard the script does DELETE-then-COPY on its own source, which destroys the
# skill directory (including the runtime .learnings library) and then fails at
# the copy step because the source no longer exists.
$srcFull = (Resolve-Path $Src).Path.TrimEnd('\')
$dstFull = if (Test-Path $SkillDest) { (Resolve-Path $SkillDest).Path.TrimEnd('\') } else { $SkillDest.TrimEnd('\') }
if ($srcFull -ieq $dstFull) {
    Write-Host "ERROR: source and destination are the SAME directory." -ForegroundColor Red
    Write-Host "  Src       = $srcFull"
    Write-Host "  SkillDest = $dstFull"
    Write-Host "This script reinstalls by delete-then-copy, so running it in place"
    Write-Host "would wipe the skill directory and the .learnings library."
    Write-Host "To reinstall: copy this folder somewhere else and run deploy.ps1"
    Write-Host "from that copy, or edit the files in place directly."
    exit 1
}

Write-Host "== Codex Self-Improve - Deploy =="
Write-Host "Skill : $Src -> $SkillDest"
Write-Host "Hooks : $HooksDir"
Write-Host "Config: $ManagedCfg"
Write-Host ""

# ---- 0. Preconditions ----
if (-not (Test-Path (Join-Path $Src "SKILL.md"))) {
    Write-Host "ERROR: SKILL.md not found in source." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $ManagedCfg)) {
    Write-Host "ERROR: managed_config.toml not found at $ManagedCfg" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path (Join-Path $DshHome "hooks\codex_hook_adapter.py"))) {
    Write-Host "ERROR: codex_hook_adapter.py not found in $HooksDir (planning-with-files hooks not installed)." -ForegroundColor Red
    exit 1
}

# ---- 1. Install skill ----
Write-Host "[1/5] Installing skill..."
if (Test-Path $SkillDest) {
    # preserve runtime learnings
    $tmpL = Join-Path $env:TEMP ("si_learn_" + [guid]::NewGuid().ToString("N"))
    $hasL = Test-Path (Join-Path $SkillDest ".learnings")
    if ($hasL) { Copy-Item (Join-Path $SkillDest ".learnings") $tmpL -Recurse -Force }
    # Restore in finally: if the copy step throws, the stashed .learnings must
    # still be put back, otherwise the library is left orphaned in %TEMP%.
    try {
        Remove-Item $SkillDest -Recurse -Force
        Copy-Item $Src $SkillDest -Recurse -Force
    } finally {
        if ($hasL -and (Test-Path $tmpL)) {
            if (-not (Test-Path $SkillDest)) { New-Item -ItemType Directory -Path $SkillDest -Force | Out-Null }
            Copy-Item $tmpL (Join-Path $SkillDest ".learnings") -Recurse -Force
            Remove-Item $tmpL -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
} else {
    Copy-Item $Src $SkillDest -Recurse -Force
}
Write-Host "  [OK] skill installed" -ForegroundColor Green

# ---- 2. Copy hooks ----
Write-Host "[2/5] Copying hook scripts..."
Copy-Item (Join-Path $Src "hooks\si_session_start.py") $HooksDir -Force
Copy-Item (Join-Path $Src "hooks\si_stop.py") $HooksDir -Force
Write-Host "  [OK] hooks copied" -ForegroundColor Green

# ---- 3+4. Modify managed_config.toml + [skills] enabled via Python ----
Write-Host "[3/5] Registering hooks + skill in config (Python)..."
$pyCode = @'
# -*- coding: utf-8 -*-
import sys, os
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

cfg = r"__CFG__"
skill = "__SKILL__"
hooksdir = r"__HOOKSDIR__"
lines = []
with open(cfg, "r", encoding="utf-8") as f:
    text = f.read()

changed = False
messages = []

# --- a) ensure hooks are registered (append blocks if not present) ---
si_session_block = f'''\n# --- Self-Improve: inject learnings at session start ---
[[hooks.SessionStart]]
[[hooks.SessionStart.hooks]]
type = "command"
command = "python3 \\"{hooksdir}/si_session_start.py\\" 2>/dev/null || true"
commandWindows = "cmd /c {hooksdir}\\pwf-hook.cmd si_session_start.py"
statusMessage = "Loading self-improve learnings"
timeout = 10
'''
si_stop_block = f'''\n# --- Self-Improve: reflection reminder at stop ---
[[hooks.Stop]]
[[hooks.Stop.hooks]]
type = "command"
command = "python3 \\"{hooksdir}/si_stop.py\\" 2>/dev/null || true"
commandWindows = "cmd /c {hooksdir}\\pwf-hook.cmd si_stop.py"
statusMessage = "Self-improve reflection check"
timeout = 10
'''

if "si_session_start.py" not in text:
    text += si_session_block
    messages.append("hooks.SessionStart (si_session_start.py) registered")
    changed = True
else:
    messages.append("hooks.SessionStart already present, skip")

if "si_stop.py" not in text:
    text += si_stop_block
    messages.append("hooks.Stop (si_stop.py) registered")
    changed = True
else:
    messages.append("hooks.Stop already present, skip")

# --- b) ensure [skills] enabled contains this skill ---
import re
m = re.search(r"\[skills\]\s*\n\s*enabled\s*=\s*\[([^\]]*)\]", text)
if m:
    inside = m.group(1)
    items = [x.strip().strip('"').strip("'") for x in inside.split(",") if x.strip()]
    if skill not in items:
        items.append(skill)
        new_list = "[" + ", ".join('"%s"' % x for x in items) + "]"
        text = text[:m.start(1)] + new_list + text[m.end(1):]
        messages.append("skills.enabled added " + skill)
        changed = True
    else:
        messages.append("skills.enabled already contains " + skill)
else:
    # no [skills] section: append
    text += f'\n[skills]\nenabled = ["{skill}"]\n'
    messages.append("skills.enabled created with " + skill)
    changed = True

with open(cfg, "w", encoding="utf-8") as f:
    f.write(text)

for msg in messages:
    print("  - " + msg)
print("changed=" + str(changed))
'@

# substitute placeholders
$pyCode = $pyCode.Replace('__CFG__', $ManagedCfg.Replace('\', '\\'))
$pyCode = $pyCode.Replace('__SKILL__', $SkillName)
$pyCode = $pyCode.Replace('__HOOKSDIR__', $HooksDir.Replace('\', '\\'))

# Backup config before modifying
$bak = Join-Path $DshHome ("managed_config.toml.bak_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
Copy-Item $ManagedCfg $bak -Force
Write-Host "  [OK] config backed up: $bak" -ForegroundColor Yellow

# Run python (prefer absolute python, fallback to py/python)
$pyTmp = Join-Path $env:TEMP ("si_deploy_" + [guid]::NewGuid().ToString("N") + ".py")
[System.IO.File]::WriteAllText($pyTmp, $pyCode, [System.Text.UTF8Encoding]::new($false))

$python = "python"
foreach ($cand in @("D:\Python\python3.14.7\python.exe", "python3", "py", "python")) {
    if ($cand -eq "python") { break }
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cand; break }
}

try {
    $out = & $python -X utf8 $pyTmp 2>&1
    $out | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] Python config update exited $LASTEXITCODE" -ForegroundColor Red
        exit 1
    }
} finally {
    Remove-Item $pyTmp -Force -ErrorAction SilentlyContinue
}
Write-Host "  [OK] config updated" -ForegroundColor Green

# ---- 5. Verify ----
Write-Host "[5/5] Verifying..."
$selfTest = & $python -X utf8 (Join-Path $SkillDest "learn.py") status 2>&1
$selfTest | ForEach-Object { Write-Host "  $_" }
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] self-test failed" -ForegroundColor Red
    exit 1
}
Write-Host ""
Write-Host "[DONE] dsh-self-improve deployed. Active on next Codex turn." -ForegroundColor Green
Write-Host "Verify hooks registered:"
Write-Host "  Select-String -Path `"$ManagedCfg`" -Pattern 'si_'"
