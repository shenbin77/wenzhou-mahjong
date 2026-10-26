# -*- coding: utf-8 -*-
"""Build the _dfull Codex home: copy bridge-home files, strip the bridge base_url line."""
import os, shutil

src_home = r"D:\CodexHome"
dst_home = r"D:\温州麻将\_dfull"
os.makedirs(dst_home, exist_ok=True)

shutil.copyfile(os.path.join(src_home, "auth.json"), os.path.join(dst_home, "auth.json"))
hooks = os.path.join(src_home, "hooks.json")
if os.path.exists(hooks):
    shutil.copyfile(hooks, os.path.join(dst_home, "hooks.json"))

with open(os.path.join(src_home, "config.toml"), encoding="utf-8") as f:
    lines = f.read().splitlines()
kept = [l for l in lines if not l.strip().startswith("openai_base_url")]
with open(os.path.join(dst_home, "config.toml"), "w", encoding="utf-8") as f:
    f.write("\n".join(kept) + "\n")
print("config lines kept:", len(kept), "removed:", len(lines) - len(kept))
print("auth copied:", os.path.exists(os.path.join(dst_home, "auth.json")))
