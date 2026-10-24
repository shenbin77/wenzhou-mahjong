# -*- coding: utf-8 -*-
"""C: drive top-consumer scan (read-only). Phase 1: C:\\ children sizes.
Phase 2: drill into the 3 largest children, listing their immediate subdirs."""
import os, sys

def dir_size(path):
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total

def children_sizes(root):
    out = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return out
    for e in entries:
        try:
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                out.append((dir_size(e.path), e.path))
            elif e.is_file(follow_symlinks=False):
                out.append((e.stat(follow_symlinks=False).st_size, e.path))
        except OSError:
            pass
    return sorted(out, reverse=True)

def gb(n):
    return f"{n / 1e9:.2f} GB"

print("=== Phase 1: C:\\ children ===")
top = children_sizes("C:\\")
for size, path in top:
    print(gb(size), path)
print()
for size, path in top[:3]:
    if os.path.isdir(path):
        print(f"=== Drill: {path} ===")
        for s2, p2 in children_sizes(path)[:12]:
            print(" ", gb(s2), p2)
        print()
sys.stdout.flush()
