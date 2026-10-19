$ErrorActionPreference='Continue'
$adb='D:\leidian\LDPlayer14\adb.exe'
$s='emulator-5554'
$root='C:\Users\Administrator\agent-tools\tea_harvest'
$log=Join-Path $root 'harvest_log.txt'
$date='2026-10-16'
$room='740195'
$pkg='com.xm.zjgamecenter'

function L($m){ $t=(Get-Date).ToString('HH:mm:ss'); "$t $m" | Tee-Object -FilePath $log -Append }
function A([string[]]$a){ & $adb -s $s @a 2>&1 }
function Tap($x,$y){ A @('shell','input','tap',"$x","$y") | Out-Null; Start-Sleep -Milliseconds 400 }
function Cap($p){
  A @('shell','screencap','-p','/sdcard/_tea.png') | Out-Null
  A @('pull','/sdcard/_tea.png',$p) | Out-Null
}
function FocusOk(){
  $f = (A @('shell','dumpsys','window') | Select-String 'mCurrentFocus' | Select-Object -First 1).ToString()
  return ($f -match [regex]::Escape($pkg))
}
function EnsurePkg(){
  if (-not (FocusOk)) {
    L 'LOST package — force-stop + relaunch com.xm.zjgamecenter ONLY; STOP harvest taps'
    A @('shell','am','force-stop',$pkg) | Out-Null
    Start-Sleep 1
    A @('shell','monkey','-p',$pkg,'-c','android.intent.category.LAUNCHER','1') | Out-Null
    return $false
  }
  return $true
}

# Proven tea coords
$X_REPLAY=1495
$Ys=@(320,403,487,570,653,734)  # visible slots 1-6
$X_CLOSE=950; $Y_CLOSE=537
$X_TAB=1540; $Y_TAB=248
$X_SPEED=860; $Y_SPEED=505
$FRAMES=3
$FRAME_WAIT=2.4

function HarvestVisible([int]$startRound, [int]$count){
  for($i=0; $i -lt $count; $i++){
    $ri = $startRound + $i
    if($ri -gt 16){ break }
    $slot = $i  # after scroll, round maps to visible slot 0..
    if($slot -ge $Ys.Count){ break }
    $pad = '{0:d2}' -f $ri
    $dir = Join-Path $root "$date\$room\r$pad"
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    # skip if already has 3 frames
    $have = @(Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue).Count
    if($have -ge 3){ L "SKIP $room r$pad already=$have"; continue }

    if(-not (EnsurePkg)){ L 'ABORT lost pkg'; return $false }

    $y = $Ys[$slot]
    L "OPEN $room r$pad tap($X_REPLAY,$y)"
    Tap $X_REPLAY $y
    Start-Sleep 4  # auto-deal — NEVER 开始游戏
    foreach($k in 1..3){ Tap $X_SPEED $Y_SPEED; Start-Sleep -Milliseconds 250 }

    $frameNames=@(); $ts=@()
    for($f=1; $f -le $FRAMES; $f++){
      $fp = Join-Path $dir ("frame_{0:d2}.png" -f $f)
      Cap $fp
      $sz = (Get-Item $fp -EA SilentlyContinue).Length
      $frameNames += "$date/$room/r$pad/frame_{0:d2}.png" -f $f
      $ts += [double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
      L "FRAME $room r$pad f$f size=$sz"
      # detect 游戏中心 by small/wrong or later visual; size alone weak
      if($f -lt $FRAMES){ Start-Sleep $FRAME_WAIT }
    }
    Cap (Join-Path $dir '_ac.png')

    # Close replay X (tea-proven)
    Tap $X_CLOSE $Y_CLOSE
    Start-Sleep 1.5
    if(-not (EnsurePkg)){ L 'ABORT after close'; return $false }

    $meta = [ordered]@{
      source='tea'; date=$date; room_id=$room; round_index=$ri
      round_uid="$room-$date-r$pad"; game_type='温州麻将'
      frames=$frameNames; capture_timestamps=$ts; status='captured'
      user_account='晓红'; harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
      safety=@{clicked_开始游戏=$false; joined_table=$false; spent_房卡=$false}
    }
    ($meta | ConvertTo-Json -Depth 6) | Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
    L "DONE $room r$pad"

    # re-ensure 回放 column
    Tap $X_TAB $Y_TAB
    Start-Sleep 0.7
  }
  return $true
}

L "=== CONTINUE 740195 from r07 (比赛场/比赛战绩 path) ==="
if(-not (EnsurePkg)){ L 'not in pkg at start'; exit 1 }

# Scroll list so r07 becomes top visible (~6 rounds)
L 'SCROLL1 for r07-r12'
A @('shell','input','swipe','800','700','800','280','400') | Out-Null
Start-Sleep 1.2
Cap (Join-Path $root '_scroll_r07.png')
Tap $X_TAB $Y_TAB
Start-Sleep 0.5

$ok = HarvestVisible 7 6
if(-not $ok){ L 'stopped during r07-12'; exit 2 }

L 'SCROLL2 for r13-r16'
A @('shell','input','swipe','800','700','800','280','400') | Out-Null
Start-Sleep 1.2
Cap (Join-Path $root '_scroll_r13.png')
Tap $X_TAB $Y_TAB
Start-Sleep 0.5

$ok = HarvestVisible 13 4
if(-not $ok){ L 'stopped during r13-16'; exit 3 }

$fc = @(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue).Count
L "ROOM $room frames=$fc"
L '=== 740195 batch complete ==='
