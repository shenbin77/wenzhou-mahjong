$ErrorActionPreference='Continue'
$adb='D:\leidian\LDPlayer14\adb.exe'; $s='emulator-5554'
$root='C:\Users\Administrator\agent-tools\tea_harvest'
$log=Join-Path $root 'harvest_log.txt'
$pkg='com.xm.zjgamecenter'
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
function Cap($p){
  for($i=0;$i -lt 4;$i++){
    A @('shell','screencap','-p','/sdcard/_tea.png') | Out-Null
    A @('pull','/sdcard/_tea.png',$p) | Out-Null
    if((Test-Path $p) -and ((Get-Item $p).Length -gt 50000)){ return $true }
    Start-Sleep 0.5
  }
  return $false
}
function FocusOk(){ $f=(A @('shell','dumpsys','window') | Select-String 'mCurrentFocus' | Select-Object -First 1).ToString(); return ($f -match [regex]::Escape($pkg)) }
function EnsurePkg(){ if(FocusOk){return $true}; L 'LOST pkg STOP'; return $false }
function EnsureReplayTab(){
  foreach($xy in @(@(1540,248),@(1525,242),@(1555,250),@(1540,235))){ Tap $xy[0] $xy[1]; Start-Sleep -Milliseconds 250 }
  Start-Sleep 0.35
}
function ScrollPages([int]$n){ for($i=0;$i -lt $n;$i++){ Swipe 800 700 800 250 500; Start-Sleep 1.15 } }
function ForceDate($preferY,$label){
  Tap $X_DATE $Y_DATE; Start-Sleep 1.0
  foreach($y in @($preferY,($preferY-15),($preferY+15),($preferY-30),($preferY+30),216,264,311,364,414,180,150,120)){
    Tap 320 $y; Start-Sleep 1.0
    $probe=Join-Path $root '_date_probe.png'
    if(-not (Cap $probe)){ continue }
    $sz=(Get-Item $probe).Length
    if($sz -gt 750000){ L "ForceDate $label ok y=$y sz=$sz"; return $true }
    Tap $X_DATE $Y_DATE; Start-Sleep 0.75
  }
  L "ForceDate $label FAIL"; return $false
}

function HarvestRound($date,$room,$ri,$slot,$nFrames,$gap){
  $pad='{0:d2}' -f $ri
  $dir=Join-Path $root "$date\$room\r$pad"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $good=@(Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  if($good -ge $nFrames){ L "SKIP $room r$pad"; return $true }
  Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -lt 700000 | Remove-Item -Force -EA SilentlyContinue
  if(-not (EnsurePkg)){ return $false }
  for($attempt=1;$attempt -le 4;$attempt++){
    EnsureReplayTab
    Tap $X_REPLAY $Ys[$slot]; Start-Sleep 4.3
    foreach($k in 1..4){ Tap $X_SPEED $Y_SPEED; Start-Sleep -Milliseconds 200 }
    $frames=@(); $ts=@(); $ok=$true
    for($f=1;$f -le $nFrames;$f++){
      $fp=Join-Path $dir ("frame_{0:d2}.png" -f $f)
      Cap $fp | Out-Null
      $sz=0; if(Test-Path $fp){ $sz=(Get-Item $fp).Length }
      if($f -eq 1 -and $sz -lt 700000){
        L "RETRY $room r$pad a=$attempt sz=$sz"
        Tap $X_CLOSE $Y_CLOSE; Start-Sleep 1.3
        $ok=$false; break
      }
      $frames += ("$date/$room/r$pad/frame_{0:d2}.png" -f $f)
      $ts += [double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
      if($f -lt $nFrames){ Start-Sleep $gap }
    }
    if(-not $ok){ continue }
    Cap (Join-Path $dir '_ac.png') | Out-Null
    Tap $X_CLOSE $Y_CLOSE; Start-Sleep 1.4
    if(-not (EnsurePkg)){ return $false }
    $meta=[ordered]@{source='tea';date=$date;room_id="$room";round_index=$ri;round_uid="$room-$date-r$pad";frames=$frames;capture_timestamps=$ts;status='captured';user_account='jingjing';harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')}
    ($meta|ConvertTo-Json -Depth 6)|Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
    L "DONE $room r$pad n=$nFrames sz1=$((Get-Item (Join-Path $dir 'frame_01.png')).Length)"
    EnsureReplayTab
    return $true
  }
  L "FAIL $room r$pad"; return $false
}

function HarvestRoom($date,$room,$pages,$slot,$maxRounds,$dateY,$label,$nFrames,$gap,$half=$false){
  L "=== $label $room pages=$pages slot=$slot rounds=$maxRounds nF=$nFrames half=$half ==="
  if(-not (EnsurePkg)){ return $false }
  if(-not (ForceDate $dateY $label)){ return $false }
  ScrollPages $pages
  if($half){ Swipe 800 650 800 470 350; Start-Sleep 1.1 }
  $lp=Join-Path $root ("lists\pre_${label}_${room}.png"); Cap $lp | Out-Null
  L "pre $room sz=$((Get-Item $lp -EA SilentlyContinue).Length)"
  Tap $X_DETAIL $DETAIL_YS[$slot]; Start-Sleep 2.5
  Cap (Join-Path $root ("lists\detail_${label}_${room}.png")) | Out-Null
  EnsureReplayTab
  $n1=[Math]::Min(6,$maxRounds)
  for($ri=1;$ri -le $n1;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-1) $nFrames $gap)){ Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.5; return $false } }
  if($maxRounds -gt 6){
    Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
    $n2=[Math]::Min(12,$maxRounds)
    for($ri=7;$ri -le $n2;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-7) $nFrames $gap)){ Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.5; return $false } }
  }
  if($maxRounds -gt 12){
    Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
    for($ri=13;$ri -le $maxRounds;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-13) $nFrames $gap)){ Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.5; return $false } }
  }
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  L "ROOM $room good_frames=$fc"
  Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
  return $true
}

function RunPlan($date,$dateY,$label,$plan,$nFrames,$gap){
  L "=== BATCH $date nF=$nFrames rooms=$($plan.Count) ==="
  New-Item -ItemType Directory -Force -Path (Join-Path $root $date) | Out-Null
  foreach($item in $plan){
    $room=$item.room
    $rounds=[int]$item.rounds
    $need=$nFrames*$rounds
    $good=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
    if($good -ge $need){ L "SKIP $room good=$good"; continue }
    $half=[bool]$item.half
    $slot=0; if($null -ne $item.slot){ $slot=[int]$item.slot }
    $ok=HarvestRoom $date $room ([int]$item.pages) $slot $rounds $dateY $label $nFrames $gap $half
    if(-not $ok){ L "WARN continue after fail $room" }
  }
  L "=== BATCH $date DONE ==="
}

# --- 10-13 remaining (3 frames) ---
$plan1013=@(
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
  @{room='639074'; pages=3; slot=2; rounds=16},
  @{room='tail_a'; pages=4; slot=0; rounds=16},
  @{room='tail_b'; pages=4; slot=1; rounds=16}
)
RunPlan '2026-10-13' 264 '1013' $plan1013 3 2.2

# Capture list for next dates then dense harvest by slot discovery
function DiscoverDense($date,$dateY,$label){
  L "=== DENSE $date START ==="
  New-Item -ItemType Directory -Force -Path (Join-Path $root $date) | Out-Null
  if(-not (ForceDate $dateY $label)){ L "DENSE $date no date"; return }
  Cap (Join-Path $root "lists\list_${date}_live.png") | Out-Null
  $emptyPages=0
  for($page=0; $page -le 6; $page++){
    if(-not (ForceDate $dateY $label)){ break }
    ScrollPages $page
    $lp=Join-Path $root ("lists\list_${date}_p${page}.png"); Cap $lp | Out-Null
    $sz=(Get-Item $lp -EA SilentlyContinue).Length
    if($sz -lt 400000){ $emptyPages++; if($emptyPages -ge 2){ break }; continue }
    if($page -gt 0){
      $prev=Join-Path $root ("lists\list_${date}_p$($page-1).png")
      if((Test-Path $prev) -and ((Get-FileHash $lp).Hash -eq (Get-FileHash $prev).Hash)){ L "DENSE $date end page=$page"; break }
    }
    for($slot=0;$slot -lt 3;$slot++){
      $room=("p{0}s{1}" -f $page,$slot)
      $good=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
      if($good -ge 6*16){ L "SKIP $room"; continue }
      $ok=HarvestRoom $date $room $page $slot 16 $dateY $label 6 1.6 $false
      if(-not $ok){ L "WARN dense $room" }
    }
  }
  L "=== DENSE $date DONE ==="
}

# After 10-13: denser (6 frames) for remaining dates
DiscoverDense '2026-10-11' 180 '1011'
DiscoverDense '2026-10-10' 150 '1010'
DiscoverDense '2026-10-09' 120 '1009'
DiscoverDense '2026-10-08' 100 '1008'
L '=== ALL HARVEST COMPLETE ==='
