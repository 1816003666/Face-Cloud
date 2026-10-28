"""WebRTC 视频推流端点（aiortc）。

链路：screenrecord H.264 裸流 → PyAV 解码为 YUV 帧 → aiortc VideoStreamTrack
重编码为 H.264 RTP → 经 WebRTC 送达客户端 <video>。

信令（一次 HTTP 交换）：
    POST /api/devices/<id>/webrtc/offer
    body: { "sdp": "<offer sdp>", "bitrate": 8000000, "fps": 60 }
    resp: { "sdp": "<answer sdp>", "type": "answer" }
"""
import asyncio
import concurrent.futures
import queue
import threading
import time

import av
from flask import Blueprint, jsonify, request, current_app

from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from .stream import _start_screenrecord, _get_serial

webrtc_bp = Blueprint("webrtc", __name__)

# 每设备一个长生命周期 asyncio 事件循环线程（aiortc 连接需持续运行）
_loop_lock = threading.Lock()
_loop = None
_loop_thread = None


def _get_loop():
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
            _loop_thread.start()
        return _loop


# 活跃的 RTCPeerConnection（device_id -> (pc, track)）
_connections: dict[int, tuple] = {}
_conn_lock = threading.Lock()


def _find_sc(buf: bytes, from_i: int = 0) -> int:
    """在 Annex-B 流中查找下一个 start code（支持 4/3 字节）。"""
    i = buf.find(b"\x00\x00\x00\x01", from_i)
    if i < 0:
        i = buf.find(b"\x00\x00\x01", from_i)
    return i


class H264StreamTrack(VideoStreamTrack):
    """从 screenrecord 解码出 YUV 帧，按需提供给 aiortc 发送。"""

    kind = "video"

    def __init__(self, serial: str, bitrate: int = 4000000, fps: int = 60):
        super().__init__()
        self._q: queue.Queue = queue.Queue(maxsize=2)  # 保留最新帧
        self._running = True
        self._idx = 0
        self._fps = fps
        self._pts_base = 0.0          # 首帧单调时钟
        self._last_pts = 0            # 上一帧 pts（90000 时基）
        self._thread = threading.Thread(
            target=self._run, args=(serial, bitrate), daemon=True
        )
        self._thread.start()

    def _run(self, serial: str, bitrate: int):
        codec = av.CodecContext.create("h264", "r")
        while self._running:
            proc = None
            buf = b""
            acc = b""
            try:
                proc = _start_screenrecord(serial, bitrate)
                while self._running:
                    chunk = proc.stdout.read1(65536)
                    if not chunk:
                        if proc.poll() is not None:
                            break
                        time.sleep(0.01)
                        continue
                    buf += chunk
                    idx = 0
                    while True:
                        sc = _find_sc(buf, idx)
                        if sc < 0:
                            break
                        nxt = sc + 4 if buf[sc:sc + 4] == b"\x00\x00\x00\x01" else sc + 3
                        sc2 = _find_sc(buf, nxt)
                        if sc2 < 0:
                            break
                        nal = buf[sc:sc2]
                        t = nal[4] & 0x1F
                        if t == 1 or t == 5:
                            # VCL NAL 是帧边界：提交累积的 access unit
                            if acc:
                                try:
                                    for frame in codec.decode(av.Packet(acc)):
                                        arr = frame.to_ndarray(format="yuv420p")
                                        if self._q.full():
                                            try:
                                                self._q.get_nowait()
                                            except Exception:
                                                pass
                                        self._q.put((arr, frame.width, frame.height))
                                except Exception:
                                    pass
                                acc = b""
                            acc = nal
                        else:
                            acc += nal
                        idx = sc2
                    buf = buf[idx:] if idx else buf
            except Exception:
                pass
            finally:
                if proc is not None:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
            # screenrecord 3 分钟到期/出错后重启
            time.sleep(0.5)

    def stop(self):
        self._running = False

    async def recv(self):
        loop = asyncio.get_event_loop()
        item = await loop.run_in_executor(None, lambda: self._q.get(timeout=5))
        arr, w, h = item
        frame = av.VideoFrame.from_ndarray(arr, format="yuv420p")
        # 用真实单调时钟生成 pts（90000 时基），避免固定 fps 推算导致
        # screenrecord 实际帧率与请求 fps 不符时取帧节奏错乱（画面卡/加速）
        now = time.monotonic()
        if self._pts_base <= 0:
            self._pts_base = now
            self._last_pts = 0
        else:
            dt = now - self._pts_base
            self._last_pts = max(self._last_pts + 1, int(dt * 90000))
        frame.pts = self._last_pts
        self._idx += 1
        return frame


def _verify_token(token: str) -> bool:
    try:
        import jwt as _jwt

        _jwt.decode(token, current_app.config["JWT_SECRET"], algorithms=["HS256"])
        return True
    except Exception:
        return False


async def _negotiate_in_loop(device_id: int, serial: str, offer_sdp: str,
                             bitrate: int, fps: int):
    """在事件循环线程内完成 PC 创建与协商（aiortc 强依赖 loop）。"""
    pc = RTCPeerConnection()
    track = H264StreamTrack(serial, bitrate, fps)
    pc.addTrack(track)

    with _conn_lock:
        _connections[device_id] = (pc, track)

    @pc.on("connectionstatechange")
    def _on_state():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            track.stop()
            with _conn_lock:
                if _connections.get(device_id, (None,))[0] is pc:
                    _connections.pop(device_id, None)
            try:
                asyncio.ensure_future(pc.close())
            except Exception:
                pass

    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    # 等待 ICE 候选收集（局域网 host 候选立即完成）
    deadline = time.time() + 3
    while time.time() < deadline:
        if pc.iceGatheringState == "complete":
            break
        await asyncio.sleep(0.1)
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


@webrtc_bp.route("/devices/<int:device_id>/webrtc/offer", methods=["POST"])
def webrtc_offer(device_id: int):
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token or not _verify_token(token):
        return jsonify({"error": "unauthorized"}), 401

    serial = _get_serial(device_id)
    if serial is None:
        return jsonify({"error": "device not found or not redroid"}), 404

    data = request.get_json(silent=True) or {}
    offer_sdp = data.get("sdp", "")
    if not offer_sdp:
        return jsonify({"error": "missing sdp"}), 400

    bitrate = int(data.get("bitrate", 4000000))
    fps = int(data.get("fps", 60))

    # 关键：同一设备已有 WebRTC 连接时，必须先停掉旧 track 并关闭旧 pc。
    # 否则重试时旧 screenrecord 进程继续运行，Android screenrecord 单实例独占
    # → 新旧全部卡死 0 帧（黑屏根因之一）。
    with _conn_lock:
        old = _connections.pop(device_id, None)
    if old:
        try:
            old[1].stop()
        except Exception:
            pass
        try:
            _loop_for = _get_loop()
            asyncio.run_coroutine_threadsafe(old[0].close(), _loop_for)
        except Exception:
            pass

    # Android screenrecord 同一设备只允许一个实例：
    # WebRTC 推流前先停掉该设备现有的共享 WebSocket 广播流，避免 screenrecord 冲突导致 0 帧
    from .stream import _active_streams, _stream_lock
    with _stream_lock:
        info = _active_streams.get(device_id)
        if info:
            for c in info["clients"]:
                try:
                    c.close()
                except Exception:
                    pass
            _active_streams.pop(device_id, None)
    time.sleep(0.6)  # 等 worker 线程退出、screenrecord 释放

    try:
        loop = _get_loop()
        fut = asyncio.run_coroutine_threadsafe(
            _negotiate_in_loop(device_id, serial, offer_sdp, bitrate, fps), loop
        )
        result = fut.result(timeout=15)
        return jsonify(result)
    except Exception as e:
        import traceback
        try:
            import os as _os
            _log = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "webrtc_err.log")
            with open(_log, "a") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " offer err: " + repr(e) + "\n")
                f.write(traceback.format_exc() + "\n")
        except Exception:
            pass
        # 协商失败：清理该设备的连接
        with _conn_lock:
            item = _connections.pop(device_id, None)
        if item:
            try:
                item[1].stop()
            except Exception:
                pass
            try:
                loop = _get_loop()
                asyncio.run_coroutine_threadsafe(item[0].close(), loop)
            except Exception:
                pass
        return jsonify({"error": str(e)}), 500


@webrtc_bp.route("/devices/<int:device_id>/webrtc/close", methods=["POST"])
def webrtc_close(device_id: int):
    """客户端放弃 WebRTC / 关闭投屏窗口时调用：停止推流、释放 screenrecord。"""
    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
    if not token or not _verify_token(token):
        return jsonify({"error": "unauthorized"}), 401
    with _conn_lock:
        item = _connections.pop(device_id, None)
    if item:
        try:
            item[1].stop()
        except Exception:
            pass
        try:
            loop = _get_loop()
            asyncio.run_coroutine_threadsafe(item[0].close(), loop)
        except Exception:
            pass
    return jsonify({"ok": True})
