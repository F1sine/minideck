<#
.SYNOPSIS
  Installs MinideckServer.exe as an always-on server on a Windows machine.

.DESCRIPTION
  - copies MinideckServer.exe to C:\Program Files\MinideckServer
  - keeps data (database, certificate, key) in C:\ProgramData\MinideckServer,
    readable only by Administrators and SYSTEM
  - registers a scheduled task that starts the server at boot as SYSTEM and
    restarts it if it stops
  - opens the server port in Windows Firewall
  On first start the administrator password is written to
  C:\ProgramData\MinideckServer\ADMIN-PASSWORD.txt (deleted after you change it).

  Run from an elevated PowerShell (Run as administrator):
    .\install-server.ps1 -Public server.example.com
#>
param(
    [string]$Public = $env:COMPUTERNAME,
    [int]$Port = 7072,
    [string]$Exe = (Join-Path $PSScriptRoot "MinideckServer.exe")
)
$ErrorActionPreference = "Stop"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this script from PowerShell started with 'Run as administrator'." }
if (-not (Test-Path $Exe)) { throw "MinideckServer.exe not found: $Exe (pass -Exe <path>)" }

$TaskName = "Minideck Server"
$AppDir   = Join-Path $env:ProgramFiles "MinideckServer"
$DataDir  = Join-Path $env:ProgramData  "MinideckServer"

# stop a running copy before replacing the file
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

New-Item -ItemType Directory -Force $AppDir  | Out-Null
New-Item -ItemType Directory -Force $DataDir | Out-Null
Copy-Item $Exe (Join-Path $AppDir "MinideckServer.exe") -Force

# data folder: Administrators and SYSTEM only (it holds the TLS key and the password file)
icacls $DataDir /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" | Out-Null

$taskArgs = "--port $Port --public $Public --data `"$DataDir`""
$action   = New-ScheduledTaskAction -Execute (Join-Path $AppDir "MinideckServer.exe") -Argument $taskArgs -WorkingDirectory $AppDir
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Minideck management server (port $Port)" -Force | Out-Null

if (-not (Get-NetFirewallRule -DisplayName $TaskName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $TaskName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
}

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4

$pw = Join-Path $DataDir "ADMIN-PASSWORD.txt"
Write-Host ""
Write-Host "Minideck server installed and started on port $Port." -ForegroundColor Green
Write-Host "  program : $AppDir"
Write-Host "  data    : $DataDir"
if (Test-Path $pw) {
    Write-Host ""
    Get-Content $pw | Write-Host
}
Write-Host ""
Write-Host "Enrollment code: sign in to the server console in Minideck.exe and open the tab for adding computers."
Write-Host "Back up $DataDir regularly. Remove with .\uninstall-server.ps1"
