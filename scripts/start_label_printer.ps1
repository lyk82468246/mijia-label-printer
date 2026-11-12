# Launch the Mijia label printer web backend without keeping a terminal open.
# Example:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start_label_printer.ps1
#   scripts\start_label_printer.ps1 -Port 8072 -NoBrowser

#Requires -Version 5.1
[CmdletBinding()]
param(
    [string] $ListenHost = "0.0.0.0",
    [ValidateRange(1, 65535)]
    [int] $Port = 8071,
    [string] $PythonExecutable = "python",
    [ValidateRange(1, 120)]
    [int] $TimeoutSeconds = 20,
    [switch] $NoBrowser
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$serverPath = Join-Path $root "mijia_label_server.py"
$logPath = Join-Path $root "label_server.log"
$errorLogPath = Join-Path $root "label_server.error.log"
$baseUrl = "http://127.0.0.1:$Port"

if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
    throw "未找到后端脚本：$serverPath"
}

$pythonCommand = Get-Command -Name $PythonExecutable -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    throw "未找到 Python：$PythonExecutable"
}
$pythonPath = $pythonCommand.Source

function Test-LabelBackend {
    param([string] $Url)

    try {
        $request = [System.Net.HttpWebRequest]::Create($Url)
        $request.Method = "GET"
        $request.Timeout = 1200
        $request.ReadWriteTimeout = 1200
        $request.AllowAutoRedirect = $false
        $response = $request.GetResponse()
        $response.Close()
        return $true
    }
    catch [System.Net.WebException] {
        # Any HTTP response means something is listening; the /api/status
        # endpoint should return 200 for the real backend.
        return ($null -ne $_.Exception.Response)
    }
    catch {
        return $false
    }
}

$listening = $false
try {
    $listening = [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}
catch {
    # Older/non-Windows PowerShell profiles may not expose this cmdlet.
    $listening = Test-LabelBackend -Url "$baseUrl/api/status"
}

if (-not $listening) {
    # Truncate old logs so the newest startup output is easy to find.
    Set-Content -LiteralPath $logPath -Value "[$(Get-Date -Format 'o')] Starting Mijia label printer backend on $ListenHost`:$Port..." -Encoding UTF8
    Set-Content -LiteralPath $errorLogPath -Value "" -Encoding UTF8

    # Start Python directly in a hidden window. The process is detached from
    # this console, so the launcher/terminal can exit while the server runs.
    $process = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @('-u', $serverPath, '--host', $ListenHost, '--port', $Port) `
        -WorkingDirectory $root `
        -WindowStyle Hidden `
        -RedirectStandardOutput $logPath `
        -RedirectStandardError $errorLogPath `
        -PassThru
    Set-Content -LiteralPath (Join-Path $root "label_server.pid") -Value $process.Id -Encoding ASCII
}

$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$ready = $false
while ([DateTime]::UtcNow -lt $deadline) {
    if (Test-LabelBackend -Url "$baseUrl/api/status") {
        $ready = $true
        break
    }
    Start-Sleep -Milliseconds 250
}

if (-not $ready) {
    $startupError = ""
    try {
        $startupError = (Get-Content -LiteralPath $errorLogPath -Raw -ErrorAction Stop).Trim()
    }
    catch {
    }
    if ($startupError) {
        throw "后端启动失败：$startupError"
    }
    throw "后端未在 $TimeoutSeconds 秒内就绪。请查看日志：$logPath"
}

if (-not $NoBrowser) {
    try {
        Start-Process -FilePath $baseUrl -ErrorAction Stop | Out-Null
    }
    catch {
        Write-Warning "无法自动打开浏览器：$($_.Exception.Message)。请手动访问 $baseUrl"
    }
}

Write-Host "Mijia label printer backend is running at $baseUrl"
Write-Host "Server log: $logPath"
Write-Host "Error log:  $errorLogPath"

# If this script was launched directly in PowerShell, this closes that console.
# When launched from start_label_printer.cmd, it sets the wrapper's exit code.
exit 0
