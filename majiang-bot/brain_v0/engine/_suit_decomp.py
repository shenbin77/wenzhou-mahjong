# -*- coding: utf-8 -*-
"""单花色穷举分解（从 shanten.py 抽出，仅为拆分文件、降低 shanten.py 体积）。

目前唯一调用方是 engine/shanten.py（_standard_shanten_plain）；不存在所谓 "v2"
引擎，也没有其他消费者。若将来新增其它向听实现，可在此复用本分解器。

返回 {(melds, partials): max_pair_used}, pair_used ∈ {0,1}。
melds=面子(刻/顺)数, partials=搭子(对/两面/卡张)数。
"""

def suit_decompositions(counts):
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
        base = t if t <= 9 else (t - 20 if t <= 29 else t - 40)
        pos = base

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
