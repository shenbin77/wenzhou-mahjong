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
# When on 10-16 dropdown: 10-15 at y=364. When already on 10-15, dropdown order may differ — use size check.

function L($m){ $t=(Get-Date).ToString('HH:mm:ss'); "$t $m" | Tee-Object -FilePath $log -Append }
function A([string[]]$a){ & $adb -s $s @a 2>&1 }
function Tap($x,$y){ A @('shell','input','tap',"$x","$y") | Out-Null; Start-Sleep -Milliseconds 350 }
function Swipe($x1,$y1,$x2,$y2,$ms=400){ A @('shell','input','swipe',"$x1","$y1","$x2","$y2","$ms") | Out-Null }
function Cap($p){ A @('shell','screencap','-p','/sdcard/_tea.png') | Out-Null; A @('pull','/sdcard/_tea.png',$p) | Out-Null }
function FocusOk(){ $f=(A @('shell','dumpsys','window') | Select-String 'mCurrentFocus' | Select-Object -First 1).ToString(); return ($f -match [regex]::Escape($pkg)) }
function EnsurePkg(){
  if(FocusOk){ return $true }
  L 'LOST pkg — relaunch ONLY com.xm.zjgamecenter; STOP'
  A @('shell','am','force-stop',$pkg)|Out-Null; Start-Sleep 1
  A @('shell','monkey','-p',$pkg,'-c','android.intent.category.LAUNCHER','1')|Out-Null
  return $false
}

function EnsureDate1015(){
  # Always force date to 2026-10-15 after returning to list
  # Cap list; if size ~615k it's 10-16 (1 room); ~836k is 10-15 multi
  $probe=Join-Path $root '_date_probe.png'
  Cap $probe
  $sz=(Get-Item $probe).Length
  if($sz -gt 750000){ L "date ok list_sz=$sz"; return $true }
  L "date likely wrong list_sz=$sz — reselect 10-15"
  Tap $X_DATE $Y_DATE; Start-Sleep 1.0
  # Prefer tap 10-15. If dropdown from 10-16: y=364. From other dates try several candidates.
  foreach($y in @(364,311,264,414,216,340,380,300)){
    Tap 320 $y; Start-Sleep 0.9
    Cap $probe
    $sz=(Get-Item $probe).Length
    if($sz -gt 750000){ L "selected date list_sz=$sz via y=$y"; return $true }
    # reopen dropdown
    Tap $X_DATE $Y_DATE; Start-Sleep 0.8
  }
  L 'FAILED to lock 10-15'
  return $false
}

function HarvestRound($date,$room,$ri,$slot){
  $pad='{0:d2}' -f $ri
  $dir=Join-Path $root "$date\$room\r$pad"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  if(@(Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue).Count -ge 3){ L "SKIP $room r$pad"; return $true }
  if(-not (EnsurePkg)){ return $false }
  Tap $X_REPLAY $Ys[$slot]
  Start-Sleep 4
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
  $meta=[ordered]@{source='tea';date=$date;room_id="$room";round_index=$ri;round_uid="$room-$date-r$pad";frames=$frames;capture_timestamps=$ts;status='captured';user_account='晓红';harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz');safety=@{clicked_开始游戏=$false;joined_table=$false;spent_房卡=$false}}
  ($meta|ConvertTo-Json -Depth 6)|Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
  L "DONE $room r$pad"
  Tap $X_TAB $Y_TAB; Start-Sleep 0.55
  return $true
}

function HarvestRoom($date,$room,$rowSlot){
  if(-not (EnsurePkg)){ return $false }
  if(-not (EnsureDate1015)){ return $false }
  $dy=$DETAIL_YS[$rowSlot]
  L "=== $date/$room detail row$rowSlot ==="
  Tap $X_DETAIL $dy; Start-Sleep 2.2
  Tap $X_TAB $Y_TAB; Start-Sleep 0.7
  for($ri=1;$ri -le 6;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-1))){ return $false } }
  Swipe 800 700 800 280 400; Start-Sleep 1.0; Tap $X_TAB $Y_TAB; Start-Sleep 0.4
  for($ri=7;$ri -le 12;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-7))){ return $false } }
  Swipe 800 700 800 280 400; Start-Sleep 1.0; Tap $X_TAB $Y_TAB; Start-Sleep 0.4
  for($ri=13;$ri -le 16;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-13))){ return $false } }
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  L "ROOM $room frames=$fc"
  Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
  # CRITICAL: re-lock date after return
  if(-not (EnsureDate1015)){ return $false }
  return $true
}

$date='2026-10-15'
L '=== RESUME 10-15 with date-lock (skip 790928) ==='
if(-not (EnsureDate1015)){ exit 1 }

# Page1: skip row0 (790928 done), do row1=291755, row2=855053
foreach($pair in @(@('291755',1),@('855053',2))){
  $room=$pair[0]; $slot=[int]$pair[1]
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  if($fc -ge 48){ L "SKIP $room frames=$fc"; continue }
  if(-not (HarvestRoom $date $room $slot)){ L "ABORT $room"; exit 2 }
}

# Scroll for more rooms — capture list and harvest next pages with known scroll
# After scroll, row0 is next unseen room. We don't OCR; use list screenshot + sequential harvest of 3 rows per page.
# Remaining ~11 rooms: scroll once so row0 becomes former partial row4, etc.

function ScrollList(){ Swipe 800 700 800 250 500; Start-Sleep 1.3 }

# Page scrolls: harvest 3 rooms per page with placeholder ids from list capture timestamp
$pages = @(
  @{ids=@('room_p2a','room_p2b','room_p2c')},
  @{ids=@('room_p3a','room_p3b','room_p3c')},
  @{ids=@('room_p4a','room_p4b','room_p4c')},
  @{ids=@('room_p5a','room_p5b')}
)
$pageIdx=2
foreach($page in $pages){
  L "SCROLL to page $pageIdx"
  ScrollList
  $lp=Join-Path $root ("lists\list_${date}_p${pageIdx}.png")
  Cap $lp
  L "LIST p$pageIdx size=$((Get-Item $lp).Length)"
  if((Get-Item $lp).Length -lt 750000){
    L 'list size suggests wrong date after scroll — relock'
    if(-not (EnsureDate1015)){ exit 3 }
    ScrollList
    Cap $lp
  }
  $i=0
  foreach($room in $page.ids){
    if(-not (HarvestRoom $date $room $i)){ L "ABORT $room"; exit 4 }
    $i++
  }
  $pageIdx++
}
L '=== 10-15 batch complete (placeholders need rename from list shots) ==='
