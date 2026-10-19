"""Rebuild the manually reviewed 34-label visual set from real screenshot pixels.

Run from any directory: python pc_bot/tools/rebuild_templates.py
Existing source captures and the previous templates are never modified.
"""
from pathlib import Path
from datetime import datetime
import hashlib
import json
import sys

import cv2
import numpy as np

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from tile_vision import face_region, TileVision, TILE_IDS, _read

# Reviewed on the original-pixel contact sheets, 2026-10-14.
CLUSTER_LABELS = {
    0:'s1',1:'s9',2:'s3',3:'t5',4:'t6',5:'s8',6:'t9',7:'w2',8:'s4',
    9:'w3',10:'w5',11:'xi',12:'t4',13:'s2',15:'t2',16:'s7',17:'t8',
    18:'s3',19:'w2',20:'t8',21:'t1',22:'w6',23:'w7',24:'w3',25:'s1',
    26:'s2',27:'bai',28:'t8',29:'nan',30:'t3',31:'w8',32:'w6',33:'w1',
    34:'w7',35:'s2',36:'t7',37:'t9',38:'s7',39:'dong',40:'t8',41:'bai',
    42:'s9',43:'t1',44:'s5',45:'xi',46:'t9',47:'t6',48:'hongzhong',
    49:'t4',51:'w4',52:'bei',56:'fa',58:'t7',69:'s1',70:'s5',71:'s8',
    72:'t9',73:'t9',95:'t3',96:'t5',98:'t7'
}


def pixel_hash(im):
    return hashlib.sha256(str(im.shape).encode()+im.tobytes()).hexdigest()


def provenance_index():
    # Replay the original harvesting box detector to recover exact screenshot
    # coordinate provenance, rather than trusting cluster filenames alone.
    from detect_tiles import detect_hand_tiles
    index = {}
    for src in sorted((BASE/'frames_raw').glob('f*.png')):
        im = _read(src)
        if im is None or im.shape[1] != 1600:
            continue
        for x,y,w,h in detect_hand_tiles(im):
            tile=im[y:y+h,x:x+w]
            index.setdefault(pixel_hash(tile), {'screenshot':str(src.relative_to(BASE)),
                                               'box':[int(x),int(y),int(x+w),int(y+h)]})
    return index


def build():
    out=BASE/'templates_hongzhong_v2'
    out.mkdir(exist_ok=True)
    # Keep a baseline before replacing a previous run of these owned outputs.
    old=out/'manifest.json'
    if old.exists():
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
        backup=BASE/'artifacts/vision_validation'/f'baseline_{stamp}'
        backup.mkdir(parents=True,exist_ok=True)
        import shutil
        for p in out.rglob('*'):
            if p.is_file():
                dest=backup/p.relative_to(out);dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(p,dest)
    provenance=provenance_index()
    records=[]
    seen={}
    def save(label,crop,source,source_info=None,notes=None):
        n=seen.get(label,0);seen[label]=n+1
        name=f'{label}.png' if n==0 else f'variants/{label}_{n:02d}.png'
        face=face_region(crop).copy()
        path=out/name;path.parent.mkdir(parents=True,exist_ok=True)
        ok=cv2.imencode('.png',face)[0]
        if not ok:
            raise RuntimeError(f'cannot encode {name}')
        cv2.imencode('.png',face)[1].tofile(str(path))
        rec={'file':name,'label':label,'tile_id':TILE_IDS[label],
             'source':str(source),'source_pixel_sha256':pixel_hash(crop),
             'template_pixel_sha256':pixel_hash(face),'template_shape':list(face.shape),
             'review':'human-readable label visually checked by Codex on source pixels'}
        if source_info:rec.update(source_info)
        if notes:rec['notes']=notes
        records.append(rec)
    for ci,label in sorted(CLUSTER_LABELS.items()):
        src=next((BASE/'harvest2/clusters').glob(f'c{ci:02d}_*.png'))
        im=_read(src)
        info=provenance.get(pixel_hash(im))
        if info is None:
            raise RuntimeError(f'cluster has no exact screenshot pixel provenance: {src}')
        save(label,im,src.relative_to(BASE),info)
    # Nine-wan is absent from the old harvest but clear in independent live data.
    src=BASE/'frames_live/live_065.png';im=_read(src)
    box=TileVision().count_tiles(im)[1][2]
    x0,y0,x1,y1=box
    save('w9',im[y0:y1,x0:x1],src.relative_to(BASE),{'screenshot':str(src.relative_to(BASE)), 'box':list(box)})
    # Six-bamboo is visible among left discards. Rotation is lossless real pixels,
    # with lower resolution explicitly recorded; no synthetic tile construction.
    src=BASE/'frames_raw/f01.png';im=_read(src)
    box=[233,536,290,575]
    crop=im[box[1]:box[3],box[0]:box[2]]
    crop=cv2.rotate(crop,cv2.ROTATE_90_CLOCKWISE)
    save('s6',crop,src.relative_to(BASE),{'screenshot':str(src.relative_to(BASE)), 'box':box,
                                      'rotation':'90_clockwise'},'lower-resolution discarded tile; full-size hand sample still required')
    covered=set(seen)
    if covered!=set(TILE_IDS):
        raise RuntimeError(f'missing labels: {sorted(set(TILE_IDS)-covered)}')
    manifest={'version':2,'created':'2026-10-14','gameplay_target':'温州红中放炮; 112 tiles; red-Zhong wildcard',
              'sample_mode':'温麻4人 screenshots are visual samples only',
              'canonical_labels':len(covered),'template_count':len(records),
              'not_gameplay_validation':True,'templates':records,
              'known_limits':['s6 uses small rotated discard source; needs full-size holdout',
                              'live_065 is training source for w9; exclude it from holdout',
                              'raw f01 is training source for s6; exclude it from holdout']}
    old.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    # A corrected label may change variant filenames. Preserve obsolete owned
    # outputs outside the active set so a future glob loader cannot misread them.
    active={r['file'] for r in records}
    for p in list(out.rglob('*.png')):
        if p.relative_to(out).as_posix() not in active:
            obsolete=BASE/'artifacts/vision_validation/obsolete_templates'/p.relative_to(out)
            obsolete.parent.mkdir(parents=True,exist_ok=True)
            if obsolete.exists():
                obsolete=obsolete.with_name(f'{obsolete.stem}_{datetime.now().strftime("%H%M%S")}.png')
            import shutil
            shutil.move(str(p),str(obsolete))
    # Useful labelled delivery sheet, generated from the actual template files.
    sheet=np.full((5*180,7*140,3),238,np.uint8)
    for i,label in enumerate(TILE_IDS):
        tile=_read(out/f'{label}.png');h,w=tile.shape[:2]
        ratio=min(105/w,135/h);tile=cv2.resize(tile,(round(w*ratio),round(h*ratio)),interpolation=cv2.INTER_NEAREST)
        yy,xx=(i//7)*180,(i%7)*140
        cv2.putText(sheet,label,(xx+8,yy+23),cv2.FONT_HERSHEY_SIMPLEX,.65,(0,0,0),1)
        sheet[yy+30:yy+30+tile.shape[0],xx+10:xx+10+tile.shape[1]]=tile
    cv2.imwrite(str(BASE/'artifacts/vision_validation/templates_34.png'),sheet)
    print(json.dumps({'canonical_labels':len(covered),'variants':len(records),'manifest':str(old)},ensure_ascii=False))


if __name__=='__main__':
    build()
