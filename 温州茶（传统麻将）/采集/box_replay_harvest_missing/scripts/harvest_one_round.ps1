# harvest_one_round.ps1 - SAFE replay capture for one round
# NEVER taps 开始游戏 / join table / 房卡
param(
  [Parameter(Mandatory=$true)][int]$RoundIndex,  # 1-16
  [Parameter(Mandatory=$true)][string]$Date,     # YYYY-MM-DD
  [Parameter(Mandatory=$true)][string]$RoomId,
  [string]$SessionStart = '',
  [string[]]$Players = @(),
  [string]$ReplayCode = $null,
  [string]$Adb = 'D:\leidian\LDPlayer14\adb.exe',
  [string]$Serial = 'emulator-5554',
  [string]$OutRoot = 'C:\Users\Administrator\agent-tools\replay_harvest',
  [int]$Frames = 5,
  [int]$FrameIntervalMs = 4500,
  [switch]$SkipOpen,   # already on detail with 回放 column
  [switch]$AlreadyInReplay
)

$ErrorActionPreference = 'Stop'
function Adb([string[]]$Args) {
  & $Adb -s $Serial @Args
  if ($LASTEXITCODE -ne 0) { throw "adb failed: $($Args -join ' ')" }
}
function Tap([int]$x,[int]$y) { Adb @('shell','input','tap',$x,$y); Start-Sleep -Milliseconds 400 }
function WaitSec([double]$s) { Start-Sleep -Milliseconds ([int]($s*1000)) }
function Cap([string]$path) {
  Adb @('shell','screencap','-p','/sdcard/_rh.png')
  Adb @('pull','/sdcard/_rh.png',$path) | Out-Null
}

$roundPad = '{0:d2}' -f $RoundIndex
$dir = Join-Path $OutRoot "$Date\r$roundPad"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$uid = "$RoomId-$Date-r$roundPad"

# Proven coords from HARVEST_REPORT.json (1600x900)
$Y_ROUNDS = @(320,403,487,570,653,734)  # r1-r6 visible; scroll for later
$X_REPLAY = 1495
$X_TAB = 1540; $Y_TAB = 248
$X_CLOSE = 1040; $Y_CLOSE = 440
$X_SPEED = 860; $Y_SPEED = 505   # 播放速度 label
$X_BACK_LIST = 1440; $Y_BACK_LIST = 133

Write-Output "HARVEST start $uid"

if (-not $AlreadyInReplay) {
  # Tap round 回放 — for rounds 1-6 use table; for 7+ caller must scroll first
  if ($RoundIndex -lt 1 -or $RoundIndex -gt 16) { throw "bad RoundIndex $RoundIndex" }
  $vis = $RoundIndex
  if ($RoundIndex -le 6) {
    $y = $Y_ROUNDS[$RoundIndex-1]
  } else {
    # After scroll so that round N is at position (N-6) of visible... caller sets -SkipOpen after positioning
    throw "RoundIndex>$RoundIndex requires pre-scroll; pass AlreadyInReplay or use harvest_batch"
  }
  Write-Output "TAP replay button ($X_REPLAY,$y) for r$roundPad"
  Tap $X_REPLAY $y
  # Wait auto-deal — NEVER tap 开始游戏
  Write-Output "WAIT auto-deal 4s"
  WaitSec 4.0
  # Max speed: tap speed control several times (X1->X2->X3->X4 cycle unknown; tap 4x)
  for ($i=0; $i -lt 4; $i++) { Tap $X_SPEED $Y_SPEED; WaitSec 0.3 }
}

$ts = @()
$frameNames = @()
for ($f=1; $f -le $Frames; $f++) {
  $fp = Join-Path $dir ("frame_{0:d2}.png" -f $f)
  Cap $fp
  $ts += [double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
  $frameNames += "$Date/r$roundPad/frame_{0:d2}.png" -f $f
  Write-Output "FRAME $f -> $fp size=$((Get-Item $fp).Length)"
  if ($f -lt $Frames) { WaitSec ($FrameIntervalMs/1000.0) }
}

# Close X
Write-Output "CLOSE X ($X_CLOSE,$Y_CLOSE)"
Tap $X_CLOSE $Y_CLOSE
WaitSec 1.2

$meta = [ordered]@{
  date = $Date
  room_id = $RoomId
  session_start = $SessionStart
  players = $Players
  round_index = $RoundIndex
  round_uid = $uid
  replay_code = $ReplayCode
  frames = $frameNames
  capture_timestamps = $ts
  status = 'captured'
  user_account = '晓红'
  harvested_at = (Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
  safety = @{ clicked_开始游戏 = $false; joined_table = $false; spent_房卡 = $false }
}
$metaPath = Join-Path $dir 'meta.json'
($meta | ConvertTo-Json -Depth 6) | Set-Content -Encoding UTF8 $metaPath
Write-Output "META $metaPath"
Write-Output "HARVEST done $uid frames=$Frames"
