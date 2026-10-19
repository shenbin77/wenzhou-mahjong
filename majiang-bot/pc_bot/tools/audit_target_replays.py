"""Read-only audit of archived target recordings; never sends game input.

This script enumerates video metadata, samples at most 20 videos / 8 frames each,
and records current vision predictions as predictions, never gold labels. Original
media remains untouched. Only the designated artifact directory is written.
"""
from pathlib import Path
import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
import time

import cv2
import numpy as np

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from tile_vision import TileVision

VIDEO_SUFFIXES={'.mp4','.mkv','.avi','.mov','.webm','.m4v'}


def file_sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_png(path,img):
    path.parent.mkdir(parents=True,exist_ok=True)
    ok,encoded=cv2.imencode('.png',img)
    if not ok:raise RuntimeError(f'PNG encode failed: {path}')
    encoded.tofile(str(path))


def metadata(path,archive):
    before=path.stat()
    cap=cv2.VideoCapture(str(path))
    opened=cap.isOpened()
    fps=float(cap.get(cv2.CAP_PROP_FPS)) if opened else 0.
    count=int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) if opened else 0
    width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) if opened else 0
    height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) if opened else 0
    fourcc=int(cap.get(cv2.CAP_PROP_FOURCC)) if opened else 0
    cap.release()
    codec=''.join(chr((fourcc>>(8*i))&255) for i in range(4)).strip('\0')
    probe=None
    ffprobe=shutil.which('ffprobe')
    if ffprobe:
        result=subprocess.run([ffprobe,'-v','error','-select_streams','v:0',
            '-show_entries','stream=width,height,codec_name,r_frame_rate,avg_frame_rate,nb_frames,duration:format=duration',
            '-of','json',str(path)],capture_output=True,text=True,encoding='utf-8',timeout=30)
        if result.returncode==0:
            probe=json.loads(result.stdout)
    sha=file_sha256(path)
    after=path.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise RuntimeError(f'source changed during read: {path}')
    record={'source':str(path),'relative_source':str(path.relative_to(archive)),
            'bytes':before.st_size,'mtime_ns':before.st_mtime_ns,'sha256':sha,
            'opened':opened,'width':width,'height':height,'fps':fps,'frame_count':count,
            'duration_s':count/fps if fps>0 else None,'codec_fourcc':codec,
            'metadata_method':'OpenCV VideoCapture; no OCR of account identifiers'}
    if probe:
        record['ffprobe']=probe
        streams=probe.get('streams',[])
        if streams and streams[0].get('duration'):
            record['duration_s']=float(streams[0]['duration'])
        record['metadata_method']='ffprobe explicit media fields + OpenCV; no account OCR'
    return record


def priority(path,archive):
    relative=path.relative_to(archive)
    if relative.parts[0]=='录屏':p=0
    elif relative.parts[0]=='数据视频':p=1
    elif len(relative.parts)==1:p=2
    else:p=3
    return p,str(relative)


def make_sheet(frames,path,title):
    tw,th=480,270;rows=math.ceil(len(frames)/4)
    sheet=np.full((max(1,rows)*310+40,tw*4,3),235,np.uint8)
    cv2.putText(sheet,title,(12,27),cv2.FONT_HERSHEY_SIMPLEX,.68,(0,0,0),1)
    for i,(frame,record) in enumerate(frames):
        scale=min(tw/frame.shape[1],th/frame.shape[0])
        thumb=cv2.resize(frame,(round(frame.shape[1]*scale),round(frame.shape[0]*scale)),interpolation=cv2.INTER_AREA)
        x=(i%4)*tw;y=(i//4)*310+40
        sheet[y:y+thumb.shape[0],x:x+thumb.shape[1]]=thumb
        label=f"{record['timestamp_s']:.2f}s #{record['frame_index']} boxes={record.get('detected_boxes',0)} accepted={record.get('accepted_tiles',0)}"
        cv2.putText(sheet,label,(x+4,y+290),cv2.FONT_HERSHEY_SIMPLEX,.45,(0,0,0),1)
    write_png(path,sheet)


def sample_video(info,out,vision,nframes=8):
    source=Path(info['source']);video_id=info['video_id']
    cap=cv2.VideoCapture(str(source))
    count=info['frame_count'];fps=info['fps']
    fractions=[0.,.04,.15,.3,.5,.7,.85,.96][:nframes]
    indices=sorted(set(min(count-1,max(0,round((count-1)*q))) for q in fractions))
    frames=[];records=[]
    for ordinal,idx in enumerate(indices):
        cap.set(cv2.CAP_PROP_POS_FRAMES,idx)
        ok,frame=cap.read()
        if not ok:
            records.append({'frame_index':idx,'read_ok':False});continue
        decoded_timestamp=float(cap.get(cv2.CAP_PROP_POS_MSEC))/1000.
        record={'frame_index':idx,'timestamp_s':decoded_timestamp,
                'timestamp_method':'OpenCV decoded frame presentation timestamp (POS_MSEC); source may be variable FPS',
                'average_fps_estimated_timestamp_s':idx/fps if fps else None,
                'read_ok':True,'source_sha256':info['sha256'],
                'frame_pixel_sha256':hashlib.sha256(frame.tobytes()).hexdigest(),
                'shape':list(frame.shape),'gold_labels':None,
                'status':'unreviewed archived frame; mode not inferred from predictions'}
        frame_file=f'{video_id}/frame_{ordinal:02d}_{idx:09d}.png'
        write_png(out/frame_file,frame);record['frame_file']=frame_file
        height,width=frame.shape[:2]
        # Preserve source geometry for the audit. Portrait recordings may contain
        # rotated/letterboxed games; do not silently rotate or fabricate a match.
        if width>=height:
            t0=time.perf_counter()
            try:
                hand=vision.recognize_hand(frame)
                record.update(detected_boxes=len(vision.diagnostics),accepted_tiles=len(hand),
                              all_boxes_accepted=vision.last_complete,
                              vision_ms=round((time.perf_counter()-t0)*1000,2),
                              predictions=[dict(d) for d in vision.diagnostics])
            except Exception as exc:
                record['vision_error']=f'{type(exc).__name__}: {exc}'
        else:
            record.update(detected_boxes=0,accepted_tiles=0,vision_skipped='portrait source; inspect orientation first')
        # Candidate crops are geometric regions, not asserted UI button labels.
        crops={'hand_candidate':(0,int(height*.70),width,height),
               'action_candidate':(int(width*.45),int(height*.45),width,int(height*.85)),
               'settlement_candidate':(int(width*.15),int(height*.12),int(width*.88),int(height*.88))}
        record['candidate_crops']={}
        for kind,(x0,y0,x1,y1) in crops.items():
            dest=f'{video_id}/{kind}_{ordinal:02d}.png'
            write_png(out/dest,frame[y0:y1,x0:x1])
            record['candidate_crops'][kind]={'file':dest,'box':[x0,y0,x1,y1]}
        frames.append((frame,record));records.append(record)
    cap.release()
    contact=f'{video_id}/contact_sheet.png'
    make_sheet(frames,out/contact,f'{video_id}: archived evidence, predictions unverified')
    after=source.stat()
    unchanged=(after.st_size==info['bytes'] and after.st_mtime_ns==info['mtime_ns'])
    return {'video_id':video_id,'contact_sheet':contact,'frames':records,'source_unchanged':unchanged}


def audit(archive,out,max_videos=20,frames_per_video=8):
    archive=archive.resolve();out=out.resolve()
    allowed=(BASE/'artifacts/target_replay_validation').resolve()
    if out!=allowed and not out.is_relative_to(allowed):
        raise ValueError(f'output must stay within {allowed}')
    if out.is_relative_to(archive):
        raise ValueError('output must not be inside archived sources')
    if max_videos>20 or frames_per_video>8:
        raise ValueError('bounded audit limit is 20 videos and 8 frames/video')
    out.mkdir(parents=True,exist_ok=True)
    videos=sorted((p for p in archive.rglob('*') if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES),key=lambda p:priority(p,archive))
    inventory=[]
    for i,p in enumerate(videos):
        info=metadata(p,archive);info['video_id']=f'video_{i:02d}'
        info['selected']=i<max_videos and info['opened'] and info['frame_count']>0
        inventory.append(info)
    (out/'inventory.json').write_text(json.dumps({'archive':str(archive),'videos':inventory},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'event':'inventory','videos':len(inventory),'selected':sum(x['selected'] for x in inventory),'output':str(out/'inventory.json')},ensure_ascii=False),flush=True)
    vision=TileVision();vision.load_templates();samples=[]
    for info in inventory:
        if not info['selected']:continue
        sample=sample_video(info,out,vision,frames_per_video);samples.append(sample)
        (out/'samples.json').write_text(json.dumps(samples,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'event':'contact_sheet','video_id':info['video_id'],'source':info['source'],'contact_sheet':str(out/sample['contact_sheet']),'frame_count':len(sample['frames'])},ensure_ascii=False),flush=True)
    summary={'archive':str(archive),'videos_enumerated':len(inventory),'videos_sampled':len(samples),
             'sample_frames':sum(sum(f.get('read_ok',False) for f in s['frames']) for s in samples),
             'all_sampled_sources_unchanged':all(s['source_unchanged'] for s in samples),
             'gold_labels_created':False,'warnings':['TileVision outputs are predictions only; no precision/recall claim without manual gold labels.',
              'Archived media identity and target rules need visual/manual confirmation.','No game or financial actions executed.'],
             'inventory':'inventory.json','samples':'samples.json'}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'event':'complete',**summary},ensure_ascii=False),flush=True)
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,default=Path('D:/archive_温州麻将202608'))
    parser.add_argument('--out',type=Path,default=BASE/'artifacts/target_replay_validation')
    parser.add_argument('--max-videos',type=int,default=20)
    parser.add_argument('--frames-per-video',type=int,default=8)
    args=parser.parse_args()
    audit(args.archive,args.out,args.max_videos,args.frames_per_video)
