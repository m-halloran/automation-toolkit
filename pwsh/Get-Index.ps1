param([string]$targetDir)

$logDir = "D:\code\var\logs\r2-index"

if (!(Test-Path $logDir)) { 
    New-Item -ItemType Directory -Path $logDir | Out-Null 
}

$folderName = Split-Path $targetDir -Leaf

# Sequential numbering: each run writes a new snapshot rather than overwriting.
$pattern = 'index_(\d+)_'
$existingNumbers = Get-ChildItem -Path $logDir -Filter "index_*_*.txt" | ForEach-Object {
    if ($_.Name -match $pattern) { [int]$matches[1] }
}

$maxNum = 0
if ($null -ne $existingNumbers) {
    $measure = $existingNumbers | Measure-Object -Maximum
    if ($null -ne $measure.Maximum) { $maxNum = [int]$measure.Maximum }
}
$nextNumStr = ($maxNum + 1).ToString("D4")

function Get-TreeOutput {
    param(
        [string]$currentPath,
        [int]$depth = 0
    )

    $indent = "    " * $depth

    try {
        $items = Get-ChildItem -Path $currentPath -ErrorAction Stop | Sort-Object PSIsContainer, Name -Descending
        
        foreach ($item in $items) {
            if ($item.PSIsContainer) {
                Write-Output "$indent$($item.Name)/"
                Get-TreeOutput -currentPath $item.FullName -depth ($depth + 1)
            } else {
                Write-Output "$indent$($item.Name)"
            }
        }
    } catch {
        # Broken symlink or access denied: label it and keep walking, rather
        # than aborting the whole index.
        $brokenFolderName = Split-Path $currentPath -Leaf
        Write-Output "$indent$($brokenFolderName)/ [BROKEN LINK]"
    }
}

if (Test-Path $targetDir) {
    $normalizedRoot = $targetDir.Replace('\', '/')
    Write-Output $normalizedRoot
    
    $fileName = "index_$($nextNumStr)_$($folderName).txt"
    $destPath = Join-Path $logDir $fileName

    Get-TreeOutput -currentPath $targetDir -depth 1 | Tee-Object -FilePath $destPath
} else {
    Write-Error "Target directory $targetDir does not exist."
}
