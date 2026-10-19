$ErrorActionPreference='Continue'
$adb='D:\leidian\LDPlayer14\adb.exe'; $s='emulator-5554'
$root='C:\Users\Administrator\agent-tools\tea_harvest'
$log=Join-Path $root 'harvest_log.txt'
$pkg='com.xm.zjgamecenter'
$X_REPLAY=1495; $Ys=@(320,403,487,570,653,734)
# X_CLOSE/Y_CLOSE kept as legacy reference ONLY — Close-ReplayBar never blind-taps these
$X_CLOSE=1050; $Y_CLOSE=404
$X_FF=980; $Y_FF=505
$X_SPEED=860; $Y_SPEED=505
$X_DETAIL=1458; $DETAIL_YS=@(265,443,629)
$X_BACK_LIST=1440; $Y_BACK_LIST=133
$X_DATE=392; $Y_DATE=145
$N_FRAMES=6; $FRAME_GAP=1.5
function L($m){ $t=(Get-Date).ToString('HH:mm:ss'); "$t $m" | Tee-Object -FilePath $log -Append }
function A([string[]]$a){ & $adb -s $s @a 2>&1 }
function Tap($x,$y){ A @('shell','input','tap',"$x","$y") | Out-Null; Start-Sleep -Milliseconds 320 }
function Swipe($x1,$y1,$x2,$y2,$ms=450){ A @('shell','input','swipe',"$x1","$y1","$x2","$y2","$ms") | Out-Null }
function Cap($p){ for($i=0;$i -lt 4;$i++){ A @('shell','screencap','-p','/sdcard/_tea.png') | Out-Null; A @('pull','/sdcard/_tea.png',$p) | Out-Null; if((Test-Path $p) -and ((Get-Item $p).Length -gt 50000)){ return $true }; Start-Sleep 0.4 }; return $false }
function FocusOk(){ $f=(A @('shell','dumpsys','window') | Select-String 'mCurrentFocus' | Select-Object -First 1).ToString(); return ($f -match [regex]::Escape($pkg)) }
# --- template match (cv2) — NEVER blind-tap promo/GameCenter close X ---
$pyexe='C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe'
$matchPy=Join-Path $root 'match_template.py'
$tmplDir=Join-Path $root 'templates\ui'
$MATCH_THRESH=0.82
$actionDetectDir=Join-Path $root 'lists\action_detect'
function Find-Template([string]$shot,[string]$tmplName,[double]$thresh=$MATCH_THRESH){
  $tmpl=Join-Path $tmplDir $tmplName
  if(-not (Test-Path $matchPy)){ L 'Find-Template NO match_template.py'; return $null }
  if(-not (Test-Path $tmpl)){ L ("Find-Template missing {0}" -f $tmplName); return $null }
  if(-not (Test-Path $shot)){ return $null }
  $raw=& $pyexe $matchPy $shot $tmpl ("{0}" -f $thresh) 2>&1
  $line=($raw | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1)
  if(-not $line){ L ("Find-Template no-json {0}" -f $tmplName); return $null }
  try { return ($line | ConvertFrom-Json) } catch { L ("Find-Template parse fail: {0}" -f $line); return $null }
}
function Find-TemplateTap([string]$tmplName,[double]$thresh=$MATCH_THRESH,[string]$shotPath=$null){
  # Cap (unless shotPath given), match, tap once at match center. Returns match obj (ok may be false) or $null.
  $shot=if($shotPath){ $shotPath } else { Join-Path $root '_match_shot.png' }
  if(-not $shotPath){ if(-not (Cap $shot)){ L ("Find-TemplateTap cap fail {0}" -f $tmplName); return $null } }
  $m=Find-Template $shot $tmplName $thresh
  if($null -eq $m){ return $null }
  if(-not $m.ok){ L ("Find-TemplateTap MISS {0} score={1}" -f $tmplName, $m.score); return $m }
  L ("Find-TemplateTap HIT {0} score={1} center=({2},{3})" -f $tmplName, $m.score, $m.x, $m.y)
  Tap ([int]$m.x) ([int]$m.y)
  Start-Sleep -Milliseconds 550
  return $m
}
function Save-MatchCrop([string]$shot,[string]$label,[object]$m,[string]$outDir){
  # Observe-only: save a padded crop around match. NEVER taps.
  if(-not $m -or -not $m.ok){ return $null }
  New-Item -ItemType Directory -Force -Path $outDir | Out-Null
  $stamp=(Get-Date).ToString('HHmmssfff')
  $sc=[math]::Round([double]$m.score,3).ToString().Replace('.','p')
  $dest=Join-Path $outDir ("pause_{0}_{1}_s{2}.png" -f $label,$stamp,$sc)
  try {
    Add-Type -AssemblyName System.Drawing -EA SilentlyContinue
    $img=[System.Drawing.Bitmap]::FromFile($shot)
    $pad=24
    $tlx=if($null -ne $m.tl_x){[int]$m.tl_x}else{[int]$m.x - [int]($m.w/2)}
    $tly=if($null -ne $m.tl_y){[int]$m.tl_y}else{[int]$m.y - [int]($m.h/2)}
    $x=[Math]::Max(0,$tlx - $pad); $y=[Math]::Max(0,$tly - $pad)
    $w=[Math]::Min($img.Width-$x, [int]$m.w + 2*$pad); $h=[Math]::Min($img.Height-$y, [int]$m.h + 2*$pad)
    if($w -lt 8 -or $h -lt 8){ $img.Dispose(); Copy-Item $shot $dest -Force; return $dest }
    $rect=New-Object System.Drawing.Rectangle($x,$y,$w,$h)
    $crop=$img.Clone($rect, $img.PixelFormat)
    $crop.Save($dest, [System.Drawing.Imaging.ImageFormat]::Png)
    $crop.Dispose(); $img.Dispose()
    return $dest
  } catch {
    Copy-Item $shot $dest -Force -EA SilentlyContinue
    return $dest
  }
}
function Detect-SaveTemplates([string]$shot,[string]$outDir,[string[]]$names){
  # Match each template; on HIT save pause crop + log. Never tap. Returns count of hits.
  $hits=0
  if(-not (Test-Path $shot)){ return 0 }
  New-Item -ItemType Directory -Force -Path $outDir | Out-Null
  foreach($tn in $names){
    $m=Find-Template $shot $tn $MATCH_THRESH
    if($null -eq $m -or -not $m.ok){ continue }
    $label=[IO.Path]::GetFileNameWithoutExtension($tn)
    $path=Save-MatchCrop $shot $label $m $outDir
    L ("DETECT_SAVE {0} score={1} center=({2},{3}) -> {4} (no tap)" -f $label,$m.score,$m.x,$m.y,$path)
    $hits++
  }
  return $hits
}
function Detect-ActionButtons([string]$shot,[string]$outDir){
  # Replay observe-only: action buttons + dealer marks + replay control bar
  $names=@(
    'btn_hu_glow.png','btn_hu.png','btn_gang.png','btn_peng.png','btn_chi.png','btn_guo.png',
    'mark_zhuang.png','avatar_dealer_mark.png','mark_caishen.png','ui_replay_controls.png'
  )
  return (Detect-SaveTemplates $shot $outDir $names)
}
function DismissPromoCloseX([int]$maxTries=3){
  # ONLY safe promo/GC dismiss: match btn_close_x then tap center. On miss: STOP — never spray X coords / never 前往游戏/启动.
  for($i=1; $i -le $maxTries; $i++){
    if(FocusOk){ return $true }
    $shot=Join-Path $root ("lists\_dismiss_{0}.png" -f $i)
    if(-not (Cap $shot)){ L ("DismissPromoCloseX cap fail try={0}" -f $i); Start-Sleep 0.5; continue }
    $m=Find-Template $shot 'btn_close_x.png' $MATCH_THRESH
    if($null -eq $m -or -not $m.ok){
      $sc=if($m){ $m.score } else { 'n/a' }
      L ("DismissPromoCloseX no-match try={0} score={1} (NO blind tap)" -f $i, $sc)
      Start-Sleep 0.4; continue
    }
    L ("DismissPromoCloseX HIT try={0} score={1} tap=({2},{3})" -f $i, $m.score, $m.x, $m.y)
    Tap ([int]$m.x) ([int]$m.y)
    Start-Sleep 1.2
    if(FocusOk){ L 'DismissPromoCloseX focus restored'; return $true }
  }
  if(FocusOk){ return $true }
  $final=Join-Path $root 'lists\_NEED_USER_CLOSE.png'
  Cap $final | Out-Null
  L 'NEED_USER_CLOSE: btn_close_x unmatched after retries; path paused (no blind X / no launch-CTA)'
  return $false
}
function Close-ReplayBar([int]$maxTries=3){
  # In-replay close: ONLY via template match on btn_close_x or ui_replay_controls (right-side X). NEVER blind X_CLOSE/Y_CLOSE.
  for($i=1; $i -le $maxTries; $i++){
    $shot=Join-Path $root ("lists\_replay_close_{0}.png" -f $i)
    if(-not (Cap $shot)){ L ("Close-ReplayBar cap fail try={0}" -f $i); Start-Sleep 0.4; continue }
    # save detect of control bar when present
    Detect-SaveTemplates $shot $actionDetectDir @('ui_replay_controls.png') | Out-Null
    $m=Find-Template $shot 'btn_close_x.png' $MATCH_THRESH
    if($m -and $m.ok){
      L ("Close-ReplayBar HIT btn_close_x score={0} tap=({1},{2})" -f $m.score,$m.x,$m.y)
      Tap ([int]$m.x) ([int]$m.y)
      Start-Sleep 1.2
      return $true
    }
    $bar=Find-Template $shot 'ui_replay_controls.png' $MATCH_THRESH
    if($bar -and $bar.ok){
      # Close X sits on the right of the pause/ff/speed bar — tap near right edge of match
      $tlx=[int]$bar.tl_x; $tly=[int]$bar.tl_y; $bw=[int]$bar.w; $bh=[int]$bar.h
      $cx=$tlx + [Math]::Max(8, $bw - 36)
      $cy=$tly + [int]($bh / 2)
      L ("Close-ReplayBar HIT ui_replay_controls score={0} bar=({1},{2},{3}x{4}) tapX=({5},{6})" -f $bar.score,$tlx,$tly,$bw,$bh,$cx,$cy)
      Tap $cx $cy
      Start-Sleep 1.2
      return $true
    }
    $sc1=if($m){$m.score}else{'n/a'}; $sc2=if($bar){$bar.score}else{'n/a'}
    L ("TEMPLATE_MISS Close-ReplayBar try={0} close_x={1} replay_bar={2} (NO blind X_CLOSE)" -f $i,$sc1,$sc2)
    Start-Sleep 0.5
  }
  L 'TEMPLATE_MISS Close-ReplayBar exhausted — skipped blind close'
  return $false
}
function Try-LobbyTemplateNav(){
  # Prefer: btn_bisai (比赛) -> btn_zhanji (比赛战绩) -> btn_huifang (回放) -> btn_enter. Log TEMPLATE_MISS on each miss.
  $hit=$false
  $shot=Join-Path $root '_match_shot.png'
  if(-not (Cap $shot)){ L 'Try-LobbyTemplateNav cap fail'; return $false }
  Detect-SaveTemplates $shot $actionDetectDir @('icon_zjgamecenter.png') | Out-Null
  foreach($pair in @(
    @{n='btn_bisai.png'; t=0.80},
    @{n='btn_zhanji.png'; t=0.80},
    @{n='btn_huifang.png'; t=0.80},
    @{n='btn_enter.png'; t=0.80}
  )){
    $m=Find-Template $shot $pair.n $pair.t
    if($m -and $m.ok){
      L ("Try-LobbyTemplateNav HIT {0} score={1} ({2},{3})" -f $pair.n,$m.score,$m.x,$m.y)
      Tap ([int]$m.x) ([int]$m.y)
      Start-Sleep 1.0
      $hit=$true
      if(-not (Cap $shot)){ break }
    } else {
      $sc=if($m){$m.score}else{'n/a'}
      L ("TEMPLATE_MISS {0} score={1}" -f $pair.n,$sc)
    }
  }
  return $hit
}
function Tap-XiangqingOrEnter([int]$slot){
  # Prefer 详情 / 进入 template; fall back to X_DETAIL slot coords only on miss + TEMPLATE_MISS log
  $shot=Join-Path $root '_match_shot.png'
  if(Cap $shot){
    foreach($tn in @('btn_xiangqing.png','btn_enter.png')){
      $m=Find-Template $shot $tn $MATCH_THRESH
      if($m -and $m.ok){
        L ("Tap-XiangqingOrEnter HIT {0} score={1} ({2},{3})" -f $tn,$m.score,$m.x,$m.y)
        Tap ([int]$m.x) ([int]$m.y)
        Start-Sleep 2.5
        return $true
      }
      $sc=if($m){$m.score}else{'n/a'}
      L ("TEMPLATE_MISS {0} score={1}" -f $tn,$sc)
    }
  } else {
    L 'TEMPLATE_MISS btn_xiangqing/btn_enter (cap fail)'
  }
  L ("TEMPLATE_MISS fallback X_DETAIL slot={0} coords=({1},{2})" -f $slot,$X_DETAIL,$DETAIL_YS[$slot])
  Tap $X_DETAIL $DETAIL_YS[$slot]; Start-Sleep 2.5
  return $false
}
function EnsurePkg(){
  if(FocusOk){ return $true }
  L 'LOST pkg — template dismiss btn_close_x only (no blind X)'
  if(DismissPromoCloseX 3){
    Try-LobbyTemplateNav | Out-Null
    return $true
  }
  L 'LOST pkg STOP (NEED_USER_CLOSE)'
  return $false
}
function EnsureReplayTab(){
  # Prefer btn_huifang template; fallback old coords only on miss + TEMPLATE_MISS
  $m=Find-TemplateTap 'btn_huifang.png' $MATCH_THRESH
  if($m -and $m.ok){ Start-Sleep 0.3; return }
  $sc=if($m){$m.score}else{'n/a'}
  L ("TEMPLATE_MISS btn_huifang.png score={0} — fallback coords EnsureReplayTab" -f $sc)
  foreach($xy in @(@(1540,248),@(1525,242),@(1555,250),@(1540,235))){ Tap $xy[0] $xy[1]; Start-Sleep -Milliseconds 220 }; Start-Sleep 0.3
}
function ScrollPages([int]$n){ for($i=0;$i -lt $n;$i++){ Swipe 800 700 800 250 500; Start-Sleep 1.1 } }
function ForceDate($preferY,$label){
  if(-not (EnsurePkg)){ L ("ForceDate {0} blocked LOST/NEED_USER_CLOSE" -f $label); return $false }
  # Prefer date field / dropdown templates before blind date coords
  $shot=Join-Path $root '_date_probe.png'
  $opened=$false
  if(Cap $shot){
    foreach($tn in @('btn_date_dropdown.png','ui_date_field.png')){
      $m=Find-Template $shot $tn $MATCH_THRESH
      if($m -and $m.ok){
        L ("ForceDate template open HIT {0} score={1} ({2},{3})" -f $tn,$m.score,$m.x,$m.y)
        Tap ([int]$m.x) ([int]$m.y); Start-Sleep 1.0; $opened=$true; break
      }
      $sc=if($m){$m.score}else{'n/a'}
      L ("TEMPLATE_MISS {0} score={1}" -f $tn,$sc)
    }
  }
  if(-not $opened){
    L ("TEMPLATE_MISS date open — fallback X_DATE=({0},{1})" -f $X_DATE,$Y_DATE)
    Tap $X_DATE $Y_DATE; Start-Sleep 1.0
  }
  foreach($y in @($preferY,($preferY-15),($preferY+15),($preferY-30),($preferY+30),216,264,311,364,414,180,150,120)){
    Tap 320 $y; Start-Sleep 1.0; $probe=Join-Path $root '_date_probe.png'
    if(-not (Cap $probe)){ continue }
    $sz=(Get-Item $probe).Length
    if($sz -gt 780000 -and $sz -lt 1100000){ L "ForceDate $label ok y=$y sz=$sz"; return $true }
    # re-open date picker via template if possible
    $re=Find-Template $probe 'btn_date_dropdown.png' $MATCH_THRESH
    if($re -and $re.ok){ Tap ([int]$re.x) ([int]$re.y); Start-Sleep 0.7 }
    else {
      $re2=Find-Template $probe 'ui_date_field.png' $MATCH_THRESH
      if($re2 -and $re2.ok){ Tap ([int]$re2.x) ([int]$re2.y); Start-Sleep 0.7 }
      else { Tap $X_DATE $Y_DATE; Start-Sleep 0.7 }
    }
  }
  L "ForceDate $label FAIL"; return $false
}
function Get-GreenRatio($path){
  try {
    Add-Type -AssemblyName System.Drawing -EA SilentlyContinue
    $img=[System.Drawing.Bitmap]::FromFile($path)
    $g=0; $n=0
    for($y=120;$y -lt [Math]::Min(700,$img.Height);$y+=40){
      for($x=200;$x -lt [Math]::Min(1400,$img.Width);$x+=50){
        $c=$img.GetPixel($x,$y); $n++
        if($c.G -gt 80 -and $c.G -gt ($c.R+25) -and $c.G -gt ($c.B+15)){ $g++ }
      }
    }
    $img.Dispose()
    if($n -eq 0){ return 1.0 }
    return ($g / $n)
  } catch { return 1.0 }
}
function CaptureSettle($dir){
  foreach($k in 1..16){ Tap $X_SPEED $Y_SPEED; Start-Sleep -Milliseconds 120 }
  foreach($k in 1..20){ Tap $X_FF $Y_FF; Start-Sleep -Milliseconds 140 }
  $best=$null; $bestScore=-1
  for($i=1;$i -le 36;$i++){
    if(($i % 4) -eq 0){ foreach($k in 1..3){ Tap $X_SPEED $Y_SPEED; Start-Sleep -Milliseconds 80 }; foreach($k in 1..4){ Tap $X_FF $Y_FF; Start-Sleep -Milliseconds 90 } }
    $cand=Join-Path $dir ("_settle_cand_{0:d2}.png" -f $i)
    Cap $cand | Out-Null
    if(-not (Test-Path $cand)){ Start-Sleep 1.2; continue }
    $sz=(Get-Item $cand).Length
    if($sz -lt 450000){ Start-Sleep 1.2; continue }
    Detect-ActionButtons $cand (Join-Path $dir 'action_detect') | Out-Null
    $gr=Get-GreenRatio $cand
    $score = (1.0 - $gr) * 1000 + ([Math]::Min($sz,1200000)/1200000.0)*50
    if($gr -lt 0.28 -and $score -gt $bestScore){ $best=$cand; $bestScore=$score; L "SETTLE cand i=$i gr=$([math]::Round($gr,3)) sz=$sz" }
    if($gr -lt 0.18 -and $sz -gt 500000){ $best=$cand; break }
    Start-Sleep 1.25
  }
  if($best){
    Copy-Item $best (Join-Path $dir 'settle_score.png') -Force
    L "SETTLE saved sz=$((Get-Item $best).Length) score=$([math]::Round($bestScore,1))"
    Get-ChildItem $dir -Filter '_settle_cand_*.png' -EA SilentlyContinue | Remove-Item -Force -EA SilentlyContinue
    return $true
  }
  L 'SETTLE miss (no low-green scoreboard)'
  Get-ChildItem $dir -Filter '_settle_cand_*.png' -EA SilentlyContinue | Remove-Item -Force -EA SilentlyContinue
  return $false
}
function HarvestRound($date,$room,$ri,$slot){
  $pad='{0:d2}' -f $ri; $dir=Join-Path $root "$date\$room\r$pad"; New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $roundDetect=Join-Path $dir 'action_detect'
  $good=@(Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  $hasSettle=Test-Path (Join-Path $dir 'settle_score.png')
  if($good -ge $N_FRAMES -and $hasSettle){ L "SKIP $room r$pad"; return $true }
  Get-ChildItem $dir -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -lt 700000 | Remove-Item -Force -EA SilentlyContinue
  if(-not (EnsurePkg)){ return $false }
  for($attempt=1;$attempt -le 4;$attempt++){
    EnsureReplayTab; Tap $X_REPLAY $Ys[$slot]; Start-Sleep 4.2
    foreach($k in 1..3){ Tap $X_SPEED $Y_SPEED; Start-Sleep -Milliseconds 200 }
    $frames=@(); $ts=@(); $ok=$true
    for($f=1;$f -le $N_FRAMES;$f++){
      $fp=Join-Path $dir ("frame_{0:d2}.png" -f $f); Cap $fp | Out-Null
      $sz=0; if(Test-Path $fp){ $sz=(Get-Item $fp).Length }
      if($f -eq 1 -and $sz -lt 700000){ L "RETRY $room r$pad a=$attempt sz=$sz"; Close-ReplayBar 2 | Out-Null; Start-Sleep 1.2; $ok=$false; break }
      # Observe-only: detect action buttons / marks / replay bar — SAVE crops, NEVER tap actions
      if(Test-Path $fp){ Detect-ActionButtons $fp $roundDetect | Out-Null; Detect-ActionButtons $fp $actionDetectDir | Out-Null }
      $frames += ("$date/$room/r$pad/frame_{0:d2}.png" -f $f)
      $ts += [double]((Get-Date).ToUniversalTime() - [datetime]'1970-01-01').TotalSeconds
      if($f -lt $N_FRAMES){ Start-Sleep $FRAME_GAP }
    }
    if(-not $ok){ continue }
    CaptureSettle $dir | Out-Null
    Cap (Join-Path $dir '_ac.png') | Out-Null
    # In-replay close via template only (btn_close_x or ui_replay_controls right X) — never blind
    if(-not (Close-ReplayBar 3)){ L 'Close-ReplayBar failed after settle — continuing carefully' }
    Start-Sleep 1.0
    if(-not (FocusOk)){
      L 'post-replay focus lost - template dismiss only'
      if(-not (EnsurePkg)){ return $false }
    } else {
      # second close attempt still template-only
      Close-ReplayBar 2 | Out-Null
      if(-not (EnsurePkg)){ return $false }
    }
    $meta=[ordered]@{source='tea';date=$date;room_id="$room";round_index=$ri;round_uid="$room-$date-r$pad";frames=$frames;capture_timestamps=$ts;status='captured';has_settle_score=(Test-Path (Join-Path $dir 'settle_score.png'));frames_per_round=$N_FRAMES;user_account='jingjing';harvested_at=(Get-Date).ToString('yyyy-MM-ddTHH:mm:sszzz')}
    ($meta|ConvertTo-Json -Depth 6)|Set-Content -Encoding UTF8 (Join-Path $dir 'meta.json')
    L "DONE $room r$pad n=$N_FRAMES settle=$(Test-Path (Join-Path $dir 'settle_score.png'))"
    EnsureReplayTab; return $true
  }
  L "FAIL $room r$pad"; return $false
}
function HarvestRoom($date,$room,$pages,$slot,$maxRounds,$dateY,$label,$half=$false){
  L "=== $label $room pages=$pages slot=$slot rounds=$maxRounds nF=$N_FRAMES settle=1 half=$half ==="
  if(-not (EnsurePkg)){ return $false }
  if(-not (ForceDate $dateY $label)){ return $false }
  ScrollPages $pages
  if($half){ Swipe 800 650 800 470 350; Start-Sleep 1.1 }
  $lp=Join-Path $root ("lists\pre_${label}_${room}.png"); Cap $lp | Out-Null; L "pre $room sz=$((Get-Item $lp -EA SilentlyContinue).Length)"
  Tap-XiangqingOrEnter $slot | Out-Null
  Cap (Join-Path $root ("lists\detail_${label}_${room}.png")) | Out-Null; EnsureReplayTab
  $n1=[Math]::Min(6,$maxRounds)
  for($ri=1;$ri -le $n1;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-1))){ Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.5; return $false } }
  if($maxRounds -gt 6){ Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab; $n2=[Math]::Min(12,$maxRounds); for($ri=7;$ri -le $n2;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-7))){ Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.5; return $false } } }
  if($maxRounds -gt 12){ Swipe 800 700 800 280 400; Start-Sleep 1.0; EnsureReplayTab; for($ri=13;$ri -le $maxRounds;$ri++){ if(-not (HarvestRound $date $room $ri ($ri-13))){ Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.5; return $false } } }
  $fc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  $sc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'settle_score.png' -EA SilentlyContinue).Count
  L "ROOM $room good_frames=$fc settles=$sc"; Tap $X_BACK_LIST $Y_BACK_LIST; Start-Sleep 1.8; return $true
}
function RunPlan($date,$dateY,$label,$plan){
  L "=== BATCH $date nF=$N_FRAMES+settle rooms=$($plan.Count) ==="
  New-Item -ItemType Directory -Force -Path (Join-Path $root $date) | Out-Null
  foreach($item in $plan){
    $room=$item.room; $rounds=[int]$item.rounds; $need=$N_FRAMES*$rounds
    $good=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
    $sc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'settle_score.png' -EA SilentlyContinue).Count
    if($good -ge $need -and $sc -ge $rounds){ L "SKIP $room good=$good settles=$sc"; continue }
    if($good -ge 3*$rounds -and $sc -lt $rounds -and $N_FRAMES -eq 6){
      L "REDO $room for settle/dense (had good=$good settles=$sc)"
    } elseif($good -ge 3*$rounds -and $label -eq '1013' -and $sc -eq 0 -and $good -lt $need){
      L "KEEP thin $room good=$good"; continue
    } elseif($good -ge 48 -and $label -eq '1013' -and $N_FRAMES -eq 6 -and $sc -eq 0){
      L "SKIP thin-complete $room good=$good (settle backfill later)"; continue
    }
    $half=[bool]$item.half; $slot=0; if($null -ne $item.slot){ $slot=[int]$item.slot }
    if(-not (HarvestRoom $date $room ([int]$item.pages) $slot $rounds $dateY $label $half)){ L "WARN continue $room" }
  }
  L "=== BATCH $date DONE ==="
}
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
$N_FRAMES=6; $FRAME_GAP=1.5
L '=== V2 settle+dense takeover ==='
foreach($item in $plan1013){
  $room=$item.room; $rounds=[int]$item.rounds
  $good=@(Get-ChildItem (Join-Path $root "2026-10-13\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
  if($good -ge (6*[int]$item.rounds)){ L "SKIP done $room good=$good"; continue }
  $half=[bool]$item.half; $slot=0; if($null -ne $item.slot){ $slot=[int]$item.slot }
  if(-not (HarvestRoom '2026-10-13' $room ([int]$item.pages) $slot $rounds 264 '1013' $half)){ L "WARN $room" }
}
L '=== 2026-10-13 DONE ==='
function DiscoverDense($date,$dateY,$label){
  L "=== DENSE+SETTLE $date START ==="; New-Item -ItemType Directory -Force -Path (Join-Path $root $date) | Out-Null
  if(-not (ForceDate $dateY $label)){ L "no date $date"; return }
  Cap (Join-Path $root "lists\list_${date}_live.png") | Out-Null
  for($page=0;$page -le 6;$page++){
    if(-not (ForceDate $dateY $label)){ break }
    ScrollPages $page
    $lp=Join-Path $root ("lists\list_${date}_p${page}.png"); Cap $lp | Out-Null
    if($page -gt 0){ $prev=Join-Path $root ("lists\list_${date}_p$($page-1).png"); if((Test-Path $prev) -and ((Get-FileHash $lp).Hash -eq (Get-FileHash $prev).Hash)){ L "end $date page=$page"; break } }
    for($slot=0;$slot -lt 3;$slot++){
      $room=("p{0}s{1}" -f $page,$slot)
      $good=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'frame_*.png' -EA SilentlyContinue | Where-Object Length -ge 700000).Count
      $sc=@(Get-ChildItem (Join-Path $root "$date\$room") -Recurse -Filter 'settle_score.png' -EA SilentlyContinue).Count
      if($good -ge 96 -and $sc -ge 16){ L "SKIP $room"; continue }
      if(-not (HarvestRoom $date $room $page $slot 16 $dateY $label $false)){ L "WARN $room" }
    }
  }
  L "=== DENSE+SETTLE $date DONE ==="
}
DiscoverDense '2026-10-11' 180 '1011'
DiscoverDense '2026-10-10' 150 '1010'
DiscoverDense '2026-10-09' 120 '1009'
DiscoverDense '2026-10-08' 100 '1008'
L '=== ALL HARVEST COMPLETE ==='
