# -*- coding: utf-8 -*-
"""Scan v2: find 'smart-router'/'router' keys in EchoBird leveldb files,
decode nearby bytes as ASCII and UTF-16LE, collect token candidates,
validate each against the local router; never prints a full key."""
import os, re, json, urllib.request

BASE = r"C:\Users\Administrator\AppData\Local\com.echobird.ai\EBWebView\Default"
DIRS = [
    os.path.join(BASE, "Local Storage", "leveldb"),
    os.path.join(BASE, "Session Storage"),
]
OUT = r"D:\温州麻将\_echobird_key.txt"
KEYWORDS = [b"smart-router", b"routerApiKey", b"router_api_key", b"apiKey", b"53683"]

def windows(path):
    try:
        data = open(path, "rb").read()
    except Exception:
        return []
    out = []
    for kw in KEYWORDS:
        start = 0
        while True:
            i = data.find(kw, start)
            if i < 0:
                break
            out.append((kw.decode(), data[i:i + 800]))
            start = i + 1
    return out

def texts(blob):
    res = []
    res += [m.decode("utf-8", "replace") for m in re.findall(rb"[\x20-\x7e]{4,}", blob)]
    try:
        u = blob.decode("utf-16le", "ignore")
        res += [m for m in re.findall(r"[\x20-\x7e]{4,}", u)]
    except Exception:
        pass
    return res

def main():
    candidates = []
    for d in DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            for kw, blob in windows(p):
                ts = texts(blob)
                print(f"--- {name} near '{kw}': {len(ts)} strings")
                for t in ts[:24]:
                    print("   ", t[:120])
                for t in ts:
                    for tok in re.findall(r"[A-Za-z0-9_\-\.]{20,128}", t):
                        candidates.append(tok)
    print("candidates:", len(set(candidates)))
    for t in sorted(set(candidates)):
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:53683/v1/models",
                headers={"Authorization": "Bearer " + t},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
            ids = [m.get("id") for m in body.get("data", [])]
            with open(OUT, "w", encoding="utf-8") as f:
                f.write(t)
            print("KEY VALIDATED masked:", t[:4] + "..." + t[-4:], "len", len(t))
            print("MODELS:", ids)
            return
        except Exception:
            continue
    print("no candidate validated")

if __name__ == "__main__":
    main()
