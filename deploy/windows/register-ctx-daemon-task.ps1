<#
  chrono-ctx watch daemon, as a Windows Scheduled Task (run at log on).

  Install (helpdesk / operator, run once on the shared machine, elevated
  PowerShell not required for a per-user logon task):
    cd path\to\chrono-ctx
    .\deploy\windows\register-ctx-daemon-task.ps1

  `uv run ctx daemon start` spawns a detached child and returns immediately
  (see spec 019), so the task action is "run once at log on", not a service
  supervised as long-running - matches how ctx daemon already backgrounds
  itself with its own PID file.
#>

$repoPath = (Get-Location).Path
$uvPath = (Get-Command uv -ErrorAction Stop).Source

$action = New-ScheduledTaskAction -Execute $uvPath -Argument "run ctx daemon start" -WorkingDirectory $repoPath
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "chrono-ctx-daemon" -Action $action -Trigger $trigger -Settings $settings -Description "Starts the chrono-ctx watch daemon at log on" -Force

Write-Host "Registered scheduled task 'chrono-ctx-daemon' for $repoPath"
Write-Host "Verify: Start-ScheduledTask -TaskName chrono-ctx-daemon; then 'uv run ctx daemon status'"
