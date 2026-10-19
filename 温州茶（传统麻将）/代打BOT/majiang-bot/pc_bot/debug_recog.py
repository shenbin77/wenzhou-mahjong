import cv2, sys
sys.path.insert(0, 'D:\\HanaAgent\\majiang-bot\\pc_bot')
from bot import TileRecognizer

bot = TileRecognizer()

# 用最新截图（从 pc-server 抓）
import urllib.request
urllib.request.urlretrieve('http://127.0.0.1:8099/shot', 'D:\\HanaAgent\\majiang-bot\\pc_bot\\debug_shot.jpg')
img = cv2.imread('D:\\HanaAgent\\majiang-bot\\pc_bot\\debug_shot.jpg')
print(f"Image shape: {img.shape}")

# 1. 测试 count_tiles()
count, boxes = bot.count_tiles(img)
print(f"\n=== count_tiles: {count} boxes ===")
for b in boxes:
    print(f"  box: x1={b[0]} y1={b[1]} x2={b[2]} y2={b[3]} w={b[2]-b[0]} h={b[3]-b[1]}")

# 2. 测试每个框的模板匹配最高分
bot.load_templates()
print(f"\n=== Loaded {len(bot.templates)} templates ===")
gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

for i, (x1,y1,x2,y2) in enumerate(boxes[:5]):  # 只测前5个
    crop = gray[y1:y2, x1:x2]
    if crop.size == 0:
        print(f"\nBox {i}: EMPTY CROP, skip")
        continue
    ch, cw = crop.shape
    print(f"\nBox {i} ({x1},{y1},{x2},{y2}) crop size {ch}x{cw}:")
    scores = []
    for name, tid, tmpl in bot.templates:
        th, tw = tmpl.shape
        # 模板缩放到 crop 尺寸（缩小，保留细节）
        if tmpl.shape != (ch, cw):
            tmpl_rs = cv2.resize(tmpl, (cw, ch), interpolation=cv2.INTER_AREA)
        else:
            tmpl_rs = tmpl
        res = cv2.matchTemplate(crop, tmpl_rs, cv2.TM_CCOEFF_NORMED)
        scores.append((float(res[0,0]), name, tid))
    scores.sort(reverse=True)
    print(f"  Top 5:")
    for s, n, t in scores[:5]:
        print(f"    {s:.4f} {n} (tid={t})")
    print(f"  Bottom 3:")
    for s, n, t in scores[-3:]:
        print(f"    {s:.4f} {n} (tid={t})")

print("\n=== DONE ===")
