$ErrorActionPreference = 'Continue'
$codex = 'D:\npm-global\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe'
$out = 'D:\温州麻将\_probe_out.txt'
Remove-Item $out -ErrorAction SilentlyContinue
Remove-Item "$out.done" -ErrorAction SilentlyContinue

"=== bridge (D:\CodexHome) gpt-6-astra $(Get-Date -Format o) ===" | Out-File $out -Encoding utf8
$env:CODEX_HOME = 'D:\CodexHome'
'' | & $codex exec --skip-git-repo-check -m gpt-6-astra 'Reply with exactly: OK' 2>&1 | Out-File $out -Append -Encoding utf8
"bridge rc=$LASTEXITCODE" | Out-File $out -Append -Encoding utf8

$dt = 'D:\温州麻将\_dtest'
if (-not (Test-Path "$dt\auth.json")) {
  New-Item -ItemType Directory -Force $dt | Out-Null
  Copy-Item 'D:\CodexHome\auth.json' "$dt\auth.json"
  Set-Content "$dt\config.toml" 'model = "gpt-5.6-sol"' -Encoding utf8
}
"=== direct (no bridge) gpt-6-astra $(Get-Date -Format o) ===" | Out-File $out -Append -Encoding utf8
$env:CODEX_HOME = $dt
'' | & $codex exec --skip-git-repo-check -m gpt-6-astra 'Reply with exactly: OK' 2>&1 | Out-File $out -Append -Encoding utf8
"direct rc=$LASTEXITCODE" | Out-File $out -Append -Encoding utf8

"done" | Out-File "$out.done" -Encoding utf8
