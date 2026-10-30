# Run a native Windows check against the complete portable ZIP or extracted folder.
# Examples:
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\verify-windows.ps1 -Package .\AirFrame-windows-x64.zip
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\verify-windows.ps1 -BundleDirectory C:\Apps\AirFrame
[CmdletBinding(DefaultParameterSetName = 'Directory')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Package')]
    [string]$Package,
    [Parameter(Mandatory = $true, ParameterSetName = 'Directory')]
    [string]$BundleDirectory,
    [string]$OutDirectory,
    [int]$RuntimeTimeoutSeconds = 180
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This check must run on Windows. macOS/Linux cannot produce a Windows pass.'
}
if (-not $OutDirectory) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $OutDirectory = Join-Path $env:LOCALAPPDATA "AirFrame\verification\$stamp-$([Guid]::NewGuid().ToString('N').Substring(0,6))"
}
New-Item -ItemType Directory -Force -Path $OutDirectory | Out-Null
$OutDirectory = (Resolve-Path -LiteralPath $OutDirectory).Path
$summary = [ordered]@{
    status = 'failed'
    system = [Environment]::OSVersion.VersionString
    powershell = $PSVersionTable.PSVersion.ToString()
    outputDirectory = $OutDirectory
    scope = 'Native Windows imports, GUI pixels, controls, local H264 WebRTC and launcher; no real phone, Wi-Fi or OBS performance guarantee.'
}
$runtime = $null
$launcher = $null
$child = $null
$exitCode = 1
try {
    if ($PSCmdlet.ParameterSetName -eq 'Package') {
        $Package = (Resolve-Path -LiteralPath $Package).Path
        # Exercise Unicode and spaces without modifying the user's original ZIP.
        $extracted = Join-Path $OutDirectory 'Windows проверка с пробелами'
        Expand-Archive -LiteralPath $Package -DestinationPath $extracted
        $BundleDirectory = Join-Path $extracted 'AirFrame'
        $summary.package = $Package
        $summary.packageSha256 = (Get-FileHash -LiteralPath $Package -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $BundleDirectory = (Resolve-Path -LiteralPath $BundleDirectory).Path
    $summary.bundle = $BundleDirectory
    $python = Join-Path $BundleDirectory 'python.exe'
    $executable = Join-Path $BundleDirectory 'AirFrame.exe'
    foreach ($file in @($python, $executable, (Join-Path $BundleDirectory 'python314._pth'))) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            throw "The portable archive is incomplete: $file"
        }
    }
    $runtimeScript = Join-Path $PSScriptRoot 'verify-windows-runtime.py'
    if (-not (Test-Path -LiteralPath $runtimeScript -PathType Leaf)) {
        throw "Runtime verification script is missing: $runtimeScript"
    }
    # Start-Process joins ArgumentList into one command line; quote every path.
    $arguments = '-I -X utf8 -u "{0}" --bundle "{1}" --out "{2}"' -f $runtimeScript, $BundleDirectory, $OutDirectory
    $runtime = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $BundleDirectory -NoNewWindow -PassThru `
        -RedirectStandardOutput (Join-Path $OutDirectory 'runtime-stdout.log') `
        -RedirectStandardError (Join-Path $OutDirectory 'runtime-stderr.log')
    Write-Host 'Checking the bundled native Windows runtime and test windows...'
    if (-not $runtime.WaitForExit($RuntimeTimeoutSeconds * 1000)) {
        throw "The runtime check exceeded $RuntimeTimeoutSeconds seconds."
    }
    $runtime.WaitForExit()
    $summary.runtimeExitCode = $runtime.ExitCode
    if ($runtime.ExitCode -ne 0) {
        throw 'The runtime check failed. Read runtime-report.json and runtime-stderr.log.'
    }
    $runtimeReport = Join-Path $OutDirectory 'runtime-report.json'
    if (-not (Test-Path -LiteralPath $runtimeReport)) {
        throw 'The native runtime did not create its JSON report.'
    }
    $nativeReport = Get-Content -Raw -Encoding UTF8 -LiteralPath $runtimeReport | ConvertFrom-Json
    if ($nativeReport.status -ne 'passed' -or $nativeReport.platform -ne 'win32') {
        throw 'The native runtime did not report a real Windows pass.'
    }
    $summary.runtimeReport = $runtimeReport

    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    $listener.Stop()
    # AirFrame.exe must forward these arguments to its bundled python.exe.
    $launcher = Start-Process -FilePath $executable -ArgumentList "--port $port --renderer auto" `
        -WorkingDirectory $BundleDirectory -PassThru
    $summary.launcherPid = $launcher.Id
    $summary.launcherPort = $port
    $deadline = (Get-Date).AddSeconds(45)
    $windowSeen = $false
    while ((Get-Date) -lt $deadline) {
        $launcher.Refresh()
        if ($launcher.HasExited) {
            throw "AirFrame.exe exited during startup (code $($launcher.ExitCode))."
        }
        if (-not $child) {
            $processes = @(Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId = $($launcher.Id)" |
                Where-Object { $_.Name -eq 'python.exe' })
            if ($processes.Count -gt 0) {
                $child = Get-Process -Id $processes[0].ProcessId
            }
        }
        if ($child) {
            $child.Refresh()
            if ($child.HasExited) {
                throw "The launcher's Python child exited during startup (code $($child.ExitCode))."
            }
            if ($child.MainWindowHandle -ne [IntPtr]::Zero -and $child.MainWindowTitle -eq 'AirFrame') {
                try {
                    $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/" -UseBasicParsing -TimeoutSec 2
                    if ($response.StatusCode -eq 200 -and $response.Content -match 'AirFrame native receiver') {
                        $windowSeen = $true
                        break
                    }
                } catch {
                    # Wait for the same process's local server to become ready.
                }
            }
        }
        Start-Sleep -Milliseconds 150
    }
    if (-not $windowSeen) {
        throw 'The launcher did not expose its AirFrame window and selected local port within 45 seconds.'
    }
    $summary.launcherChildPid = $child.Id
    $summary.launcherWindow = $child.MainWindowTitle
    if (-not $child.CloseMainWindow()) {
        throw 'The test could not request a normal close of its AirFrame window.'
    }
    if (-not $launcher.WaitForExit(15000)) {
        throw 'AirFrame.exe did not finish after its window was closed.'
    }
    $launcher.WaitForExit()
    $summary.launcherExitCode = $launcher.ExitCode
    if ($launcher.ExitCode -ne 0) {
        throw "AirFrame.exe exited with code $($launcher.ExitCode)."
    }
    $summary.status = 'passed'
    $exitCode = 0
    Write-Host 'PASS: native runtime, rendered patches, settings and real AirFrame.exe startup/shutdown.'
} catch {
    $summary.error = $_.Exception.Message
    Write-Host "FAIL: $($summary.error)" -ForegroundColor Red
} finally {
    # Only processes started by this check are eligible for cleanup.
    foreach ($process in @($runtime, $child, $launcher)) {
        if ($process) {
            try {
                $process.Refresh()
                if (-not $process.HasExited) {
                    [void]$process.CloseMainWindow()
                    if (-not $process.WaitForExit(1500)) { $process.Kill() }
                }
            } catch { }
        }
    }
    $startupLog = Join-Path $env:LOCALAPPDATA 'AirFrame\startup.log'
    if (Test-Path -LiteralPath $startupLog -PathType Leaf) {
        Copy-Item -LiteralPath $startupLog -Destination (Join-Path $OutDirectory 'launcher-startup.log')
    }
    $summary | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $OutDirectory 'windows-report.json') -Encoding UTF8
    Write-Host "Report folder: $OutDirectory"
}
exit $exitCode
