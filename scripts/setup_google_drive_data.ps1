param(
    [string]$SourceData = (Join-Path (Split-Path $PSScriptRoot -Parent) 'data'),
    [string]$Destination = '',
    [switch]$ReplaceExisting
)

$ErrorActionPreference = 'Stop'
$sourcePath = (Resolve-Path -LiteralPath $SourceData).Path

if ([string]::IsNullOrWhiteSpace($Destination)) {
    $tasklistParents = @(
        Get-ChildItem -LiteralPath 'H:\' -Directory -ErrorAction Stop |
            ForEach-Object { Join-Path $_.FullName 'tasklist' } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Container }
    )
    if ($tasklistParents.Count -ne 1) {
        throw 'Google Drive tasklist folder could not be selected automatically. Pass -Destination explicitly.'
    }
    $Destination = Join-Path $tasklistParents[0] 'shared-data'
}

$driveRoot = Split-Path -Path $Destination -Qualifier
if (-not $driveRoot -or -not (Test-Path -LiteralPath $driveRoot)) {
    throw "Google Drive is not mounted: $driveRoot"
}

foreach ($name in @('tasks.csv', 'tags.csv')) {
    if (-not (Test-Path -LiteralPath (Join-Path $sourcePath $name) -PathType Leaf)) {
        throw "Required source file is missing: $name"
    }
}

function Assert-CsvHeader {
    param(
        [string]$Path,
        [string[]]$RequiredColumns
    )

    $firstLine = [System.IO.File]::ReadLines($Path) | Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($firstLine)) {
        throw "CSV header is missing: $Path"
    }
    $columns = @($firstLine.TrimStart([char]0xFEFF).Split(','))
    foreach ($column in $RequiredColumns) {
        if ($column -notin $columns) {
            throw "CSV required column '$column' is missing: $Path"
        }
    }
}

Assert-CsvHeader -Path (Join-Path $sourcePath 'tasks.csv') -RequiredColumns @('id', 'title', 'tag', 'score', 'completed', 'due_date')
Assert-CsvHeader -Path (Join-Path $sourcePath 'tags.csv') -RequiredColumns @('tag')

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

$leaseDir = Join-Path $Destination '_tasklist_sync\leases'
$recentLease = Get-ChildItem -LiteralPath $leaseDir -Filter 'active-*.json' -File -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTimeUtc -gt (Get-Date).ToUniversalTime().AddMinutes(-5) } |
    Select-Object -First 1
if ($recentLease) {
    throw "Another Tasklist session may still be active: $($recentLease.FullName)"
}

$managedNames = @('tasks.csv', 'tags.csv', 'tag_rules.json', '.tasklist-shared.json')
$existingManaged = @(
    foreach ($name in $managedNames) {
        $path = Join-Path $Destination $name
        if (Test-Path -LiteralPath $path -PathType Leaf) { $path }
    }
)
if ($existingManaged.Count -gt 0 -and -not $ReplaceExisting) {
    throw 'Shared data files already exist. Use -ReplaceExisting only after confirming the app is stopped.'
}

$migrationId = [guid]::NewGuid().ToString('N')
$stagedFiles = @{}
$disabledSentinel = Join-Path $Destination ".tasklist-shared.disabled-$migrationId.json"
$sentinelPath = Join-Path $Destination '.tasklist-shared.json'
$sentinelTemp = Join-Path $Destination ".tasklist-shared.$migrationId.tmp"
$backupDir = $null
$replacementStarted = $false

try {
    foreach ($name in @('tasks.csv', 'tags.csv', 'tag_rules.json')) {
        $sourceFile = Join-Path $sourcePath $name
        if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) { continue }

        $stagePath = Join-Path $Destination ".migration-$migrationId-$name"
        Copy-Item -LiteralPath $sourceFile -Destination $stagePath
        if ((Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $stagePath -Algorithm SHA256).Hash) {
            throw "Copied file hash does not match: $name"
        }
        $stagedFiles[$name] = $stagePath
    }

    if ($existingManaged.Count -gt 0) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $backupDir = Join-Path $Destination "_migration_previous\$stamp"
        New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
        foreach ($path in $existingManaged) {
            Copy-Item -LiteralPath $path -Destination $backupDir
        }
    }

    if (Test-Path -LiteralPath $sentinelPath -PathType Leaf) {
        Move-Item -LiteralPath $sentinelPath -Destination $disabledSentinel
    }
    $replacementStarted = $true

    foreach ($name in $stagedFiles.Keys) {
        Move-Item -LiteralPath $stagedFiles[$name] -Destination (Join-Path $Destination $name) -Force
    }

    $sentinel = [ordered]@{
        format = 'tasklist-google-drive-shared-data'
        schema_version = 1
        created_at = (Get-Date).ToUniversalTime().ToString('o')
        source = $sourcePath
    }
    $json = $sentinel | ConvertTo-Json
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($sentinelTemp, $json, $utf8NoBom)
    Move-Item -LiteralPath $sentinelTemp -Destination $sentinelPath -Force

    if (Test-Path -LiteralPath $disabledSentinel -PathType Leaf) {
        Move-Item -LiteralPath $disabledSentinel -Destination (Join-Path $backupDir '.tasklist-shared.previous.json') -Force
    }
}
catch {
    if (-not $replacementStarted -and
        (Test-Path -LiteralPath $disabledSentinel -PathType Leaf) -and
        -not (Test-Path -LiteralPath $sentinelPath -PathType Leaf)) {
        Move-Item -LiteralPath $disabledSentinel -Destination $sentinelPath
    }
    throw
}
finally {
    foreach ($path in @($stagedFiles.Values) + @($sentinelTemp)) {
        if ($path -and (Test-Path -LiteralPath $path -PathType Leaf)) {
            Remove-Item -LiteralPath $path -Force
        }
    }
}

Write-Host "Shared data prepared: $Destination"
Get-ChildItem -LiteralPath $Destination -File | Select-Object Name, Length, LastWriteTime
