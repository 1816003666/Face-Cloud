"""WebSocket 实时视频流端点。

支持两种格式：
  - H.264 裸流（默认）：screenrecord 直出，前端用 WebCodecs/Broadway 解码
  - fMP4（format=fmp4）：screenrecord → ffmpeg 转封装 → 前端 MSE 播放
"""
import json
import os
import select
import subprocess
import threading
import time
from flask import request
from flask_sock import Sock
from .extensions import db
from .models import Device
from . import orchestrator

sock = Sock()

# 活跃流记录：device_id -> {process, thread, clients}
_active_streams = {}
_stream_lock = threading.Lock()

# 每台设备的 SPS/PPS 缓存（serial -> 原始 NAL 字节，含 start code）。
# screenrecord 只在流会话开头输出一次 SPS/PPS，中途接入的 WebSocket 客户端拿不到，
# 前端 Broadway 解码器缺少 SPS/PPS 会导致参考帧管理错误 → 画面底部条带/拖影。
_sps_pps_cache: dict[str, bytes] = {}
_sps_pps_lock = threading.Lock()

# 每台设备最近一个完整 GOP 的原始 chunk 缓存（serial -> list[bytes]）。
# screenrecord 的关键帧（IDR）间隔约 10 秒，中途接入的客户端若等 IDR 会黑屏；
# 新客户端加入时先重放完整 GOP（含 IDR），可秒开且不花屏。
_gop_cache: dict[str, list] = {}
_gop_lock = threading.Lock()
_MAX_GOP = 8 * 1024 * 1024  # 单设备 GOP 缓冲上限（约 10s @4Mbps ≈ 5MB）


def _has_idr(chunk: bytes) -> bool:
    """检测 chunk 是否包含 IDR（NAL type 5）。

    必须用 type & 0x1F == 5 判断，不能只匹配 0x65：
    不同设备/ROM 的 IDR nal_ref_idc 可能为 1/2/3（0x25/0x45/0x65），
    只匹配 0x65 会漏掉 0x25 等 → GOP 缓存无关键帧 → 新客户端黑屏。
    """
    i = 0
    n = len(chunk)
    while True:
        i = chunk.find(b"\x00\x00\x00\x01", i)
        if i < 0 or i + 5 > n:
            return False
        if chunk[i + 4] & 0x1F == 5:
            return True
        i += 4


def _normalize_sc(buf: bytes):
    """将 3 字节 start code (00 00 01) 规范化为 4 字节 (00 00 00 01)。

    Android screenrecord 的 H.264 流里 SPS/PPS/IDR 等 NAL 边界混用 3/4 字节 start code，
    前端 WebCodecs 解析器只认 4 字节 → 3 字节边界的 NAL（尤其 IDR）会被吞掉 → 黑屏。
    返回 (规范化数据, 跨块残余字节)，残余用于处理 start code 横跨两个 chunk 的情况。
    """
    out = bytearray()
    i = 0
    n = len(buf)
    while i < n:
        if (i + 3 <= n and buf[i] == 0 and buf[i + 1] == 0 and buf[i + 2] == 1
                and (i == 0 or buf[i - 1] != 0)):
            out += b"\x00\x00\x00\x01"
            i += 3
        else:
            out.append(buf[i])
            i += 1
    # 末尾最多 2 字节可能是"跨块 start code"的前缀（00 / 00 00）。
    # ⚠️ 这几字节在上面循环里**已经写进 out 了**，必须先撤回再作为残余返回；
    # 否则调用方 _normalize_sc(sc_tail + chunk) 会把它再插一次，
    # 每个 chunk 边界多 2 字节乱码 → 画面一动就花屏（静态时 chunk 少，偶尔插一次能恢复）。
    keep = 0
    if n and buf[-1] == 0:
        keep = 2 if (n >= 2 and buf[-2] == 0) else 1
    if keep:
        del out[len(out) - keep:]
    return bytes(out), buf[n - keep:]


def _find_sps_pps(buf: bytes):
    """从 Annex-B H.264 流字节中提取 SPS(type7)/PPS(type8) NAL（含 start code）。

    返回 (sps_bytes, pps_bytes)，找不到对应类型时为空 bytes。
    """
    sps = b""
    pps = b""
    idx = 0
    while True:
        sc = buf.find(b"\x00\x00\x00\x01", idx)
        if sc < 0:
            break
        sc2 = buf.find(b"\x00\x00\x00\x01", sc + 4)
        if sc2 < 0:
            nal = buf[sc:]
        else:
            nal = buf[sc:sc2]
        if len(nal) >= 5:
            t = nal[4] & 0x1F
            if t == 7 and not sps:
                sps = nal
            elif t == 8 and not pps:
                pps = nal
        idx = sc + 4
        if sps and pps:
            break
    return sps, pps


def _ffmpeg_path() -> str:
    """获取 ffmpeg 可执行文件路径。"""
    # 优先使用项目自带的 bin/ffmpeg.exe
    local = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin", "ffmpeg.exe")
    if os.path.exists(local):
        return local
    # 回退到系统 PATH
    return "ffmpeg"


def _get_serial(device_id: int) -> str | None:
    """根据设备 ID 获取 ADB serial。"""
    d = db.session.get(Device, device_id)
    if d is None or d.backend != "redroid":
        return None
    return d.serial


def _kill_device_screenrecord(serial: str) -> int:
    """杀掉设备端残留的 screenrecord 进程，返回被杀数量。

    screenrecord 同一时刻只允许**一个**实例。上一次流异常退出（进程被强杀、网络中断、
    adb 传输中断）时，设备端进程往往不会跟着退出，会一直占着采集会话；之后每次启动
    新实例都会立即空转退出（产出 0 字节），GOP 缓存停在残羹 → 前端**持续黑屏**。
    所以每次启动新实例前先强制清理，形成自愈：哪怕上次异常退出，下次连接也能恢复。
    只杀 shell 用户自己的 screenrecord，避免误伤其他进程。

    ⚠️ 目前**默认不启用**（下方两处调用已注释掉）：每次开流前会多一次 adb 往返，
    且部分设备 `ps -A | grep` 的计数与真实进程不一致，实测清理结果不稳定。
    需要启用时，把 `_start_screenrecord` 开头和 `_stream_worker` 收尾处的调用放开即可。
    """
    # 计数与清理都交给设备端 shell 完成：
    # 用 Python 解析 `ps -A` 的输出格式不稳（不同 ROM 列不同，实测会漏判），
    # 而在设备端用 grep/wc/pkill 则始终与设备自身的 ps 语义一致。
    def _count():
        try:
            rc, out, _ = orchestrator._run_adb(
                ["-s", serial, "shell", "ps -A | grep screenrecord | grep -v grep | wc -l"], timeout=8)
            if rc != 0 or not out:
                return 0
            return int((out or b"").decode("utf-8", errors="ignore").strip().split()[0])
        except Exception:  # noqa: BLE001
            return 0

    try:
        before = _count()
        if not before:
            return 0
        orchestrator._run_adb(["-s", serial, "shell", "pkill -f screenrecord"], timeout=8)
        after = _count()
        killed = max(0, before - after)
    except Exception:  # noqa: BLE001
        return 0
    if killed:
        print(f"[stream] {serial} 清理残留 screenrecord {killed} 个", flush=True)
    return killed


def _parse_size(raw):
    """解析 ?size=WxH。客户端按设备分辨率等比例算好后传入，用于降低软解压力。

    做严格校验，非法值一律忽略（返回 None 即用默认 STREAM_FRAME_SIZE）：
      - 必须是 正整数 x 正整数
      - 宽高必须是偶数：H.264 的宏块要求，奇数会让编码器直接失败
      - 240 ~ 2160 之间
    注意：**帧尺寸 != 设备分辨率**，触控坐标仍按设备分辨率下发，两者独立。
    """
    if not raw:
        return None
    try:
        w_s, h_s = str(raw).lower().split("x", 1)
        w, h = int(w_s), int(h_s)
    except Exception:
        return None
    if w < 240 or h < 240 or w > 2160 or h > 2160:
        return None
    return (w - w % 2, h - h % 2)


def _replay_to_pending(serial, clients):
    """把缓存的 SPS/PPS + 最近一个完整 GOP 立刻补发给新加入的客户端。

    背景（2026-10-08 修复黑屏）：redroid 画面静止时，编码器可以几十秒不产生
    任何新 chunk（实测 25 秒只有 1 个 IDR + 1 个 slice）。原实现在读取到新
    chunk 时才会给新客户端补发 GOP，
    于是设备一静止、新挂上来的客户端就永远等不到关键帧 → 一直黑屏，
    刷新页面也没用（旧的 worker 还活着，条目还在）。
    返回 (是否还有没服务到的新客户端, 是否已补发)。
    """
    waiting = [w for w in list(clients) if not getattr(w, "_gop_ok", False)]
    if not waiting:
        return (False, False)
    with _gop_lock:
        gop = _gop_cache.get(serial)
        gop = list(gop) if gop else []
    with _sps_pps_lock:
        prefix = _sps_pps_cache.get(serial, b"")
    if not gop:
        return (True, False)          # 有新客户端但没有可用 GOP → 需要重启会话
    payload = (prefix or b"") + b"".join(gop)
    for ws in waiting:
        try:
            ws.send(payload)
            ws._gop_ok = True
        except Exception:
            pass
    print("[stream] %s 静止期补发关键帧 %dB -> %d 个客户端"
          % (serial, len(payload), len(waiting)), flush=True)
    return (False, True)


def _flush_pending_fmp4(clients, init):
    """fmp4 通道：把 init 段(ftyp+moov)补发给新客户端。"""
    if not init:
        return
    for ws in list(clients):
        if getattr(ws, "_gop_ok", False):
            continue
        try:
            ws.send(init)
            ws._gop_ok = True
        except Exception:
            pass


def _start_screenrecord(serial: str, bitrate: int = 4000000, size=None) -> subprocess.Popen:
    """启动 screenrecord H.264 直出进程。

    分辨率按 orchestrator.STREAM_FRAME_SIZE 缩放（默认 720x1280）。
    注意：缩放后**帧尺寸 ≠ 设备分辨率**，前端 letterbox 与触摸坐标必须分开用，
    否则点击会整体偏移（1080→720 就是 1.5 倍）。
    """
    # 自愈暂不启用，见 _kill_device_screenrecord 的说明
    # _kill_device_screenrecord(serial)
    adb = orchestrator._adb_path()
    cmd = [
        adb, "-s", serial, "exec-out",
        "screenrecord",
        "--bit-rate", str(bitrate),
        "--output-format", "h264",
    ]
    if not size:
        size = getattr(orchestrator, "STREAM_FRAME_SIZE", None)
    if size:
        cmd += ["--size", f"{size[0]}x{size[1]}"]
    cmd += ["-"]
    creationflags = 0x08000000 if os.name == "nt" else 0
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        bufsize=65536,
    )


def _start_fmp4_pipeline(serial: str, bitrate: int = 4000000, size=None):
    """启动 screenrecord + ffmpeg 转封装为 fMP4 的管道。

    返回 (screenrecord_proc, ffmpeg_proc)，读取 ffmpeg_proc.stdout 获取 fMP4 数据。
    """
    creationflags = 0x08000000 if os.name == "nt" else 0
    adb = orchestrator._adb_path()

    # 1. screenrecord 输出 H.264 裸流（无缓冲，尽快输出）
    sr_cmd = [
        adb, "-s", serial, "exec-out",
        "screenrecord",
        "--bit-rate", str(bitrate),
    ]
    # 修复（2026-10-08）：原实现漏了 --size，按设备原生分辨率录制（如 1080x1920@60fps）：
    #   1) 数据量是 720p 的 3 倍以上，WS 推送更容易阻塞/丢帧；
    #   2) 部分设备的原生码流 ffmpeg 4.1.4 解析不出帧，只写出 24 字节 ftyp（实测设备 272）。
    # 与 h264 通道（_start_screenrecord）保持一致，统一按 720p 缩放。
    # 与 h264 通道（_start_screenrecord）保持一致：按 orchestrator.STREAM_FRAME_SIZE
    # 缩放（默认 720x1280），否则按设备原生分辨率录制
    if not size:
        size = getattr(orchestrator, "STREAM_FRAME_SIZE", None)
    if size:
        sr_cmd += ["--size", f"{size[0]}x{size[1]}"]
    sr_cmd += [
        "--output-format", "h264",
        "-",
    ]
    sr_proc = subprocess.Popen(
        sr_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        bufsize=0,
    )

    # 2. ffmpeg 从 stdin 读取 H.264，转封装为 fMP4 输出到 stdout
    ff_cmd = [
        _ffmpeg_path(),
        # 修复（2026-10-08）：原为 "-fflags", "+genpts+nobuffer" 与 "-flags", "low_delay"。
        # 本机 ffmpeg 4.1.4-1firefly2 上这两个标志导致转封装处理 0 帧、只写几百字节
        # 且退出码为 0（静默失败）→ fmp4 通道完全无数据。去掉后实测正常产出。
        "-fflags", "+genpts",
        "-i", "-",
        "-c:v", "copy",
        "-f", "mp4",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-flush_packets", "1",
        "-",
    ]
    ff_proc = subprocess.Popen(
        ff_cmd,
        stdin=sr_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        bufsize=65536,
    )

    return sr_proc, ff_proc


def _start_source(serial: str, bitrate: int, size=None):
    """选择视频源：优先 scrcpy server（GOP 可控），失败一律回落 screenrecord。

    为什么要换源见 scripts/patch_live_scrcpy_source.py 的说明 —— 一句话：
    screenrecord 没有 GOP 参数（IDR 间隔约 10s），而前端丢帧后要等下一个 IDR，
    两者相乘会把一次普通卡顿放大成最长约 10 秒的冻结；
    scrcpy 可以把 iFrameInterval 设成 0（全关键帧），彻底消除这个等待。

    返回对象只需具备 subprocess.Popen 的鸭子接口
    （stdout.fileno() / poll() / kill() / wait() / pid），下游逻辑完全不用改。
    """
    if os.environ.get("STREAM_SOURCE", "scrcpy").lower() == "screenrecord":
        return _start_screenrecord(serial, bitrate, size)
    try:
        from . import scrcpy_source  # 延迟导入：这个模块出问题也不该影响整体
        src = scrcpy_source.start_scrcpy_source(serial, bitrate=bitrate, size=size)
        print("[stream] %s 视频源=scrcpy iFrameInterval=%s"
              % (serial, scrcpy_source.I_FRAME_INTERVAL), flush=True)
        return src
    except Exception as e:
        print("[stream] %s scrcpy 源不可用，回落 screenrecord: %r" % (serial, e), flush=True)
        return _start_screenrecord(serial, bitrate, size)


def _stream_worker(serial: str, bitrate: int, clients: list, fmt: str = "h264", size=None):
    """后台线程：持续启动视频流并将数据广播给所有客户端。

    screenrecord 单次最长 3 分钟，到期后自动重启实现无限流。
    """
    while clients:
        sr_proc = None
        ff_proc = None
        proc = None
        try:
            if fmt == "fmp4":
                sr_proc, ff_proc = _start_fmp4_pipeline(serial, bitrate, size)
                out_stream = ff_proc.stdout
            else:
                proc = _start_source(serial, bitrate, size)
                out_stream = proc.stdout
            print(f"[stream] {serial} fmt={fmt} size={size} screenrecord 已启动 pid={proc.pid if proc else sr_proc.pid}", flush=True)

            # 读取数据，分块发送（使用 read1 避免阻塞）
            pending = b""
            fmp4_init = b""        # fmp4 init 段(ftyp+moov)，供中途接入的客户端补发
            fmp4_init_done = False
            sc_tail = b""  # 跨块 start code 残余
            idle_rounds = 0   # 连续「读不到数据」的轮数，用于判断设备是否静止
            while clients:
                # 带超时读取：设备静止时编码器不产帧，若在这里无限阻塞，
                # 新加入的客户端就永远收不到缓存的 GOP → 黑屏（见 _replay_to_pending）
                try:
                    ready, _, _ = select.select([out_stream], [], [], 1.0)
                except Exception:
                    ready = [out_stream]
                if not ready:
                    if fmt == "fmp4":
                        if ff_proc.poll() is not None:
                            break
                        _flush_pending_fmp4(clients, fmp4_init)
                    else:
                        if proc.poll() is not None:
                            break
                        need_restart, _served = _replay_to_pending(serial, clients)
                        if need_restart:
                            idle_rounds += 1
                            if idle_rounds >= 3:
                                break
                        else:
                            idle_rounds = 0
                    time.sleep(0.05)
                    continue
                try:
                    chunk = os.read(out_stream.fileno(), 65536)
                except Exception:
                    chunk = None
                if not chunk:
                    # 读到 EOF：检查进程是否还活着，否则结束本会话
                    time.sleep(0.01)
                    if fmt == "fmp4":
                        if ff_proc.poll() is not None:
                            break
                    else:
                        if proc.poll() is not None:
                            break
                    continue
                idle_rounds = 0
                # 统一 start code 为 4 字节（仅 h264 裸流通道；fmp4 由 ffmpeg 封装不受影响）
                if fmt == "h264":
                    chunk, sc_tail = _normalize_sc(sc_tail + chunk)
                # 提取并缓存 SPS/PPS（仅 h264 通道需要）
                if fmt == "h264":
                    pending = (pending + chunk)[-262144:]
                    sps, pps = _find_sps_pps(pending)
                    if sps and pps:
                        with _sps_pps_lock:
                            _sps_pps_cache[serial] = sps + pps
                    with _sps_pps_lock:
                        prefix = _sps_pps_cache.get(serial, b"")
                    # GOP 缓冲：含 IDR 的 chunk 开启新缓冲，其余追加（供新客户端重放秒开）
                    has_idr = _has_idr(chunk)
                    with _gop_lock:
                        g = _gop_cache.get(serial)
                        if has_idr:
                            _gop_cache[serial] = [chunk]
                        else:
                            if g is None:
                                g = []
                                _gop_cache[serial] = g
                            g.append(chunk)
                            gop_bytes = sum(len(c) for c in g)
                            # 弹出旧块时保留第一块（含 IDR 的关键帧），
                            # 否则 IDR 每 ~10s 一次、8MB 缓冲会被 P 帧顶掉，
                            # 中途接入的新客户端将永远等不到关键帧 → 黑屏
                            while gop_bytes > _MAX_GOP and len(g) > 1:
                                gop_bytes -= len(g.pop(0))
                else:
                    prefix = b""
                    # ---- fmp4 专用转发路径 ----
                    # 不走 h264 那套 GOP / SPS-PPS 逻辑：原实现里 has_idr 只在 h264 分支赋值，
                    # fmp4 分支引用它会抛 NameError，被下面的 except 静默吞掉
                    # → 客户端被标记 dead 且收不到任何数据（表现就是 fmp4 通道 0 字节）。
                    # MSE 的要求：新客户端先拿到 init 段(ftyp+moov)，之后每个分片顺序送达即可。
                    chunk_in_init = not fmp4_init_done
                    if chunk_in_init:
                        fmp4_init += chunk
                        if b"moov" in fmp4_init:
                            fmp4_init_done = True
                    _fdead = []
                    for _w in list(clients):
                        try:
                            if not getattr(_w, "_gop_ok", False):
                                if not chunk_in_init and fmp4_init:
                                    _w.send(fmp4_init)
                                _w._gop_ok = True
                            _w.send(chunk)
                        except Exception:
                            _fdead.append(_w)
                    for _w in _fdead:
                        if _w in clients:
                            clients.remove(_w)
                    continue
                # 广播给所有客户端（每个 chunk 前拼上 SPS/PPS，保证解码器始终有上下文）
                # 新客户端（_gop_ok 未标记）先补发最近完整 GOP，保证秒开且不花屏；
                # 若无可用 GOP（screenrecord 静止、无 IDR 缓冲）→ 重启会话强制产生 IDR
                restart_needed = False
                dead = []
                for ws in clients:
                    try:
                        if not getattr(ws, "_gop_ok", False):
                            with _gop_lock:
                                gop = _gop_cache.get(serial)
                            gop_size = sum(len(c) for c in gop) if gop else 0
                            print(f"[stream] {serial} 新客户端接入: gop={gop_size}B has_idr={has_idr} prefix={len(prefix)}B chunk={len(chunk)}B", flush=True)
                            if gop:
                                # 重放 GOP 时必须带上参数集，否则解码器不知道分辨率/帧结构
                                ws.send((prefix or b"") + b"".join(gop))
                            else:
                                restart_needed = True
                            ws._gop_ok = True
                        # ⚠️ 参数集(SPS/PPS)只能挂在**关键帧**前面。
                        # chunk 是按字节读出来的，边界几乎必然落在某个 NAL 中间；
                        # 若每个 chunk 都拼一遍 SPS/PPS，就等于往 NAL 中间插入乱码，
                        # 解码器读到后参考帧错乱 → 画面花屏/垂直拉丝
                        # （117 只是碰巧块边界靠近 NAL 边界才看着正常）。
                        if prefix and has_idr:
                            ws.send(prefix + chunk)
                        else:
                            ws.send(chunk)
                    except Exception:
                        dead.append(ws)
                if restart_needed:
                    print(f"[stream] {serial} 无 GOP 触发重启", flush=True)
                    break  # 跳出内层循环 → 外层重启 screenrecord，新会话开头带 IDR
                for ws in dead:
                    if ws in clients:
                        clients.remove(ws)
        except Exception as e:
            print(f"[stream] {serial} worker 异常: {e!r}", flush=True)
            time.sleep(0.5)
        finally:
            # 清理所有进程
            for p in [sr_proc, ff_proc, proc]:
                if p:
                    try:
                        p.kill()
                        p.wait(timeout=2)
                    except Exception:
                        pass
        # 流停止（到期/出错），短暂等待后重启
        time.sleep(0.3)

    # 没有客户端了，清理
    # 自愈暂不启用，见 _kill_device_screenrecord 的说明
    # _kill_device_screenrecord(serial)
    with _stream_lock:
        for did, info in list(_active_streams.items()):
            if info["clients"] is clients and not clients:
                del _active_streams[did]
                break


def _handle_control(msg, serial, ws=None):
    """处理客户端经视频流 WebSocket 上行的控制消息。

    触摸手势协议（JSON）：
      {"type":"touch","event":"down|move|up|cancel|tap|longpress|swipe",
       "x":..,"y":.., "x1/y1/x2/y2/duration"(swipe/longpress)}
    走持久化 TouchBridge（单条 adb shell 长连接 + MOVE 自动合并），
    复用这条已认证的 WS 连接，避免每个触摸事件新建 HTTP 请求 + adb shell 冷启动。

    能力探测：{"type":"probe"} -> 回 {"type":"touch-ack"}，
    客户端据此判断服务端是否支持低延迟触控（不支持则回退 REST）。

    任何异常都不得打断接收循环。
    """
    try:
        msg = (msg or "").strip()
        if not msg or msg in ("ping", "heartbeat", "ping\n"):
            return
        obj = json.loads(msg)
        if not isinstance(obj, dict):
            return
        if obj.get("type") == "probe":
            # 只有设备端真的能低延迟注入才应答；否则让客户端回退 REST。
            # 背景：RK3399 板卡上 `input motionevent` 单次约 450ms（JVM 冷启动），
            # 会误触发长按且完全不跟手；touch.py 会自动改用 evdev 直写
            # （单次约 55ms）。两种模式都不可用时这里不应答，
            # 客户端就会走 REST tap/swipe —— 不跟手但至少不误触。
            if ws is not None:
                try:
                    from .touch import get_bridge
                    if get_bridge(serial).supports_low_latency():
                        ws.send('{"type":"touch-ack"}')
                except Exception:
                    pass
            return
        if obj.get("type") != "touch":
            return
        from .touch import get_bridge
        bridge = get_bridge(serial)

        def _xy(prefix=""):
            return (int(round(float(obj.get(prefix + "x", 0)))),
                    int(round(float(obj.get(prefix + "y", 0)))))

        ev = str(obj.get("event", "")).lower()
        if ev == "down":
            x, y = _xy(); bridge.down(x, y)
        elif ev == "move":
            x, y = _xy(); bridge.move(x, y)
        elif ev == "up":
            x, y = _xy(); bridge.up(x, y)
        elif ev == "cancel":
            x, y = _xy(); bridge.cancel(x, y)
        elif ev == "tap":
            x, y = _xy(); bridge.tap(x, y)
        elif ev == "longpress":
            x, y = _xy()
            bridge.long_press(x, y, int(float(obj.get("duration", 600))))
        elif ev == "swipe":
            x1 = int(round(float(obj.get("x1", 0))))
            y1 = int(round(float(obj.get("y1", 0))))
            x2 = int(round(float(obj.get("x2", 0))))
            y2 = int(round(float(obj.get("y2", 0))))
            bridge.swipe(x1, y1, x2, y2, int(float(obj.get("duration", 300))))
    except Exception as e:  # noqa: BLE001
        print(f"[stream] control 消息处理失败: {e!r}", flush=True)


@sock.route("/ws/devices/<int:device_id>/stream")
def device_stream(ws, device_id):
    """WebSocket 端点：实时视频流。

    连接时可通过 query 参数传递：
      - token: JWT 认证令牌
      - bitrate: 码率（默认 4000000）
      - format: 输出格式，h264（默认）或 fmp4
    """
    # 简单认证：从 query 参数获取 token
    token = request.args.get("token", "")
    if not token:
        ws.close(1008, "missing token")
        return

    # 验证 token
    try:
        from flask import current_app
        import jwt as _jwt
        _jwt.decode(token, current_app.config["JWT_SECRET"], algorithms=["HS256"])
    except Exception:
        ws.close(1008, "invalid token")
        return

    serial = _get_serial(device_id)
    if serial is None:
        ws.close(1008, "device not found or not redroid")
        return

    bitrate = request.args.get("bitrate", type=int) or 4000000
    # 修复（2026-10-08）：位率必须钳制范围。客户端传入非法值（实测有客户端发
    # bitrate=15）会让 screenrecord 立即失败退出，worker 随即无限重启——
    # 实测每秒 5~8 次、进程号飙升，画面全黑且拖垮整台服务器。
    if bitrate < 200_000:
        bitrate = 200_000
    elif bitrate > 8_000_000:
        bitrate = 8_000_000
    fmt = request.args.get("format", "h264")
    if fmt not in ("h264", "fmp4"):
        fmt = "h264"
    # 帧尺寸（可选）：客户端可请求更低分辨率以提升弱端解码帧率
    size = _parse_size(request.args.get("size"))

    # 加入/创建流
    with _stream_lock:
        if device_id in _active_streams:
            info = _active_streams[device_id]
            # 如果格式/尺寸不同、或 worker 线程已经退出，都需要重建流。
            # 修复（2026-10-08）：原实现只比较 format/size，worker 退出后若
            # _active_streams 还残留条目，新客户端会被挂到已死线程的 clients
            # 列表上，永远收不到任何数据 —— 前端表现就是"黑屏"，刷新也不恢复。
            _th = info.get("thread")
            # 修复（2026-10-14）：**不再因为 size 不同就重建流**。
            # 原实现把 size 差异也算作"必须重建"，于是同一台设备被两个不同画质档位
            # （size）的客户端同时观看时，后进来的会把先进来的关掉并重建流；被关的
            # 立刻重连、又把另一个关掉 —— 无限互踢，约 1~2 秒一轮。
            # 客户端表现：状态栏一直闪「重连中」、画面反复重启。
            # 现在流已在跑就复用它现有的编码尺寸，新客户端按实际帧尺寸解码；
            # 只有 format 变化（h264/fmp4 不兼容）或 worker 线程已死才重建。
            if (info.get("format") != fmt
                    or _th is None or not _th.is_alive()):
                # 标记旧流停止，等待线程退出
                old_clients = info["clients"]
                for c in old_clients:
                    try:
                        c.close()
                    except Exception:
                        pass
                # 等待旧线程退出
                # 修复（2026-10-08）：清空旧 clients 列表，让旧 worker 的 while clients:
                # 立即为假 -> 走到 finally 回收自己的 screenrecord/ffmpeg。
                # 否则旧 worker 成为孤儿：已不在 _active_streams 里、没人清理，
                # 却仍占着设备（screenrecord 单实例），新流抢占失败反复重启，
                # 实测每秒 5~8 次重启风暴，画面全黑。
                old_clients.clear()
                time.sleep(0.5)
                # 创建新流
                clients = [ws]
                info = {"clients": clients, "thread": None, "format": fmt, "size": size}
                _active_streams[device_id] = info
                with _gop_lock:
                    _gop_cache.pop(serial, None)
                t = threading.Thread(
                    target=_stream_worker,
                    args=(serial, bitrate, clients, fmt, size),
                    daemon=True,
                )
                t.start()
                info["thread"] = t
            else:
                if info.get("size") != size:
                    print(f"[stream] {serial} 新客户端请求 size={size}，"
                          f"复用现有流 size={info.get('size')}（不重建，避免互踢）",
                          flush=True)
                info["clients"].append(ws)
        else:
            clients = [ws]
            info = {"clients": clients, "thread": None, "format": fmt, "size": size}
            _active_streams[device_id] = info
            # 新会话：清掉旧的 GOP 缓存，从新流开头重新缓冲
            with _gop_lock:
                _gop_cache.pop(serial, None)
            # 启动流线程
            t = threading.Thread(
                target=_stream_worker,
                args=(serial, bitrate, clients, fmt, size),
                daemon=True,
            )
            t.start()
            info["thread"] = t

    try:
        while True:
            try:
                msg = ws.receive(timeout=30)
            except Exception:
                break  # 连接关闭/异常
            if msg is None:
                # 超时无消息：客户端仍在正常接收广播（前端有心跳），保持连接不断
                continue
            # 客户端上行消息：触摸控制（JSON）/ 心跳；二进制帧不会出现在这里
            if isinstance(msg, str):
                _handle_control(msg, serial, ws)
    except Exception:
        pass
    finally:
        # 客户端断开，从列表移除
        with _stream_lock:
            if device_id in _active_streams:
                clients = _active_streams[device_id]["clients"]
                if ws in clients:
                    clients.remove(ws)
