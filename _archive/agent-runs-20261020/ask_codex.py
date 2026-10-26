# -*- coding: utf-8 -*-
"""Hana->Codex bridge runner.
Usage: python ask_codex.py prompt.txt out.txt [model]
Reads the prompt file as UTF-8 (falls back to GBK), re-encodes to UTF-8,
feeds it to `codex exec` via stdin, streams stdout/stderr to files,
and writes out.txt.done with the exit code when finished.
"""
import subprocess, sys, os

CODEX = r"D:\npm-global\node_modules\@openai\codex\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe"
WORKDIR = r"D:\温州麻将"

def read_prompt(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")

def main():
    prompt_file, out_file = sys.argv[1], sys.argv[2]
    model = sys.argv[3] if len(sys.argv) > 3 else "gpt-5.6-sol"
    prompt_text = read_prompt(prompt_file)
    err_file = out_file + ".err"
    done_file = out_file + ".done"
    if os.path.exists(done_file):
        os.remove(done_file)
    tmp_prompt = out_file + ".prompt_utf8"
    with open(tmp_prompt, "w", encoding="utf-8") as f:
        f.write(prompt_text)
    with open(tmp_prompt, "rb") as fin, open(out_file, "wb") as fout, open(err_file, "wb") as ferr:
        p = subprocess.Popen(
            [CODEX, "exec", "--skip-git-repo-check", "-m", model],
            stdin=fin, stdout=fout, stderr=ferr, cwd=WORKDIR,
        )
        rc = p.wait()
    with open(done_file, "w", encoding="utf-8") as f:
        f.write(str(rc))
    print("finished rc=", rc)

if __name__ == "__main__":
    main()
