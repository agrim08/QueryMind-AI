# Registers the daily eval as a Windows scheduled task. Run once:
#   powershell -ExecutionPolicy Bypass -File backend\evals\schedule.ps1
# Remove it with:
#   Unregister-ScheduledTask -TaskName "QueryMind daily eval" -Confirm:$false
#
# Runs at 1:30 AM, or as soon as possible afterwards if the PC was off or asleep. It only runs
# while you're signed in, because Docker Desktop needs your session. pythonw.exe runs it
# without a console window; output goes to backend\evals\reports\daily.log.

$backend = Split-Path $PSScriptRoot -Parent
$python = Join-Path $backend ".venv\Scripts\pythonw.exe"

$action = New-ScheduledTaskAction -Execute $python -Argument "-m evals.daily" -WorkingDirectory $backend
$trigger = New-ScheduledTaskTrigger -Daily -At 1:30am
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName "QueryMind daily eval" `
    -Description "Runs QueryMind's accuracy eval within the Gemini free-tier budget (backend\evals)." `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Force | Out-Null

Write-Output "Registered 'QueryMind daily eval' (daily at 1:30 AM; runs later if the PC was off)."
