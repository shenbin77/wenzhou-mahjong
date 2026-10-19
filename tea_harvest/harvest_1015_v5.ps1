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
function Swipe($x1,$y1,$x2,$y2,$ms=450){ A @('shell','input','swipe',"$x1","$y1","$x2","$y2","$ms") | Out-Null }
function Cap($p){ A @('shell','screencap','-p','/sdcard/_tea.png') | Out-Null; A @('pull','/sdcard/_tea.png',$p) | Out-Null }
function FocusOk(){ $f=(A @('shell','dumpsys','window') | Select-String 'mCurrentFocus' | Select-Object -First 1).ToString(); return ($f -match [regex]::Escape($pkg)) }
function EnsurePkg(){ if(FocusOk){return $true}; L 'LOST pkg STOP'; return $false }

function ForceDate1015(){
  Tap $X_DATE $Y_DATE; Start-Sleep 1.0
  foreach($y in @(364,311,264,414,340,380,216)){
    Tap 320 $y; Start-Sleep 0.9
    $probe=Join-Path $root '_date_probe.png'
    Cap $probe
    if((Get-Item $probe).Length -gt 750000){ L "ForceDate1015 ok y=$y sz=$((Get-Item $probe).Length)"; return $true }
    Tap $X_DATE $Y_DATE; Start-Sleep 0.7
  }
  return $false
}

function ScrollPages([int]$n){ for($i=0;$i -lt $n;$i++){ Swipe 800 700 800 250 500; Start-Sleep 1.15 } }

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

function HarvestRoom($room,[int]$pages,[int]$slot){
  L "=== V5 $room pages=$pages slot=$slot ==="
  if(-not (EnsurePkg)){ return $false }
  if(-not (ForceDate1015)){ return $false }
  ScrollPages $pages
  $lp=Join-Path $root ("lists\pre_v5_${room}.png"); Cap $lp
  L "pre $room sz=$((Get-Item $lp).Length)"
  Tap $X_DETAIL $DETAIL_YS[$slot]; Start-Sleep 2.2
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

# Empirical large-swipe map (700->250):
# p0: 0,1,2 = 790928,291755,855053
# p1: slot0,1,2 = 543271,026997,424821  (skips room_p2a #4)
# p2: 059418,569283,426128
# p2+tiny: 125000
# p3: 215121,905891,286734
$plan=@(
  @{room='424821'; pages=1; slot=2},
  @{room='059418'; pages=2; slot=0},
  @{room='426128'; pages=2; slot=2},
  @{room='125000'; pages=2; slot=2; extraSwipe=$true}, # after p2, one more half-swipe then slot0 - handled below
  @{room='905891'; pages=3; slot=1},
  @{room='286734'; pages=3; slot=2}
)

L '=== V5 large-swipe remaining 10-15 ==='

# Special helper for 125000: pages=2 then small scroll to bring #11 to slot0? 
# After p2 top is #8. One large more goes to #11. Better: pages=2, swipe small to shift one row, tap slot0 for #11? 
# Simpler: pages=3 puts #12 at slot0. For #11=125000: pages=2 + ScrollPages almost, then...
# Use: ForceDate, ScrollPages 2, Swipe 800 700 800 400 (half), Cap, tap slot0 expecting 125000

function Harvest125000(){
  L '=== V5 125000 special ==='
  if(-not (EnsurePkg)){ return $false }
  if(-not (ForceDate1015)){ return $false }
  ScrollPages 2
  Swipe 800 700 800 400 400; Start-Sleep 1.1
  $lp=Join-Path $root 'lists\pre_v5_125000.png'; Cap $lp
  L "pre 125000 sz=$((Get-Item $lp).Length)"
  Tap $X_DETAIL $DETAIL_YS[0]; Start-Sleep 2.2
  Tap $X_TAB $Y_TAB; Start-Sleep 0.7
  for($ri=1;$ri -le 6;$ri++){ if(-not (HarvestRound '125000' $ri ($ri-1))){ return $false } }
  Swipe 800 700 800 280 400; Start-Sleep 1.0; Tap $X_TAB $Y_TAB; Start-Sleep 0.4
  for($ri=7;$ri -le 12;$ri++){ if(-not (HarvestRound '125000' $ri ($ri-7))){ return $false } }
  Swipe 800 700 800 280 400; Start-Sleep 1.0; Tap $X_TAB $Y_TAB; Start-Sleep 0.4
  for($ri=13;$ri -le 16;$ri++){ if(-not (HarvestRound '125000' $ri ($ri-13))){ return $false } }
  $fc=@(Get-ChildItem (Join-Path $root "$date\125000") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  L "ROOM 125000 frames=$fc"
  Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
  return $true
}

foreach($item in @(
  @{room='424821'; pages=1; slot=2},
  @{room='059418'; pages=2; slot=0},
  @{room='426128'; pages=2; slot=2}
)){
  $room=$item.room
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  if($fc -ge 48){ L "SKIP $room frames=$fc"; continue }
  if(-not (HarvestRoom $room ([int]$item.pages) ([int]$item.slot))){ L "ABORT $room"; exit 2 }
}

$fc=@(Get-ChildItem (Join-Path $root "$date\125000") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
if($fc -ge 48){ L "SKIP 125000 frames=$fc" } else {
  if(-not (Harvest125000)){ L 'ABORT 125000'; exit 3 }
}

foreach($item in @(
  @{room='905891'; pages=3; slot=1},
  @{room='286734'; pages=3; slot=2}
)){
  $room=$item.room
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  if($fc -ge 48){ L "SKIP $room frames=$fc"; continue }
  if(-not (HarvestRoom $room ([int]$item.pages) ([int]$item.slot))){ L "ABORT $room"; exit 4 }
}

L '=== V5 10-15 remaining complete ==='
