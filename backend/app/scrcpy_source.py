"""scrcpy server 视频源 —— 替代 screenrecord（关键收益：GOP 可控）。

## 为什么要换

`screenrecord` **没有 GOP 参数**，IDR 间隔实测约 10 秒（见 stream.py:32 注释与
`服务器侧待办.md` D2）。而前端在解码队列积压时会「丢弃所有 P 帧直到下一个 IDR」
（`mobile/src/lib/streamPlayer.js`），于是**一次普通的解码跟不上会被放大成最长
约 10 秒的画面冻结**。这也是「画面一动就卡很久」的直接原因。

scrcpy server 的编码参数可在运行时协商 —— `iFrameInterval` / `maxFps` / `bitrate` /
`bounds` 都是协议内建字段，设成 1~2 秒即可把这个放大效应消掉。

## 设备端已经就绪

`orchestrator._deploy_scrcpy_server()` 建容器时就已部署并常驻：

    CLASSPATH=/data/local/tmp/scrcpy-server.jar nohup app_process /
        com.genymobile.scrcpy.Server 1.19-ws8 web ERROR 8886 true

即 ws-scrcpy fork 的 **web 模式**：server 监听设备 TCP **8886**
（与 PC 客户端 `proxy-adb remote=tcp:8886` 同一个入口）。

## 一个容易踩的坑：它是 WebSocket，不是裸 TCP

`ws-scrcpy/src/server/mw/WebsocketProxy.ts:47` —— Node 侧拿到 `adb forward` 的本地端口后，
用的是 `new WS(remoteUrl)`，即 **WebSocket 客户端**。
所以直接对 `127.0.0.1:<port>` 做裸 TCP 读会一直读不到任何字节（握手就超时）。
本模块自带一个极简 WS 客户端（不引入新的 pip 依赖）。

顺带一个好处：**WS 的消息边界就是协议边界** —— 初始信息是第一条消息，
之后每条消息是一块视频数据。前端 `StreamReceiver.onSocketMessage` 正是靠这个
（比较每条消息开头的 magic），我们也一样按消息解析。

## 对外接口

伪装成 `subprocess.Popen`（`stdout.fileno()` / `poll()` / `kill()` / `wait()` / `pid`），
于是 `stream.py` 的 `_stream_worker` 下游逻辑（start code 归一化、SPS/PPS 缓存、
GOP 缓存、广播、静止期补发）**一行都不用改**。

## 失败一律回落

任何一步失败（server 没起、WS 握手失败、协议不符）都抛异常，
由 `stream.py` 的 `_start_source()` 捕获后回落到原来的 `screenrecord`。

## 协议出处（逐字节核对过源码）

- `ws-scrcpy/src/common/Constants.ts:16` —— server 启动参数 `[version,'web','ERROR',8886,true]`
- `ws-scrcpy/src/app/client/StreamReceiver.ts:56` —— 初始信息块布局
- `ws-scrcpy/src/app/VideoSettings.ts:52,154` —— 视频参数读写顺序
- `ws-scrcpy/src/app/controlMessage/CommandControlMessage.ts:34` —— 101 消息封装
"""

import base64
import os
import socket
import struct
import subprocess
import threading
import time

# ---------------------------------------------------------------------------
# 协议常量
# ---------------------------------------------------------------------------
SCRCPY_PORT = 8886
MAGIC_INITIAL = b"scrcpy_initial"          # 14 字节
DEVICE_NAME_LEN = 64
DISPLAY_INFO_LEN = 24
SCREEN_INFO_LEN = 25
VIDEO_SETTINGS_BASE_LEN = 35               # VideoSettings.BASE_BUFFER_LENGTH
TYPE_CHANGE_STREAM_PARAMETERS = 101
SERVER_PACKAGE = "com.genymobile.scrcpy.Server"
SERVER_VERSION = "1.19-ws8"
SERVER_TYPE = "web"
DEFAULT_JAR = "/data/local/tmp/scrcpy-server.jar"

_MAX_FIELD = 65536          # 协议字段长度上限（防御异常数据）
_MAX_MSG = 16 * 1024 * 1024  # 单条 WS 消息上限

# 关键帧间隔（秒）。**0 = 全关键帧**，这是实测出来的最优解：
#
#   实测（2026-10-15，36.156.109.243 / .209 两台，720x1280，持续滑动）：
#     iFrameInterval=0 → 每一帧都是 IDR（GOP 0.07s），码率 1092~1741 kbps
#     iFrameInterval=1 → GOP 约 4.0s，            码率 1163~1215 kbps
#     iFrameInterval=2 → GOP 约 7.9s，            码率 1208 kbps
#     iFrameInterval=5/10 → GOP > 12s（实测不到第二次 IDR）
#
# 注意服务端把请求值放大了约 4 倍（1→4s、2→8s），所以「调小」这条路走不通，
# 但 0 是特殊值 = 全关键帧。
#
# 为什么选全关键帧：前端在解码队列积压时是「丢弃所有非关键帧直到下一个 IDR」，
# 于是丢帧的代价 = 等一个 GOP。全关键帧下**任何一帧都能直接解码**，
# 这个等待被彻底消除（前端只需把「超限丢帧」放宽到任何帧类型即可，见 streamPlayer.js）。
# 代价：intra 编码效率低于 P 帧，同画质下码率略高（实测约 +0~40%，仍低于 2 Mbps）。
#
# 可用环境变量 SCRCPY_I_FRAME_INTERVAL 覆盖（想回到「4 秒 GOP」就设 1）。
I_FRAME_INTERVAL = int(os.environ.get("SCRCPY_I_FRAME_INTERVAL", "0"))

# 连接后等待首帧的时间。设备端 server 在客户端接入时会立刻产生一帧
# （实测静止屏幕下 1.4~1.8s 到达），所以超过这个时间还没有任何视频数据，
# 基本可以判定这条链路有问题 → 抛异常让上层回落到 screenrecord。
FIRST_FRAME_TIMEOUT = float(os.environ.get("SCRCPY_FIRST_FRAME_TIMEOUT", "5.0"))


def _log(msg: str) -> None:
    print("[scrcpy-src] %s" % msg, flush=True)


def _adb_path() -> str:
    """复用 orchestrator 的 adb 探测，避免两处硬编码不一致。"""
    try:
        from . import orchestrator  # noqa: WPS433
        return orchestrator._adb_path()
    except Exception:
        return os.environ.get("ADB_PATH") or "adb"


def _run_adb(serial: str, args, timeout: int = 20):
    cmd = [_adb_path(), "-s", serial] + list(args)
    creationflags = 0x08000000 if os.name == "nt" else 0
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=creationflags)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        return -1, "", repr(e)


# ---------------------------------------------------------------------------
# 极简 WebSocket 客户端（只支持我们需要的：握手 + 二进制帧 + ping/pong）
# ---------------------------------------------------------------------------
class WsClient:
    def __init__(self, sock):
        self._sock = sock
        self._buf = bytearray()
        self.closed = False

    # ---- 底层 ----
    def _read_exact(self, n):
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                raise
            except OSError as e:
                raise ConnectionError("WS socket 读取失败: %r" % e)
            if not chunk:
                raise ConnectionError("WS 连接被对端关闭")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def handshake(self, host, port, path="/", timeout=6.0):
        """发 HTTP Upgrade 并校验 101。"""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ) % (path, host, port, key)
        self._sock.sendall(req.encode("ascii"))

        deadline = time.time() + timeout
        while b"\r\n\r\n" not in self._buf:
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError("WS 握手响应超时")
            self._sock.settimeout(remain)
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS 握手阶段连接被关闭")
            self._buf += chunk
        head, _, rest = bytes(self._buf).partition(b"\r\n\r\n")
        self._buf = bytearray(rest)
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in first:
            raise ValueError("WS 握手失败: %s" % first[:120])

    def _send_frame(self, opcode, payload):
        """客户端 → 服务端必须加掩码（RFC 6455）。"""
        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        n = len(payload)
        if n < 126:
            header = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
        self._sock.sendall(header + mask + masked)

    def send_binary(self, payload):
        self._send_frame(0x2, payload)

    def _read_frame(self):
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        ln = b1 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._read_exact(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read_exact(8))[0]
        if ln > _MAX_MSG:
            raise ValueError("WS 消息过大: %d" % ln)
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(ln) if ln else b""
        if mask:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return fin, opcode, payload

    def recv_message(self):
        """读一条完整的（可能分片的）数据消息；连接关闭返回 b""。"""
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:                      # close
                self.closed = True
                return b""
            if opcode == 0x9:                      # ping → pong
                try:
                    self._send_frame(0xA, payload)
                except Exception:
                    pass
                continue
            if opcode == 0xA:                      # pong
                continue
            if opcode in (0x1, 0x2):
                if fin:
                    return payload
                parts = [payload]
                while True:
                    fin2, op2, p2 = self._read_frame()
                    if op2 == 0x8:
                        self.closed = True
                        return b""
                    if op2 in (0x9, 0xA):
                        continue
                    parts.append(p2)               # 0x0 continuation
                    if fin2:
                        return b"".join(parts)
            # 其它 opcode 忽略
            continue

    def close(self, code=1000):
        """发一个标准的 WS close 帧再关 TCP。

        ⚠️ 这一步**不能省**：实测发现，直接 close TCP 时设备端 server 会以为客户端
        还在（它按连接数决定是否重建编码器），下一个连接就再也收不到视频
        （表现为握手正常、但一条消息都没有）。发 close 帧后服务端才会把客户端摘掉。
        """
        if self.closed:
            return
        self.closed = True
        try:
            self._send_frame(0x8, struct.pack(">H", code))
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 控制消息：配置编码器
# ---------------------------------------------------------------------------
def build_change_stream_parameters(bitrate, max_fps=0, i_frame_interval=None,
                                   size=None, codec_options=None, encoder_name=None,
                                   display_id=0):
    """构造 `TYPE_CHANGE_STREAM_PARAMETERS(101)` 消息体。

    字节布局严格按 `VideoSettings.toBuffer()`（源码 VideoSettings.ts:154-197）：

        int32BE bitrate
        int32BE maxFps
        int8    iFrameInterval
        int16BE width / height             （bounds，0 表示不限制）
        int16BE left / top / right / bottom（crop，全 0 不裁剪）
        int8    sendFrameMeta
        int8    lockedVideoOrientation     （-1 = 不锁定）
        int32BE displayId
        int32BE codecOptions 长度 + 内容
        int32BE encoderName  长度 + 内容

    注意 `maxFps` 0 表示「不限」，不是 0fps。
    """
    if i_frame_interval is None:
        i_frame_interval = I_FRAME_INTERVAL
    w, h = (size or (0, 0))
    if w and h:
        # H.264 宏块要求偶数；int16 范围限制
        w, h = min(int(w) & ~1, 32766), min(int(h) & ~1, 32766)
    else:
        w = h = 0

    buf = bytearray()
    buf += struct.pack(">i", int(bitrate or 0))
    buf += struct.pack(">i", int(max_fps or 0))
    buf += struct.pack(">b", max(-1, min(int(i_frame_interval), 127)))
    buf += struct.pack(">hh", w, h)
    buf += struct.pack(">hhhh", 0, 0, 0, 0)
    buf += struct.pack(">b", 0)                 # sendFrameMeta 关闭：我们按 Annex-B 解析
    buf += struct.pack(">b", -1)                # lockedVideoOrientation：不锁定
    buf += struct.pack(">i", int(display_id or 0))
    for text in (codec_options, encoder_name):
        raw = text.encode("utf-8") if text else b""
        buf += struct.pack(">i", len(raw)) + raw
    if len(buf) < VIDEO_SETTINGS_BASE_LEN:
        raise AssertionError("VideoSettings 缓冲区长度异常: %d" % len(buf))
    return bytes([TYPE_CHANGE_STREAM_PARAMETERS]) + bytes(buf)


# ---------------------------------------------------------------------------
# 初始信息解析
# ---------------------------------------------------------------------------
def parse_initial_info(payload):
    """解析初始信息消息（**一整条 WS 消息**，不是字节流）。

    布局（`StreamReceiver.handleInitialInfo`，源码 :56-103）：

        "scrcpy_initial"(14) + deviceName(64)
        int32 displaysCount
          per display:
            DisplayInfo(24) = displayId,w,h,rotation,layerStack,flags（各 int32BE）
            int32 connectionCount
            int32 screenInfoLen  + ScreenInfo
            int32 videoSettingsLen + VideoSettings
        int32 encodersCount
          per encoder: int32 nameLen + name
        int32 clientId
    """
    if not payload.startswith(MAGIC_INITIAL):
        raise ValueError("不是初始信息消息，开头=%r" % payload[:16])
    off = len(MAGIC_INITIAL)

    def take(n):
        nonlocal off
        if off + n > len(payload):
            raise ValueError("初始信息长度不足（需要 %d，剩余 %d）" % (n, len(payload) - off))
        out = payload[off:off + n]
        off += n
        return out

    def int32():
        return struct.unpack(">i", take(4))[0]

    raw_name = take(DEVICE_NAME_LEN)
    info = {
        "device_name": raw_name.split(b"\x00")[0].decode("utf-8", "replace"),
        "displays": [], "encoders": [], "client_id": -1, "connection_count": 0,
    }

    displays_count = int32()
    if not 0 <= displays_count <= 8:
        raise ValueError("displaysCount 异常: %d" % displays_count)
    for _ in range(displays_count):
        d = take(DISPLAY_INFO_LEN)
        display_id, dw, dh, rotation, layer_stack, flags = struct.unpack(">iiiiii", d)
        conn = int32()
        si_len = int32()
        if not 0 <= si_len <= _MAX_FIELD:
            raise ValueError("screenInfoLen 异常: %d" % si_len)
        si = take(si_len) if si_len else b""
        vs_len = int32()
        if not 0 <= vs_len <= _MAX_FIELD:
            raise ValueError("videoSettingsLen 异常: %d" % vs_len)
        vs = take(vs_len) if vs_len else b""
        info["displays"].append({
            "display_id": display_id, "width": dw, "height": dh,
            "rotation": rotation, "layer_stack": layer_stack, "flags": flags,
            "connection_count": conn, "screen_info": si, "video_settings": vs,
        })
        info["connection_count"] += max(0, conn)

    enc_count = int32()
    if not 0 <= enc_count <= 128:
        raise ValueError("encodersCount 异常: %d" % enc_count)
    for _ in range(enc_count):
        n = int32()
        if not 0 <= n <= _MAX_FIELD:
            raise ValueError("encoderNameLen 异常: %d" % n)
        enc = take(n) if n else b""
        info["encoders"].append(enc.decode("utf-8", "replace"))

    info["client_id"] = int32()
    return info


def parse_video_settings(buf):
    """解析 VideoSettings 字节（用于日志：确认服务端当前用的是多少 GOP）。"""
    if len(buf) < VIDEO_SETTINGS_BASE_LEN:
        return {}
    bitrate, max_fps = struct.unpack(">ii", buf[0:8])
    i_frame_interval = struct.unpack(">b", buf[8:9])[0]
    w, h = struct.unpack(">hh", buf[9:13])
    return {"bitrate": bitrate, "max_fps": max_fps,
            "i_frame_interval": i_frame_interval, "bounds": (w, h)}


# ---------------------------------------------------------------------------
# 设备端 server 管理
# ---------------------------------------------------------------------------
def server_listening(serial: str) -> bool:
    """设备上是否有进程在监听 8886。

    直接读 `/proc/net/tcp{,6}` 找本地端口 0x22B6(=8886) 且状态 LISTEN(0A)。
    比 `ss`/`netstat` 可靠 —— 很多精简 Android 镜像里没有这两个命令。
    """
    rc, out, _ = _run_adb(serial, ["shell", "cat /proc/net/tcp /proc/net/tcp6"], timeout=15)
    if rc != 0 or not out:
        return False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] == "sl":
            continue
        local, state = parts[1], parts[3]
        if ":" not in local:
            continue
        if local.rsplit(":", 1)[1].upper() == "22B6" and state.upper() == "0A":
            return True
    return False


def ensure_server(serial: str, jar_path: str = "") -> bool:
    """确认设备端 scrcpy server 在跑；不在则拉起。返回是否已就绪。

    拉起命令与 `orchestrator._deploy_scrcpy_server()` 完全一致
    （同一个 server、同一端口、同一版本），不会产生第二套 server。
    """
    if server_listening(serial):
        return True

    jar = jar_path or DEFAULT_JAR
    rc, out, _ = _run_adb(serial, ["shell", "ls", jar], timeout=15)
    # 设备根本连不上（offline / not found）时直接放弃：再推 jar 也没用，
    # 只会让上层多等几个 adb 超时。实测一台离线设备在 25 分钟里触发了 98 次回落。
    if "not found" in out or "device offline" in out:
        _log("%s adb 不可用（%s），跳过 scrcpy" % (serial, out.strip()[:60]))
        return False
    if rc != 0 or "No such file" in out or not out.strip():
        local_jar = ""
        try:
            from flask import current_app  # noqa: WPS433
            local_jar = current_app.config.get("SCRCPY_SERVER_JAR", "")
        except Exception:
            local_jar = os.environ.get("SCRCPY_SERVER_JAR", "")
        if not local_jar or not os.path.isfile(local_jar):
            _log("%s 缺少 %s，且本机没有可用 jar，无法拉起" % (serial, jar))
            return False
        rc, out, err = _run_adb(serial, ["push", local_jar, jar], timeout=60)
        if rc != 0:
            _log("%s push jar 失败: %s" % (serial, (err or out).strip()[:160]))
            return False

    start_cmd = ("CLASSPATH=%s setsid nohup app_process / %s %s %s ERROR %d true "
                 ">/dev/null 2>&1 &"
                 % (jar, SERVER_PACKAGE, SERVER_VERSION, SERVER_TYPE, SCRCPY_PORT))
    _run_adb(serial, ["shell", start_cmd], timeout=20)

    for _ in range(12):          # 最多等 ~6 秒
        time.sleep(0.5)
        if server_listening(serial):
            _log("%s scrcpy server 已拉起" % serial)
            return True
    _log("%s scrcpy server 拉起后仍未监听 %d" % (serial, SCRCPY_PORT))
    return False


# ---------------------------------------------------------------------------
# 视频源：伪装成 subprocess.Popen
# ---------------------------------------------------------------------------
class ScrcpySource:
    """把设备端 scrcpy server 的视频流伪装成 `subprocess.Popen`。

    `_stream_worker` 只用到：`stdout.fileno()` / `select.select([stdout])` /
    `os.read(...)` / `poll()` / `pid` / `kill()` / `wait()`，这里提供同名成员即可。

    实现：后台线程从 WS 读消息 → 写进 `os.pipe()`，`stdout` 是管道读端，
    于是 `select` 与 `os.read` 天然可用，下游逻辑零改动。
    """

    def __init__(self, serial, ws, local_port, info, read_size=65536):
        self.serial = serial
        self.pid = -1                 # 仅用于日志
        self.info = info
        self._ws = ws
        self._local_port = local_port
        self._read_size = read_size
        self._exit_code = None
        self._stopped = threading.Event()
        self._bytes = 0
        self._msgs = 0
        self._last_error = ""

        r, w = os.pipe()
        self._wfd = w
        self.stdout = os.fdopen(r, "rb", buffering=0)
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="scrcpy-pump-%s" % serial)
        self._thread.start()

    def _pump(self):
        try:
            while not self._stopped.is_set():
                try:
                    data = self._ws.recv_message()
                except socket.timeout:
                    continue
                except Exception as e:  # noqa: BLE001
                    self._last_error = repr(e)
                    break
                if not data:          # 连接关闭
                    break

                # 控制消息不能混进视频流：下发编码参数后，服务端会把**更新后的
                # 初始信息**重新广播一条（实测 239 字节，以 "scrcpy_initial" 开头）。
                # 它本身不是 NAL，混进去会在解码器前多一段垃圾字节。
                # 顺便把里面**实际生效**的参数打出来 —— 这是唯一能确认
                # iFrameInterval/bitrate 到底有没有被采纳的地方。
                if data.startswith(MAGIC_INITIAL):
                    try:
                        _info = parse_initial_info(data)
                        for d in _info["displays"]:
                            vs = parse_video_settings(d.get("video_settings") or b"")
                            if vs:
                                _log("%s 服务端生效参数: %s" % (self.serial, vs))
                    except Exception:
                        pass
                    continue
                if data.startswith(b"scrcpy_message"):
                    continue

                self._bytes += len(data)
                self._msgs += 1
                try:
                    os.write(self._wfd, data)
                except OSError:
                    break
        finally:
            if self._exit_code is None:
                self._exit_code = 0
            try:
                os.close(self._wfd)
            except OSError:
                pass

    # ---- Popen 鸭子接口 ----
    def poll(self):
        return self._exit_code

    def wait(self, timeout=None):
        self._thread.join(timeout)
        return self._exit_code

    def kill(self):
        self._stopped.set()
        try:
            self._ws.close()
        except Exception:
            pass
        if self._local_port:
            _run_adb(self.serial, ["forward", "--remove", "tcp:%d" % self._local_port],
                     timeout=10)

    def terminate(self):
        self.kill()

    def stats(self):
        return {"bytes": self._bytes, "messages": self._msgs, "error": self._last_error}


def start_scrcpy_source(serial, bitrate=4000000, size=None, max_fps=0,
                        i_frame_interval=None, apply_settings=True,
                        handshake_timeout=6.0, first_frame_timeout=None,
                        jar_path=""):
    """连设备端 scrcpy server 并返回 `ScrcpySource`；失败抛异常（调用方回落 screenrecord）。

    :param bitrate: 目标码率（bps）
    :param size: 期望帧尺寸 (w, h)；缺省/0 时用握手拿到的显示尺寸
    :param max_fps: 0 = 不限帧率
    :param i_frame_interval: 关键帧间隔（秒）；0 = 全关键帧（默认，见模块顶部说明）
    :param apply_settings: 是否下发 101 改编码参数
    :param first_frame_timeout: 等首帧的超时；超时即抛异常（安全阀，保证能回落）
    """
    if not ensure_server(serial, jar_path):
        raise RuntimeError("设备端 scrcpy server 不可用")

    # 1) 端口转发：tcp:0 让 adb 自选空闲本地端口，端口号打印在 stdout
    rc, out, err = _run_adb(serial, ["forward", "tcp:0", "tcp:%d" % SCRCPY_PORT], timeout=20)
    if rc != 0:
        raise RuntimeError("adb forward 失败: %s" % (err or out).strip()[:160])
    try:
        local_port = int(out.strip().splitlines()[-1])
    except Exception:
        raise RuntimeError("adb forward 未返回端口: %r" % out.strip()[:120])

    def _cleanup_port():
        _run_adb(serial, ["forward", "--remove", "tcp:%d" % local_port], timeout=10)

    # 2) 连上 + WebSocket 握手
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(handshake_timeout)
    try:
        sock.connect(("127.0.0.1", local_port))
    except Exception as e:
        try:
            sock.close()
        except Exception:
            pass
        _cleanup_port()
        raise RuntimeError("连接 scrcpy server 失败: %r" % e)

    ws = WsClient(sock)
    try:
        ws.handshake("127.0.0.1", local_port, timeout=handshake_timeout)
        first = ws.recv_message()
        if not first:
            raise ConnectionError("WS 已连接但初始信息为空")
        info = parse_initial_info(first)
    except Exception:
        try:
            ws.close()
        except Exception:
            pass
        _cleanup_port()
        raise

    disp = info["displays"][0] if info["displays"] else {}
    cur = parse_video_settings(disp.get("video_settings") or b"")
    _log("%s 握手完成 设备名=%s 分辨率=%sx%s 已有客户端=%d 编码器=%d 服务端当前GOP=%ss"
         % (serial, info.get("device_name"), disp.get("width"), disp.get("height"),
            info.get("connection_count"), len(info.get("encoders") or []),
            cur.get("i_frame_interval", "?")))

    # ⚠️ bounds 必须给**明确的尺寸**，不能给 0×0。
    # 实测（2026-10-15，36.156.109.243:5901）：传 (0,0) 时设备端
    # `ScreenEncoder.configure()` 抛异常 → 一条视频数据都没有（6 秒仅 239 字节）；
    # 传 (720,1280) 时正常出帧。所以 size 缺省时用握手拿到的显示尺寸兜底。
    eff_size = size
    if not eff_size or not eff_size[0] or not eff_size[1]:
        dw, dh = disp.get("width") or 0, disp.get("height") or 0
        if dw and dh:
            eff_size = (dw, dh)
            _log("%s 未指定 size，改用显示尺寸 %sx%s（传 0×0 会让编码器配置失败）"
                 % (serial, dw, dh))

    # 3) 下发编码参数（GOP 是本次改造的核心）
    if apply_settings:
        try:
            ws.send_binary(build_change_stream_parameters(
                bitrate=bitrate, max_fps=max_fps,
                i_frame_interval=i_frame_interval, size=eff_size,
            ))
            _log("%s 已下发编码参数 bitrate=%d iFrameInterval=%ss size=%s"
                 % (serial, bitrate, i_frame_interval or I_FRAME_INTERVAL, eff_size or "原生"))
        except Exception as e:  # noqa: BLE001
            # 下发失败不致命：流仍可用，只是 GOP 还是服务端默认值
            _log("%s 下发编码参数失败（继续用服务端默认）: %r" % (serial, e))

    # 4) 小超时交给泵线程（超时不算错，继续等下一块）
    try:
        sock.settimeout(0.5)
    except Exception:
        pass
    src = ScrcpySource(serial, ws, local_port, info)

    # 5) 安全阀：等首帧。拿不到就抛异常，让上层回落 screenrecord。
    #    设备端 server 在客户端接入时一定会产生一帧（实测静止屏幕 1.4~1.8s 到达，
    #    约 10~18KB），所以这里超时基本可以判定链路有问题（例如编码器 configure 失败）。
    #    阈值取 4KB 而不是 >0：控制消息已被 _pump 过滤，但留一点余量更稳。
    wait = FIRST_FRAME_TIMEOUT if first_frame_timeout is None else first_frame_timeout
    deadline = time.time() + wait
    while time.time() < deadline and src._bytes < 4096:
        time.sleep(0.05)
    if src._bytes < 4096:
        src.kill()
        raise RuntimeError("scrcpy 已连接但 %.1fs 内没有视频数据（只有 %d 字节，"
                           "编码器可能配置失败）" % (wait, src._bytes))
    _log("%s 首帧已到（%.2fs，%d 字节）" % (serial, wait - (deadline - time.time()), src._bytes))
    return src
