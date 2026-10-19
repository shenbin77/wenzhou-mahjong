$ErrorActionPreference='Continue'
$adb='D:\leidian\LDPlayer14\adb.exe'; $s='emulator-5554'
$root='C:\Users\Administrator\agent-tools\tea_harvest'
$log=Join-Path $root 'harvest_log.txt'
$pkg='com.xm.zjgamecenter'
$date='2026-10-15'
$X_REPLAY=1495; $Ys=@(320,403,487,570,653,734)
$X_CLOSE=1050; $Y_CLOSE=404
$X_TAB=1540; $Y_TAB=248
$X_SPEED=860; $Y_SPEED=505
$X_DETAIL=1458; $DETAIL_YS=@(265,443,629)
$X_BACK_LIST=1440; $Y_BACK_LIST=133
$X_DATE=392; $Y_DATE=145

function L($m){ $t=(Get-Date).ToString('HH:mm:ss'); "$t $m" | Tee-Object -FilePath $log -Append }
function A([string[]]$a){ & $adb -s $s @a 2>&1 }
function Tap($x,$y){ A @('shell','input','tap',"$x","$y") | Out-Null; Start-Sleep -Milliseconds 350 }
function Swipe($x1,$y1,$x2,$y2,$ms=400){ A @('shell','input','swipe',"$x1","$y1","$x2","$y2","$ms") | Out-Null }
function Cap($p){ A @('shell','screencap','-p','/sdcard/_tea.png') | Out-Null; A @('pull','/sdcard/_tea.png',$p) | Out-Null }
function FocusOk(){ $f=(A @('shell','dumpsys','window') | Select-String 'mCurrentFocus' | Select-Object -First 1).ToString(); return ($f -match [regex]::Escape($pkg)) }
function EnsurePkg(){ if(FocusOk){return $true}; L 'LOST pkg STOP'; return $false }

function ForceDate1015(){
  # ALWAYS reselect to reset scroll to top
  Tap $X_DATE $Y_DATE; Start-Sleep 1.0
  $ok=$false
  foreach($y in @(364,311,264,414,340,380,216)){
    Tap 320 $y; Start-Sleep 0.9
    $probe=Join-Path $root '_date_probe.png'
    Cap $probe
    $sz=(Get-Item $probe).Length
    if($sz -gt 750000){ L "ForceDate1015 ok y=$y sz=$sz"; $ok=$true; break }
    Tap $X_DATE $Y_DATE; Start-Sleep 0.7
  }
  return $ok
}

function ScrollOne(){ Swipe 800 650 800 472 350; Start-Sleep 0.85 }

function HarvestRound($room,$ri,$slot){
  $pad='{0:d2}' -f $ri
  $dir=Join-Path $root "$date\$room\r$pad"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  if(@(Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue).Count -ge 3){ L "SKIP $room r$pad"; return $true }
  if(-not (EnsurePkg)){ return $false }
  Tap $X_REPLAY $Ys[$slot]; Start-Sleep 4
  foreach($k in 1..3){ Tap $X_SPEED $Y_SPEED; Start-Sleep -Milliseconds 220 }
  $frames=@(); $ts=@()
  for($f=1;$f -le 3;$f++){
    $fp=Join-Path $dir ("frame_{0:d2}.png" -f $f)
    Cap $fp
    $frames += "$date/$room/r$pad/frame_{0:d2}.png" -f $f
    $ts += [double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
    if($f -lt 3){ Start-Sleep 2.3 }
  }
  Cap (Join-Path $dir '_ac.png')
  Tap $X_CLOSE $Y_CLOSE; Start-Sleep 1.4
  if(-not (EnsurePkg)){ return $false }
  $meta=[ordered]@{source='tea';date=$date;room_id="$room";round_index=$ri;round_uid="$room-$date-r$pad";frames=$frames;capture_timestamps=$ts;status='captured';user_account='jingjing';harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')}
  ($meta|ConvertTo-Json -Depth 6)|Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
  L "DONE $room r$pad"
  Tap $X_TAB $Y_TAB; Start-Sleep 0.5
  return $true
}

function HarvestAtIndex($room,[int]$index){
  L "=== V4 $room index=$index ==="
  if(-not (EnsurePkg)){ return $false }
  if(-not (ForceDate1015)){ L 'date lock fail'; return $false }
  for($i=0;$i -lt $index;$i++){ ScrollOne }
  $lp=Join-Path $root ("lists\pre_v4_${room}.png"); Cap $lp
  L "pre $room sz=$((Get-Item $lp).Length)"
  Tap $X_DETAIL $DETAIL_YS[0]; Start-Sleep 2.2
  Tap $X_TAB $Y_TAB; Start-Sleep 0.7
  for($ri=1;$ri -le 6;$ri++){ if(-not (HarvestRound $room $ri ($ri-1))){ return $false } }
  Swipe 800 700 800 280 400; Start-Sleep 1.0; Tap $X_TAB $Y_TAB; Start-Sleep 0.4
  for($ri=7;$ri -le 12;$ri++){ if(-not (HarvestRound $room $ri ($ri-7))){ return $false } }
  Swipe 800 700 800 280 400; Start-Sleep 1.0; Tap $X_TAB $Y_TAB; Start-Sleep 0.4
  for($ri=13;$ri -le 16;$ri++){ if(-not (HarvestRound $room $ri ($ri-13))){ return $false } }
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  L "ROOM $room frames=$fc"
  Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
  return $true
}

# 0-based index in 10-15 list (14 rooms total)
$plan=@(
  @{room='424821'; idx=6},
  @{room='059418'; idx=7},   # partial 18 frames; skip existing rounds
  @{room='426128'; idx=9},
  @{room='125000'; idx=10},
  @{room='215121'; idx=11},
  @{room='905891'; idx=12},
  @{room='286734'; idx=13}
)

L '=== V4 remaining 10-15 rooms ==='
foreach($item in $plan){
  $room=$item.room; $idx=[int]$item.idx
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  if($fc -ge 48){ L "SKIP $room frames=$fc"; continue }
  if(-not (HarvestAtIndex $room $idx)){ L "ABORT $room"; exit 2 }
}
L '=== V4 10-15 remaining complete ==='
