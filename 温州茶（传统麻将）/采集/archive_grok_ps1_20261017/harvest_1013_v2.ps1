$ErrorActionPreference='Continue'
$adb='D:\leidian\LDPlayer14\adb.exe'; $s='emulator-5554'
$root='C:\Users\Administrator\agent-tools\tea_harvest'
$log=Join-Path $root 'harvest_log.txt'
$pkg='com.xm.zjgamecenter'
$date='2026-10-13'
$X_REPLAY=1495; $Ys=@(320,403,487,570,653,734)
$X_CLOSE=1050; $Y_CLOSE=404
$X_TAB=1540; $Y_TAB=248
$X_SPEED=860; $Y_SPEED=505
$X_DETAIL=1458; $DETAIL_YS=@(265,443,629)
$X_BACK_LIST=1440; $Y_BACK_LIST=133
$X_DATE=392; $Y_DATE=145
$DATE_Y=264

function L($m){ $t=(Get-Date).ToString('HH:mm:ss'); "$t $m" | Tee-Object -FilePath $log -Append }
function A([string[]]$a){ & $adb -s $s @a 2>&1 }
function Tap($x,$y){ A @('shell','input','tap',"$x","$y") | Out-Null; Start-Sleep -Milliseconds 350 }
function Swipe($x1,$y1,$x2,$y2,$ms=450){ A @('shell','input','swipe',"$x1","$y1","$x2","$y2","$ms") | Out-Null }
function Cap($p){ A @('shell','screencap','-p','/sdcard/_tea.png') | Out-Null; A @('pull','/sdcard/_tea.png',$p) | Out-Null }
function FocusOk(){ $f=(A @('shell','dumpsys','window') | Select-String 'mCurrentFocus' | Select-Object -First 1).ToString(); return ($f -match [regex]::Escape($pkg)) }
function EnsurePkg(){ if(FocusOk){return $true}; L 'LOST pkg STOP'; return $false }
function ForceDate1013(){
  Tap $X_DATE $Y_DATE; Start-Sleep 1.0
  foreach($y in @($DATE_Y,250,280,216,300,311)){
    Tap 320 $y; Start-Sleep 0.95
    $probe=Join-Path $root '_date_probe.png'; Cap $probe
    if((Get-Item $probe).Length -gt 750000){ L "ForceDate1013 ok y=$y sz=$((Get-Item $probe).Length)"; return $true }
    Tap $X_DATE $Y_DATE; Start-Sleep 0.7
  }
  return $false
}
function ScrollPages([int]$n){ for($i=0;$i -lt $n;$i++){ Swipe 800 700 800 250 500; Start-Sleep 1.15 } }
function EnsureReplayTab(){ Tap $X_TAB $Y_TAB; Start-Sleep 0.35; Tap $X_TAB $Y_TAB; Start-Sleep 0.45 }

function HarvestRound($room,$ri,$slot,[int]$maxRetry=3){
  $pad='{0:d2}' -f $ri
  $dir=Join-Path $root "$date\$room\r$pad"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $good=@(Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  if($good -ge 3){ L "SKIP $room r$pad"; return $true }
  if(-not (EnsurePkg)){ return $false }
  for($attempt=1; $attempt -le $maxRetry; $attempt++){
    EnsureReplayTab
    Tap $X_REPLAY $Ys[$slot]; Start-Sleep 4.2
    foreach($k in 1..3){ Tap $X_SPEED $Y_SPEED; Start-Sleep -Milliseconds 220 }
    $fp1=Join-Path $dir 'frame_01.png'; Cap $fp1
    $sz1=(Get-Item $fp1).Length
    if($sz1 -lt 700000){
      L "RETRY $room r$pad attempt=$attempt sz=$sz1"
      Tap $X_CLOSE $Y_CLOSE; Start-Sleep 1.2; EnsureReplayTab; continue
    }
    $frames=@("$date/$room/r$pad/frame_01.png")
    $ts=@([double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds)
    Start-Sleep 2.3; $fp2=Join-Path $dir 'frame_02.png'; Cap $fp2
    $frames += "$date/$room/r$pad/frame_02.png"; $ts += [double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
    Start-Sleep 2.3; $fp3=Join-Path $dir 'frame_03.png'; Cap $fp3
    $frames += "$date/$room/r$pad/frame_03.png"; $ts += [double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
    Cap (Join-Path $dir '_ac.png'); Tap $X_CLOSE $Y_CLOSE; Start-Sleep 1.4
    if(-not (EnsurePkg)){ return $false }
    $meta=[ordered]@{source='tea';date=$date;room_id="$room";round_index=$ri;round_uid="$room-$date-r$pad";frames=$frames;capture_timestamps=$ts;status='captured';user_account='jingjing';harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')}
    ($meta|ConvertTo-Json -Depth 6)|Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
    L "DONE $room r$pad sz=$sz1"; EnsureReplayTab; return $true
  }
  L "FAIL $room r$pad"; return $false
}

function HarvestRoom($room,[int]$pages,[int]$slot,[int]$maxRounds=16){
  L "=== 1013v2 $room pages=$pages slot=$slot rounds=$maxRounds ==="
  if(-not (EnsurePkg)){ return $false }
  if(-not (ForceDate1013)){ return $false }
  ScrollPages $pages
  $lp=Join-Path $root ("lists\pre_1013_${room}.png"); Cap $lp
  L "pre $room sz=$((Get-Item $lp).Length)"
  Tap $X_DETAIL $DETAIL_YS[$slot]; Start-Sleep 2.4
  Cap (Join-Path $root ("lists\detail_1013_${room}.png"))
  EnsureReplayTab
  $n1=[Math]::Min(6,$maxRounds)
  for($ri=1;$ri -le $n1;$ri++){ if(-not (HarvestRound $room $ri ($ri-1))){ return $false } }
  if($maxRounds -gt 6){
    Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
    $n2=[Math]::Min(12,$maxRounds)
    for($ri=7;$ri -le $n2;$ri++){ if(-not (HarvestRound $room $ri ($ri-7))){ return $false } }
  }
  if($maxRounds -gt 12){
    Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
    for($ri=13;$ri -le $maxRounds;$ri++){ if(-not (HarvestRound $room $ri ($ri-13))){ return $false } }
  }
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  L "ROOM $room good_frames=$fc"
  Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
  return $true
}

function HarvestHalfSwipe($room,[int]$pagesBefore,[int]$maxRounds=16){
  L "=== 1013v2 $room halfswipe after pages=$pagesBefore ==="
  if(-not (EnsurePkg)){ return $false }
  if(-not (ForceDate1013)){ return $false }
  ScrollPages $pagesBefore
  Swipe 800 650 800 470 350; Start-Sleep 1.0
  $lp=Join-Path $root ("lists\pre_1013_${room}.png"); Cap $lp
  L "pre $room sz=$((Get-Item $lp).Length)"
  Tap $X_DETAIL $DETAIL_YS[0]; Start-Sleep 2.4
  Cap (Join-Path $root ("lists\detail_1013_${room}.png"))
  EnsureReplayTab
  $n1=[Math]::Min(6,$maxRounds)
  for($ri=1;$ri -le $n1;$ri++){ if(-not (HarvestRound $room $ri ($ri-1))){ return $false } }
  if($maxRounds -gt 6){
    Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
    $n2=[Math]::Min(12,$maxRounds)
    for($ri=7;$ri -le $n2;$ri++){ if(-not (HarvestRound $room $ri ($ri-7))){ return $false } }
  }
  if($maxRounds -gt 12){
    Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
    for($ri=13;$ri -le $maxRounds;$ri++){ if(-not (HarvestRound $room $ri ($ri-13))){ return $false } }
  }
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  L "ROOM $room good_frames=$fc"
  Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
  return $true
}

L '=== HARVEST 2026-10-13 v2 remaining ==='
# Large-swipe map (700->250):
# p0: 207547,748439,529766
# p1: 698763,323028,496708  (145627 may need half)
# p2: 208964,693873,018368
# p2+ : 936742
# p3: 964065,996744,639074
# p4: room15, room16 — filled after OCR; use placeholders then rename

$plan=@(
  @{room='207547'; pages=0; slot=0; rounds=12},
  @{room='748439'; pages=0; slot=1; rounds=16},
  @{room='529766'; pages=0; slot=2; rounds=16},
  @{room='698763'; pages=1; slot=0; rounds=16},
  @{room='323028'; pages=1; slot=1; rounds=16},
  @{room='496708'; pages=1; slot=2; rounds=16},
  @{room='145627'; pages=1; rounds=16; half=$true},
  @{room='208964'; pages=2; slot=0; rounds=16},
  @{room='693873'; pages=2; slot=1; rounds=16},
  @{room='018368'; pages=2; slot=2; rounds=16},
  @{room='936742'; pages=2; rounds=16; half=$true},
  @{room='964065'; pages=3; slot=0; rounds=16},
  @{room='996744'; pages=3; slot=1; rounds=16},
  @{room='639074'; pages=3; slot=2; rounds=16}
)

foreach($item in $plan){
  $room=$item.room
  $need=3*[int]$item.rounds
  $good=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  if($good -ge $need){ L "SKIP $room good=$good"; continue }
  if($item.half){
    if(-not (HarvestHalfSwipe $room ([int]$item.pages) ([int]$item.rounds))){ L "ABORT $room"; exit 2 }
  } else {
    if(-not (HarvestRoom $room ([int]$item.pages) ([int]$item.slot) ([int]$item.rounds))){ L "ABORT $room"; exit 2 }
  }
}

# Capture bottom pages for rooms 15-16 ids then harvest
L '=== 1013 capture p4 for last rooms ==='
if(-not (ForceDate1013)){ L 'ABORT date'; exit 3 }
ScrollPages 4
Cap (Join-Path $root 'lists\p4_1013.png')
ScrollPages 1
Cap (Join-Path $root 'lists\p5_1013.png')
L '=== 1013 v2 core rooms done; check p4 for room15/16 ==='

