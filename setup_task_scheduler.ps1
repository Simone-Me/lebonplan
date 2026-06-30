# setup_task_scheduler.ps1
# Enregistre run_pipeline.bat dans le Planificateur de taches Windows
# pour une execution automatique chaque matin a 7h00.
#
# Execution (PowerShell admin) :
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup_task_scheduler.ps1
#
# Pour changer l'heure : modifier $RunAt ci-dessous.

param(
    [string]$RunAt = "07:00",          # Heure de declenchement (HH:mm)
    [string]$TaskName = "LeBonPlan_Pipeline_Morning"
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatchFile = Join-Path $ScriptDir "run_pipeline.bat"

if (-not (Test-Path $BatchFile)) {
    Write-Error "Fichier introuvable : $BatchFile"
    exit 1
}

$Action  = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatchFile`""
$Trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false

# Supprimer si existant
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Ancienne tache '$TaskName' supprimee."
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -RunLevel Highest `
    -Description "Pipeline LeBonPlan : Bronze -> Silver -> Gold, execute chaque matin." | Out-Null

Write-Host ""
Write-Host "Tache planifiee creee avec succes !"
Write-Host "  Nom    : $TaskName"
Write-Host "  Heure  : $RunAt chaque jour"
Write-Host "  Script : $BatchFile"
Write-Host ""
Write-Host "Pour modifier l'heure : .\setup_task_scheduler.ps1 -RunAt '06:00'"
Write-Host "Pour supprimer        : Unregister-ScheduledTask -TaskName '$TaskName'"
