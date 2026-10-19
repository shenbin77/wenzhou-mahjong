$ErrorActionPreference='Continue'
$adb='D:\leidian\LDPlayer14\adb.exe'; $s='emulator-5554'
$root='C:\Users\Administrator\agent-tools\tea_harvest'
$log=Join-Path $root 'harvest_log.txt'
$pkg='com.xm.zjgamecenter'
# Denser: 6 frames/round, 1.6s apart
$FRAMES_PER_ROUND=6
$FRAME_GAP_SEC=1.6
$MIN_FRAME_SZ=700000
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
function EnsureReplayTab(){ Tap $X_TAB $Y_TAB; Start-Sleep 0.35; Tap $X_TAB $Y_TAB; Start-Sleep 0.45 }
function ScrollPages([int]$n){ for($i=0;$i -lt $n;$i++){ Swipe 800 700 800 250 500; Start-Sleep 1.15 } }

function ForceDate($dateY, $label){
  Tap $X_DATE $Y_DATE; Start-Sleep 1.0
  foreach($y in @($dateY, ($dateY-20), ($dateY+20), ($dateY-40), ($dateY+40), 216,264,311,364,414,180,150)){
    Tap 320 $y; Start-Sleep 0.95
    $probe=Join-Path $root '_date_probe.png'; Cap $probe
    $sz=(Get-Item $probe).Length
    if($sz -gt 750000){ L "ForceDate $label ok y=$y sz=$sz"; return $true }
    Tap $X_DATE $Y_DATE; Start-Sleep 0.7
  }
  L "ForceDate $label FAIL"; return $false
}

function HarvestRound($date,$room,$ri,$slot){
  $pad='{0:d2}' -f $ri
  $dir=Join-Path $root "$date\$room\r$pad"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $good=@(Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge $MIN_FRAME_SZ).Count
  if($good -ge $FRAMES_PER_ROUND){ L "SKIP $room r$pad"; return $true }
  if(-not (EnsurePkg)){ return $false }
  for($attempt=1; $attempt -le 3; $attempt++){
    EnsureReplayTab
    Tap $X_REPLAY $Ys[$slot]; Start-Sleep 4.0
    foreach($k in 1..3){ Tap $X_SPEED $Y_SPEED; Start-Sleep -Milliseconds 200 }
    $frames=@(); $ts=@()
    $ok=$true
    for($f=1; $f -le $FRAMES_PER_ROUND; $f++){
      $fp=Join-Path $dir ("frame_{0:d2}.png" -f $f)
      Cap $fp
      $sz=(Get-Item $fp).Length
      if($f -eq 1 -and $sz -lt $MIN_FRAME_SZ){
        L "RETRY $room r$pad attempt=$attempt sz=$sz"
        Tap $X_CLOSE $Y_CLOSE; Start-Sleep 1.2; EnsureReplayTab
        $ok=$false; break
      }
      $frames += ("$date/$room/r$pad/frame_{0:d2}.png" -f $f)
      $ts += [double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
      if($f -lt $FRAMES_PER_ROUND){ Start-Sleep $FRAME_GAP_SEC }
    }
    if(-not $ok){ continue }
    Cap (Join-Path $dir '_ac.png')
    Tap $X_CLOSE $Y_CLOSE; Start-Sleep 1.3
    if(-not (EnsurePkg)){ return $false }
    $meta=[ordered]@{source='tea';date=$date;room_id="$room";round_index=$ri;round_uid="$room-$date-r$pad";frames=$frames;capture_timestamps=$ts;status='captured';frames_per_round=$FRAMES_PER_ROUND;user_account='jingjing';harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')}
    ($meta|ConvertTo-Json -Depth 6)|Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
    L "DONE $room r$pad frames=$FRAMES_PER_ROUND"; EnsureReplayTab; return $true
  }
  L "FAIL $room r$pad"; return $false
}

function HarvestRoom($date,$room,$pages,$slot,$maxRounds,$dateY,$label){
  L "=== DENSE $date $room pages=$pages slot=$slot ==="
  if(-not (EnsurePkg)){ return $false }
  if(-not (ForceDate $dateY $label)){ return $false }
  ScrollPages $pages
  $lp=Join-Path $root ("lists\pre_${label}_${room}.png"); Cap $lp
  L "pre $room sz=$((Get-Item $lp).Length)"
  Tap $X_DETAIL $DETAIL_YS[$slot]; Start-Sleep 2.4
  Cap (Join-Path $root ("lists\detail_${label}_${room}.png"))
  EnsureReplayTab
  $n1=[Math]::Min(6,$maxRounds)
  for($ri=1;$ri -le $n1;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-1))){ return $false } }
  if($maxRounds -gt 6){
    Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
    $n2=[Math]::Min(12,$maxRounds)
    for($ri=7;$ri -le $n2;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-7))){ return $false } }
  }
  if($maxRounds -gt 12){
    Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
    for($ri=13;$ri -le $maxRounds;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-13))){ return $false } }
  }
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge $MIN_FRAME_SZ).Count
  L "ROOM $room good_frames=$fc"
  Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
  return $true
}

function HarvestDateFromList($date,$dateY,$label,$plan){
  L "=== DENSE DATE $date START rooms=$($plan.Count) ==="
  New-Item -ItemType Directory -Force -Path (Join-Path $root $date) | Out-Null
  if(-not (ForceDate $dateY $label)){ L "ABORT date $date"; return $false }
  Cap (Join-Path $root "lists\list_${date}_live.png")
  foreach($item in $plan){
    $room=$item.room
    $need=$FRAMES_PER_ROUND*[int]$item.rounds
    $good=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge $MIN_FRAME_SZ).Count
    if($good -ge $need){ L "SKIP $room good=$good"; continue }
    if(-not (HarvestRoom $date $room ([int]$item.pages) ([int]$item.slot) ([int]$item.rounds) $dateY $label)){ L "ABORT $room"; return $false }
  }
  L "=== DENSE DATE $date COMPLETE ==="
  return $true
}

# Finish any 10-13 leftovers with standard 3-frame if v2 left p4 rooms — handled below after list capture
# --- 10-11: date y from 10-16 dropdown ~ need scroll; try y=180/150 after opening ---
# First lock and capture list to discover rooms dynamically by harvesting slot positions

function DiscoverAndHarvest($date,$dateY,$label){
  L "=== DISCOVER $date ==="
  if(-not (ForceDate $dateY $label)){ return $false }
  Cap (Join-Path $root "lists\list_${date}_p0.png")
  # Harvest visible 3 rooms with provisional ids room_p0_0 etc; rename later from detail caps
  $provisional=@()
  for($slot=0;$slot -lt 3;$slot++){
    $room=("p0s{0}" -f $slot)
    # Open detail to read — harvest under temp then keep
    if(-not (EnsurePkg)){ return $false }
    if(-not (ForceDate $dateY $label)){ return $false }
    Tap $X_DETAIL $DETAIL_YS[$slot]; Start-Sleep 2.4
    $det=Join-Path $root ("lists\detail_${label}_${room}.png"); Cap $det
    # Use temp room name; agent renames from OCR later
    EnsureReplayTab
    $maxRounds=16
    for($ri=1;$ri -le 6;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-1))){ return $false } }
    Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
    for($ri=7;$ri -le 12;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-7))){ return $false } }
    Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
    for($ri=13;$ri -le 16;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-13))){ return $false } }
    $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge $MIN_FRAME_SZ).Count
    L "ROOM $room good_frames=$fc"
    Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
    $provisional += $room
  }
  # Page through more rooms until list stops changing or 20 rooms
  for($page=1; $page -le 6; $page++){
    if(-not (ForceDate $dateY $label)){ return $false }
    ScrollPages $page
    $lp=Join-Path $root ("lists\list_${date}_p${page}.png"); Cap $lp
    $prev=Join-Path $root ("lists\list_${date}_p$($page-1).png")
    if((Test-Path $prev) -and (Get-FileHash $lp).Hash -eq (Get-FileHash $prev).Hash){ L "page $page duplicate — end of list"; break }
    for($slot=0;$slot -lt 3;$slot++){
      $room=("p{0}s{1}" -f $page,$slot)
      $good=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge $MIN_FRAME_SZ).Count
      if($good -ge $FRAMES_PER_ROUND*16){ L "SKIP $room"; continue }
      if(-not (ForceDate $dateY $label)){ return $false }
      ScrollPages $page
      Tap $X_DETAIL $DETAIL_YS[$slot]; Start-Sleep 2.4
      Cap (Join-Path $root ("lists\detail_${label}_${room}.png"))
      EnsureReplayTab
      for($ri=1;$ri -le 6;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-1))){ return $false } }
      Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
      for($ri=7;$ri -le 12;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-7))){ return $false } }
      Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
      for($ri=13;$ri -le 16;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-13))){ return $false } }
      $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge $MIN_FRAME_SZ).Count
      L "ROOM $room good_frames=$fc"
      Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
    }
  }
  L "=== DISCOVER $date COMPLETE ==="
  return $true
}

# Finish 10-13 p4 rooms if detail caps exist / harvest room15 room16 placeholders
L '=== DENSE chain: finish 10-13 tail then 10-11 then 10-10 ==='

# 10-13: harvest any missing known rooms quickly with 3-frame path already in v2; dense script focuses next dates
# Date Y from 10-16: 10-13=264, 10-12=216, 10-11 needs higher in dropdown (~180), 10-10 (~150)

# Immediately after v2: capture and harvest remaining 10-13 with EXISTING 3-frame style via quick helper
function HarvestRound3($date,$room,$ri,$slot){
  $pad='{0:d2}' -f $ri
  $dir=Join-Path $root "$date\$room\r$pad"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $good=@(Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  if($good -ge 3){ L "SKIP $room r$pad"; return $true }
  if(-not (EnsurePkg)){ return $false }
  for($attempt=1;$attempt -le 3;$attempt++){
    EnsureReplayTab
    Tap $X_REPLAY $Ys[$slot]; Start-Sleep 4.0
    foreach($k in 1..3){ Tap $X_SPEED $Y_SPEED; Start-Sleep -Milliseconds 200 }
    $fp1=Join-Path $dir 'frame_01.png'; Cap $fp1
    if((Get-Item $fp1).Length -lt 700000){ L "RETRY $room r$pad sz=$((Get-Item $fp1).Length)"; Tap $X_CLOSE $Y_CLOSE; Start-Sleep 1.2; continue }
    $frames=@("$date/$room/r$pad/frame_01.png"); $ts=@([double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds)
    Start-Sleep 2.2; Cap (Join-Path $dir 'frame_02.png'); $frames+="$date/$room/r$pad/frame_02.png"; $ts+=[double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
    Start-Sleep 2.2; Cap (Join-Path $dir 'frame_03.png'); $frames+="$date/$room/r$pad/frame_03.png"; $ts+=[double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
    Cap (Join-Path $dir '_ac.png'); Tap $X_CLOSE $Y_CLOSE; Start-Sleep 1.3
    $meta=[ordered]@{source='tea';date=$date;room_id="$room";round_index=$ri;round_uid="$room-$date-r$pad";frames=$frames;capture_timestamps=$ts;status='captured';user_account='jingjing';harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')}
    ($meta|ConvertTo-Json -Depth 6)|Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
    L "DONE $room r$pad"; EnsureReplayTab; return $true
  }
  return $false
}

# Tail 10-13: rooms at pages 4+
foreach($item in @(
  @{room='r15'; pages=4; slot=1},
  @{room='r16'; pages=4; slot=2}
)){
  $room=$item.room
  $good=@(Get-ChildItem (Join-Path $root "2026-10-13\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  if($good -ge 48){ L "SKIP 1013 $room"; continue }
  L "=== 1013 tail $room ==="
  if(-not (ForceDate 264 '1013')){ break }
  ScrollPages ([int]$item.pages)
  Cap (Join-Path $root "lists\pre_1013_${room}.png")
  Tap $X_DETAIL $DETAIL_YS[[int]$item.slot]; Start-Sleep 2.4
  Cap (Join-Path $root "lists\detail_1013_${room}.png")
  EnsureReplayTab
  for($ri=1;$ri -le 6;$ri++){ if(-not (HarvestRound3 '2026-10-13' $room $ri ($ri-1))){ break } }
  Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
  for($ri=7;$ri -le 12;$ri++){ if(-not (HarvestRound3 '2026-10-13' $room $ri ($ri-7))){ break } }
  Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab
  for($ri=13;$ri -le 16;$ri++){ if(-not (HarvestRound3 '2026-10-13' $room $ri ($ri-13))){ break } }
  Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
}

# Denser dates: 10-11 then 10-10 (dropdown ys approximate from 10-16 base)
L '=== switching to DENSE 6 frames/round ==='
DiscoverAndHarvest '2026-10-11' 180 '1011' | Out-Null
DiscoverAndHarvest '2026-10-10' 150 '1010' | Out-Null
# Also try earlier if list non-empty: 10-09
DiscoverAndHarvest '2026-10-09' 120 '1009' | Out-Null
L '=== ALL DENSE DATES DONE ==='
