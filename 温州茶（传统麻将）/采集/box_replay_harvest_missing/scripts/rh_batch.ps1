$ErrorActionPreference='Continue'
$adb='D:\leidian\LDPlayer14\adb.exe'
$s='emulator-5554'
$out='C:\Users\Administrator\agent-tools\replay_harvest'
$log="$out\batch_log.txt"
New-Item -ItemType Directory -Force -Path $out | Out-Null
function L($m){ $t=(Get-Date).ToString('HH:mm:ss'); "$t $m" | Tee-Object -FilePath $log -Append }
function A([string[]]$a){ & $adb -s $s @a 2>&1 }
function Tap($x,$y){ A @('shell','input','tap',"$x","$y") | Out-Null; Start-Sleep -Milliseconds 450 }
function Cap($p){ A @('shell','screencap','-p','/sdcard/_rh.png') | Out-Null; A @('pull','/sdcard/_rh.png',$p) | Out-Null }

L 'START batch'
# Launch app
A @('shell','am','start','-a','android.intent.action.MAIN','-c','android.intent.category.LAUNCHER','-p','com.xm.zjgamecenter') | Out-Null
Start-Sleep 10
Tap 800 450
Start-Sleep 2
Cap "$out\_boot.png"
L "boot size=$((Get-Item "$out\_boot.png" -EA SilentlyContinue).Length)"

# If live table: try back / close carefully (top-left back, avoid 开始游戏)
# Common exit: top-left 返回 ~80,50 ; menu X
foreach($pt in @(@(80,50),@(60,40),@(100,60))){ Tap $pt[0] $pt[1]; Start-Sleep 1 }

# Open 比赛战绩: try top-right icons then mid entries
foreach($pt in @(@(1520,55),@(1480,80),@(1550,90),@(1400,70),@(1300,80))){ Tap $pt[0] $pt[1]; Start-Sleep 1 }
Cap "$out\_after_tr.png"
L "after_tr=$((Get-Item "$out\_after_tr.png" -EA SilentlyContinue).Length)"

# Tap 详情 on first row (today list)
Tap 1450 265
Start-Sleep 2
Cap "$out\_detail.png"
L "detail=$((Get-Item "$out\_detail.png" -EA SilentlyContinue).Length)"

# Ensure 回放 column (tap header 回放 tab)
Tap 1540 248
Start-Sleep 1
Cap "$out\_replaycol.png"
L "replaycol=$((Get-Item "$out\_replaycol.png" -EA SilentlyContinue).Length)"

# Round Y for r1-r6
$Ys=@(320,403,487,570,653,734)
$X=1495
# Harvest today 346299 r02-r06 first (priority), then stop for sync
$date='2026-10-15'; $room='346299'
$players=@('百合花','晓红','任海明','徐徐')
$codes=@($null,'73064548','73064914','73065111','73065564','73065777',$null)
# r02..r06
foreach($ri in 2..6){
  $pad='{0:d2}' -f $ri
  $dir=Join-Path $out "$date\r$pad"
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $uid="$room-$date-r$pad"
  L "OPEN $uid"
  Tap $X $Ys[$ri-1]
  Start-Sleep 4   # auto-deal, NEVER 开始游戏
  # max speed taps
  foreach($i in 1..4){ Tap 860 505; Start-Sleep -Milliseconds 250 }
  $frames=@(); $ts=@()
  for($f=1;$f -le 5;$f++){
    $fp=Join-Path $dir ("frame_{0:d2}.png" -f $f)
    Cap $fp
    $frames += "$date/r$pad/frame_{0:d2}.png" -f $f
    $ts += [double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
    L "frame$f size=$((Get-Item $fp -EA SilentlyContinue).Length)"
    if($f -lt 5){ Start-Sleep 4 }
  }
  # close X
  Tap 1040 440
  Start-Sleep 1.5
  # write meta
  $code=$null; if($ri -lt $codes.Count){ $code=$codes[$ri] }
  $meta=@{
    date=$date; room_id=$room; session_start='17:47'; players=$players
    round_index=$ri; round_uid=$uid; replay_code=$code
    frames=$frames; capture_timestamps=$ts; status='captured'; user_account='晓红'
    harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')
    safety=@{clicked_开始游戏=$false; joined_table=$false; spent_房卡=$false}
  }
  ($meta|ConvertTo-Json -Depth 6) | Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
  L "DONE $uid"
  # ensure still on detail with 回放 column
  Tap 1540 248
  Start-Sleep 0.8
}
Cap "$out\_batch_done.png"
L 'BATCH_COMPLETE r02-r06'
