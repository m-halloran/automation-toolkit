# Sample-encodes several short clips through av1an --target-quality to find the
# CRF each needs to hit a VMAF target, and recommends the most conservative one
# for the full-length encode. Also projects total encode time. Backs `r2 av1-crf`.

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$InputFile,
    [int]$Preset = 3,
    [int]$Tq = 95,
    [int]$Probes = 6,
    [int]$Samples = 3,
    [int]$ClipSeconds = 20
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $InputFile)) {
    Write-Host "ABORT: input not found: $InputFile" -ForegroundColor Red
    exit 1
}
$InputFile = (Resolve-Path $InputFile).Path

# --- source characteristics -------------------------------------------------
$probeJson = ffprobe -v error -select_streams v:0 `
    -show_entries "stream=width,height,r_frame_rate,pix_fmt,bit_rate,nb_frames:format=duration,bit_rate" `
    -of json $InputFile | ConvertFrom-Json

$stream = $probeJson.streams[0]
$format = $probeJson.format

$width    = [int]$stream.width
$height   = [int]$stream.height
$fpsParts = $stream.r_frame_rate -split '/'
$fps      = [double]$fpsParts[0] / [double]$fpsParts[1]
$duration = [double]$format.duration
$bitrate  = if ($stream.bit_rate) { [double]$stream.bit_rate } else { [double]$format.bit_rate }
$bpp      = $bitrate / ($width * $height * $fps)

# Total source frames for the time estimate: prefer the container's nb_frames,
# fall back to duration*fps (MKV commonly reports nb_frames = N/A).
$totalFrames = if ($stream.nb_frames -match '^\d+$' -and [long]$stream.nb_frames -gt 0) {
    [long]$stream.nb_frames
} else {
    [long][Math]::Round($duration * $fps)
}

Write-Host ""
Write-Host "Source: ${width}x${height} @ $([Math]::Round($fps,2))fps, $([Math]::Round($bitrate/1000))kb/s, bpp=$([Math]::Round($bpp,4)), duration=$([Math]::Round($duration))s" -ForegroundColor Cyan
Write-Host "Probing $Samples x ${ClipSeconds}s samples, target VMAF $Tq, preset $Preset, $Probes probes/sample..." -ForegroundColor Cyan
Write-Host ""

# --- sample positions (skip first/last 5% to dodge logos/credits) ----------
$pad = [Math]::Min(30, $duration * 0.05)
$usable = $duration - (2 * $pad)
if ($usable -le 0) { $pad = 0; $usable = $duration }

$positions = 1..$Samples | ForEach-Object {
    $frac = $_ / ($Samples + 1)
    $pad + ($usable * $frac)
}

$tempDir = "D:\dev\av1an\media\av1-suggest"
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

$base = [System.IO.Path]::GetFileNameWithoutExtension($InputFile)
$results = @()

# Throughput accumulators for the time estimate.
$totalEncFrames  = 0.0
$totalEncSeconds = 0.0

for ($i = 0; $i -lt $positions.Count; $i++) {
    $start = [Math]::Round($positions[$i], 3)
    $n     = $i + 1
    $clip  = Join-Path $tempDir "${base}_s${n}.mkv"
    $out   = Join-Path $tempDir "${base}_s${n}_out.mkv"
    $log   = Join-Path $tempDir "${base}_s${n}.log"

    Write-Host "Sample $n/$($positions.Count) @ $([Math]::Round($start))s..." -ForegroundColor Yellow

    ffmpeg -y -v error -ss $start -i $InputFile -t $ClipSeconds -an -c:v copy -avoid_negative_ts make_zero $clip
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $clip)) {
        Write-Host "  skip: clip extraction failed" -ForegroundColor DarkYellow
        continue
    }

    av1an -i $clip -o $out --encoder svt-av1 --chunk-method bestsource --cache-mode temp `
        --pix-format yuv420p10le --video-params "--preset $Preset --keyint 240 --scd 1 --tune 0" `
        --target-quality $Tq --probes $Probes -w 4 --log-file $log --verbose

    if (Test-Path $log) {
        $qs = Select-String -Path $log -Pattern "Final Q=([\d.]+)" | ForEach-Object { [double]$_.Matches.Groups[1].Value }
        if ($qs) {
            $sampleCrf = [Math]::Round(($qs | Measure-Object -Average).Average, 1)
            $results += [PSCustomObject]@{ Position = $start; Crf = $sampleCrf }
            Write-Host "  -> CRF $sampleCrf" -ForegroundColor Green

            # Timing pass: re-encode at a fixed CRF, exactly as the real encode
            # runs. The probe encodes above are far slower, so their timings
            # cannot be reused for the estimate.
            $timeCrf = [int][Math]::Round($sampleCrf)   # SVT-AV1 --crf takes an integer, as `r2 av1` passes
            $timeOut = Join-Path $tempDir "${base}_s${n}_time.mkv"
            $timeLog = Join-Path $tempDir "${base}_s${n}_time.log"
            $sw = [System.Diagnostics.Stopwatch]::StartNew()
            av1an -i $clip -o $timeOut --encoder svt-av1 --chunk-method bestsource --cache-mode temp `
                --pix-format yuv420p10le --video-params "--preset $Preset --crf $timeCrf --keyint 240 --scd 1 --tune 0" `
                -w 4 --log-file $timeLog
            $sw.Stop()
            if (Test-Path $timeOut) {
                $encFramesRaw = ffprobe -v error -select_streams v:0 -count_packets `
                    -show_entries "stream=nb_read_packets" -of csv=p=0 $timeOut
                $encDigits = "$encFramesRaw" -replace '[^\d]', ''
                $encFrames = if ($encDigits) { [double]$encDigits } else { 0 }
                $secs = $sw.Elapsed.TotalSeconds
                if ($encFrames -gt 0 -and $secs -gt 0) {
                    $totalEncFrames  += $encFrames
                    $totalEncSeconds += $secs
                    Write-Host ("     timing: {0} frames in {1:n1}s = {2:n1} fps" -f [int]$encFrames, $secs, ($encFrames / $secs)) -ForegroundColor DarkGray
                }
            }
            Remove-Item $timeOut, $timeLog -ErrorAction SilentlyContinue
        } else {
            Write-Host "  skip: no 'Final Q=' found in log (encode may have failed)" -ForegroundColor DarkYellow
        }
    }

    Remove-Item $clip, $out, $log -ErrorAction SilentlyContinue
}

if (-not $results) {
    Write-Host ""
    Write-Host "ABORT: no samples produced a result." -ForegroundColor Red
    exit 1
}

$recommended = [Math]::Floor(($results | Measure-Object -Property Crf -Minimum).Minimum)
$hardest = $results | Sort-Object Crf | Select-Object -First 1

Write-Host ""
Write-Host "Sample results:" -ForegroundColor Cyan
$results | Sort-Object Position | ForEach-Object {
    Write-Host ("  {0,6}s -> CRF {1}" -f [Math]::Round($_.Position), $_.Crf)
}
Write-Host ""
Write-Host "Recommended CRF: $recommended  (preset $Preset, VMAF $Tq target, hardest sample @ $([Math]::Round($hardest.Position))s)" -ForegroundColor Green
Write-Host ""

# --- full-encode time estimate ---------------------------------------------
# Scale measured throughput up to the source's total frame count.
$estHms   = 'n/a'
$estFps   = $null
$estSecs  = $null
if ($totalEncSeconds -gt 0 -and $totalEncFrames -gt 0) {
    $estFps  = $totalEncFrames / $totalEncSeconds
    $estSecs = $totalFrames / $estFps
    $ts      = [TimeSpan]::FromSeconds($estSecs)
    $estHms  = '{0:d2}:{1:d2}:{2:d2}' -f [int][Math]::Floor($ts.TotalHours), $ts.Minutes, $ts.Seconds
    Write-Host ("Est. full encode: ~{0}  ({1:n0} frames @ {2:n1} fps measured, preset $Preset crf $recommended)" -f $estHms, $totalFrames, $estFps) -ForegroundColor Green
    Write-Host "  basis: $($results.Count) fixed-CRF sample encode(s), $([int]$totalEncFrames) frames in $([Math]::Round($totalEncSeconds))s. Short clips underutilize -w parallelism, so treat this as an upper-ish bound." -ForegroundColor DarkGray
} else {
    Write-Host "Est. full encode: unavailable (no sample timing captured)." -ForegroundColor DarkYellow
}
Write-Host ""
Write-Host "r2 av1 `"$InputFile`" $recommended $Preset" -ForegroundColor White
Write-Host ""

# --- log to running CSV -----------------------------------------------------
$logCsv = "D:\code\var\logs\r2-av1-crf\av1-crf.csv"
New-Item -ItemType Directory -Force -Path (Split-Path $logCsv) | Out-Null
$row = [PSCustomObject]@{
    Date        = (Get-Date -Format 'yyyy-MM-dd HH:mm')
    File        = $InputFile
    Width       = $width
    Height      = $height
    Fps         = [Math]::Round($fps,2)
    BitrateKbps = [Math]::Round($bitrate/1000)
    Bpp         = [Math]::Round($bpp,4)
    Samples     = ($results | Sort-Object Position | ForEach-Object { "$([Math]::Round($_.Position))s=$($_.Crf)" }) -join '; '
    Recommended = $recommended
    Preset      = $Preset
    Tq          = $Tq
    TotalFrames = $totalFrames
    EncFps      = if ($estFps)  { [Math]::Round($estFps,1) }  else { '' }
    EstEncode   = $estHms
}
$row | Export-Csv -Path $logCsv -Append -NoTypeInformation -Force
