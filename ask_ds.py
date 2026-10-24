# -*- coding: utf-8 -*-
"""Hana -> DeepSeek (EchoBird router, via OpenCode CLI) task runner.
Usage: python ask_ds.py <prompt.txt> <out.txt> [model]
The prompt file holds the full assignment; opencode is told to read and execute it.
Output -> out.txt, stderr -> out.txt.err, exit code -> out.txt.done
"""
import subprocess, sys, os

def main():
    prompt_file = os.path.abspath(sys.argv[1])
    out_file = os.path.abspath(sys.argv[2])
    model = sys.argv[3] if len(sys.argv) > 3 else "echobird/auto"
    workdir = r"D:\温州麻将"
    msg = (
        "Your complete assignment is in this file: " + prompt_file + " . "
        "Read it as UTF-8 and execute everything it says. "
        "Working directory: " + workdir + " . "
        "When finished, end your reply with a section titled '## Report' "
        "summarizing what you changed and the exact test results."
    )
    cmdline = 'opencode run --model ' + model + ' "' + msg.replace('"', "'") + '"'
    with open(out_file, "w", encoding="utf-8") as out, open(out_file + ".err", "w", encoding="utf-8") as err:
        rc = subprocess.run(cmdline, shell=True, stdout=out, stderr=err, cwd=workdir).returncode
    with open(out_file + ".done", "w", encoding="utf-8") as f:
        f.write(str(rc))

if __name__ == "__main__":
    main()
