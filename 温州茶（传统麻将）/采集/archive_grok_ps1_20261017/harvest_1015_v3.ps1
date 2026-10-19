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
function EnsurePkg(){ if(FocusOk){return $true}; L 'LOST pkg STOP'; A @('shell','am','force-stop',$pkg)|Out-Null; A @('shell','monkey','-p',$pkg,'-c','android.intent.category.LAUNCHER','1')|Out-Null; return $false }

function EnsureDate1015(){
  $probe=Join-Path $root '_date_probe.png'
  Cap $probe
  if((Get-Item $probe).Length -gt 750000){ L "date ok sz=$((Get-Item $probe).Length)"; return $true }
  L "relock date sz=$((Get-Item $probe).Length)"
  Tap $X_DATE $Y_DATE; Start-Sleep 1.0
  foreach($y in @(364,311,264,414,216,340,380)){
    Tap 320 $y; Start-Sleep 0.85
    Cap $probe
    if((Get-Item $probe).Length -gt 750000){ L "locked via y=$y sz=$((Get-Item $probe).Length)"; return $true }
    Tap $X_DATE $Y_DATE; Start-Sleep 0.7
  }
  return $false
}

function ScrollPages([int]$n){ for($i=0;$i -lt $n;$i++){ Swipe 800 700 800 250 500; Start-Sleep 1.1 } }

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
  $meta=[ordered]@{source='tea';date=$date;room_id="$room";round_index=$ri;round_uid="$room-$date-r$pad";frames=$frames;capture_timestamps=$ts;status='captured';user_account='晓红';harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz');safety=@{clicked_开始游戏=$false;joined_table=$false;spent_房卡=$false}}
  ($meta|ConvertTo-Json -Depth 6)|Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
  L "DONE $room r$pad"
  Tap $X_TAB $Y_TAB; Start-Sleep 0.5
  return $true
}

function HarvestAtIndex($room,[int]$index){
  $pages=[math]::Floor($index/3); $slot=$index%3
  L "=== $room index=$index pages=$pages slot=$slot ==="
  if(-not (EnsurePkg)){ return $false }
  if(-not (EnsureDate1015)){ return $false }
  ScrollPages $pages
  $lp=Join-Path $root ("lists\pre_${room}.png"); Cap $lp; L "pre list sz=$((Get-Item $lp).Length)"
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

# Known + next rooms (index 0..). Skip done.
# index3 = 21:34 room currently as room_p2a — skip duplicate by using room_p2a skip if exists
# index4+= known from p2 list
$plan=@(
  @{room='790928'; idx=0},
  @{room='291755'; idx=1},
  @{room='855053'; idx=2},
  @{room='room_p2a'; idx=3},  # 21:34 — rename later
  @{room='543271'; idx=4},
  @{room='026997'; idx=5},
  @{room='424821'; idx=6}
  # more after capturing next lists during harvest
)

L '=== V3 index-scroll harvest 10-15 ==='
if(-not (EnsureDate1015)){ exit 1 }

foreach($item in $plan){
  $room=$item.room; $idx=[int]$item.idx
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  if($fc -ge 48){ L "SKIP $room frames=$fc"; continue }
  if(-not (HarvestAtIndex $room $idx)){ L "ABORT $room"; exit 2 }
}

# Continue scrolling for remaining ~7 rooms: capture list at pages 2,3,4 and harvest by index 7..13 with temp names then manual rename
for($idx=7; $idx -le 13; $idx++){
  $room = 'r{0:d2}' -f ($idx+1)   # r08..r14 placeholders
  # better: after scrolling to page, we'll use idx-based folder
  $room = ('idx{0:d2}' -f $idx)
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  if($fc -ge 48){ L "SKIP $room"; continue }
  if(-not (HarvestAtIndex $room $idx)){ L "ABORT $room"; exit 3 }
}

L '=== V3 10-15 complete ==='
