"""把 _accept_pip_eval.json 整理成交付表格（命令行直接打印）。"""
import json
import sys
from pathlib import Path

BOT = Path(__file__).resolve().parent
sys.path.insert(0, str(BOT))
from tile_vision import TILE_IDS  # noqa: E402


def main():
    pattern = sys.argv[1] if len(sys.argv) > 1 else ''
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    data = json.loads((BOT / '_accept_pip_eval.json').read_text(encoding='utf-8'))
    rows = data['frames']
    total = len(rows)
    fixed = [r for r in rows if r['after_accepted'] > r['before_accepted']]
    worse = [r for r in rows if r['after_accepted'] < r['before_accepted']]
    became_complete = [r for r in rows if r['after_complete'] and not r['before_complete']]
    lost_complete = [r for r in rows if r['before_complete'] and not r['after_complete']]
    ambiguous = [r for r in rows if any(x['reason'] == 'ambiguous'
                                        for x in r['before_rejections'])]
    still = [r for r in rows if r['after_rejections']]
    pip_rows = [r for r in rows if r['after_pip']]
    print(f'frames={total}  fixed={len(fixed)}  worse={len(worse)}  '
          f'complete+={len(became_complete)}  complete-={len(lost_complete)}')
    print(f'frames_with_ambiguous_dot_before={len(ambiguous)}  '
          f'frames_settled_by_pip={len(pip_rows)}  still_rejecting={len(still)}')
    if worse:
        print('WORSE:')
        for r in worse[:20]:
            print('  ', r['frame'], r['before_accepted'], '->', r['after_accepted'],
                  r['after_rejections'])
    if lost_complete:
        print('LOST COMPLETE:')
        for r in lost_complete[:20]:
            print('  ', r['frame'])
    # "非歧义路径逐位一致"的机器校验：除被点数判据新增的那几张牌之外，
    # 每一帧的牌集必须与改动前完全相同。
    broken = []
    for r in rows:
        expected = sorted(r['before_tiles'] +
                          [TILE_IDS[p['label']] for p in r['after_pip']])
        if expected != sorted(r['after_tiles']):
            broken.append(r['frame'])
    print(f'frames_with_unexpected_tile_change={len(broken)}')
    for name in broken[:10]:
        print('  ', name)

    print('\n-- settled rows --')
    for r in pip_rows[:limit]:
        before = r['before_rejections'][0] if r['before_rejections'] else {}
        pip = r['after_pip'][0]
        reading = pip['pip'] or {}
        print(f"{Path(r['frame']).name}")
        print(f"    before: accepted={r['before_accepted']} "
              f"top1={before.get('label')} runner={before.get('runner_up')} "
              f"score={before.get('score')} margin={before.get('margin')} "
              f"reason={before.get('reason')}")
        print(f"    after : accepted={r['after_accepted']} complete={r['after_complete']} "
              f"label={pip['label']} reason={pip['reason']} count={reading.get('count')} "
              f"layout={reading.get('layout_ok')} red={reading.get('red_count')}")
    print('\n-- still rejecting (first rows) --')
    for r in still[:limit]:
        reasons = {x['reason'] for x in r['after_rejections']}
        print(f"  {Path(r['frame']).name} accepted={r['before_accepted']}"
              f"->{r['after_accepted']} reasons={sorted(reasons)}")


if __name__ == '__main__':
    main()
