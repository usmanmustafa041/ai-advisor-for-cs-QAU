# QAU CS Advisor - one-step deployment for a Windows machine with Docker Desktop.
#
# Package layout (as produced for the USB bundle):
#   START.bat, deploy.ps1                 <- run START.bat (double-click)
#   images\qau-images.tar.gz              <- prebuilt Docker images (no build, no internet)
#   DockerDesktopInstaller.exe            <- optional, used only if Docker is missing
#   ai-advisor-for-cs-QAU\                <- project, with .env and deploy-bundle\engine-data.tgz
#
# Safe to run again: every step checks whether it is already done.
# -Unattended: no prompts and no elevation (for testing); the firewall step is skipped
# unless already running as administrator.

param([switch]$Unattended, [string]$ComposeProject = 'ai-advisor-for-cs-qau')

$ErrorActionPreference = 'Continue'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Project = Join-Path $Here 'ai-advisor-for-cs-QAU'
$ImagesFile = Join-Path $Here 'images\qau-images.tar.gz'
$Installer = Join-Path $Here 'DockerDesktopInstaller.exe'

function Step($text) { Write-Host ''; Write-Host "==> $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "    OK: $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }
function Fail($text) {
    Write-Host ''
    Write-Host "RUKIYE: $text" -ForegroundColor Red
    Write-Host ''
    Pause-ForUser
    exit 1
}
function Pause-ForUser { if (-not $Unattended) { Read-Host 'Enter dabayein' | Out-Null } }
# Docker writes progress to stderr; turn it into plain text so PowerShell does not show it as errors.
function Compose { & docker compose -p $ComposeProject @args 2>&1 | ForEach-Object { "$_" } }
function New-Secret([int]$bytes) {
    $buffer = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
    return ([Convert]::ToBase64String($buffer)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}
function Get-EnvValue($name) {
    $line = Get-Content (Join-Path $Project '.env') | Where-Object { $_ -match "^$name=" } | Select-Object -First 1
    if ($line) { return $line.Substring($name.Length + 1) } else { return '' }
}
function Set-EnvValue($name, $value) {
    $path = Join-Path $Project '.env'
    $lines = Get-Content $path | ForEach-Object { if ($_ -match "^$name=") { "$name=$value" } else { $_ } }
    Set-Content -Path $path -Value $lines -Encoding ASCII
}

# --- 0. Administrator rights (needed for the firewall rule) -----------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$IsAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin -and -not $Unattended) {
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`""
    exit
}

Write-Host '============================================' -ForegroundColor Cyan
Write-Host '   QAU CS Advisor - deployment' -ForegroundColor Cyan
Write-Host '============================================' -ForegroundColor Cyan

if (-not (Test-Path (Join-Path $Project 'docker-compose.yml'))) {
    Fail "Project folder nahi mila: $Project. Zip ko poora extract karein, folders ke naam na badlein."
}

# --- 1. Windows ------------------------------------------------------------------------
Step 'Windows check'
$os = (Get-CimInstance Win32_OperatingSystem).Caption
Ok $os
if ($os -match 'Server') {
    Warn 'Ye Windows SERVER hai. Docker Desktop is par officially support nahi karta.'
    Warn 'Agar Docker pehle se chal raha hai to aage barhte hain, warna Claude se poochein.'
}

# --- 2. Docker ---------------------------------------------------------------------------
Step 'Docker check'
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    if ($os -notmatch 'Server' -and (Test-Path $Installer)) {
        Warn 'Docker install nahi hai. Docker Desktop install ho raha hai (5-10 minute)...'
        Start-Process -FilePath $Installer -ArgumentList 'install', '--quiet', '--accept-license' -Wait
        Write-Host ''
        Write-Host 'Docker install ho gaya. Ab COMPUTER RESTART karein, login ke baad Docker Desktop' -ForegroundColor Yellow
        Write-Host 'khud khulega (pehli dafa terms accept karein), phir START.bat dobara chalayein.' -ForegroundColor Yellow
        Pause-ForUser
        exit 0
    }
    Fail 'Docker install nahi hai, aur is computer par Docker Desktop installer nahi chal sakta.'
}

& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (Test-Path $desktop) {
        Warn 'Docker Desktop band hai, chala rahe hain...'
        Start-Process $desktop
    }
    $deadline = (Get-Date).AddMinutes(4)
    do {
        Start-Sleep -Seconds 5
        & docker info *> $null
    } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
    if ($LASTEXITCODE -ne 0) {
        Fail 'Docker engine start nahi hua. Docker Desktop khol kar dekhein (WSL update maang raha ho to karein), phir START.bat dobara chalayein.'
    }
}
Ok ((& docker version --format '{{.Server.Version}}') -join '')

# --- 3. Settings file --------------------------------------------------------------------
Step 'Settings (.env)'
$envPath = Join-Path $Project '.env'
if (-not (Test-Path $envPath)) {
    Copy-Item (Join-Path $Project '.env.example') $envPath
}
if (-not (Get-EnvValue 'POSTGRES_PASSWORD')) { Set-EnvValue 'POSTGRES_PASSWORD' (New-Secret 24) }
if (-not (Get-EnvValue 'AUTH_SECRET')) { Set-EnvValue 'AUTH_SECRET' (New-Secret 48) }
if (-not (Get-EnvValue 'GEMINI_API_KEY')) { Warn 'GEMINI_API_KEY khali hai - jawab slow/kamzor honge. Admin panel se baad mein daal sakte hain.' }
Ok '.env tayyar'

# Port 80 already taken by something else (IIS etc.) -> use 8080.
$webPort = Get-EnvValue 'WEB_PORT'
if (-not $webPort) { $webPort = '80' }
$running = (Compose ps --status running --services 2>$null) -join ' '
if ($running -notmatch 'web') {
    $listener = Get-NetTCPConnection -LocalPort ([int]$webPort) -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $owner = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        if ($owner -notmatch 'docker|wslrelay|com.docker') {
            Warn "Port $webPort pehle se '$owner' ke paas hai. Port 8080 use karenge."
            $webPort = '8080'
            Set-EnvValue 'WEB_PORT' $webPort
        }
    }
}
Ok "Website port: $webPort"

# --- 4. Docker images (prebuilt, no internet needed) -----------------------------------
Step 'Docker images'
$needed = @('ai-advisor-for-cs-qau-backend:latest', 'ai-advisor-for-cs-qau-advisor:latest',
            'ai-advisor-for-cs-qau-web:latest', 'pgvector/pgvector:pg16')
$missing = @($needed | Where-Object { & docker image inspect $_ *> $null; $LASTEXITCODE -ne 0 })
if ($missing.Count -gt 0) {
    if (-not (Test-Path $ImagesFile)) { Fail "Images file nahi mili: $ImagesFile" }
    Warn 'Images load ho rahi hain (5-15 minute, ruk jayein)...'
    & docker load -i $ImagesFile
    if ($LASTEXITCODE -ne 0) { Fail 'docker load nakam. Disk space check karein (kam az kam 15 GB khali).' }
}
Ok 'Images mojood'

# --- 5. Start --------------------------------------------------------------------------
Step 'Services start'
Push-Location $Project
Compose up -d --no-build
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail 'docker compose up nakam. Upar wala error dekhein.' }

Warn 'Services healthy hone ka intezar (2-5 minute)...'
$deadline = (Get-Date).AddMinutes(8)
do {
    Start-Sleep -Seconds 10
    $status = (Compose ps --format '{{.Service}}={{.Health}}') -join ' '
    Write-Host "    $status"
} while (($status -notmatch 'web=healthy' -or $status -notmatch 'advisor=healthy') -and (Get-Date) -lt $deadline)
if ($status -notmatch 'web=healthy') {
    Compose logs --tail 30 backend advisor
    Pop-Location
    Fail 'Services healthy nahi huin. Upar logs dekhein.'
}
Ok 'Sab services chal rahi hain'

# --- 6. Chatbot knowledge index --------------------------------------------------------
Step 'Chatbot knowledge index'
Compose exec -T advisor test -f /data/indexes/CURRENT
if ($LASTEXITCODE -ne 0) {
    $bundle = Join-Path $Project 'deploy-bundle\engine-data.tgz'
    if (-not (Test-Path $bundle)) { Pop-Location; Fail "Index bundle nahi mila: $bundle" }
    Warn 'Tayyar index copy ho raha hai...'
    Compose cp $bundle advisor:/tmp/engine-data.tgz
    Compose exec -T advisor tar xzf /tmp/engine-data.tgz -C /data
    Compose exec -T -u root advisor rm -f /tmp/engine-data.tgz
    Compose restart advisor
    $deadline = (Get-Date).AddMinutes(4)
    do {
        Start-Sleep -Seconds 5
        $health = (Compose ps advisor --format '{{.Health}}') -join ''
    } while ($health -ne 'healthy' -and (Get-Date) -lt $deadline)
}
try {
    $ready = Invoke-RestMethod "http://localhost:$webPort/advisor/api/v1/ready" -TimeoutSec 20
    Ok "Index ready: $($ready.knowledge.chunks) chunks"
} catch { Warn "Index status nahi mila: $_" }

# --- 7. Replace the publicly known seed admin password ---------------------------------
Step 'Admin password'
$adminEmail = 'admin@cs.qau.edu.pk'
$body = @{ email = $adminEmail; password = 'ChangeMeAdmin123!' } | ConvertTo-Json
$defaultWorks = $false
try {
    Invoke-RestMethod "http://localhost:$webPort/api/v1/auth/login" -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 20 | Out-Null
    $defaultWorks = $true
} catch { }
if ($defaultWorks) {
    $adminPassword = New-Secret 12
    # Windows PowerShell does not pipe stdin to docker reliably, so hand the password to
    # set_admin_password.py through an environment variable in place of its prompts.
    $setPassword = "import getpass,os,runpy,sys; getpass.getpass=lambda prompt='': os.environ['NEW_ADMIN_PASSWORD']; " +
                   "sys.argv=['set_admin_password.py']; runpy.run_path('scripts/set_admin_password.py', run_name='__main__')"
    Compose exec -T -e "NEW_ADMIN_PASSWORD=$adminPassword" backend python -c $setPassword
    if ($LASTEXITCODE -ne 0) { Pop-Location; Fail 'Admin password set nahi hua.' }
    $loginFile = Join-Path $Here 'ADMIN_LOGIN.txt'
    "Website: http://<server-ip>:$webPort`r`nEmail:    $adminEmail`r`nPassword: $adminPassword" | Set-Content $loginFile -Encoding ASCII
    Ok "Naya admin password set ho gaya aur $loginFile mein save hai"
} else {
    Ok 'Default password pehle hi badla ja chuka hai'
}
Pop-Location

# --- 8. Firewall -----------------------------------------------------------------------
Step 'Firewall'
$ruleName = "QAU Advisor web $webPort"
if (-not $IsAdmin) {
    Warn 'Administrator nahi - firewall rule skip kiya.'
} else {
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort ([int]$webPort) -Action Allow | Out-Null
    }
    Ok "Port $webPort network ke liye khula"
}

# --- Done --------------------------------------------------------------------------------
$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[01])\.)' } |
    Select-Object -ExpandProperty IPAddress
$suffix = if ($webPort -eq '80') { '' } else { ":$webPort" }

Write-Host ''
Write-Host '============================================' -ForegroundColor Green
Write-Host '   DEPLOYMENT MUKAMMAL' -ForegroundColor Green
Write-Host '============================================' -ForegroundColor Green
Write-Host 'Department ke kisi bhi computer se kholein:'
foreach ($ip in $ips) { Write-Host "    http://$ip$suffix" -ForegroundColor White }
if ($defaultWorks) {
    Write-Host ''
    Write-Host 'Admin login (LIKH LEIN, ye dobara nahi dikhega):' -ForegroundColor Yellow
    Write-Host "    Email:    $adminEmail" -ForegroundColor Yellow
    Write-Host "    Password: $adminPassword" -ForegroundColor Yellow
    Write-Host "    (ADMIN_LOGIN.txt mein bhi save hai)" -ForegroundColor Yellow
}
Write-Host ''
Pause-ForUser
