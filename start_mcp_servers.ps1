<#
    start_mcp_servers.ps1

    Starts Postgres (Docker, pgvector image), prepares the database, then the
    four MCP servers (indexing 8000, retrieval 8001, db 8002, code 8003) and the
    Agentic API (8100) in separate windows, and optionally nginx in front.

    IMPORTANT: server.py uses absolute imports like "from backend.core.storage...",
    so it must be launched as a MODULE (-m) with the repo root as the working
    directory - NOT as a bare script path - or Python won't resolve the "backend"
    package.
#>

$ErrorActionPreference = "Stop"

function Start-PythonServer {
    <#
        Launches a Python MCP server module in its own persistent console window.

        Runs via a nested `powershell -NoExit` (not Start-Process -FilePath
        $pythonExe directly) so that if the module throws on import/startup,
        the window stays open with the traceback visible instead of flashing
        shut the instant python.exe exits. Output is also captured to a log
        file under .\logs (via Start-Transcript) for post-mortem.

        Deliberately does NOT redirect/merge the child's stderr with 2>&1:
        in Windows PowerShell 5.1 that wraps every stderr line (which is
        where uvicorn/FastMCP write their INFO logs) in a fake
        NativeCommandError, spamming the window with bogus errors even on a
        clean startup. Start-Transcript logs the console as-is instead.

        Wrapped in try/catch so one failed launch (e.g. transient resource
        contention) is reported as a warning instead of aborting the rest of
        this script under $ErrorActionPreference = "Stop".
    #>
    param(
        [string]$Name,
        [string]$Module,
        [string]$PythonExe,
        [string]$WorkDir,
        [string]$LogDir
    )
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $logFile = Join-Path $LogDir ("{0}.log" -f ($Name -replace '[^\w\-]', '_'))
    $innerCommand = "Start-Transcript -Path '$logFile' | Out-Null; & '$PythonExe' -u -m $Module"
    try {
        $proc = Start-Process -FilePath "powershell.exe" `
            -ArgumentList "-NoExit", "-Command", $innerCommand `
            -WorkingDirectory $WorkDir `
            -PassThru -WindowStyle Normal
        return $proc
    } catch {
        Write-Warning "Failed to launch $Name`: $_"
        return $null
    }
}

function Wait-ForPort {
    param(
        [string]$ServerName,
        [int]$Port,
        [int]$TimeoutSeconds = 30
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $client.Connect("127.0.0.1", $Port)
            if ($client.Connected) {
                $client.Close()
                return $true
            }
        } catch {
            # not up yet - keep polling
        } finally {
            $client.Dispose()
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Warning "$ServerName did not start listening on port $Port within $TimeoutSeconds seconds."
    return $false
}

# --- Config: adjust these if needed ---
$pythonExe = "C:\Users\Nigia\source\repos\AgenticApp\.venv\Scripts\python.exe"   # or full path to your venv's python.exe (e.g. venv\Scripts\python.exe)
$repoRoot  = "C:\Users\Nigia\source\repos\AgenticApp"
$logDir    = Join-Path $repoRoot "logs"   # each server's console output is tee'd here for post-mortem

$indexingModule  = "backend.mcp_servers.indexing.server"
$retrievalModule = "backend.mcp_servers.retrieval.server"
$dbModule = "backend.mcp_servers.db.server"
$sandboxImage = "agenticapp-code-sandbox"
$sandboxName  = "agenticapp-code-sandbox"
$apiModule = "backend.apps.api.main"

# Set this to the folder containing nginx.exe if you want this script to also launch nginx.
# Leave as $null to skip starting nginx automatically.
$nginxDir       = "C:\Users\Nigia\nginx-1.31.3"   # e.g. "C:\nginx-1.25.3"

# --- Postgres: the one database (users, chats, documents, vectors, DB-tool tables) ---
# Matches backend/.env (DATABASE_URL). Change together. The pgvector image is
# plain Postgres 16 plus the `vector` extension, so it reuses the same data
# volume as the old postgres:16 container.
$pgContainerName = "agenticapp-postgres"
$pgImage         = "pgvector/pgvector:pg16"
$pgUser          = "raguser"
$pgPassword      = "changeme"
$pgDb            = "ragdb"
$pgPort          = 5432
$pgVolume        = "agenticapp_pgdata"
$dbSetupModule   = "backend.scripts.db_setup"

# --- Start/ensure local Postgres in Docker ---
# Native commands (docker/psql) write harmless status lines (progress,
# "NOTICE: extension already exists", etc.) to stderr. With
# $ErrorActionPreference = "Stop" in effect, PowerShell would otherwise treat
# every such line as a terminating error, so relax it just for this block.
$prevErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"

Write-Host "Checking Docker..." -ForegroundColor Cyan
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker daemon not running - starting Docker Desktop..." -ForegroundColor Cyan
    $dockerDesktopExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dockerDesktopExe) {
        Start-Process -FilePath $dockerDesktopExe
    } else {
        Write-Warning "Docker Desktop.exe not found at '$dockerDesktopExe'. Start Docker manually, then re-run this script."
    }
    Write-Host "Waiting for Docker daemon (up to 2 minutes)..." -ForegroundColor Cyan
    $dockerReady = $false
    for ($i = 0; $i -lt 24; $i++) {
        Start-Sleep -Seconds 5
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { $dockerReady = $true; break }
    }
    if (-not $dockerReady) {
        Write-Warning "Docker did not become ready in time - skipping Postgres startup. Nothing will work until Docker/Postgres is up."
    }
}

docker info *> $null
if ($LASTEXITCODE -eq 0) {
    $existing = docker ps -a --filter "name=^/$pgContainerName$" --format "{{.Names}}"
    if ($existing -eq $pgContainerName) {
        # A container from before the move to pgvector runs plain postgres:16,
        # which lacks the `vector` extension. Replace the container (the data
        # lives in the $pgVolume volume, so nothing is lost).
        $existingImage = docker inspect -f "{{.Config.Image}}" $pgContainerName
        if ($existingImage -ne $pgImage) {
            Write-Host "Recreating '$pgContainerName' on $pgImage (was $existingImage; data volume kept)..." -ForegroundColor Cyan
            docker rm -f $pgContainerName *> $null
            $existing = $null
        }
    }
    if ($existing -eq $pgContainerName) {
        $running = docker ps --filter "name=^/$pgContainerName$" --format "{{.Names}}"
        if ($running -ne $pgContainerName) {
            Write-Host "Starting existing Postgres container '$pgContainerName'..." -ForegroundColor Cyan
            docker start $pgContainerName *> $null
        } else {
            Write-Host "Postgres container '$pgContainerName' already running." -ForegroundColor Green
        }
    } else {
        Write-Host "Creating Postgres container '$pgContainerName' (image $pgImage, port $pgPort)..." -ForegroundColor Cyan
        docker run -d --name $pgContainerName `
            -e "POSTGRES_USER=$pgUser" `
            -e "POSTGRES_PASSWORD=$pgPassword" `
            -e "POSTGRES_DB=$pgDb" `
            -p "127.0.0.1:${pgPort}:5432" `
            -v "${pgVolume}:/var/lib/postgresql/data" `
            $pgImage *> $null
    }

    Write-Host "Waiting for Postgres to accept connections..." -ForegroundColor Cyan
    $pgReady = $false
    for ($i = 0; $i -lt 20; $i++) {
        docker exec $pgContainerName pg_isready -U $pgUser -d $pgDb *> $null
        if ($LASTEXITCODE -eq 0) { $pgReady = $true; break }
        Start-Sleep -Seconds 2
    }

    if ($pgReady) {
        Write-Host "Postgres is ready." -ForegroundColor Green
        # Migrations + the DB MCP server's restricted role, before any server
        # starts (they all expect the schema to exist).
        Write-Host "Preparing the database ($dbSetupModule)..." -ForegroundColor Cyan
        Push-Location $repoRoot
        & $pythonExe -m $dbSetupModule
        Pop-Location
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Database setup failed - see the output above."
        }
    } else {
        Write-Warning "Postgres did not become ready in time - the servers will fail to connect."
    }
}

$ErrorActionPreference = $prevErrorActionPreference

# --- Start Indexing server (port 8000, hardcoded in server.py) ---
Write-Host "Starting Indexing MCP server (port 8000)..." -ForegroundColor Cyan
$indexingProc = Start-PythonServer -Name "indexing" -Module $indexingModule `
    -PythonExe $pythonExe -WorkDir $repoRoot -LogDir $logDir

# --- Start Retrieval server (port 8001, assumed hardcoded in server.py) ---
Write-Host "Starting Retrieval MCP server (port 8001)..." -ForegroundColor Cyan
$retrievalProc = Start-PythonServer -Name "retrieval" -Module $retrievalModule `
    -PythonExe $pythonExe -WorkDir $repoRoot -LogDir $logDir

# --- Start DB server (port 8002, assumed hardcoded in server.py) ---
Write-Host "Starting DB MCP server (port 8002)..." -ForegroundColor Cyan
$dbProc = Start-PythonServer -Name "db" -Module $dbModule `
    -PythonExe $pythonExe -WorkDir $repoRoot -LogDir $logDir

# --- Start the code sandbox (port 8003) in its own locked-down container ---
# Same image and restrictions as docker-compose.yml's code-sandbox service:
# no secrets, read-only filesystem, minimal capabilities, resource limits;
# each submission runs as its own unprivileged uid inside. Only published on
# 127.0.0.1. It fetches attached spreadsheets from the API on the host
# (host.docker.internal) with one-time tokens. Unlike compose, Docker Desktop
# can't cut a container off from the internet while still letting it reach
# the host, so submitted code here does have outbound network access — and
# can reach ports on this PC via host.docker.internal (including Postgres).
# Its own network keeps it away from the other containers, but use a real
# Postgres password (not the default) if anyone else can use this app; for a
# fully isolated sandbox use docker compose instead.
Write-Host "Building/starting the code sandbox container (port 8003)..." -ForegroundColor Cyan
$prevErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
docker build -q -t $sandboxImage -f (Join-Path $repoRoot "docker\sandbox\Dockerfile") $repoRoot | Out-Null
if ($pgPassword -eq "changeme") {
    Write-Warning "Postgres still uses the default password 'changeme' - code run in the sandbox could reach it via host.docker.internal. Change it before sharing this app."
}
docker network inspect agenticapp-sandbox *> $null
if ($LASTEXITCODE -ne 0) { docker network create agenticapp-sandbox *> $null }
docker rm -f $sandboxName *> $null
# The API sends MCP_INTERNAL_TOKEN (backend/.env) to every MCP server; the
# host servers read it from backend/.env themselves, the container gets it here.
$mcpInternalToken = ""
$envFile = Join-Path $repoRoot "backend\.env"
if (Test-Path $envFile) {
    $line = Select-String -Path $envFile -Pattern '^\s*MCP_INTERNAL_TOKEN\s*=(.*)$' | Select-Object -First 1
    if ($line) { $mcpInternalToken = $line.Matches[0].Groups[1].Value.Trim() }
}
docker run -d --name $sandboxName --network agenticapp-sandbox --init --read-only `
    --tmpfs "/work:size=1g,mode=0711" --tmpfs "/tmp:size=256m" `
    --cap-drop ALL --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add KILL `
    --security-opt "no-new-privileges:true" --memory 2g --pids-limit 256 --cpus 2 `
    -p "127.0.0.1:8003:8003" `
    -e "CODE_MCP_FILE_SOURCE_URL=http://host.docker.internal:8100/internal/files" `
    -e "MCP_INTERNAL_TOKEN=$mcpInternalToken" `
    $sandboxImage *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Could not start the code sandbox container - code execution will be unavailable."
}
$ErrorActionPreference = $prevErrorActionPreference

Write-Host "DB server PID:        $($dbProc.Id)"
Write-Host "Code sandbox:         container '$sandboxName' (docker logs $sandboxName)"

Write-Host "Indexing server PID:  $($indexingProc.Id)"
Write-Host "Retrieval server PID: $($retrievalProc.Id)"

# --- Start the A2A agents service (port 9100) ---
# Deep Research, Data Analyst, ... (backend/apps/agents). Needs
# AGENTS_INTERNAL_TOKEN in backend/.env (same value the API reads).
Write-Host "Starting the agents service (port 9100)..." -ForegroundColor Cyan
$agentsProc = Start-PythonServer -Name "agents" -Module "backend.apps.agents.main" `
    -PythonExe $pythonExe -WorkDir $repoRoot -LogDir $logDir

# --- Start Agentic API server (port 8100) ---
# AgentRuntime connects to all MCP servers at startup and fails fast
# if they're not reachable yet, so wait for their ports before launching it.
Write-Host "Waiting for MCP servers before starting the Agentic API..." -ForegroundColor Cyan
$indexingReady  = Wait-ForPort -ServerName "Indexing MCP server"  -Port 8000
$retrievalReady = Wait-ForPort -ServerName "Retrieval MCP server" -Port 8001
$dbReady        = Wait-ForPort -ServerName "DB MCP server"        -Port 8002
$codeReady      = Wait-ForPort -ServerName "Code sandbox" -Port 8003 -TimeoutSeconds 120

if ($indexingReady -and $retrievalReady -and $dbReady -and $codeReady) {
    Write-Host "Starting Agentic API server (port 8100)..." -ForegroundColor Cyan
    $apiProc = Start-PythonServer -Name "api" -Module $apiModule `
        -PythonExe $pythonExe -WorkDir $repoRoot -LogDir $logDir
    Write-Host "Agentic API server PID: $($apiProc.Id)"
} else {
    Write-Warning "Skipping Agentic API startup - one or more MCP servers never came up. Start it manually once they're ready: python -m $apiModule"
}

# --- Optionally start nginx ---
if ($nginxDir) {
    $nginxExe = Join-Path $nginxDir "nginx.exe"
    if (Test-Path $nginxExe) {
        Write-Host "Starting nginx from $nginxDir..." -ForegroundColor Cyan
        Start-Process -FilePath $nginxExe `
            -ArgumentList "-c", "$repoRoot\nginx.conf" `
            -WorkingDirectory $nginxDir
    } else {
        Write-Warning "nginx.exe not found at $nginxExe - skipping nginx startup."
    }
} else {
    Write-Host "Skipping nginx auto-start (set `$nginxDir in this script to enable it)." -ForegroundColor Yellow
    Write-Host "To start it manually: nginx.exe -c C:\path\to\nginx.conf"
}

Write-Host ""
Write-Host "All set. Servers are running in their own windows." -ForegroundColor Green
Write-Host "Closing this window will NOT stop them (they were launched as separate processes)."
$allPids = @($indexingProc.Id, $retrievalProc.Id, $dbProc.Id)
if ($agentsProc) { $allPids += $agentsProc.Id }
if ($apiProc) { $allPids += $apiProc.Id }
Write-Host "To stop them manually: Stop-Process -Id $($allPids -join ',') ; docker stop $sandboxName"