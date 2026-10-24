# -*- coding: utf-8 -*-
"""Scan EchoBird WebView Local Storage leveldb for the router API key.
Never prints the key; writes it to the target file and reports masked info,
then verifies it against the local router /v1/models endpoint."""
import os, re, sys, json, urllib.request

BASE = r"C:\Users\Administrator\AppData\Local\com.echobird.ai\EBWebView\Default"
CANDIDATE_DIRS = [
    os.path.join(BASE, "Local Storage", "leveldb"),
    os.path.join(BASE, "Session Storage"),
    os.path.join(BASE, "IndexedDB"),
]
OUT = r"D:\温州麻将\_echobird_key.txt"

def strings_from(path):
    try:
        data = open(path, "rb").read()
    except Exception:
        return []
    return re.findall(rb"[\x20-\x7e]{6,}", data)

def main():
    hits = []
    for d in CANDIDATE_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            for s in strings_from(p):
                low = s.lower()
                if b"router" in low or b"echobird" in low:
                    hits.append((p, s.decode("utf-8", "replace")))
    print("== context strings mentioning router/echobird ==")
    seen = set()
    for p, s in hits:
        if s in seen:
            continue
        seen.add(s)
        print(os.path.basename(p), "|", s[:160])
    # token candidates: long opaque tokens in those files
    token_re = re.compile(r"^[A-Za-z0-9_\-\.]{24,128}$")
    tokens = {}
    for d in CANDIDATE_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            for s in strings_from(p):
                t = s.decode("utf-8", "replace").strip()
                if token_re.match(t) and not t.startswith("http"):
                    tokens.setdefault(t, os.path.basename(p))
    print("== token candidates:", len(tokens), "==")
    # try each candidate against the router, longest-context first
    for t, src in tokens.items():
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
            print("KEY FOUND in", src, "masked:", t[:4] + "..." + t[-4:], "len", len(t))
            print("MODELS:", ids)
            return
        except Exception:
            continue
    print("no candidate key validated")

if __name__ == "__main__":
    main()
