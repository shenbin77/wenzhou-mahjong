$ErrorActionPreference='Continue'
$adb='D:\leidian\LDPlayer14\adb.exe'; $s='emulator-5554'
$root='C:\Users\Administrator\agent-tools\tea_harvest'
$log=Join-Path $root 'harvest_log.txt'
$pkg='com.xm.zjgamecenter'
$date='2026-10-14'
$X_REPLAY=1495; $Ys=@(320,403,487,570,653,734)
$X_CLOSE=1050; $Y_CLOSE=404
$X_TAB=1540; $Y_TAB=248
$X_SPEED=860; $Y_SPEED=505
$X_DETAIL=1458; $DETAIL_YS=@(265,443,629)
$X_BACK_LIST=1440; $Y_BACK_LIST=133
$X_DATE=392; $Y_DATE=145
$DATE_Y=311  # 10-14 from 10-16 dropdown

function L($m){ $t=(Get-Date).ToString('HH:mm:ss'); "$t $m" | Tee-Object -FilePath $log -Append }
function A([string[]]$a){ & $adb -s $s @a 2>&1 }
function Tap($x,$y){ A @('shell','input','tap',"$x","$y") | Out-Null; Start-Sleep -Milliseconds 350 }
function Swipe($x1,$y1,$x2,$y2,$ms=450){ A @('shell','input','swipe',"$x1","$y1","$x2","$y2","$ms") | Out-Null }
function Cap($p){ A @('shell','screencap','-p','/sdcard/_tea.png') | Out-Null; A @('pull','/sdcard/_tea.png',$p) | Out-Null }
function FocusOk(){ $f=(A @('shell','dumpsys','window') | Select-String 'mCurrentFocus' | Select-Object -First 1).ToString(); return ($f -match [regex]::Escape($pkg)) }
function EnsurePkg(){ if(FocusOk){return $true}; L 'LOST pkg STOP'; return $false }

function ForceDate1014(){
  Tap $X_DATE $Y_DATE; Start-Sleep 1.0
  foreach($y in @($DATE_Y,280,300,320,290,350)){
    Tap 320 $y; Start-Sleep 0.95
    $probe=Join-Path $root '_date_probe.png'
    Cap $probe
    $sz=(Get-Item $probe).Length
    # 10-14 multi-room ~840k; also accept >750k then verify by continuing
    if($sz -gt 750000){ L "ForceDate1014 ok y=$y sz=$sz"; return $true }
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
  L "=== 1014 $room pages=$pages slot=$slot ==="
  if(-not (EnsurePkg)){ return $false }
  if(-not (ForceDate1014)){ return $false }
  ScrollPages $pages
  $lp=Join-Path $root ("lists\pre_1014_${room}.png"); Cap $lp
  L "pre $room sz=$((Get-Item $lp).Length)"
  Tap $X_DETAIL $DETAIL_YS[$slot]; Start-Sleep 2.2
  # capture detail hdr for room-id verify
  Cap (Join-Path $root ("lists\detail_1014_${room}.png"))
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

function HarvestRoom4(){
  # one-room swipe from top then slot0
  L '=== 1014 room4 one-swipe ==='
  if(-not (EnsurePkg)){ return $false }
  if(-not (ForceDate1014)){ return $false }
  Swipe 800 650 800 470 350; Start-Sleep 1.0
  $lp=Join-Path $root 'lists\pre_1014_room4.png'; Cap $lp
  L "pre room4 sz=$((Get-Item $lp).Length)"
  Tap $X_DETAIL $DETAIL_YS[0]; Start-Sleep 2.2
  Cap (Join-Path $root 'lists\detail_1014_room4.png')
  # harvest under temp name room4; rename later from detail
  $room='room4'
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

L '=== HARVEST 2026-10-14 (8 rooms) ==='
$plan=@(
  @{room='400960'; pages=0; slot=0},
  @{room='614395'; pages=0; slot=1},
  @{room='425556'; pages=0; slot=2},
  @{room='578740'; pages=1; slot=0},
  @{room='684875'; pages=1; slot=1},
  @{room='034929'; pages=1; slot=2},
  @{room='710418'; pages=2; slot=2}
)

foreach($item in $plan){
  $room=$item.room
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  if($fc -ge 48){ L "SKIP $room frames=$fc"; continue }
  if(-not (HarvestRoom $room ([int]$item.pages) ([int]$item.slot))){ L "ABORT $room"; exit 2 }
}

$fc=@(Get-ChildItem (Join-Path $root "$date\room4") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
if($fc -ge 48){ L "SKIP room4 frames=$fc" } else {
  if(-not (HarvestRoom4)){ L 'ABORT room4'; exit 3 }
}

L '=== 2026-10-14 complete ==='
