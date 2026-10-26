# ADB 取证报告 · dumpsys 超时 / adb 崩溃真因排查

- 日期：2026-10-26
- 排查人：general-purpose-1（只读排查，未修改任何 `bot/` 业务代码）
- 代码根目录：`D:/温州麻将/温州茶（传统麻将）/bot/`
- 标注约定：**【实测】** = 本次亲自跑出来的数据；**【推断】** = 未直接观测、由代码/证据推得；**【未取到】** = 拿不到，不猜。

---

## ① 结论摘要

### 真因是否已确认：**未确认**（但**首要线索已改向**，旧线索基本被证伪）

三条结论，按证据强度排列：

1. **【实测】"15 秒 dumpsys 超时" 在全部留存台账里一次都没有出现过。**
   对 `采集/dev_auto_play/` 下全部 16 个代打会话的 `actions.jsonl`（合计 1454 行）+
   `follow_session.jsonl` 全量检索 `timed out` / `TimeoutExpired`，命中 **0 条**。
   所以这条观测**没有落盘证据**，它要么来自 Tk 界面上一闪而过的状态字符串
   （`assist_window.py:3990` 把 `future.result()` 的异常只写进 `full_auto_status`，
   窗口一关就没了），要么发生在**根本不写台账的冷启动门禁**里（见第 ④ 节第 1 条）。

2. **【实测】台账里真实存在的 adb 失败只有 2 次，两次都是"内存/提交额度耗尽"，不是超时、也不是 adb 抢端口。**

   ```
   2026-10-25T18:45:49.463471+08:00  emulator-5558  tick=87
   gate=gate_foreground  reason="无法确认前台应用: ADB 调用失败: [WinError 1455] 页面文件太小，无法完成操作。"

   2026-10-25T18:45:52.127547+08:00  emulator-5556  tick=87
   gate=gate_foreground  reason="无法确认前台应用: ADB 调用失败: Command '['C:\\leidian\\LDPlayer14\\adb.exe', '-s', 'emulator-5556', 'shell', 'dumpsys', 'window', 'windows']' returned non-zero exit status 3221225794."
   ```
   - `WinError 1455` = `ERROR_COMMITMENT_LIMIT`（页面文件太小）。
   - `3221225794` = **0xC0000142** = `STATUS_DLL_INIT_FAILED`（进程起不来，典型是内存/桌面堆耗尽）。
   - 两台机**相差 3 秒、同一个 tick（87）** 同时炸 → 指向**宿主机级**故障，不是某一台模拟器的问题，
     也不是某一套 adb 的问题。

3. **【实测】代码里对 `3221225773` 的注释是错的，这个错把排查方向带偏了。**
   `bot/adb_runtime.py:8-10` 写的是 “3221225773 = 0xC0000005 访问违例”。
   实测换算：`3221225773 = 0xC000012D = STATUS_COMMITMENT_LIMIT`。
   真访问违例是 `3221225477 = 0xC0000005`。
   → **0xC000012D 与 WinError 1455 是同一件事的两个面：系统提交额度（内存+页面文件）被打满。**
   这与 `bot/mem_guard.py` 头部写的"第二个真实故障：OpenCV OOM"是**同一个真因**，不是两个。

### 被证伪 / 降级的方向（不要再写进结论）

- **"两套 adb 抢 server 互踢"** —— 已被团队用 MD5/SHA256 字节比对推翻，本次复测再次确认：
  本机此刻 `adb.exe` 进程**只有 1 个**（PID 18368，2026-10-26 08:04:17 启动）。
- **"并发 adb 把雷电打假死"** —— 本次压测**未复现**。见第 ③ 节：16 路并发仍全部 rc=0，最大 0.877s。
- **"dumpsys 本身很慢所以 15s 不够"** —— 实测 dumpsys 中位数 0.034s，15s 是 400 倍余量。

---

## ② adb 调用点全表

`timeout` 列标 **粗体** 的是 `15` 秒（即"15 秒超时"这个数字的候选来源）。

### bot/adb_runtime.py（唯一 adb 真源；只读，默认不杀进程）

| 行号 | 命令 | timeout |
|---|---|---|
| 184 | `adb version`（候选探测，逐个候选试） | 3.0 |
| 279 | `tasklist /FI "IMAGENAME eq adb.exe" /FO CSV /NH`（psutil 不可用时的进程枚举） | 5.0 |
| 321 | `adb version` | 5.0 |
| 345 | `adb devices` | 5.0 |
| 406 | `adb devices`（`ensure_single_server` 探测） | 5.0 |
| 428 | `adb kill-server` | 10.0（**仅 `allow_restart=True`**） |
| 432 | `adb start-server` | 10.0（同上） |
| 439 | `adb devices`（重启后复测） | 5.0 |

**【实测】`ensure_single_server()` 在 `bot/` 生产代码中没有任何调用点**，全仓仅
`tests/test_adb_runtime.py`、`tests/test_auto_play_loop.py` 引用。→ 第 ④ 节第 4 问的答案：
**热路径里没有任何 `kill-server` / `start-server`。**

### bot/auto_play_loop.py

| 行号 | 命令 | timeout | 调用频率 |
|---|---|---|---|
| 895-896 | `-s <serial> shell dumpsys window windows` | **15** | 见下 |
| 901-902 | `-s <serial> shell dumpsys activity activities` | **15** | 同上，仅当上一条没匹配到包名 |
| 909-917 | `_run_adb()` 公共执行器（`check=True`，异常→`RuntimeError`） | 默认 20.0 | — |
| 920 | `-s <serial> exec-out screencap -p` | 20.0（默认） | 每 tick 一次 |

调用点：
- `check_gates():850` —— **每次 `run()` 启动一次**（`run()` 在 3631 行第一行就调）。
- `tick():1103` —— **每 tick 一次，无缓存**。interval_s=1.2（`assist_window.py:3908`）。
  → 稳态约 **1 次 dumpsys + 1 次 screencap / 1.65 秒 / 台**，两台机约 1.2 次/秒。

> 对比：`live_capture.LiveCapture` **有** 3 秒前台缓存（`live_capture.py:307-321`），
> `auto_play_loop._adb_foreground` **没有**。这是同一份代码里两套不同口径。

### bot/tap_executor.py

| 行号 | 命令 | timeout |
|---|---|---|
| 2504-2510 | `_adb()` 公共执行器（`check=True`） | 未指定时 = `self.screenshot_timeout` = **30.0** |
| 2514 | `exec-out screencap -p` | 30.0 |
| 2531-2534 | `shell dumpsys activity activities` / `shell dumpsys window windows` | **15** |
| 2561-2562 | `shell input tap x y` | **15** |

`_require_game_foreground()`（2529）的调用点（**每个动作前后各一次，每次最多 2 条 dumpsys**）：
`722/748`（execute_claim）、`875/889`（execute_invitation_cancel）、`982/996`（execute_leave_table）、
`1098/1115`（execute_close_popup）、`1274/1292`（execute_open_table）、`1424/1448`（execute_chi_panel_group）、
`1773/1791/1885`、`1994/2008`（execute_recovery_second_tap）、`2095`。
→ 一次 `execute_claim` 满路径：**最多 4 条 dumpsys + 3 次 screencap + 1 次 input tap**。

### 其它模块

| 文件:行号 | 命令 | timeout |
|---|---|---|
| `live_capture.py:110-114` | `shell dumpsys activity activities` | **15** |
| `live_capture.py:124-126` | `exec-out screencap -p` | 20 |
| `assist_tea.py:614-617` | `shell dumpsys activity activities` | **15** |
| `assist_tea.py:621-624` | `exec-out screencap -p` | 20 |
| `assist_shu.py:245-248` | `shell dumpsys activity activities` | **15** |
| `assist_shu.py:252-253` | `exec-out screencap -p` | 20 |
| `harvest_tea.py:105-114` | `adb()` 通用 | 默认 30 |
| `harvest_tea.py:675` | `shell dumpsys ×2` | 10 |
| `device_connection.py:30-34` | `adb devices -l` | 8 |
| `device_connection.py:43-47` | `exec-out screencap -p` | 14 |
| `launch_multi.py:47-48` | `adb devices -l` | 12 |
| `lobby_follow.py:62` | → `live_capture.read_foreground`（dumpsys） | **15** |
| `lobby_follow.py:63-68` | `shell input tap x y` | 12 |
| `date_picker_cv.py:176-189` | 通用 `run()` / screencap / input tap | 30 |
| `tap_probe.py:37-38` | `exec-out screencap -p` | **无 timeout**（开发脚本） |
| `tap_probe.py:45-46` | `shell input tap` | **无 timeout**（开发脚本） |

### 仓外（**本次新发现，之前没进过排查范围**）

`D:/HanaAgent/pc-server.py:54-92` —— 一个 HTTP（8099）截图/点击服务，`run_adb()` 硬超时 12s 并 kill。
**【实测】当前有 2 个实例在跑**：

```
PID 17664 / 18092   python.exe D:\HanaAgent\pc-server.py --port 8099 --adb D:\leidian\LDPlayer14\adb.exe
```

它用的是**同一个 adb**、**同一批模拟器**，但**完全不受本项目的超时/门禁治理**。
它自己的注释里写着 `原版 screencap 卡死导致 MemoryError 崩溃`
（`pc-server.py:10`）—— 即**本机 screencap 会卡死这件事，是已被另一个工具链记录过的既有现象**。

**结论：全仓没有一条 adb 调用是无超时的（生产路径）。所有 `dumpsys` 都是 10~15s，所有 `screencap` 是 14~30s。**
→ "dumpsys 类调用没有超时保护" 这个假设**不成立**。

---

## ③ 并发与锁分析

### 并发度（代码事实）

单个 `assist_window.py` 进程内有 3 个 `ThreadPoolExecutor`（`assist_window.py:429/430/491`），
每个 `max_workers=1`，都能发 adb：

| 执行器 | 会发 adb 的任务 |
|---|---|
| `executor` | 跟随抓帧（997/3030/3753/3803）、大厅流程（2762）、录屏（922）|
| `auto_executor` | `AutoPlayLoop.run`（3917）、回合过渡（2658）|
| `_check_executor` | 设备绑定体检（1451）、adb 诊断（1496）、身份/模式核验（1906/2031）|

→ **单进程最多 3 条并发 adb 子进程**。
→ 两台机各一个窗口进程 = **最多 6 条**；再加 HanaAgent 的 2 个按需进程 = **最多 8 条**。

### 锁（代码事实）

- **没有任何全局 adb 串行锁。**
- `serial_lease.py` 的 `hold_serial()` 是**按 serial 的跨进程互斥量**，管的是"谁有真实点击权限"，
  **不串行化 adb 只读调用**，也不跨 serial。
- 结论：**各线程各跑各的，无任何串行化。**

### 实测压测（本次亲跑）

第 1 轮 —— 并发梯度（一次性突发）：

| 并发 N | wall | 单次 mean | 单次 max | rc |
|---|---|---|---|---|
| 4 | 0.336s | 0.112s | 0.335s | 全 0 |
| 8 | 0.421s | 0.135s | 0.420s | 全 0 |
| 16 | 0.604s | 0.225s | 0.603s | 全 0 |

第 2 轮 —— 持续负载（4 线程 × 45 秒，模拟两台机每 1.2s 一个 tick）：

| 命令 | n | mean | p50 | p95 | max | rc |
|---|---|---|---|---|---|---|
| `dumpsys window windows` | 108 | 0.062s | 0.034s | 0.166s | **0.877s** | 全 0 |
| `exec-out screencap -p` | 108 | 0.436s | 0.390s | 0.780s | **1.470s** | 全 0 |

**【实测】结论：并发到 16 路、持续 45 秒，没有一次退化，更没有挂起。**
dumpsys 最大值 0.877s 距离 15s 门槛还有 17 倍余量。

> ⚠️ 这两轮压测是在**当前画面（静态桌面/大厅）**下跑的，**不是游戏对局中**。
> 已实测有一个 `assist_window --serial emulator-5556`（PID 27956）在跑，
> 但另一台 5558 无窗口进程。所以"游戏渲染满载下的并发表现"**【未取到】**，
> 不能据此断言"并发永远安全"。

**【实测】超时语义已验证**：用 `adb shell sleep 25` + `subprocess.run(timeout=3)` 实测，
3.025s 就抛 `TimeoutExpired`，不会长期卡住；`check=True` 路径同样是 3.004s 抛异常。
杀掉客户端后设备端**未留残留进程**（`ps -ef` 复查无 `sleep`）。

---

## ④ 最可疑链路排序

### 第 1 名 ——【推断，但证据很硬】宿主机内存/提交额度耗尽 → adb 客户端进程起不来 → dumpsys 失败 → 误判为"前台包确认不了"

证据：
- **【实测】** 两条真实失败记录，错误码分别是 `WinError 1455` 与 `0xC0000142`，
  且两台机 3 秒内同时炸、同一个 tick。这两个码**都不是 adb 的错**，是 Windows 起进程的错。
- **【实测】** 崩溃前 1.2 秒（18:45:50.881）同一会话已先出现
  `capture_failed: OpenCV ... !buf.empty() in function 'cv::imdecode_'`
  —— screencap 返回了**空缓冲**，随后 2 秒 dumpsys 起不来。**顺序完全自洽**：
  先是进程分配不出内存 → 输出为空 → 再往后新进程根本起不来。
- **【实测】** `mem_guard.py` 头部白纸黑字记着"OpenCV OOM"是第二个真实故障。
- **【推断】** 那时 `WZMJ_RSS_LIMIT_MB` 未设置（看门狗 10-26 才接进 `auto_play_loop`），
  所以内存涨到爆也没有任何停手或留证动作 —— 16 个会话里 `resource_guard` 事件数 = **0**（实测）。

当前本机基线【实测】：物理内存 32,692 MB / 已提交 29,706 MB / 提交上限 118,708 MB（25%）；
页面文件 `D:\pagefile.sys` 系统托管（注册表 `PagingFiles = {?:\pagefile.sys}`），当前用量 180 MB。
D 盘余 2032.7 GB，**C 盘只剩 12.4 GB**。

### 第 2 名 ——【推断】`check_gates()` 冷启动失败不留证 → 所以"15 秒超时"查不到源头

代码事实（`auto_play_loop.py`）：
- `check_gates():850` 的 `package = self._foreground()` **没有 try/except**。
  dumpsys 超时/崩溃会直接抛 `RuntimeError`，**绕过** `_gate_blocked()`，
  → `report` 里已经采到的 `adb_inventory` 现场**不会写进任何文件**。
- 就算走到了 `_gate_blocked()`，第 818 行的条件
  `if self._dirs_ready or self.ledger_path.exists():` 在**冷启动时两个都是 False**
  → 明确设计成"冷启动失败什么都不建"，**现场照样一行不落**。
- `run():3631` 的 `self.check_gates()` 在 `while` 之外，异常直接冒给
  `assist_window.py:3917` 的 `auto_executor.submit(...)`，最终只在
  `assist_window.py:3990` 变成一句 `full_auto_status` 字符串。窗口一关 = 证据归零。

→ 这正好解释了团队的描述："单点观测、未复现、未定位到是哪个 shell 命令、哪条调用链"。

> 反证：如果 15s 超时发生在 `tick()` 里，`tick():1104` 会把它转成
> `_stop("gate_foreground", ...)` 并**写进 actions.jsonl**。
> 而全量检索 `gate_foreground` 只有上面那 2 条内存故障、没有超时条目。
> **【推断】那条 15 秒超时极可能发生在冷启动 `check_gates()` 里。**

### 第 3 名 ——【推断】`_adb_foreground` 每 tick 无缓存，把偶发挂起的暴露面放大了 ~20 倍

- `tick():1103` 每 1.65 秒调一次 dumpsys，**无缓存**；
- 对照 `live_capture.LiveCapture` 有 3 秒缓存（`live_capture.py:317`）。
- 一次 12 小时的代打 ≈ 26,000 次 dumpsys。哪怕挂起概率只有万分之一，也会撞上 2~3 次。
- 这**不解释为什么会挂**，但解释了"为什么是它先撞上"。

### 第 4 名 ——【推断，弱】仓外 adb 使用者不受治理

`D:/HanaAgent/pc-server.py`（PID 17664/18092）用同一个 adb 打同一批模拟器，
12 秒硬超时 + kill，且它自己的注释承认本机 screencap 会卡死。
**【未取到】** 崩溃时刻它是否在发命令 —— 它不写本项目台账，无从回溯。
若下次崩溃时要排除它，必须在崩溃瞬间抓它的活跃连接（见第 ⑤ 节命令 6）。

### 已排除

- `kill-server` / `start-server` 落在热路径 —— **【实测】不存在**（`ensure_single_server` 生产零调用）。
- adb 调用无超时保护 —— **【实测】不存在**（生产路径全都有）。
- 并发把雷电打假死 —— **【实测】16 路并发未复现**。

---

## ⑤ 下一步取证命令清单

> 目标：下次崩溃**瞬间**抓到 6 类证据。以下命令全部可直接复制执行。
> 第 1~4 条是"崩溃后立刻跑"，第 5~7 条是"崩溃前一直挂着"。
> 所有命令都是只读的，不杀任何进程、不改任何文件。

### 崩溃后立刻跑（30 秒内，快照越早越值钱）

**命令 1 —— 抓 adb 现场（一条命令出全部结论）**

```bash
cd "D:/温州麻将/温州茶（传统麻将）/bot" && python -c "
import json, adb_runtime
print(json.dumps(adb_runtime.adb_inventory(), ensure_ascii=False, indent=2))
"
```

要看的字段：`servers`（几个 adb.exe，PID 是谁）、`devices`（靶机还在不在）、`chosen`（用的哪个 adb）。

**命令 2 —— adb server 还活着吗 / 有几个 adb.exe**

```powershell
Get-Process -Name adb -ErrorAction SilentlyContinue |
  Select-Object Id,StartTime,WS,Path | Format-Table -AutoSize
```

**命令 3 —— 设备详细的在线状态与 transport（比 `devices` 多 product/model/transport_id）**

```bash
"D:/leidian/LDPlayer14/adb.exe" devices -l
```

**命令 4 —— 单条命令是否还能通（区分"adb 挂了"还是"某条命令挂了"）**

```bash
"D:/leidian/LDPlayer14/adb.exe" -s emulator-5556 shell dumpsys window windows | findstr /i "mCurrentFocus mFocusedApp"
"D:/leidian/LDPlayer14/adb.exe" -s emulator-5558 shell dumpsys window windows | findstr /i "mCurrentFocus mFocusedApp"
```

### 崩溃前一直挂着（这三条才是能定性真因的关键）

**命令 5 —— 内存/提交额度采样（真因第 1 名的直接证据，每 2 秒一行，跑到崩为止）**

```powershell
$out = "D:/温州麻将/SPECS/_commit_watch_$(Get-Date -f yyyyMMdd_HHmmss).csv"
1..100000 | ForEach-Object {
  $os = Get-CimInstance Win32_OperatingSystem
  $c  = Get-Counter '\Process(*)\Private Bytes' -ErrorAction SilentlyContinue
  $top = $c.CounterSamples | Where-Object {$_.InstanceName -match 'python|adb|dnplayer|Ld9Box'} |
         Sort-Object CookedValue -Desc | Select-Object -First 8
  $row = [pscustomobject]@{
    t=(Get-Date -f o)
    commitMB=[math]::Round(($os.TotalVirtualMemorySize-$os.FreeVirtualMemory)/1KB)
    limitMB =[math]::Round($os.TotalVirtualMemorySize/1KB)
    freeRAM =[math]::Round($os.FreePhysicalMemory/1KB)
    top = ($top | ForEach-Object { "$($_.InstanceName)=$( [math]::Round($_.CookedValue/1MB) )MB" }) -join ';'
  }
  $row | Export-Csv -Path $out -Append -NoTypeInformation -Encoding UTF8
  Start-Sleep 2
}
```

**崩溃后立刻读最后 5 行**，看 `commitMB` 是否顶到 `limitMB`。这是 WinError 1455 / 0xC000012D 的判定依据。

**命令 6 —— 谁在那一刻连着 adb server（抓仓外的 HanaAgent 等未治理调用者）**

```powershell
Get-NetTCPConnection -State Established -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -eq 5037 } |
  ForEach-Object {
    $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
    [pscustomobject]@{ LocalPort=5037; Remote="$($_.RemoteAddress):$($_.RemotePort)";
                      PID=$_.OwningProcess; Name=$p.ProcessName;
                      Cmd=(Get-CimInstance Win32_Process -Filter "ProcessId=$($_.OwningProcess)").CommandLine }
  } | Format-List
```

**命令 7 —— 让代打自己把内存读数留下（去掉冷启动盲区）**

在启动代打**之前**设环境变量，让 `auto_play_loop` 的资源看门狗真正落盘
（不设的话它只观测、不写文件，16 个历史会话里 0 条记录就是这个原因）：

```bash
cd "D:/温州麻将/温州茶（传统麻将）/bot" && WZMJ_RSS_LIMIT_MB=4000 python run_auto_play_dev.py --serial emulator-5556
```

> 说明：4000 是**先随便给一个数让它开始落盘**，不是标定值。
> 真正的阈值要按 `python mem_guard.py --watch 600` 的 `peak_guard_mb` 再定。
> 想完全不停手、只留证，就别设这个变量 —— 但那样就**不会有内存读数**，
> 这就是为什么上次崩完什么都没留下。

### 崩溃后回溯台账（验证是不是同一条链路）

```bash
cd "D:/温州麻将/温州茶（传统麻将）/采集/dev_auto_play"
grep -h -o -E "\"(gate|event)\": \"[a-z_]+\", \"reason\": \"[^\"]{0,300}\"" */actions.jsonl |
  grep -E "ADB|adb|前台|截图|点击|timed out|TimeoutExpired" | sort | uniq -c | sort -rn
grep -h -o -E ".{0,160}timed out.{0,200}" */actions.jsonl
```

### 顺手要修的两个"证据黑洞"（不在本次授权内，只登记）

1. `auto_play_loop.py:850` —— `self._foreground()` 应包 try/except，失败时走 `_gate_blocked()`
   把已采到的 `adb_inventory` 落盘（现在会整个绕过）。
2. `auto_play_loop.py:818` —— 冷启动门禁失败时 `_dirs_ready=False` 导致一行不写；
   建议"冷启动门禁失败"单独写一个 `gate_coldstart.jsonl` 到固定目录，不依赖会话目录是否存在。
3. `bot/adb_runtime.py:8-10` —— 注释里 `3221225773 = 0xC0000005 访问违例` 是错的，
   实为 **0xC000012D STATUS_COMMITMENT_LIMIT**。这个错注释会持续把后人带向"adb 崩溃"而不是"内存耗尽"。

---

## 附：本次亲自执行过的命令（可复核）

| # | 命令 | 结果 |
|---|---|---|
| 1 | `python -c "import adb_runtime; adb_inventory()"` | chosen=`D:/leidian/LDPlayer14/adb.exe`，servers=`[18368]`，devices=`['emulator-5556','emulator-5558']` |
| 2 | 串行 dumpsys 基线（每台 ×2 种 ×5 次） | window windows 0.049~0.065s；activity activities 0.032~0.062s；全 rc=0 |
| 3 | 串行 screencap（每台 ×5） | 5556 mean 0.404s / 5558 mean 0.364s |
| 4 | 并发梯度 N=4/8/16 | wall 0.336/0.421/0.604s，全 rc=0 |
| 5 | 持续 4 线程 × 45s | dumpsys n=108 max 0.877s；screencap n=108 max 1.470s；全 rc=0 |
| 6 | `adb shell getprop ro.build.version.release` | 两台均为 Android 14 |
| 7 | 超时语义验证（`sleep 25` + `timeout=3`，两遍） | 3.025s / 3.004s 抛 `TimeoutExpired`；设备端无残留 |
| 8 | `Get-Process -Name adb` | 仅 1 个：PID 18368，启动 2026-10-26 08:04:17 |
| 9 | `Get-CimInstance Win32_Process`（python/pythonw） | 发现 `pc-server.py --adb D:\leidian\...`（PID 17664/18092）；仅 1 个 assist_window（PID 27956，5556） |
| 10 | 全量检索 16 个代打会话台账 | `timed out`/`TimeoutExpired` = **0 条**；adb 失败仅 2 条（18:45:49 / 18:45:52） |
| 11 | NTSTATUS 换算 | 3221225773 = **0xC000012D**；3221225794 = **0xC0000142**；0xC0000005 = 3221225477 |
| 12 | `Get-CimInstance Win32_OperatingSystem` / `Win32_PageFileUsage` | RAM 32,692MB；已提交 29,706MB；上限 118,708MB；页面文件 D:\ 系统托管 |
| 13 | 注册表 `PagingFiles` | `{?:\pagefile.sys}`（系统托管） |
| 14 | `Get-PSDrive` | C: 用 225.8GB / 剩 **12.4GB**；D: 用 761.8GB / 剩 2032.7GB |
| 15 | 会话目录核对 | 崩溃会话 `frames/` 87 个 PNG / 70MB；`live/tea_2p/` 3958 个 / 3.2GB（该目录被跨运行复用） |

**未取到**：
- 游戏对局满载状态下的并发 adb 表现（只有一台窗口在跑，且画面非对局中）。
- 崩溃时刻（2026-10-25 18:45）的 commit/RSS 读数 —— 当时看门狗尚未接线，无记录。
- 崩溃时刻 HanaAgent `pc-server.py` 是否在发 adb 命令 —— 它不写本项目台账，无法回溯。
