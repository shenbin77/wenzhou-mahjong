# -*- coding: utf-8 -*-
"""Label harvested tile crops without vision:
- pin (筒): count circle-like contours
- sou (条): count bar-like contours (1s = bird, single big colorful blob)
- man (万) & honors: Windows OCR (zh-CN) on the crop
Output: labels.json  {cluster_file: label}
"""
import os, glob, json, subprocess, sys
import cv2
import numpy as np

TMP = "_ocr_tmp"
os.makedirs(TMP, exist_ok=True)

PS = r'''
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile,Windows.Foundation,ContentType=WindowsRuntime]
Await($WinRtAction, $ResultType) {
  $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
  $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
  $netTask = $asTask.Invoke($null, @($WinRtAction))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}
$path = $args[0]
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bmp = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$ocrEngine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new("zh-CN"))
$result = Await ($ocrEngine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
Write-Output $result.Text
'''

PS_PATH = os.path.abspath("ocr.ps1")
with open(PS_PATH, "w", encoding="utf-8-sig") as f:
    f.write(PS)

def ocr(img):
    p = os.path.abspath(os.path.join(TMP, "t.png"))
    # upscale 3x for better OCR
    big = cv2.resize(img, (img.shape[1]*3, img.shape[0]*3), interpolation=cv2.INTER_CUBIC)
    cv2.imwrite(p, big)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-File", PS_PATH, p],
                             capture_output=True, timeout=30)
        return out.stdout.decode("utf-8", "ignore").strip()
    except Exception as e:
        return ""

def count_shapes(img):
    """Return (n_circle, n_bar) contours inside tile face (exclude cream bg)."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sat = cv2.inRange(hsv, np.array([0, 60, 60]), np.array([179, 255, 255]))
    sat |= cv2.inRange(hsv, np.array([0, 0, 0]), np.array([179, 255, 90]))  # dark ink
    # remove thin border of tile itself
    h, w = sat.shape
    cv2.rectangle(sat, (0, 0), (w, h), 0, 4)
    # clean noise
    sat = cv2.morphologyEx(sat, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    cnts, _ = cv2.findContours(sat, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    circles, bars, blobs = 0, 0, 0
    areas = []
    for c in cnts:
        x, y, bw, bh = cv2.boundingRect(c)
        a = bw * bh
        if a < 40:
            continue
        areas.append((x, y, bw, bh, a))
        ar = bw / bh
        if 0.65 <= ar <= 1.5 and a >= 150:
            circles += 1
        elif 0.30 <= ar <= 0.62 and bh >= 28:
            bars += 1
        else:
            blobs += 1
    return circles, bars, blobs, areas

if __name__ == "__main__":
    files = sorted(glob.glob("harvest2/clusters/*.png"))
    results = {}
    for fp in files:
        img = cv2.imread(fp)
        c, b, bl, areas = count_shapes(img)
        txt = ocr(img)
        results[os.path.basename(fp)] = {"circles": c, "bars": b, "blobs": bl,
                                         "ocr": txt, "areas": [list(map(int, a)) for a in areas[:12]]}
        print(os.path.basename(fp), "circles=", c, "bars=", b, "blobs=", bl, "ocr=", repr(txt), flush=True)
    with open("labels.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
