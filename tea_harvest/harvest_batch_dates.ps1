$ErrorActionPreference='Continue'
$adb='D:\leidian\LDPlayer14\adb.exe'; $s='emulator-5554'
$root='C:\Users\Administrator\agent-tools\tea_harvest'
$log=Join-Path $root 'harvest_log.txt'
$pkg='com.xm.zjgamecenter'
$X_REPLAY=1495; $Ys=@(320,403,487,570,653,734)
$X_CLOSE=1050; $Y_CLOSE=404   # green control-bar X (tea settle)
$X_TAB=1540; $Y_TAB=248
$X_SPEED=860; $Y_SPEED=505
$X_DETAIL=1458
$DETAIL_YS=@(265,443,629)  # 3 visible room rows
$X_BACK_LIST=1440; $Y_BACK_LIST=133
$X_DATE=392; $Y_DATE=145
# dropdown order when current is 10-16: 12,13,14,15,16 at y 216,264,311,364,414
# We'll reopen dropdown each time from known current date

function L($m){ $t=(Get-Date).ToString('HH:mm:ss'); "$t $m" | Tee-Object -FilePath $log -Append }
function A([string[]]$a){ & $adb -s $s @a 2>&1 }
function Tap($x,$y){ A @('shell','input','tap',"$x","$y") | Out-Null; Start-Sleep -Milliseconds 350 }
function Swipe($x1,$y1,$x2,$y2,$ms=400){ A @('shell','input','swipe',"$x1","$y1","$x2","$y2","$ms") | Out-Null }
function Cap($p){ A @('shell','screencap','-p','/sdcard/_tea.png') | Out-Null; A @('pull','/sdcard/_tea.png',$p) | Out-Null }
function FocusOk(){ $f=(A @('shell','dumpsys','window') | Select-String 'mCurrentFocus' | Select-Object -First 1).ToString(); return ($f -match [regex]::Escape($pkg)) }
function EnsurePkg(){
  if(FocusOk){ return $true }
  L 'LOST pkg — force-stop+relaunch com.xm.zjgamecenter; STOP (manual re-nav needed)'
  A @('shell','am','force-stop',$pkg)|Out-Null; Start-Sleep 1
  A @('shell','monkey','-p',$pkg,'-c','android.intent.category.LAUNCHER','1')|Out-Null
  return $false
}

function HarvestRound($date,$room,$ri,$slot){
  $pad='{0:d2}' -f $ri
  $dir=Join-Path $root "$date\$room\r$pad"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $have=@(Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue).Count
  if($have -ge 3){ L "SKIP $room r$pad"; return $true }
  if(-not (EnsurePkg)){ return $false }
  $y=$Ys[$slot]
  L "OPEN $room r$pad"
  Tap $X_REPLAY $y
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
  Tap $X_CLOSE $Y_CLOSE
  Start-Sleep 1.4
  if(-not (EnsurePkg)){ return $false }
  $meta=[ordered]@{source='tea';date=$date;room_id="$room";round_index=$ri;round_uid="$room-$date-r$pad";frames=$frames;capture_timestamps=$ts;status='captured';user_account='晓红';harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz');safety=@{clicked_开始游戏=$false;joined_table=$false;spent_房卡=$false}}
  ($meta|ConvertTo-Json -Depth 6)|Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
  L "DONE $room r$pad"
  Tap $X_TAB $Y_TAB; Start-Sleep 0.6
  return $true
}

function HarvestRoom($date,$room,$rowSlot){
  if(-not (EnsurePkg)){ return $false }
  $dy=$DETAIL_YS[$rowSlot]
  L "=== $date/$room detail row$rowSlot ($X_DETAIL,$dy) ==="
  Tap $X_DETAIL $dy
  Start-Sleep 2.2
  Tap $X_TAB $Y_TAB; Start-Sleep 0.8
  # r01-r06
  for($ri=1;$ri -le 6;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-1))){ return $false } }
  # scroll for r07-12
  Swipe 800 700 800 280 400; Start-Sleep 1.0
  Tap $X_TAB $Y_TAB; Start-Sleep 0.5
  for($ri=7;$ri -le 12;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-7))){ return $false } }
  # scroll for r13-16
  Swipe 800 700 800 280 400; Start-Sleep 1.0
  Tap $X_TAB $Y_TAB; Start-Sleep 0.5
  for($ri=13;$ri -le 16;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-13))){ return $false } }
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  L "ROOM $room frames=$fc"
  Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8
  return $true
}

# --- Harvest visible rooms on current date list (assumes already on 比赛战绩 list for target date) ---
# Rooms for 2026-10-15 from screenshot: 790928, 291755, 855053 (+ more after scroll)
# We harvest by row position; room id folder named from OCR-less sequence with list screenshot

function CapList($date){
  $p=Join-Path $root "lists\list_${date}_live.png"
  New-Item -ItemType Directory -Force -Path (Split-Path $p) | Out-Null
  Cap $p
  L "LIST $date size=$((Get-Item $p).Length)"
}

# Start: currently on 2026-10-15 list with rooms visible
$date='2026-10-15'
L "=== BATCH START date=$date ==="
CapList $date

# Harvest first 3 visible rooms — use provisional ids from vision
$rooms=@('790928','291755','855053')
for($i=0;$i -lt 3;$i++){
  $room=$rooms[$i]
  $dir=Join-Path $root "$date\$room"
  $fc=@(Get-ChildItem $dir -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
  if($fc -ge 48){ L "SKIP room $room already $fc"; continue }
  if(-not (HarvestRoom $date $room $i)){ L "ABORT at $room"; exit 2 }
  # after back to list, re-cap
  CapList $date
}

# Scroll list for more rooms and continue with generic names until 14 rooms or empty
# For additional pages: swipe list up, harvest 3 more — room ids unknown; use screenshot+placeholder then rename later
# Use row-based ids page2_r1 etc — better: tap detail and read room from later OCR. For now harvest as room_p2_1...

L 'SCROLL list page2'
Swipe 800 720 800 260 450; Start-Sleep 1.2
CapList "${date}_p2"

# We don't know room ids for page2 without OCR. Capture list and harvest as unknown_
# Try to extract room numbers via simple digit template — skip: use sequential placeholder and log
$more=@('p2r1','p2r2','p2r3')
for($i=0;$i -lt 3;$i++){
  $room=$more[$i]
  if(-not (HarvestRoom $date $room $i)){ L "ABORT at $room"; exit 3 }
  CapList $date
}

L '=== BATCH page1+2 done for 10-15; continue later for remaining + other dates ==='
