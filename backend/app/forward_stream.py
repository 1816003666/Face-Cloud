"""转发流（流媒体房间）与媒体服务器状态同步。

平台自身不实现 WebRTC/RTMP 媒体转发，媒体接入由外部媒体服务器承担。
本模块负责：

1. 生成对外可用的推流地址前缀（WebRTC / RTMP），供客户端「流媒体」页展示；
2. 若配置了媒体服务器 HTTP API（STREAM_MEDIA_API），读取其真实流列表，
   把在线状态 / 码率 / 来源 IP 同步进房间记录（同时兼容 SRS 与 ZLMediaKit）；
3. 停止房间时尽力通知媒体服务器断开该流。

未配置媒体服务器时，房间状态保持 waiting（等待推流），**不会伪造在线状态**。
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .extensions import db
from .models import ForwardStream

# 码率采样缓存：room_id -> (累计字节数, 采样时间)。
# 媒体服务器 API 一般只给累计字节，这里按两次采样的差值算瞬时码率。
_bitrate_cache: dict = {}


def _cfg(key, default=""):
    """读配置：优先 Flask config，脱离应用上下文时退回环境变量。"""
    try:
        from flask import current_app
        return current_app.config.get(key, default)
    except RuntimeError:
        return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# 推流地址
# ---------------------------------------------------------------------------
def public_host(request=None) -> str:
    """对外可用的媒体服务器主机名。

    优先 STREAM_PUBLIC_HOST（部署时显式指定，例如媒体服务器与后端不同机），
    否则用当前请求的 Host（客户端访问哪个地址就用哪个地址）。
    """
    host = str(_cfg("STREAM_PUBLIC_HOST", "") or "").strip()
    if host:
        return host.split("://")[-1].split("/")[0]
    if request is not None:
        raw = str(getattr(request, "host", "") or "").strip()
        if raw:
            # 去掉端口；IPv6 形如 [::1]:8003
            if raw.startswith("["):
                return raw.split("]")[0].lstrip("[")
            return raw.split(":")[0]
    return "127.0.0.1"


def stream_config(request=None) -> dict:
    """推流地址配置（客户端「转发流」页顶部展示的两行地址）。

    WebRTC/RTMP 基地址可用 STREAM_WEBRTC_BASE / STREAM_RTMP_BASE 精确指定
    （例如 MediaMTX 的 WHIP 地址是 http://host:8889/live），否则按 Host 推导。
    """
    host = public_host(request)
    prefix = str(_cfg("STREAM_WEBRTC_PREFIX", "live") or "live").strip("/") or "live"
    try:
        rtmp_port = int(_cfg("STREAM_RTMP_PORT", 1935))
    except (TypeError, ValueError):
        rtmp_port = 1935
    api = str(_cfg("STREAM_MEDIA_API", "") or "").strip()

    webrtc_base = str(_cfg("STREAM_WEBRTC_BASE", "") or "").strip().rstrip("/")
    rtmp_base = str(_cfg("STREAM_RTMP_BASE", "") or "").strip().rstrip("/")
    hls_base = str(_cfg("STREAM_HLS_BASE", "") or "").strip().rstrip("/")
    webrtc = webrtc_base or f"webrtc://{host}/{prefix}"
    rtmp = rtmp_base or f"rtmp://{host}:{rtmp_port}/{prefix}"

    publish_user = str(_cfg("STREAM_PUBLISH_USER", "") or "").strip()
    publish_pass = str(_cfg("STREAM_PUBLISH_PASS", "") or "").strip()
    auth_on = bool(publish_user and publish_pass)
    cfg = {
        # 基地址不带账号：RTMP 的账号要跟在流名后面（MediaMTX 从 query 取），
        # HTTP(WHIP/WHEP) 走 Basic 认证，播放地址不需要账号。
        "webrtc": webrtc,
        "rtmp": rtmp,
        "webrtc_path": prefix,
        "rtmp_port": rtmp_port,
        # 是否已接入媒体服务器：未接入时房间只能登记，看不到真实在线状态
        "media_api_configured": bool(api),
        # 是否开了推流鉴权（前端据此显示"推流账号"一行）
        "publish_auth": auth_on,
    }
    if auth_on:
        cfg["publish_user"] = publish_user
        cfg["publish_pass"] = publish_pass
    if hls_base:
        cfg["hls"] = hls_base

    # 用法提示：把房间号和鉴权写法都写清楚（RTMP 用 query，WHIP 用 Basic）
    q = ""
    basic = ""
    if auth_on:
        q = (f"?user={urllib.parse.quote(publish_user, safe='')}"
             f"&pass={urllib.parse.quote(publish_pass, safe='')}")
        basic = f"（HTTP Basic：{publish_user} / {publish_pass}）"
    if webrtc.lower().startswith("http"):
        cfg["hint"] = (f"WebRTC(WHIP) 推流 {webrtc}/<房间号>/whip{basic}；"
                       f"RTMP 推流 {rtmp}/<房间号>{q}")
    else:
        cfg["hint"] = f"推流地址后追加 /<房间号>{q}，例如 {rtmp}/1001{q}"
    return cfg


# ---------------------------------------------------------------------------
# 媒体服务器（SRS / ZLMediaKit）状态
# ---------------------------------------------------------------------------
def rtmp_url(room_id: str, request=None) -> str:
    """RTMP 播放地址（VLC 等播放器用；读流不需要账号）。"""
    if not room_id:
        return ""
    base = str(stream_config(request).get("rtmp", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/{urllib.parse.quote(str(room_id), safe='')}"


def whep_url(room_id: str, request=None) -> str:
    """WebRTC(WHEP) 播放地址。

    云手机自带浏览器（Chromium 系）不原生支持 HLS，但有 WebRTC，
    所以给云手机播放画面走 WHEP，延迟也更低。
    """
    if not room_id:
        return ""
    base = str(stream_config(request).get("webrtc", "") or "").rstrip("/")
    if not base.lower().startswith("http"):
        return ""
    return f"{base}/{urllib.parse.quote(str(room_id), safe='')}/whep"


def hls_url(room_id: str, request=None) -> str:
    """HLS 播放地址（浏览器/播放器通用，但 Android 浏览器不支持 HLS）。"""
    if not room_id:
        return ""
    base = str(stream_config(request).get("hls", "") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/{urllib.parse.quote(str(room_id), safe='')}/index.m3u8"


def _http_json(url: str, timeout: float = 4.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, ValueError, OSError):
        return None


def _http_get(url: str, timeout: float = 4.0) -> None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            resp.read(256)
    except Exception:  # noqa: BLE001  尽力而为，失败不影响本地登记
        pass


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bitrate_from_total(room_id: str, total_bytes: int) -> int:
    """由「累计字节数」的两次采样差值估算瞬时码率（bps）。"""
    now = time.time()
    prev = _bitrate_cache.get(room_id)
    _bitrate_cache[room_id] = (total_bytes, now)
    if not prev or total_bytes < prev[0]:
        return 0
    dt = now - prev[1]
    if dt <= 0:
        return 0
    return int((total_bytes - prev[0]) * 8 / dt)


def _fetch_mediamtx(api: str):
    """MediaMTX：GET /v3/paths/list（在线流）+ rtmp/webrtc 连接（取来源 IP）。"""
    data = _http_json(api + "/v3/paths/list")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    sources = {}
    for ep in ("/v3/rtmpconns/list", "/v3/webrtcsessions/list"):
        conns = _http_json(api + ep)
        if isinstance(conns, dict) and isinstance(conns.get("items"), list):
            for conn in conns["items"]:
                if not isinstance(conn, dict):
                    continue
                name = str(conn.get("path") or "")
                addr = str(conn.get("remoteAddr") or "")
                if name and addr and str(conn.get("state") or "publish") == "publish":
                    sources.setdefault(name, addr.split(":")[0])
    out = []
    for item in data["items"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        room = name.split("/")[-1]
        out.append({
            "room_id": room,
            "app": name.split("/")[0] if "/" in name else "",
            "bitrate": _bitrate_from_total(room, _int(item.get("bytesReceived"))),
            "source_ip": sources.get(name, ""),
            "live": bool(item.get("ready")),
        })
    return out


def fetch_media_streams():
    """读取媒体服务器上正在推的流；未配置或不可达返回 None（区别于「没有流」= []）。

    返回 [{room_id, app, bitrate(bps), source_ip, live}, ...]
    兼容 MediaMTX（/v3/paths/list）、ZLMediaKit（/index/api/getMediaList）、SRS（/api/v1/streams）。
    """
    api = str(_cfg("STREAM_MEDIA_API", "") or "").strip().rstrip("/")
    if not api:
        return None

    # MediaMTX
    streams = _fetch_mediamtx(api)
    if streams is not None:
        return streams

    # ZLMediaKit：GET /index/api/getMediaList
    data = _http_json(api + "/index/api/getMediaList")
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        out = []
        for m in data["data"]:
            if not isinstance(m, dict):
                continue
            stream = str(m.get("stream") or "").strip()
            if not stream:
                continue
            out.append({
                "room_id": stream,
                "app": str(m.get("app") or "live"),
                "bitrate": _int(m.get("bytesSpeed")) * 8,   # bytes/s -> bps
                "source_ip": str(m.get("originUrl") or m.get("vhost") or ""),
                "live": True,
            })
        return out

    # SRS：GET /api/v1/streams
    data = _http_json(api + "/api/v1/streams")
    if isinstance(data, dict) and isinstance(data.get("streams"), list):
        out = []
        for s in data["streams"]:
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "").strip()
            if not name:
                continue
            kbps = s.get("kbps") or {}
            rate = max(_int(kbps.get("recv_30s")), _int(kbps.get("send_30s")))
            publish = s.get("publish") or {}
            out.append({
                "room_id": name.split("/")[-1],
                "app": str(s.get("app") or "live"),
                "bitrate": rate * 1000,                       # kbps -> bps
                "source_ip": str(publish.get("ip") or s.get("client_ip") or ""),
                "live": bool(publish.get("active", True)),
            })
        return out

    return None


def sync_media_state() -> bool:
    """把媒体服务器的真实状态同步到房间记录。已同步返回 True。"""
    streams = fetch_media_streams()
    if streams is None:
        return False

    live = {s["room_id"]: s for s in streams if s.get("live")}
    rows = db.session.query(ForwardStream).all()
    known = {r.room_id for r in rows}
    changed = False

    for r in rows:
        s = live.get(r.room_id)
        if s:
            if (r.status != "live" or r.bitrate != s["bitrate"]
                    or (r.source_ip or "") != s["source_ip"]):
                r.status = "live"
                r.bitrate = s["bitrate"]
                r.source_ip = s["source_ip"]
                r.stopped_at = None
                r.updated_at = datetime.utcnow()
                changed = True
        elif r.status == "live":
            # 流已经断了：回到等待推流，而不是继续显示"直播中"
            r.status = "waiting"
            r.bitrate = 0
            r.source_ip = ""
            r.updated_at = datetime.utcnow()
            changed = True

    # 外部直接推上来的流也纳入列表（否则"推了却看不到"）
    for room_id, s in live.items():
        if room_id not in known:
            db.session.add(ForwardStream(
                room_id=room_id, status="live",
                bitrate=s["bitrate"], source_ip=s["source_ip"],
            ))
            changed = True

    if changed:
        db.session.commit()
    return True


def stop_media_stream(room_id: str) -> None:
    """停止房间时尽力通知媒体服务器踢流（未配置则忽略）。"""
    api = str(_cfg("STREAM_MEDIA_API", "") or "").strip().rstrip("/")
    if not api or not room_id:
        return
    q = urllib.parse.quote(str(room_id))
    # ZLMediaKit：close_streams
    _http_get(f"{api}/index/api/close_streams?force=1&stream={q}")
    # SRS：按 stream id 删除
    data = _http_json(api + "/api/v1/streams")
    if isinstance(data, dict):
        for s in data.get("streams") or []:
            if not isinstance(s, dict):
                continue
            if str(s.get("name") or "").split("/")[-1] == str(room_id) and s.get("id"):
                _http_get(f"{api}/api/v1/streams/{urllib.parse.quote(str(s['id']))}")
