$ErrorActionPreference='Continue'
$root='C:\Users\Administrator\agent-tools\tea_harvest'
$log=Join-Path $root 'harvest_log.txt'
function L($m){ $t=(Get-Date).ToString('HH:mm:ss'); "$t $m" | Tee-Object -FilePath $log -Append }

L '=== ORCH: waiting for harvest_1013.ps1 (pid check / first3 done) ==='
$deadline=(Get-Date).AddMinutes(40)
while((Get-Date) -lt $deadline){
  $p=Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match 'harvest_1013\.ps1' -and $_.CommandLine -notmatch 'v2' -and $_.CommandLine -notmatch 'orch' }
  $tail=Get-Content $log -Tail 5 -EA SilentlyContinue
  if(-not $p){ L 'ORCH: first3 process gone'; break }
  if($tail -match 'first3 done'){ L 'ORCH: first3 done signal'; break }
  Start-Sleep 8
}
# Ensure no overlap
Start-Sleep 2
L '=== ORCH: launching harvest_1013_v2.ps1 NOW ==='
$p2=Start-Process -FilePath powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"$root\harvest_1013_v2.ps1" -PassThru -WindowStyle Hidden
L "ORCH: v2 pid=$($p2.Id)"
$p2.WaitForExit()
L "ORCH: v2 exited code=$($p2.ExitCode)"

# After 10-13 core: denser harvest for remaining dates
L '=== ORCH: launching denser next dates ==='
if(Test-Path "$root\harvest_next_dense.ps1"){
  $p3=Start-Process -FilePath powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"$root\harvest_next_dense.ps1" -PassThru -WindowStyle Hidden
  L "ORCH: dense pid=$($p3.Id)"
  $p3.WaitForExit()
  L "ORCH: dense exited code=$($p3.ExitCode)"
} else {
  L 'ORCH: harvest_next_dense.ps1 missing — will be written by agent'
}
L '=== ORCH complete ==='
