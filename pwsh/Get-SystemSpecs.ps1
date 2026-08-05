# Machine specs and drive detail. `r2 system-info` gives the full report;
# `r2 drive-info` passes -DrivesOnly for disks and live I/O alone.
#
# Each block is flushed independently (| Out-Host) so PowerShell's formatter
# never mixes object types -- that mixing is what produced trailing blank lines
# when the specs ran as a single pwsh -Command stream.

param([switch]$DrivesOnly)

function Section($t) {
  Write-Host ''
  Write-Host $t -ForegroundColor Cyan
  Write-Host ('-' * $t.Length) -ForegroundColor DarkGray
}

function Convert-Size([ulong]$bytes) {
  if ($bytes -ge 1TB) { return '{0:N2} TB' -f ($bytes/1TB) }
  if ($bytes -ge 1GB) { return '{0:N2} GB' -f ($bytes/1GB) }
  if ($bytes -ge 1MB) { return '{0:N2} MB' -f ($bytes/1MB) }
  if ($bytes -ge 1KB) { return '{0:N2} KB' -f ($bytes/1KB) }
  return "$bytes B"
}

function Show-LogicalDrives {
  Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' |
    Select-Object DeviceID, VolumeName,
      @{n='SizeGB';e={[math]::Round($_.Size/1GB)}},
      @{n='FreeGB';e={[math]::Round($_.FreeSpace/1GB)}},
      @{n='Free%';e={ if ($_.Size) { [math]::Round($_.FreeSpace / $_.Size * 100) } }} |
    Sort-Object DeviceID |
    Format-Table -AutoSize | Out-Host
}

function Show-PhysicalDisks([switch]$WithIO) {
  $disks = Get-PhysicalDisk |
    Select-Object DeviceId, FriendlyName, MediaType, BusType, Size,
      @{n='Model';e={$_.Model}},
      @{n='Serial';e={$_.SerialNumber}},
      @{n='SectorSize';e={$_.PhysicalSectorSize}}

  # Live I/O counters (1s sample) -- only for the drive-info view
  $ioHash = @{}
  if ($WithIO) {
    $ioCounters = Get-Counter -Counter `
      '\PhysicalDisk(*)\Disk Reads/sec','\PhysicalDisk(*)\Disk Writes/sec', `
      '\PhysicalDisk(*)\Disk Read Bytes/sec','\PhysicalDisk(*)\Disk Write Bytes/sec' `
      -SampleInterval 1 -MaxSamples 1 -ErrorAction SilentlyContinue
    if ($ioCounters) {
      foreach ($s in $ioCounters.CounterSamples) {
        $inst = ($s.Path -replace '^.+\\','') -replace '^\d+\s*',''
        $name = ($s.Path -split '\\')[-1]
        if (-not $ioHash[$inst]) { $ioHash[$inst] = @{} }
        $ioHash[$inst][$name] = [math]::Round($s.CookedValue,2)
      }
    }
  }

  $rows = foreach ($d in $disks) {
    $io = $ioHash["$($d.DeviceId)"]
    if (-not $io) { $io = @{} }
    [PSCustomObject]@{
      DeviceId         = $d.DeviceId
      FriendlyName     = $d.FriendlyName
      Model            = $d.Model
      MediaType        = $d.MediaType
      BusType          = $d.BusType
      Size             = Convert-Size $d.Size
      Serial           = $d.Serial
      SectorSize       = $d.SectorSize
      'Reads/sec'      = ($io['Disk Reads/sec']        -as [double]) -as [string]
      'Writes/sec'     = ($io['Disk Writes/sec']       -as [double]) -as [string]
      'ReadBytes/sec'  = ($io['Disk Read Bytes/sec']   -as [double]) -as [string]
      'WriteBytes/sec' = ($io['Disk Write Bytes/sec']  -as [double]) -as [string]
    }
  }

  $cols = @(
    @{n='Dev';e={$_.DeviceId};w=4},
    @{n='Name';e={$_.FriendlyName};w=24},
    @{n='Model';e={$_.Model};w=24},
    @{n='Type';e={$_.MediaType};w=8},
    @{n='Bus';e={$_.BusType};w=6},
    @{n='Size';e={$_.Size};w=11},
    @{n='Serial';e={$_.Serial};w=22},
    @{n='Sector';e={$_.SectorSize};w=7}
  )
  if ($WithIO) {
    $cols += @{n='R/s';e={$_.'Reads/sec'};w=8}
    $cols += @{n='W/s';e={$_.'Writes/sec'};w=8}
    $cols += @{n='RB/s';e={$_.'ReadBytes/sec'};w=12}
    $cols += @{n='WB/s';e={$_.'WriteBytes/sec'};w=12}
  }

  $rows | Sort-Object {[int]$_.DeviceId} | Format-Table $cols -AutoSize | Out-Host
}

if (-not $DrivesOnly) {
  Section 'CPU  (cores/threads -> av1an --workers & chunk parallelism)'
  Get-CimInstance Win32_Processor |
    Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed |
    Format-Table -AutoSize | Out-Host

  Section 'Memory  (caps safe VapourSynth worker count)'
  Write-Host ('{0:N1} GB total' -f ((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB))

  Section 'GPU  (hw decode/encode, VMAF/CUDA)'
  Get-CimInstance Win32_VideoController |
    Select-Object Name, DriverVersion |
    Format-Table -AutoSize | Out-Host

  Section 'OS'
  Get-CimInstance Win32_OperatingSystem |
    Select-Object @{n='OS';e={$_.Caption}}, Version, BuildNumber, @{n='Host';e={$_.CSName}} |
    Format-Table -AutoSize | Out-Host
}

Section 'Logical drives  (C/D/E -> F/G/R/K backup web; av1an temp on D:)'
Show-LogicalDrives

Section 'Physical disks  (SSD vs HDD -> per-drive I/O bottlenecks)'
Show-PhysicalDisks -WithIO:$DrivesOnly
