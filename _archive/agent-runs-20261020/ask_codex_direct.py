# -*- coding: utf-8 -*-
"""Direct-route Codex runner v2: uses the FULL copy of the bridge home
(_dfull = D:\\CodexHome files with the openai_base_url line removed),
so trust/sandbox/hooks match the known-good setup while traffic goes
to the official backend. Usage: python ask_codex_direct.py <prompt> <out> [model]
"""
import subprocess, sys, os

CODEX_EXE = r"D:\npm-global\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe"

def main():
    prompt_file = sys.argv[1]
    out_file = sys.argv[2]
    model = sys.argv[3] if len(sys.argv) > 3 else "gpt-5.6-sol"
    workdir = r"D:\温州麻将"
    env = dict(os.environ)
    env["CODEX_HOME"] = r"D:\温州麻将\_dfull"
    with open(prompt_file, "rb") as pf, open(out_file, "wb") as out, open(out_file + ".err", "wb") as err:
        rc = subprocess.run(
            [CODEX_EXE, "exec", "--skip-git-repo-check", "-m", model],
            stdin=pf, stdout=out, stderr=err, cwd=workdir, env=env,
        ).returncode
    with open(out_file + ".done", "w", encoding="utf-8") as f:
        f.write(str(rc))

if __name__ == "__main__":
    main()
