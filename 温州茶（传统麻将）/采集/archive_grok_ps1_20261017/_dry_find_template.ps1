$ErrorActionPreference='Continue'
$adb='D:\leidian\LDPlayer14\adb.exe'; $s='emulator-5554'
$root='C:\Users\Administrator\agent-tools\tea_harvest'
$log=Join-Path $root 'harvest_log.txt'
$pkg='com.xm.zjgamecenter'
function L($m){ $t=(Get-Date).ToString('HH:mm:ss'); "$t DRY $m" | Tee-Object -FilePath $log -Append }
$pyexe='C:\Users\Administrator\AppData\Local\Programs\Python\Python311\python.exe'
$matchPy=Join-Path $root 'match_template.py'
$tmplDir=Join-Path $root 'templates\ui'
$MATCH_THRESH=0.82
function Find-Template([string]$shot,[string]$tmplName,[double]$thresh=$MATCH_THRESH){
  $tmpl=Join-Path $tmplDir $tmplName
  $raw=& $pyexe $matchPy $shot $tmpl ("{0}" -f $thresh) 2>&1
  $line=($raw | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1)
  return ($line | ConvertFrom-Json)
}
$m1=Find-Template (Join-Path $root 'lists\_gc_now.png') 'btn_close_x.png'
$m2=Find-Template (Join-Path $root 'lists\_gc3.png') 'btn_close_x.png'
$m3=Find-Template (Join-Path $root '_after_close.png') 'btn_close_x.png'
"HIT_gc_now ok=$($m1.ok) score=$($m1.score) center=($($m1.x),$($m1.y))"
"MISS_gc3 ok=$($m2.ok) score=$($m2.score)"
"MISS_after ok=$($m3.ok) score=$($m3.score)"
if($m1.ok -and -not $m2.ok -and -not $m3.ok){ 'DRY_PASS: match gates correct; NO tap performed; harvest NOT started' } else { 'DRY_UNEXPECTED' }
