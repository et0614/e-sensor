<#
  E-Sensor (USB-MIDI: VID_04D8 & PID_0057) hot-plug watcher.
  Shows, in real time, WHICH physical USB port each unit (serial) appears on / leaves.

  - Port id (USBPath) is fixed to the physical socket -> anchor for a wind tunnel.
  - Serial travels with the board -> NOT used to identify a tunnel.
  Purpose: verify "a unit plugged into a specific port => that tunnel's calibration starts".

  Usage: powershell -ExecutionPolicy Bypass -File watch_esensor_ports.ps1 -Seconds 180 -LogPath watch.log
  (ASCII-only on purpose: Windows PowerShell 5.1 mis-decodes BOM-less UTF-8 .ps1)
#>
param([int]$Seconds = 180, [string]$LogPath)

# Emit to console AND (if -LogPath) append+flush to a file, so events are visible
# live even when stdout is redirected/buffered by a background host.
function Emit([string]$msg, [string]$color = 'Gray') {
  Write-Host $msg -ForegroundColor $color
  if ($LogPath) { Add-Content -Path $LogPath -Value $msg -Encoding utf8 }
}
if ($LogPath -and (Test-Path $LogPath)) { Remove-Item $LogPath -Force }

function Get-ESensorSnapshot {
  $map = @{}
  Get-PnpDevice -PresentOnly |
    Where-Object { $_.InstanceId -like 'USB\VID_04D8&PID_0057*MI_00*' } |
    ForEach-Object {
      $lpath  = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_LocationPaths' -ErrorAction SilentlyContinue).Data
      $usb    = ($lpath | Where-Object { $_ -like 'PCIROOT*USBMI*' } | Select-Object -First 1)
      $addr   = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_Address' -ErrorAction SilentlyContinue).Data
      $serial = ($_.FriendlyName -replace '^E-Sensor\s*', '')
      if ($usb) {
        $map[$usb] = [pscustomobject]@{ Port = $usb; Addr = $addr; Serial = $serial; Name = $_.FriendlyName }
      }
    }
  return $map
}

$prev = Get-ESensorSnapshot
Emit "=== watch start (${Seconds}s) ==="
Emit "initial state:"
if ($prev.Count -eq 0) { Emit "  (no E-Sensor)" }
$prev.Values | Sort-Object Addr | ForEach-Object {
  Emit ("  port_addr={0}  serial={1}" -f $_.Addr, $_.Serial)
}
Emit "--- please unplug / replug (same or different port) ---"

$deadline = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $deadline) {
  Start-Sleep -Milliseconds 600
  $cur = Get-ESensorSnapshot

  foreach ($k in $cur.Keys) {
    if (-not $prev.ContainsKey($k)) {
      $d = $cur[$k]
      Emit ("[+ CONNECT] port_addr={0}  serial={1}   -> recognized as this port's tunnel" -f $d.Addr, $d.Serial) 'Green'
      Emit ("           path={0}" -f $d.Port) 'DarkGray'
    }
    elseif ($prev[$k].Serial -ne $cur[$k].Serial) {
      Emit ("[~ SWAP   ] port_addr={0}  {1} -> {2}   (port fixed, board changed)" -f $cur[$k].Addr, $prev[$k].Serial, $cur[$k].Serial) 'Cyan'
    }
  }
  foreach ($k in $prev.Keys) {
    if (-not $cur.ContainsKey($k)) {
      $d = $prev[$k]
      Emit ("[- REMOVE ] port_addr={0}  serial={1}" -f $d.Addr, $d.Serial) 'Yellow'
    }
  }
  $prev = $cur
}
Emit "=== watch end ==="
