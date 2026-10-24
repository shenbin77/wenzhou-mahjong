# -*- coding: utf-8 -*-
"""Decrypt EchoBird smart-router apiKey per the app's own scheme:
AES key = HMAC-SHA256(file_key, "{MachineGuid}|{USERNAME}"), AES-256-GCM,
stored as enc:v1:<hex(nonce12 + ct+tag)>. Prints masked info only;
saves plaintext key to the target file and validates against the router."""
import json, hmac, hashlib, subprocess, urllib.request, os

CFG = r"C:\Users\Administrator\.echobird\config\smart-router.json"
KEYF = r"C:\Users\Administrator\.echobird\config\.encryption_key"
OUT = r"D:\温州麻将\_echobird_key.txt"

cfg = json.load(open(CFG, encoding="utf-8"))
stored = cfg["apiKey"]
assert stored.startswith("enc:v1:"), stored[:12]
file_key = open(KEYF, "rb").read()
print("file_key len:", len(file_key))

out = subprocess.run(["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography",
                      "/v", "MachineGuid"], capture_output=True, text=True).stdout
guid = None
for line in out.splitlines():
    if "MachineGuid" in line:
        guid = line.split("REG_SZ")[-1].strip()
print("guid tail:", guid[-6:] if guid else None)

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def try_identity(identity, label):
    derived = hmac.new(file_key, identity.encode(), hashlib.sha256).digest()
    blob = bytes.fromhex(stored[len("enc:v1:"):])
    try:
        pt = AESGCM(derived).decrypt(blob[:12], blob[12:], None)
        print("DECRYPTED with", label)
        return pt.decode()
    except Exception as e:
        print("fail", label, type(e).__name__)
        return None

plain = None
for ident, label in [
    (f"{guid}|Administrator", "guid|Administrator"),
    (f"{guid.lower()}|Administrator", "guid-lower|Administrator"),
    (f"{guid}|administrator", "guid|administrator"),
]:
    plain = try_identity(ident, label)
    if plain:
        break

if plain:
    open(OUT, "w", encoding="utf-8").write(plain)
    print("key masked:", plain[:4] + "..." + plain[-4:], "len", len(plain))
    req = urllib.request.Request("http://127.0.0.1:53683/v1/models",
                                 headers={"Authorization": "Bearer " + plain})
    body = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
    print("MODELS:", [m.get("id") for m in body.get("data", [])])
else:
    print("decryption failed for all identity variants")
