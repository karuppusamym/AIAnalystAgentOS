<#
.SYNOPSIS
  One command to a ready AnalystOS demo on Windows with Docker Desktop (Linux/macOS/WSL: scripts/demo.sh).

.DESCRIPTION
  Brings the stack up with docker compose, waits for health, runs migrate, seed and demo-seed (all idempotent)
  and prints the URLs and logins. Walkthrough: docs/30-runbooks/05-demo-walkthrough.md.

  .\scripts\demo.ps1            full demo stack: standard (Redis, Temporal, worker, scheduler) + Superset + ServiceNow mock
  .\scripts\demo.ps1 -Lite      lite: Postgres, API (local orchestrator), web + ServiceNow mock; publishing goes to the preview
  .\scripts\demo.ps1 -Reset     delete the demo's data (docker compose down -v) and start again from nothing
  .\scripts\demo.ps1 -Down      stop everything (data is kept)

  If script execution is blocked: powershell -ExecutionPolicy Bypass -File .\scripts\demo.ps1
#>
param(
  [switch]$Lite,
  [switch]$Reset,
  [switch]$Down
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Fail([string]$message) { Write-Host $message -ForegroundColor Red; exit 1 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "docker is not installed or not on PATH (install Docker Desktop)" }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "the Docker daemon is not running: start Docker Desktop and wait until it says 'Engine running'" }
if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host "created .env from .env.example (development defaults; never commit it)"
}

$mode = if ($Lite) { "lite" } else { "full" }
$composeArgs = @("compose", "--env-file", ".env", "--env-file", "deploy/compose/demo.env")
$profileArgs = @("--profile", "demo")
if (-not $Lite) {
  $composeArgs += @("--env-file", "deploy/compose/standard.env", "--env-file", "deploy/compose/bi.env")
  $profileArgs += @("--profile", "standard", "--profile", "bi")
}

function Invoke-Compose([string[]]$rest) {
  & docker @composeArgs @profileArgs @rest
  if ($LASTEXITCODE -ne 0) { Fail "docker compose $($rest -join ' ') failed (exit $LASTEXITCODE)" }
}

if ($Down) { Invoke-Compose @("down"); exit 0 }
if ($Reset) { Write-Host "removing containers and volumes (all demo data)"; Invoke-Compose @("down", "-v") }

Write-Host "starting the $mode stack (the first build takes several minutes)"
Invoke-Compose @("up", "-d", "--build")

function Wait-Http([string]$url, [string]$service, [int]$seconds) {
  $deadline = (Get-Date).AddSeconds($seconds)
  Write-Host -NoNewline "waiting for $service "
  while ($true) {
    try {
      $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
      if ($r.StatusCode -eq 200) { Write-Host " up"; return }
    } catch { }
    if ((Get-Date) -gt $deadline) { Fail " not ready after $seconds s; see: docker compose logs $service" }
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 5
  }
}
Wait-Http "http://localhost:8000/api/health" "api" 600
Wait-Http "http://localhost:5173/healthz" "web" 300
if (-not $Lite) { Wait-Http "http://localhost:8088/health" "superset" 900 }

Invoke-Compose @("exec", "-T", "api", "analystos", "migrate")
Invoke-Compose @("exec", "-T", "api", "analystos", "seed")
Invoke-Compose @("exec", "-T", "api", "analystos", "demo-seed", "--api", "http://localhost:8000", "--web", "http://localhost:5173")

try {
  $health = Invoke-RestMethod -Uri "http://localhost:8000/api/health" -TimeoutSec 10
  $states = $health.checks.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value.state)" }
  Write-Host ""
  Write-Host ("health: " + ($states -join ", "))
  foreach ($p in $health.problems) { Write-Host ("  ! " + $p.message) -ForegroundColor Yellow }
} catch { }

Write-Host ""
Write-Host "AnalystOS demo is ready ($mode)" -ForegroundColor Green
Write-Host "  Web UI      http://localhost:5173"
Write-Host "  API docs    http://localhost:8000/docs"
if (-not $Lite) { Write-Host "  Superset    http://localhost:8088   (admin / admin)" }
Write-Host "  Sign in     analyst@analystos.local   ChangeMe123!   (runs investigations)"
Write-Host "              approver@analystos.local  ChangeMe123!   (approves publication)"
Write-Host "              admin@analystos.local     ChangeMe123!   (administration)"
Write-Host "  Walkthrough docs\30-runbooks\05-demo-walkthrough.md"
