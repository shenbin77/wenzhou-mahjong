"""Windows/Python 3.10+: same-font date recognition, no OCR engine.

reference.png must contain the five COMPLETE dates in --dates order.
Use --image for offline detection, or --adb for the screenshot/tap/verify loop.
"""
import argparse
import itertools
import json
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np


def read_image(path):
    im = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    if im is None:
        raise ValueError(f"Cannot decode {path}")
    return im


def write_image(path, im):
    if path.exists():
        raise FileExistsError(path)
    ok, data = cv2.imencode('.png', im)
    if not ok:
        raise RuntimeError('PNG encoding failed')
    data.tofile(str(path))


def overlap(a, b):
    x, y = max(a[0], b[0]), max(a[1], b[1])
    r, d = min(a[0]+a[2], b[0]+b[2]), min(a[1]+a[3], b[1]+b[3])
    inter = max(0, r-x)*max(0, d-y)
    return inter / max(1, a[2]*a[3]+b[2]*b[3]-inter)


def normalized(mask):
    pts = cv2.findNonZero(mask)
    if pts is None:
        raise ValueError('Empty text mask')
    x, y, w, h = cv2.boundingRect(pts)
    im = cv2.resize(mask[y:y+h, x:x+w], (200, 32), interpolation=cv2.INTER_AREA)
    im = cv2.GaussianBlur(im.astype(np.float32)/255., (3, 3), .5)
    return im


def date_shape(mask):
    """YYYY-MM-DD: eight tall glyphs and two short dashes, in that order.

    This intentionally rejects broken/joined glyphs instead of guessing a label.
    """
    h = mask.shape[0]
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    glyphs = [s for s in stats[1:n] if s[4] >= max(2, h*h*.012)]
    glyphs.sort(key=lambda s: s[0])
    if len(glyphs) != 10:
        return False
    return all((s[3] < .5*h if i in (4, 7) else s[3] >= .65*h)
               for i, s in enumerate(glyphs))


def text_rows(im):
    """Both polarities; multiscale morphology; coordinates stay in source pixels."""
    gray = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    candidates = []
    # These are glyph-height search scales, not screen coordinates.
    # Extend the range for exceptionally large/small text.
    for ht in (8, 12, 18, 26, 38, 56, 80):
        k = cv2.getStructuringElement(cv2.MORPH_RECT, (ht | 1, ht | 1))
        for op in (cv2.MORPH_BLACKHAT, cv2.MORPH_TOPHAT):
            response = cv2.morphologyEx(gray, op, k)
            _, mask = cv2.threshold(response, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
            # Join glyphs along a row without joining adjacent date rows.
            joined = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                     np.ones((1, max(3, round(ht*.55))), np.uint8))
            n, _, stats, _ = cv2.connectedComponentsWithStats(joined, 8)
            for x, y, w, h, _ in stats[1:n]:
                if not (.5*ht <= h <= 1.8*ht and 4.2 <= w/h <= 10.5):
                    continue
                patch = mask[y:y+h, x:x+w]
                density = np.count_nonzero(patch)/patch.size
                if .12 <= density <= .65 and date_shape(patch):
                    candidates.append({'box': tuple(map(int, (x, y, w, h))),
                                       'glyph': normalized(patch)})
    return candidates


def unique_rows(rows, key=lambda r: 0.):
    out = []
    for row in sorted(rows, key=key, reverse=True):
        if not any(overlap(row['box'], old['box']) > .55 for old in out):
            out.append(row)
    return out


def chains(rows, count):
    """Five aligned, near-equally spaced text rows; no fixed list origin/pitch."""
    rows = sorted(rows, key=lambda r: r['box'][1])
    found = []
    for first, second in itertools.combinations(range(len(rows)), 2):
        a, b = rows[first]['box'], rows[second]['box']
        pitch = b[1]+b[3]/2-a[1]-a[3]/2
        if not 1.2*a[3] <= pitch <= 5*a[3]:
            continue
        chain = [rows[first], rows[second]]
        for j in range(2, count):
            expected = a[1]+a[3]/2+j*pitch
            options = []
            for row in rows[second+1:]:
                x, y, w, h = row['box']
                if (abs(y+h/2-expected) < .16*pitch
                        and abs(x-a[0]) < .8*a[3]
                        and .8 < w/a[2] < 1.25 and .7 < h/a[3] < 1.4):
                    options.append(row)
            if not options:
                break
            chain.append(min(options, key=lambda r: abs(r['box'][1]+r['box'][3]/2-expected)))
        if len(chain) == count:
            # Also validate the second row's alignment and size.
            if abs(b[0]-a[0]) < .8*a[3] and .8 < b[2]/a[2] < 1.25:
                found.append(chain)
    return found


def make_bank(reference, dates):
    groups = chains(unique_rows(text_rows(reference)), len(dates))
    if len(groups) != 1:
        raise RuntimeError(f'Reference has {len(groups)} possible date lists; '
                           'crop to exactly one complete five-date list.')
    return dict(zip(dates, [r['glyph'] for r in groups[0]]))


def similarity(a, b):
    # Cosine scores, not probabilities. Give the distinguishing day suffix weight.
    def cosine(x, y):
        return float(np.sum(x*y)/(np.linalg.norm(x)*np.linalg.norm(y)+1e-8))
    best = 0.
    p = np.pad(a, ((2, 2), (2, 2)))
    for dy in range(5):
        for dx in range(5):
            shifted = p[dy:dy+32, dx:dx+200]
            best = max(best, .30*cosine(shifted, b)
                       + .70*cosine(shifted[:, 150:], b[:, 150:]))
    return best


def recognize(im, bank, threshold=.83, margin=.035):
    accepted = []
    for row in text_rows(im):
        scores = sorted(((similarity(row['glyph'], glyph), date)
                         for date, glyph in bank.items()), reverse=True)
        score, date = scores[0]
        gap = score-scores[1][0]
        if score >= threshold and gap >= margin:
            accepted.append(dict(row, date=date, score=score, margin=gap))
    return unique_rows(accepted, key=lambda r: r['score'])


def find_list(im, bank):
    dates = list(bank)
    groups = [g for g in chains(recognize(im, bank), len(dates))
              if [r['date'] for r in g] == dates]
    if len(groups) != 1:
        raise RuntimeError(f'Expected one validated date list, found {len(groups)}')
    return groups[0]


class ADB:
    def __init__(self, executable, serial):
        self.prefix = [executable, '-s', serial]

    def run(self, *args):
        p = subprocess.run(self.prefix+list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=30, check=True)
        return p.stdout

    def shot(self):
        # Binary pipe avoids PowerShell/terminal PNG transcoding and CRLF changes.
        data = self.run('exec-out', 'screencap', '-p')
        im = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if im is None:
            raise RuntimeError('Invalid ADB PNG')
        return im

    def tap(self, x, y):
        self.run('shell', 'input', 'tap', str(round(x)), str(round(y)))


def select_date(adb, bank, target, output):
    # Start with dropdown OPEN, using the user's existing reliable button matcher.
    # Two independently captured frames must identify the same target row.
    frames, lists = [], []
    for _ in range(2):
        im = adb.shot()
        frames.append(im)
        lists.append(find_list(im, bank))
        time.sleep(.25)
    index = list(bank).index(target)
    first, row = lists[0][index], lists[1][index]
    if frames[0].shape != frames[1].shape or overlap(first['box'], row['box']) < .85:
        raise RuntimeError('List still moving; no tap sent')
    x, y, w, h = row['box']
    write_image(output/'before.png', frames[-1])
    adb.tap(x+w/2, y+h/2)
    # Verify BOTH dropdown closure and a single selected-date field near the list.
    # The field may be above OR below a popup; its pixel location is not fixed.
    min_y = min(r['box'][1] for r in lists[-1])
    max_y = max(r['box'][1]+r['box'][3] for r in lists[-1])
    streak = 0
    for probe in range(12):
        time.sleep(.35)
        after = adb.shot()
        write_image(output/f'after_{probe+1:02d}.png', after)
        if after.shape != frames[-1].shape:
            raise RuntimeError('Display geometry changed; verification aborted')
        recognized = recognize(after, bank)
        still_open = bool(chains(recognized, len(bank)))
        near = []
        for candidate in recognized:
            xx, yy, ww, hh = candidate['box']
            if (abs(xx-x) < 2*w and
                    (0 < min_y-(yy+hh/2) < 4*h or
                     0 < (yy+hh/2)-max_y < 4*h)):
                near.append(candidate)
        valid = not still_open and len(near) == 1 and near[0]['date'] == target
        streak = streak+1 if valid else 0
        if streak >= 2:
            return {'verified': True, 'selected': target,
                    'field_score': near[0]['score'],
                    'field_box': list(near[0]['box']),
                    'tap': [x+w/2, y+h/2]}
    raise RuntimeError('Tap sent, but selected date NOT verified; inspect after.png. '
                       'No blind retry was sent. A different field font needs its own templates.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference', required=True)
    p.add_argument('--dates', default='2026-10-13,2026-10-14,2026-10-15,2026-10-16,2026-10-17')
    p.add_argument('--target', default='2026-10-16')
    p.add_argument('--image', help='Offline: detect only, no ADB or taps')
    p.add_argument('--adb', default='adb')
    p.add_argument('--serial', default='emulator-5556')
    p.add_argument('--out', default='date_debug')
    a = p.parse_args()
    dates = a.dates.split(',')
    if a.target not in dates or len(set(dates)) != len(dates) or len(dates) < 2:
        p.error('Target must be in a list of distinct dates')
    bank = make_bank(read_image(a.reference), dates)
    output = Path(a.out)
    output.mkdir(parents=True, exist_ok=True)
    if a.image:
        im = read_image(a.image)
        group = find_list(im, bank)
        for r in group:
            x, y, w, h = r['box']
            cv2.rectangle(im, (x, y), (x+w, y+h), (0, 255, 0), 1)
        write_image(output/'detected.png', im)
        result = [{k: r[k] for k in ('date', 'box', 'score', 'margin')} for r in group]
    else:
        result = select_date(ADB(a.adb, a.serial), bank, a.target, output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
