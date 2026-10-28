"""持久化触摸控制通道（云手机实时操控）。

每台设备保持**一条长连接的 adb shell 进程**，触摸指令以行写入 stdin，
避免每个事件都重新走一次「HTTP → adb shell 协商 → 设备端 input(Java) 冷启动」。

## 两种注入模式（按设备自动选择）

| 模式 | 命令 | 适用 | 实测单次耗时 |
|---|---|---|---|
| `motionevent` | `input motionevent DOWN/MOVE/UP` | `input` 快的设备（redroid/x86） | **28ms** |
| `evdev` | 直接写 `/dev/input/eventX` | `input` 慢的设备（RK3399 板卡） | **28ms** |

### 为什么板卡必须走 evdev

`input` 每条命令都要起一次 `app_process`（JVM）。实测（2026-10-15）：

    板卡 SR-C11G-3399JD4 / RK3399 / Android 10：input 空跑 **448~509ms**，motionevent MOVE **445~467ms**
    redroid / x86_64 / Android 12：           motionevent MOVE **28ms**

板卡上每个触摸事件要 450ms，直接导致两个可见故障：

1. **误触发长按**：DOWN 之后第一个 MOVE 要 450ms 才到，而 Android 的长按判定是 500ms
   → 稍微按久一点就被判成长按（用户反馈的"会显示长按状态"）。
2. **完全不跟手**：每个 MOVE 间隔近半秒，拖动只能是跳着走。

evdev 直接写输入设备节点，**不经过 JVM**：一次 MOVE 只要 28ms，与 redroid 持平。

### 为什么用 `printf` 而不是 `sendevent`

`sendevent` 每个事件都要 fork 一次，一条 MOVE（X/Y/SYN 三个事件）要 3 次 fork ≈ **81ms**；
用 shell 的 `printf` 把整条事件序列**一次写完**只要 **28ms**（内核的 evdev_write 会
逐个处理缓冲区里的 `input_event` 结构体）。

⚠️ `struct input_event` 大小随架构变化：64 位（aarch64/x86_64）是 **24 字节**
（tv_sec 8 + tv_usec 8 + type 2 + code 2 + value 4），32 位是 16 字节。
写错长度会被内核**静默忽略**（实测踩过）。时间字段留 0 即可，内核会自己打时间戳。

## 指令映射

  - DOWN / MOVE / UP / CANCEL → 按模式选 motionevent 或 evdev
  - TAP                       → motionevent 模式用 `input tap`；evdev 模式用 DOWN+UP
  - LONG_PRESS                → `input swipe x y x y duration`（按住不动，触发系统长按）
  - SWIPE                     → `input swipe`（单进程内部插值，滑动最顺滑）

MOVE 事件自动**合并（coalesce）**：队列尾部若已有待发 MOVE，直接替换为最新坐标。
网络抖动时排队的也只是最新点，不会越拖越滞后；DOWN/UP 等关键事件不合并。
"""
import collections
import os
import re
import struct
import subprocess
import threading
import time

# 单设备待发指令上限。MOVE 已合并，正常队列极短；
# 超限说明设备侧 shell 卡死（如 input 阻塞），丢弃重同步。
_MAX_QUEUE = 24

# 持久 shell 空闲多久后回收（秒），避免设备删除后残留进程
_IDLE_CLOSE = 120

# 探测结果缓存：serial -> dict（服务重启后重新探测，同一设备只探一次）
_probe_cache: dict = {}
_probe_lock = threading.Lock()

# input 单次耗时超过这个值就认为该设备不适合走 motionevent（板卡 450ms / redroid 28ms）
_SLOW_INPUT_MS = 150

# evdev 事件常量
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT = 0x2F        # 47
ABS_MT_POSITION_X = 0x35  # 53
ABS_MT_POSITION_Y = 0x36  # 54
ABS_MT_TRACKING_ID = 0x39  # 57
BTN_TOUCH = 0x14A         # 330

MODE_MOTIONEVENT = "motionevent"
MODE_EVDEV = "evdev"
MODE_NONE = "none"          # 两种都不可用 → 让客户端回退 REST（至少不会误触发长按）


# ---------------------------------------------------------------------------
# 设备探测
# ---------------------------------------------------------------------------
def _sh(serial: str, cmd: str, timeout: int = 20) -> str:
    """一次性在设备上执行命令并取回输出（仅用于探测）。"""
    try:
        from . import orchestrator
        adb = orchestrator._adb_path()
    except Exception:
        adb = os.environ.get("ADB_PATH") or "adb"
    creationflags = 0x08000000 if os.name == "nt" else 0
    try:
        p = subprocess.run(
            [adb, "-s", serial, "shell", cmd],
            capture_output=True, timeout=timeout, creationflags=creationflags,
        )
        return (p.stdout or b"").decode("utf-8", "replace") + \
               (p.stderr or b"").decode("utf-8", "replace")
    except Exception:
        return ""


def _parse_touch_node(out: str):
    """从 `getevent -pl` 输出里找触摸节点。

    只接受 **MT 协议 B** 的设备（有 ABS_MT_SLOT）—— 协议 A 的事件序列不同，
    宁可不启用也不要乱注入。返回 dict 或 None。
    """
    node = None
    dev: dict = {}
    for line in out.splitlines():
        m = re.search(r"add device \d+:\s*(/dev/input/event\d+)", line)
        if m:
            if node and dev.get("has_slot") and dev.get("has_xy"):
                return {"node": node, **dev}
            node = m.group(1)
            dev = {"has_slot": False, "has_xy": False, "has_btn": False}
            continue
        if not node:
            continue
        if "BTN_TOUCH" in line:
            dev["has_btn"] = True
        for key, name in (("xmin", "ABS_MT_POSITION_X"), ("ymin", "ABS_MT_POSITION_Y")):
            if name in line and "min" in line:
                mm = re.search(r"min (-?\d+), max (-?\d+)", line)
                if mm:
                    if key == "xmin":
                        dev["xmin"], dev["xmax"] = int(mm.group(1)), int(mm.group(2))
                    else:
                        dev["ymin"], dev["ymax"] = int(mm.group(1)), int(mm.group(2))
                    dev["has_xy"] = "xmin" in dev and "ymin" in dev
        if "ABS_MT_SLOT" in line:
            dev["has_slot"] = True
    if node and dev.get("has_slot") and dev.get("has_xy"):
        return {"node": node, **dev}
    return None


def _input_cost_ms(serial: str):
    """测一次 `input motionevent` 的耗时（毫秒）；同时判断这个子命令是否被支持。

    返回 (耗时ms | None, 是否支持)。不支持的设备跑 `input motionevent` 会打印用法，
    据此可以可靠区分（实测板卡支持、无输出；不支持时会打印 "Usage: input ..."）。

    ⚠️ 这个调用本身要起一次 JVM：慢设备上 **466ms**。所以只在**没有 evdev 节点**
    时才需要它 —— 探测总耗时直接决定客户端能不能在超时前拿到 ack。
    """
    out = _sh(serial, "S=$(date +%s%N); input motionevent CANCEL 0 0 2>&1; "
                     "E=$(date +%s%N); echo $(( (E-S)/1000000 ))")
    supported = ("Usage" not in out) and ("Unknown" not in out)
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line), supported
    return None, supported


def _pack_events(events, size: int) -> str:
    """把事件列表编成 printf 用的八进制转义串（`input_event` 结构体序列）。"""
    raw = bytearray()
    for t, c, v in events:
        e = bytearray(size)
        e[size - 8:size - 6] = struct.pack("<H", t & 0xFFFF)
        e[size - 6:size - 4] = struct.pack("<H", c & 0xFFFF)
        e[size - 4:size] = struct.pack("<i", int(v))
        raw += e
    return "".join("\\%03o" % b for b in raw)


def _evdev_writable(serial: str, node: str, ev_size: int) -> bool:
    """写一条"抬手"事件验证节点可写。

    无副作用：本来就没有按下状态，`TRACKING_ID=-1 + BTN_TOUCH=0 + SYN` 只是
    向系统报告"没有触摸"，不会产生点击。
    """
    esc = _pack_events([(EV_ABS, ABS_MT_TRACKING_ID, -1),
                        (EV_KEY, BTN_TOUCH, 0), (EV_SYN, 0, 0)], ev_size)
    out = _sh(serial, "printf '%s' > %s && echo EVDEV_OK" % (esc, node))
    return "EVDEV_OK" in out


def probe_device(serial: str) -> dict:
    """探测设备用哪种注入模式。结果按 serial 缓存。

    选择规则（**按节点优先，而不是先测耗时** —— 探测速度直接影响
    客户端能否在 1.5s 超时前拿到 ack）：

        有可写的 MT-B 触摸节点 → evdev（不经过 JVM，慢设备唯一可用的路）
        否则                  → input motionevent（快设备，如 redroid）
        两者都不可用           → none（让客户端回退 REST，至少不误触发长按）
    """
    with _probe_lock:
        hit = _probe_cache.get(serial)
    if hit:
        return hit

    info = {"mode": MODE_MOTIONEVENT, "why": "默认"}
    try:
        node = _parse_touch_node(_sh(serial, "getevent -pl 2>/dev/null"))
        if node:
            arch = (_sh(serial, "uname -m").strip().splitlines() or [""])[0].strip()
            ev_size = 24 if ("64" in arch or arch in ("aarch64", "arm64-v8a")) else 16
            mm = re.search(r"(\d+)x(\d+)", _sh(serial, "wm size 2>/dev/null") or "")
            if mm and _evdev_writable(serial, node["node"], ev_size):
                info = {
                    "mode": MODE_EVDEV,
                    "why": "有可写的 MT-B 触摸节点，直写 %s（不经过 JVM）" % node["node"],
                    "node": node["node"],
                    "xmin": node.get("xmin", 0), "xmax": node.get("xmax", 0),
                    "ymin": node.get("ymin", 0), "ymax": node.get("ymax", 0),
                    "screen_w": int(mm.group(1)), "screen_h": int(mm.group(2)),
                    "ev_size": ev_size,
                }
            else:
                cost, mv_ok = _input_cost_ms(serial)
                info = ({"mode": MODE_MOTIONEVENT, "why": "节点不可用，退回 motionevent"}
                        if mv_ok else
                        {"mode": MODE_NONE, "why": "节点不可用且 motionevent 不支持"})
        else:
            cost, mv_ok = _input_cost_ms(serial)
            if mv_ok:
                info = {"mode": MODE_MOTIONEVENT, "why": "无 evdev 节点，input 单次 %s ms" % cost}
            else:
                info = {"mode": MODE_NONE, "why": "无 evdev 节点且 motionevent 不支持"}
    except Exception as e:  # noqa: BLE001
        info = {"mode": MODE_MOTIONEVENT, "why": "探测异常，保持默认: %r" % e}

    with _probe_lock:
        _probe_cache[serial] = info
    print("[touch] %s 注入模式=%s（%s）" % (serial, info["mode"], info.get("why")), flush=True)
    return info


# ---------------------------------------------------------------------------
# TouchBridge
# ---------------------------------------------------------------------------
class TouchBridge:
    """单台设备的持久触摸通道（内部由一个工作线程串行写指令）。"""

    def __init__(self, serial: str):
        self.serial = serial
        self._q: collections.deque = collections.deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._alive = True
        self._last_use = time.time()
        self._fail = 0
        self._track_id = 1

        # 探测放到后台线程：不让第一条指令等探测；但 worker 会等探测完成再渲染
        # （渲染方式依赖探测结果，必须先知道模式）
        self._probe_done = threading.Event()
        self._info: dict = {"mode": MODE_MOTIONEVENT, "why": "未探测"}
        threading.Thread(target=self._do_probe, name=f"touch-probe-{serial}",
                         daemon=True).start()

        self._thread = threading.Thread(
            target=self._worker, name=f"touch-{serial}", daemon=True)
        self._thread.start()

    def _do_probe(self):
        try:
            self._info = probe_device(self.serial)
        except Exception:
            pass
        self._probe_done.set()

    def supports_low_latency(self) -> bool:
        """是否值得让客户端走低延迟通道（服务端据此决定要不要回 touch-ack）。

        探测没完成时**等待**（客户端等 ack 有超时，最多等 3 秒）。
        只有两种注入模式都不可用时才返回 False —— 那种情况必须让客户端回退 REST，
        否则会重演「板卡上 motionevent 慢到误触发长按」的问题。
        """
        self._probe_done.wait(timeout=3.0)
        return self._info.get("mode") != MODE_NONE

    # ------------------------------------------------------------------
    # 对外方法（均非阻塞，仅入队）
    # ------------------------------------------------------------------
    def down(self, x, y):
        self._enqueue(("DOWN", int(x), int(y)))

    def move(self, x, y):
        self._enqueue(("MOVE", int(x), int(y)), coalesce=True)

    def up(self, x, y):
        self._enqueue(("UP", int(x), int(y)))

    def cancel(self, x=None, y=None):
        # CANCEL 坐标会被系统忽略，缺省用 0
        self._enqueue(("CANCEL", int(x or 0), int(y or 0)))

    def tap(self, x, y):
        self._enqueue(("TAP", int(x), int(y)))

    def long_press(self, x, y, duration: int = 600):
        duration = max(300, min(int(duration), 3000))
        self._enqueue(("LONG", int(x), int(y), duration))

    def swipe(self, x1, y1, x2, y2, duration: int = 300):
        duration = max(50, min(int(duration), 1500))
        self._enqueue(("SWIPE", int(x1), int(y1), int(x2), int(y2), duration))

    def close(self):
        if not self._alive:
            return
        self._alive = False
        self._wake.set()
        self._close_proc()

    # ------------------------------------------------------------------
    # 队列
    # ------------------------------------------------------------------
    def _enqueue(self, item, coalesce: bool = False):
        with self._lock:
            self._last_use = time.time()
            if coalesce and self._q and self._q[-1][0] == "MOVE":
                # 替换队尾未发出的 MOVE（关键：丢弃过期坐标点）
                self._q[-1] = item
            else:
                if len(self._q) >= _MAX_QUEUE:
                    # 设备侧严重卡死：清空待发指令，后续手势以新的 DOWN 重新开始
                    self._q.clear()
                    self._close_proc_locked()
                self._q.append(item)
            self._wake.set()

    # ------------------------------------------------------------------
    # 工作线程
    # ------------------------------------------------------------------
    def _worker(self):
        while self._alive:
            if not self._wake.wait(timeout=2.0):
                # 空闲超时回收
                with self._lock:
                    idle = time.time() - self._last_use
                    if not self._q and idle > _IDLE_CLOSE:
                        self._close_proc_locked()
                continue
            self._wake.clear()

            item = None
            with self._lock:
                if self._q:
                    item = self._q.popleft()
            if item is None:
                continue

            # 渲染方式取决于注入模式，必须等探测完成
            self._probe_done.wait(timeout=3.0)
            line = self._render(item)
            if line is None:
                continue
            if not self._ensure_proc():
                # 通道拉不起来：退避后重试一次（指令不丢，放回队首）
                with self._lock:
                    self._q.appendleft(item)
                time.sleep(min(1.0 + self._fail * 0.5, 5.0))
                continue
            try:
                assert self._proc is not None and self._proc.stdin is not None
                self._proc.stdin.write(line.encode("utf-8"))
                self._proc.stdin.flush()
                self._fail = 0
            except Exception:
                # 写入失败（连接断开）：关进程，指令回队，下轮重连
                self._close_proc()
                with self._lock:
                    self._q.appendleft(item)
                self._fail += 1
                time.sleep(min(0.3 * self._fail, 2.0))

    # ------------------------------------------------------------------
    # 指令渲染
    # ------------------------------------------------------------------
    def _render(self, item) -> str | None:
        kind = item[0]
        evdev = self._info.get("mode") == MODE_EVDEV

        if kind in ("DOWN", "MOVE", "UP", "CANCEL"):
            _, x, y = item
            if evdev:
                return self._evdev_cmd(kind, x, y)
            return f"input motionevent {kind} {x} {y}\n"
        if kind == "TAP":
            _, x, y = item
            if evdev:
                # DOWN 与 UP 之间留一点时间，避免被当成"零时长"点击
                return (self._evdev_events(self._down_events(x, y)).rstrip("\n")
                        + "; sleep 0.03; "
                        + self._evdev_events(self._up_events()).rstrip("\n") + "\n")
            return f"input tap {x} {y}\n"
        if kind == "LONG":
            _, x, y, duration = item
            return f"input swipe {x} {y} {x} {y} {duration}\n"
        if kind == "SWIPE":
            _, x1, y1, x2, y2, duration = item
            return f"input swipe {x1} {y1} {x2} {y2} {duration}\n"
        return None

    # ---- evdev ----
    def _map(self, x, y):
        """屏幕坐标 → 设备 ABS 坐标。"""
        i = self._info
        sw, sh = max(1, i.get("screen_w") or 1), max(1, i.get("screen_h") or 1)
        xmin, xmax = i.get("xmin", 0), i.get("xmax", 0)
        ymin, ymax = i.get("ymin", 0), i.get("ymax", 0)
        ax = xmin + int(round(x * (xmax - xmin) / sw))
        ay = ymin + int(round(y * (ymax - ymin) / sh))
        ax = max(xmin, min(ax, xmax))
        ay = max(ymin, min(ay, ymax))
        return ax, ay

    def _down_events(self, x, y):
        ax, ay = self._map(x, y)
        self._track_id = (self._track_id % 65534) + 1
        return [
            (EV_ABS, ABS_MT_SLOT, 0),
            (EV_ABS, ABS_MT_TRACKING_ID, self._track_id),
            (EV_KEY, BTN_TOUCH, 1),
            (EV_ABS, ABS_X, ax), (EV_ABS, ABS_Y, ay),
            (EV_ABS, ABS_MT_POSITION_X, ax), (EV_ABS, ABS_MT_POSITION_Y, ay),
            (EV_SYN, 0, 0),
        ]

    def _move_events(self, x, y):
        ax, ay = self._map(x, y)
        return [
            (EV_ABS, ABS_X, ax), (EV_ABS, ABS_Y, ay),
            (EV_ABS, ABS_MT_POSITION_X, ax), (EV_ABS, ABS_MT_POSITION_Y, ay),
            (EV_SYN, 0, 0),
        ]

    def _up_events(self):
        return [(EV_ABS, ABS_MT_TRACKING_ID, -1), (EV_KEY, BTN_TOUCH, 0), (EV_SYN, 0, 0)]

    def _evdev_events(self, events) -> str:
        """把事件列表编成 `printf '\\ooo...' > /dev/input/eventX` 一条命令。

        关键：一次 printf 写完**整条序列**，内核的 evdev_write 会逐个处理缓冲区里的
        input_event —— 一条 MOVE 只要 1 次 fork（实测 28ms），比 sendevent 三次 fork（81ms）快得多。
        时间字段留 0：内核注入时会自己打时间戳。
        """
        size = int(self._info.get("ev_size") or 24)
        return "printf '%s' > %s\n" % (_pack_events(events, size), self._info["node"])

    def _evdev_cmd(self, kind, x, y) -> str:
        if kind == "DOWN":
            return self._evdev_events(self._down_events(x, y))
        if kind == "MOVE":
            return self._evdev_events(self._move_events(x, y))
        # UP / CANCEL 都是抬手
        return self._evdev_events(self._up_events())

    # ------------------------------------------------------------------
    # 持久 shell 进程管理
    # ------------------------------------------------------------------
    def _ensure_proc(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._proc = None
        try:
            adb = None
            try:
                # 延迟导入，避免 touch ↔ orchestrator 循环导入
                from . import orchestrator
                if not orchestrator._ensure_adb_connected(self.serial):
                    self._fail += 1
                    return False
                adb = orchestrator._adb_path()
            except ImportError:
                # 脱离包运行（诊断脚本）时退化到 PATH 里的 adb，不要因此完全不可用
                adb = os.environ.get("ADB_PATH") or "adb"
            creationflags = 0x08000000 if os.name == "nt" else 0
            self._proc = subprocess.Popen(
                [adb, "-s", self.serial, "shell"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                bufsize=0,
            )
            self._fail = 0
            return True
        except Exception:
            self._proc = None
            self._fail += 1
            return False

    def _close_proc_locked(self):
        """调用方已持锁时用。"""
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    def _close_proc(self):
        with self._lock:
            self._close_proc_locked()


# ---------------------------------------------------------------------------
# 注册表：serial -> TouchBridge（全局复用，同一设备只有一条通道）
# ---------------------------------------------------------------------------
_bridges: dict[str, TouchBridge] = {}
_bridges_lock = threading.Lock()


def get_bridge(serial: str) -> TouchBridge:
    with _bridges_lock:
        b = _bridges.get(serial)
        if b is None or not b._alive:
            b = TouchBridge(serial)
            _bridges[serial] = b
        return b


def close_bridge(serial: str):
    with _bridges_lock:
        b = _bridges.pop(serial, None)
    if b is not None:
        b.close()
