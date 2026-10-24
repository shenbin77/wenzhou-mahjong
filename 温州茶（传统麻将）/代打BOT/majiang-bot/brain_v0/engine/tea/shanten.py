# -*- coding: utf-8 -*-
"""温州茶向听数/听牌/胡牌判定。

从 brain_v0/engine/shanten.py（熟项目，14张）移植，适配温州茶17张制。

关键差异：
- 张数：17张胡牌（1将+5面子 或 8对+1单），16张听牌
- 公式：标准型 10 - 2*面子 - 搭子 - (1 if 有将 else 0)；八对 8 - 对子数
- 字牌：东南西北白（61-64, 73），只支持刻/对/单，无顺子
- 财神：动态 caishen_id 参数（非固定55），白板翻财神时由调用方传入

牌ID编码（brain_v0/engine/tea/tiles.py）：
  1-9: 万，21-29: 筒，41-49: 条
  61-64: 东南西北，71: 中，72: 发，73: 白
  默认无中发，32种牌。

注意：tile_vision 用白=67，引擎用白=73。调用方需先转换：
  vision 67 → engine 73；vision 66 → engine 72

副露计数契约（全模块统一，见 logical_slot_count / visible_physical_count）：
- 逻辑牌型视角：每组副露（吃/碰/杠同价）只占一个面子 = 3 张逻辑位。
  杠的四张牌全部已被杠消耗，第 4 张不得再参与顺子、对子或财神分配。
  杠后补牌使暗手张数仍按"每组副露减少 3 张"计算：
  n 组副露时，等待态暗手 16-3n 张、摸牌态暗手 17-3n 张。
- 物理可见视角：吃/碰 3 张、杠 4 张。杠的第 4 张只进入物理可见计数
  （同种≤4张校验、进张剩余张数），不进入逻辑牌型。
"""

from collections import Counter
from functools import lru_cache
from itertools import combinations_with_replacement

# 茶牌定义（内联，避免跨目录导入问题；与 tea/tiles.py 保持一致）
_WAN = tuple(range(1, 10))
_TONG = tuple(range(21, 30))
_TIAO = tuple(range(41, 50))
_NUMERIC = _WAN + _TONG + _TIAO
_WINDS = (61, 62, 63, 64)
_BAI = 73
_ZHONG, _FA = 71, 72
# 默认32种（无中发）；如需含中发，调用方传入 full_honors=True
_TEA_TILES_32 = tuple(sorted(_NUMERIC + _WINDS + (_BAI,)))
_TEA_TILES_34 = tuple(sorted(_NUMERIC + _WINDS + (_ZHONG, _FA, _BAI)))

_SUIT_RANGES = {"m": (1, 9), "p": (21, 29), "s": (41, 50)}


def _is_honor(t):
    return t in _WINDS or t in (_ZHONG, _FA, _BAI)


def _split_by_suit(counts):
    """把 {tile: count} 拆成三门数牌 + 字牌的独立计数 dict。"""
    suits = {"m": {}, "p": {}, "s": {}, "h": {}}
    for t, c in counts.items():
        if c == 0:
            continue
        if 1 <= t <= 9:
            suits["m"][t] = c
        elif 21 <= t <= 29:
            suits["p"][t - 20] = c
        elif 41 <= t <= 49:
            suits["s"][t - 40] = c
        elif _is_honor(t):
            suits["h"][t] = c
    return suits


def _suit_decompositions(counts):
    """单个数牌花色穷举分解（从熟项目 _suit_decomp.py 移植）。

    返回 {(melds, partials): max_pair_used}。
    """
    tiles = sorted(counts)
    results = {}
    used = {t: 0 for t in counts}

    def dfs(idx, melds, partials, pair_used):
        if idx >= len(tiles):
            key = (melds, partials)
            if pair_used > results.get(key, -1):
                results[key] = pair_used
            return
        t = tiles[idx]
        c = counts[t] - used.get(t, 0)
        # 还原为 1-9 位置
        if t <= 9:
            pos = t
        elif t <= 29:
            pos = t - 20
        else:
            pos = t - 40

        if c == 0:
            dfs(idx + 1, melds, partials, pair_used)
            return
        if c >= 3:
            used[t] = used.get(t, 0) + 3
            dfs(idx, melds + 1, partials, pair_used)
            used[t] -= 3
        if pos <= 7 and c >= 1:
            t2, t3 = t + 1, t + 2
            if used.get(t2, 0) < counts.get(t2, 0) and used.get(t3, 0) < counts.get(t3, 0):
                used[t] += 1; used[t2] += 1; used[t3] += 1
                dfs(idx, melds + 1, partials, pair_used)
                used[t] -= 1; used[t2] -= 1; used[t3] -= 1
        if c >= 2 and not pair_used:
            used[t] += 2
            dfs(idx + 1, melds, partials, True)
            used[t] -= 2
        if c >= 2:
            used[t] += 2
            dfs(idx + 1, melds, partials + 1, pair_used)
            used[t] -= 2
        if c >= 1 and pos <= 8:
            t2 = t + 1
            if used.get(t2, 0) < counts.get(t2, 0):
                used[t] += 1; used[t2] += 1
                dfs(idx + 1, melds, partials + 1, pair_used)
                used[t] -= 1; used[t2] -= 1
        if c >= 1 and pos <= 7:
            t3 = t + 2
            if used.get(t3, 0) < counts.get(t3, 0):
                used[t] += 1; used[t3] += 1
                dfs(idx + 1, melds, partials + 1, pair_used)
                used[t] -= 1; used[t3] -= 1
        # 孤张
        dfs(idx + 1, melds, partials, pair_used)

    dfs(0, 0, 0, False)
    return results


def _honor_decompositions(counts):
    """字牌穷举分解：只支持刻/对/单，无顺子。

    返回 {(melds, partials): max_pair_used}。
    """
    tiles = sorted(counts)
    results = {}
    used = {t: 0 for t in counts}

    def dfs(idx, melds, partials, pair_used):
        if idx >= len(tiles):
            key = (melds, partials)
            if pair_used > results.get(key, -1):
                results[key] = pair_used
            return
        t = tiles[idx]
        c = counts[t] - used.get(t, 0)

        if c == 0:
            dfs(idx + 1, melds, partials, pair_used)
            return
        # 刻子
        if c >= 3:
            used[t] = used.get(t, 0) + 3
            dfs(idx, melds + 1, partials, pair_used)
            used[t] -= 3
        # 对子做将
        if c >= 2 and not pair_used:
            used[t] += 2
            dfs(idx + 1, melds, partials, True)
            used[t] -= 2
        # 对子做搭子
        if c >= 2:
            used[t] += 2
            dfs(idx + 1, melds, partials + 1, pair_used)
            used[t] -= 2
        # 孤张
        dfs(idx + 1, melds, partials, pair_used)

    dfs(0, 0, 0, False)
    return results


@lru_cache(maxsize=300000)
def _standard_shanten_plain(tiles_tuple, meld_count=0, meld_logical=0):
    """无财神标准型向听（17张制）。tiles_tuple 为排序后实牌元组（不含财神）。

    meld_count: 已成立面子组数（闭手之外）；meld_logical: 副露的逻辑张数
    （3×组数，杠也算3），用于判断等待态（闭手+副露逻辑账=16张）时把
    负向听钳到 0。
    """
    counts = {}
    for t in tiles_tuple:
        counts[t] = counts.get(t, 0) + 1
    n = len(tiles_tuple)
    suits = _split_by_suit(counts)

    best = 10
    per_suit_opts = []
    for key in ("m", "p", "s"):
        sc = suits[key]
        if not sc:
            continue
        opts = _suit_decompositions(sc)
        entries = [(m, p, pr > 0) for (m, p), pr in opts.items()]
        per_suit_opts.append(entries)
    # 字牌独立分解
    hc = suits["h"]
    if hc:
        opts = _honor_decompositions(hc)
        entries = [(m, p, pr > 0) for (m, p), pr in opts.items()]
        per_suit_opts.append(entries)

    def combine(idx, melds, partials, pair):
        nonlocal best
        if idx == len(per_suit_opts):
            total_melds = melds + meld_count
            rem = 5 - total_melds
            take = min(partials, rem)
            s = 10 - 2 * total_melds - take
            if pair:
                s -= 1
            if n + meld_logical == 16 and s < 0:
                s = 0
            if s < best:
                best = s
            return
        for m, p, pr in per_suit_opts[idx]:
            nm, np_ = melds + m, partials + p
            # 块数（面子+搭子+对子位）≤6 的粗筛（17张：5面子+1将）
            if nm + meld_count <= 5 and np_ + nm + meld_count <= 6:
                combine(idx + 1, nm, np_, pair or pr)

    combine(0, 0, 0, False)
    return best


def _badui_shanten_plain(tiles_tuple):
    """无财神八对向听。17张：8对+1单胡牌。"""
    counts = {}
    for t in tiles_tuple:
        counts[t] = counts.get(t, 0) + 1
    n = len(tiles_tuple)
    pairs = sum(c // 2 for c in counts.values())
    s = 8 - pairs
    if n == 16 and s < 0:
        s = 0
    # 17张时8对即胡（8对=16张+1单=17张）
    if n == 17 and pairs >= 8:
        s = -1
    return s


def _assignments(k, candidates):
    """流式生成财神替代组合。"""
    return combinations_with_replacement(candidates, k)


def _get_tea_tiles(full_honors=False):
    return _TEA_TILES_34 if full_honors else _TEA_TILES_32


def _normalise_melds(melds):
    """归一化副露列表。

    melds: 每组是牌ID列表；3张=吃/碰，4张=杠（含暗杠——暗杠屏幕只显示3个
    牌位，第4张由调用方按"3个连续同label框"信号补全后显式传入，引擎不虚构）。
    返回 (组数, 物理总张数, 归一化组列表)。物理总张数 = visible_physical_count
    口径：吃/碰3张、杠4张；逻辑账（每组3张）由 logical_slot_count 给出。
    melds 为 None（调用方未提供）时返回 (0, 0, None)，走 meld_count 标量接口。
    """
    if melds is None:
        return 0, 0, None
    groups = []
    for meld in melds:
        group = sorted(int(t) for t in meld)
        if len(group) not in (3, 4):
            raise ValueError(f"副露面子必须为3张(吃/碰)或4张(杠): {meld}")
        groups.append(group)
    return len(groups), sum(len(g) for g in groups), groups


def _meld_args(meld_count, melds):
    """统一解析 meld_count / melds 两种传参，供各公共入口复用。

    返回 (组数, 副露物理总张数, 归一化组列表或None)。
    两者同时给出且矛盾时直接拒绝，避免静默用错账目。
    """
    m_count, m_total, m_groups = _normalise_melds(melds)
    if melds is not None:
        if meld_count not in (0, m_count):
            raise ValueError(
                f"meld_count({meld_count}) 与 melds({m_count}组)不一致")
        return m_count, m_total, m_groups
    if not isinstance(meld_count, int) or not 0 <= meld_count <= 5:
        raise ValueError(f"明牌面子数无效: {meld_count}")
    return meld_count, 3 * meld_count, None


#: 每组副露在逻辑牌型中占用的张数（吃/碰/杠同价：杠只占一个面子）
LOGICAL_TILES_PER_MELD = 3


def logical_slot_count(melds=None, meld_count=0):
    """逻辑牌型视角的副露占用张数：3 × 组数。

    杠的四张牌全部已被杠消耗，第 4 张不得再参与顺子、对子或财神分配，
    因此杠与吃、碰同价，只占一个面子（3 张逻辑位）。向听/胡牌/听牌/
    弃牌与张数守恒一律使用这个口径。
    """
    count, _physical, _groups = _meld_args(meld_count, melds)
    return LOGICAL_TILES_PER_MELD * count


def visible_physical_count(melds=None, meld_count=0):
    """物理可见牌张数：吃/碰 3 张、杠 4 张。

    仅用于画面物理计数（同种≤4张校验、进张剩余张数统计等）。
    标量接口 meld_count 无法区分杠，按 3 张/组计；需要精确账目时
    必须传 melds。
    """
    _count, physical, _groups = _meld_args(meld_count, melds)
    return physical


def expected_concealed_count(melds=None, meld_count=0, drawn=False):
    """按契约推算暗手应有张数：等待态 16-3n、摸牌态 17-3n（n=副露组数）。

    杠后补牌已含在内——杠不多占逻辑位，暗手不因杠少一张。
    """
    base = 17 if drawn else 16
    return base - logical_slot_count(melds=melds, meld_count=meld_count)


def validate_tiles(tiles, caishen_id=None, full_honors=False, meld_count=0,
                   meld_total=None, meld_tiles=None, melds=None):
    """校验茶手牌（可带副露）。

    melds 或 meld_count/meld_total/meld_tiles 二选一地描述副露：
    - melds: 各组的牌ID列表（3张=吃/碰，4张=杠，含暗杠）
    - meld_count: 组数（标量接口，每组按一个面子计）
    - meld_total: 副露物理总张数；None 时按各组实际张数（标量接口 3*组数），
      给出时与实际不符直接拒绝
    - meld_tiles: 各组的牌ID（用于"同种≤4张"的跨副露校验）
    副露局面强制逻辑张数守恒：闭手+副露逻辑账(3×组数) ∈ {16(等待),
    17(刚摸牌)}——杠后补牌使暗手不因杠少一张，杠的第4张只进物理账。
    因此"12张+1组杠"这类非法形态在此被拒；meld_count=0 且无副露时
    保持旧的宽松行为（引擎内部小手牌探测调用不受影响）。
    """
    hand = [int(t) for t in tiles]
    valid = set(_get_tea_tiles(full_honors))
    # 财神本身也必须是合法牌
    invalid = sorted(set(hand) - valid)
    if invalid:
        raise ValueError(f"非法牌编码: {invalid}")
    if melds is not None:
        if meld_count or meld_total is not None or meld_tiles is not None:
            raise ValueError("melds 与 meld_count/meld_total/meld_tiles 不可混用")
        meld_count, meld_total, meld_tiles = _meld_args(0, melds)
    meld_groups = [sorted(int(t) for t in group) for group in (meld_tiles or [])]
    if meld_count > 0 or meld_groups:
        if meld_total is not None:
            # meld_total 是物理口径；与各组实际张数不符时拒绝，防静默错账
            physical = (visible_physical_count(melds=meld_groups)
                        if meld_groups
                        else LOGICAL_TILES_PER_MELD * meld_count)
            if int(meld_total) != physical:
                raise ValueError(
                    f"meld_total({meld_total}) 与副露物理张数({physical})不一致")
        # 张数守恒用逻辑账：每组副露(含杠)只占一个面子=3张逻辑位
        logical = LOGICAL_TILES_PER_MELD * meld_count
        total = len(hand) + logical
        if total not in (16, 17):
            raise ValueError(
                f"闭手{len(hand)}张+副露{meld_count}组(逻辑{logical}张)={total}张，"
                "应为16张(等待)或17张(刚摸牌)；杠后补牌不多扣暗手")
    if len(hand) > 17:
        raise ValueError(f"手牌不能超过17张,得到{len(hand)}张")
    counts = Counter(hand)
    for group in meld_groups:
        bad = sorted(set(group) - valid)
        if bad:
            raise ValueError(f"副露含非法牌编码: {bad}")
        counts.update(group)
    over = {t: c for t, c in counts.items() if c > 4}
    if over:
        raise ValueError(f"同一种牌最多4张(含副露): {over}")
    if caishen_id is not None and int(caishen_id) not in valid:
        raise ValueError(f"财神ID非法: {caishen_id}")
    return hand


@lru_cache(maxsize=100000)
def _calc_shanten_cached(real_key, k, use_badui, caishen_id_key, full_honors,
                         meld_count=0, meld_logical=0):
    """real_key: 排序后实牌元组；k: 财神张数。

    caishen_id_key 参与缓存key，防止不同财神局串味。
    meld_logical 是副露逻辑张数（3×组数，杠也只算一个面子）：向听下限由
    "闭手实牌+财神+副露逻辑账" 是否到达 17 张决定。杠的第 4 张已被杠
    消耗、只进物理账，不得计入此处的张数下限，否则暗杠局面会把
    13张等待态错算成已胡（-1）。
    """
    real = list(real_key)

    def eval_merged(merged):
        key = tuple(sorted(merged))
        s = _standard_shanten_plain(key, meld_count, meld_logical)
        if use_badui and meld_count == 0:
            s = min(s, _badui_shanten_plain(key))
        return s

    best = 99
    if real:
        best = eval_merged(real) + k
    if k > 0:
        if k >= 3:
            # 3+财神极罕见，用启发式：每张财神最多降1向听
            # 先算无财神向听，再减k（不低于理论下限）
            base = eval_merged(real)
            # 理论下限：17张时-1，16张时0
            n = len(real) + k + meld_logical
            floor = -1 if n >= 17 else 0
            best = max(floor, base - k)
        else:
            # k=1,2：枚举精简候选集
            candidates = _useful_candidates(real, full_honors)
            floor = 0 if len(real) + k + meld_logical < 17 else -1
            for assign in _assignments(k, candidates):
                s = eval_merged(real + list(assign))
                if s < best:
                    best = s
                if best <= floor:
                    break
    return best


def _useful_candidates(real, full_honors):
    """为财神枚举生成精简候选集，按有用度排序。

    排序原理：与实牌相同的牌 > 邻张 > 字牌。
    好方案先出现，配合 floor 提前 break。
    """
    from collections import Counter
    rc = Counter(real)
    # 优先级1：实牌本身（补刻/补对）
    p1 = list(rc.keys())
    # 优先级2：数牌邻张（组顺子）
    p2 = set()
    for t in real:
        if 1 <= t <= 9:
            for d in (-2, -1, 1, 2):
                nt = t + d
                if 1 <= nt <= 9 and nt not in rc:
                    p2.add(nt)
        elif 21 <= t <= 29:
            for d in (-2, -1, 1, 2):
                nt = t + d
                if 21 <= nt <= 29 and nt not in rc:
                    p2.add(nt)
        elif 41 <= t <= 49:
            for d in (-2, -1, 1, 2):
                nt = t + d
                if 41 <= nt <= 49 and nt not in rc:
                    p2.add(nt)
    # 优先级3：字牌
    p3 = list(_WINDS) + [_BAI]
    if full_honors:
        p3 += [_ZHONG, _FA]
    p3 = [h for h in p3 if h not in rc and h not in p2]
    return tuple(p1 + sorted(p2) + p3)


def calc_shanten(tiles, caishen_id=None, use_badui=True, full_honors=False,
                 meld_count=0, melds=None):
    """向听数。-1=胡牌；等待态下限0。

    tiles: 闭手牌ID列表（茶引擎编码，不含副露里的牌）
    caishen_id: 财神牌ID，None表示无财神
    meld_count: 已完成面子组数（标量接口，每组按一个面子计）
    melds: 副露列表，每组为牌ID列表（3张=吃/碰，4张=杠，含暗杠）；
        给出时以它为准。杠的第 4 张只进物理账（同种≤4张校验、进张
        剩余统计），不进逻辑牌型。

    数学：n 组副露等于已成立 n 个面子（杠同价），闭手只需向 (5−n) 个
    面子 + 1 将收敛；不向闭手塞任何虚拟牌，杠的余牌也不回灌暗手，
    财神计数与重复张校验不受污染。暗手张数按 16/17−3n 守恒。
    八对(8对+1单)形态只在无副露时参与取小——带副露的八对需要"刻子当
    对+单"的拆法且无法从张数核实面子类型，为不虚报进张一律不评估。
    """
    count, _physical, groups = _meld_args(meld_count, melds)
    hand = validate_tiles(tiles, caishen_id, full_honors,
                          meld_count=count, meld_tiles=groups)
    if not hand:
        return 0
    cid = int(caishen_id) if caishen_id is not None else None
    k = hand.count(cid) if cid is not None else 0
    real = tuple(sorted(t for t in hand if t != cid))
    # caishen_id_key: 用 -1 表示无财神，确保缓存隔离
    ckey = cid if cid is not None else -1
    logical = LOGICAL_TILES_PER_MELD * count
    return _calc_shanten_cached(real, k, bool(use_badui), ckey,
                                bool(full_honors), count, logical)


def is_win(tiles, caishen_id=None, use_badui=True, full_honors=False,
           meld_count=0, melds=None):
    return calc_shanten(tiles, caishen_id, use_badui, full_honors,
                        meld_count, melds) == -1


def calc_ting_tiles(tiles, caishen_id=None, use_badui=True, full_honors=False,
                    meld_count=0, melds=None):
    """等待态（闭手+副露逻辑账=16张）的进张集合（含财神）。"""
    count, _physical, groups = _meld_args(meld_count, melds)
    hand = validate_tiles(tiles, caishen_id, full_honors,
                          meld_count=count, meld_tiles=groups)
    logical = LOGICAL_TILES_PER_MELD * count
    if len(hand) + logical != 16:
        raise ValueError(
            f"calc_ting_tiles 需要等待态(闭手{16 - logical}张+副露{count}组)，"
            f"得到闭手{len(hand)}张")
    candidates = _get_tea_tiles(full_honors)
    # 财神本身也可作为进张（摸到财神）
    if caishen_id is not None:
        cid = int(caishen_id)
        if cid not in candidates:
            candidates = tuple(sorted(set(candidates) | {cid}))
    meld_counts = Counter(t for group in (groups or []) for t in group)
    ting = []
    for t in candidates:
        if hand.count(t) + meld_counts[t] >= 4:
            continue
        if is_win(hand + [t], caishen_id, use_badui, full_honors,
                  meld_count=count, melds=groups):
            ting.append(t)
    return ting


def calc_ukeire(tiles, caishen_id=None, visible_counts=None, use_badui=True,
               full_honors=False, meld_count=0, melds=None):
    """返回能降低向听数的进张及其剩余张数。"""
    count, _physical, groups = _meld_args(meld_count, melds)
    hand = validate_tiles(tiles, caishen_id, full_honors,
                          meld_count=count, meld_tiles=groups)
    current = calc_shanten(hand, caishen_id, use_badui, full_honors,
                           meld_count=count, melds=groups)
    # 副露里的牌同样已经在桌上可见，剩余张数按 闭手+副露 统计
    visible = Counter(hand)
    for group in (groups or []):
        visible.update(group)
    if visible_counts:
        for tile, seen in visible_counts.items():
            visible[int(tile)] += max(0, int(seen))
    candidates = _get_tea_tiles(full_honors)
    if caishen_id is not None:
        cid = int(caishen_id)
        if cid not in candidates:
            candidates = tuple(sorted(set(candidates) | {cid}))
    waits = {}
    for tile in candidates:
        remaining = max(0, 4 - visible[tile])
        if remaining and calc_shanten(hand + [tile], caishen_id, use_badui,
                                      full_honors, meld_count=count,
                                      melds=groups) < current:
            waits[tile] = remaining
    return waits


def discard_analysis(tiles, caishen_id=None, played_counts=None, use_badui=True,
                     full_honors=False, meld_count=0, melds=None):
    """刚摸牌态（闭手+副露逻辑账=17张）的弃牌效率明细：先最小向听，再最大化有效进张数。"""
    count, _physical, groups = _meld_args(meld_count, melds)
    hand = validate_tiles(tiles, caishen_id, full_honors,
                          meld_count=count, meld_tiles=groups)
    logical = LOGICAL_TILES_PER_MELD * count
    if len(hand) + logical != 17:
        raise ValueError(
            f"需要刚摸牌态(闭手{17 - logical}张+副露{count}组)，"
            f"得到闭手{len(hand)}张")
    candidates = []
    seen = set()
    for i, t in enumerate(hand):
        if t in seen:
            continue
        seen.add(t)
        rest = hand[:i] + hand[i + 1:]
        sh_after = calc_shanten(rest, caishen_id, use_badui, full_honors,
                                meld_count=count, melds=groups)
        candidates.append((t, rest, sh_after))
    min_shanten = min(s for _, _, s in candidates)
    rows = []
    for t, rest, sh_after in candidates:
        ukeire = (
            calc_ukeire(rest, caishen_id, played_counts, use_badui,
                         full_honors, meld_count=count, melds=groups)
            if sh_after == min_shanten else {}
        )
        total_ukeire = sum(ukeire.values())
        rows.append({
            "tile": t,
            "shanten_after": sh_after,
            "ukeire": ukeire,
            "ukeire_count": total_ukeire,
            "eff_score": -1000 * sh_after + total_ukeire,
        })
    rows.sort(key=lambda r: (r["shanten_after"], -r["ukeire_count"], r["tile"]))
    return rows


def best_discard_by_efficiency(tiles, caishen_id=None, use_badui=True,
                               played_counts=None, full_honors=False,
                               meld_count=0, melds=None):
    """兼容接口：返回 [(score, tile)]，score越大越优。"""
    return [
        (row["eff_score"], row["tile"])
        for row in discard_analysis(tiles, caishen_id, played_counts,
                                    use_badui, full_honors, meld_count, melds)
    ]


# 编码对照表（文档用）
ENCODING_TABLE = """
温州茶32牌编码对照表（默认无中发）
=====================================
牌名      tile_vision ID    茶引擎 ID    说明
----      --------------    ---------    ----
1-9万     1-9               1-9          一致
1-9筒     21-29             21-29        一致
1-9条     41-49             41-49        一致
东        61                61           一致
南        62                62           一致
西        63                63           一致
北        64                64           一致
发        66                72           不一致！需转换
白        67                73           不一致！需转换
中        -                 71           vision无（无中发时不用）

转换函数：
  def vision_to_engine(vid):
      return {66: 72, 67: 73}.get(vid, vid)
"""
