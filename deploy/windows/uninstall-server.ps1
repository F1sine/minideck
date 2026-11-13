<#
.SYNOPSIS
  Removes the Minideck server installed by install-server.ps1.
  Data (database, certificate, journal) is kept unless -RemoveData is given.

  Run from an elevated PowerShell:  .\uninstall-server.ps1 [-RemoveData]
#>
param([switch]$RemoveData)
$ErrorActionPreference = "Stop"

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this script from PowerShell started with 'Run as administrator'." }

$TaskName = "Minideck Server"
$AppDir   = Join-Path $env:ProgramFiles "MinideckServer"
$DataDir  = Join-Path $env:ProgramData  "MinideckServer"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
Get-Process MinideckServer -ErrorAction SilentlyContinue | Stop-Process -Force
Get-NetFirewallRule -DisplayName $TaskName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Start-Sleep -Seconds 1
Remove-Item $AppDir -Recurse -Force -ErrorAction SilentlyContinue

if ($RemoveData) {
    Remove-Item $DataDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Minideck server and its data removed."
} else {
    Write-Host "Minideck server removed. Data kept in $DataDir"
}
