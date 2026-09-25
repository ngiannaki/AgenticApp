<#
    start_mcp_servers.ps1

    Starts the Indexing MCP server (hardcoded port 8000) and Retrieval MCP server
    (assumed hardcoded port 8001) in separate windows, and optionally starts nginx
    as the reverse proxy in front of them.

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
$codeModule = "backend.mcp_servers.code.server"
$apiModule = "backend.apps.api.main"

# Chroma exe lives alongside python.exe in the venv's Scripts folder.
$chromaExe = Join-Path (Split-Path $pythonExe -Parent) "chroma.exe"
$chromaPort = 8010
$chromaDataDir = Join-Path $repoRoot "data\vector_store"

# Set this to the folder containing nginx.exe if you want this script to also launch nginx.
# Leave as $null to skip starting nginx automatically.
$nginxDir       = "C:\Users\Nigia\nginx-1.31.3"   # e.g. "C:\nginx-1.25.3"

# --- Postgres (backs the DB MCP server) ---
# Matches backend/mcp_servers/db/.env (DB_MCP_DATABASE_URL). Change together.
$pgContainerName = "agenticapp-postgres"
$pgImage         = "postgres:16"
$pgUser          = "raguser"
$pgPassword      = "changeme"
$pgDb            = "ragdb"
$pgPort          = 5432
$pgVolume        = "agenticapp_pgdata"
$pgMigration     = Join-Path $repoRoot "backend\mcp_servers\db\migrations\0001_init.sql"

# --- MongoDB (backs conversation/message persistence for the Agentic API) ---
# Matches backend/.env (API_MONGO_URL / API_MONGO_DB_NAME defaults). Change together.
$mongoContainerName = "agenticapp-mongo"
$mongoImage         = "mongo:7"
$mongoPort          = 27017
$mongoVolume        = "agenticapp_mongodata"

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
        Write-Warning "Docker did not become ready in time - skipping Postgres startup. The DB MCP server will fail to connect until Docker/Postgres is up."
    }
}

docker info *> $null
if ($LASTEXITCODE -eq 0) {
    $existing = docker ps -a --filter "name=^/$pgContainerName$" --format "{{.Names}}"
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
            -p "${pgPort}:5432" `
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
        if (Test-Path $pgMigration) {
            # 0001_init.sql uses CREATE TABLE/EXTENSION IF NOT EXISTS, so it is
            # safe to re-apply on every start (no-op once the schema exists).
            Write-Host "Applying $pgMigration ..." -ForegroundColor Cyan
            Get-Content $pgMigration -Raw | docker exec -i $pgContainerName psql -U $pgUser -d $pgDb *> $null
        }
    } else {
        Write-Warning "Postgres did not become ready in time - the DB MCP server may fail to connect."
    }

    # --- Start/ensure local MongoDB in Docker ---
    $existingMongo = docker ps -a --filter "name=^/$mongoContainerName$" --format "{{.Names}}"
    if ($existingMongo -eq $mongoContainerName) {
        $runningMongo = docker ps --filter "name=^/$mongoContainerName$" --format "{{.Names}}"
        if ($runningMongo -ne $mongoContainerName) {
            Write-Host "Starting existing MongoDB container '$mongoContainerName'..." -ForegroundColor Cyan
            docker start $mongoContainerName *> $null
        } else {
            Write-Host "MongoDB container '$mongoContainerName' already running." -ForegroundColor Green
        }
    } else {
        Write-Host "Creating MongoDB container '$mongoContainerName' (image $mongoImage, port $mongoPort)..." -ForegroundColor Cyan
        docker run -d --name $mongoContainerName `
            -p "${mongoPort}:27017" `
            -v "${mongoVolume}:/data/db" `
            $mongoImage *> $null
    }

    Write-Host "Waiting for MongoDB to accept connections..." -ForegroundColor Cyan
    $mongoReady = $false
    for ($i = 0; $i -lt 20; $i++) {
        docker exec $mongoContainerName mongosh --quiet --eval "db.adminCommand('ping')" *> $null
        if ($LASTEXITCODE -eq 0) { $mongoReady = $true; break }
        Start-Sleep -Seconds 2
    }

    if ($mongoReady) {
        Write-Host "MongoDB is ready." -ForegroundColor Green
    } else {
        Write-Warning "MongoDB did not become ready in time - conversation persistence will be unavailable until it is reachable."
    }
}

$ErrorActionPreference = $prevErrorActionPreference

# --- Start Chroma vector store server ---
# Indexing (VectorStore) and Retrieval (DocumentRetriever) are separate
# processes, so they can't each open the on-disk Chroma store directly
# (PersistentClient) and reliably see each other's writes — one process's
# in-memory view of the store can go stale relative to the other's writes.
# Routing both through a single `chroma run` server via HttpClient fixes
# that; both must connect to it, so start it before them and wait for it.
Write-Host "Starting Chroma vector store server (port $chromaPort)..." -ForegroundColor Cyan
if (-not (Test-Path $chromaExe)) {
    Write-Warning "chroma.exe not found at $chromaExe - the indexing/retrieval servers will fail to connect to the vector store."
} else {
    New-Item -ItemType Directory -Force -Path $chromaDataDir | Out-Null
    $chromaProc = Start-Process -FilePath $chromaExe `
        -ArgumentList "run", "--path", $chromaDataDir, "--port", $chromaPort `
        -WorkingDirectory $repoRoot `
        -PassThru -WindowStyle Normal
    Write-Host "Chroma server PID: $($chromaProc.Id)"
    $chromaReady = Wait-ForPort -ServerName "Chroma vector store server" -Port $chromaPort
    if (-not $chromaReady) {
        Write-Warning "Chroma server never came up - the indexing/retrieval MCP servers will fail to connect to the vector store."
    }
}

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

# --- Start Code Execution server (port 8003, assumed hardcoded in server.py) ---
Write-Host "Starting Code Execution MCP server (port 8003)..." -ForegroundColor Cyan
$codeProc = Start-PythonServer -Name "code" -Module $codeModule `
    -PythonExe $pythonExe -WorkDir $repoRoot -LogDir $logDir

Write-Host "DB server PID:        $($dbProc.Id)"
Write-Host "Code server PID:      $($codeProc.Id)"

Write-Host "Indexing server PID:  $($indexingProc.Id)"
Write-Host "Retrieval server PID: $($retrievalProc.Id)"

# --- Start Agentic API server (port 8100) ---
# AgentRuntime connects to all MCP servers at startup and fails fast
# if they're not reachable yet, so wait for their ports before launching it.
Write-Host "Waiting for MCP servers before starting the Agentic API..." -ForegroundColor Cyan
$indexingReady  = Wait-ForPort -ServerName "Indexing MCP server"  -Port 8000
$retrievalReady = Wait-ForPort -ServerName "Retrieval MCP server" -Port 8001
$dbReady        = Wait-ForPort -ServerName "DB MCP server"        -Port 8002
$codeReady      = Wait-ForPort -ServerName "Code Execution MCP server" -Port 8003

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
$allPids = @($indexingProc.Id, $retrievalProc.Id, $dbProc.Id, $codeProc.Id)
if ($chromaProc) { $allPids += $chromaProc.Id }
if ($apiProc) { $allPids += $apiProc.Id }
Write-Host "To stop them manually: Stop-Process -Id $($allPids -join ',')"