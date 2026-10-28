"""流媒体（转发流）API。

对应客户端「流媒体 → 转发流」页：

    GET    /api/streams                 房间列表（含真实在线状态/码率/来源 IP）
    POST   /api/streams                 新建房间 {room_id}
    DELETE /api/streams/<room_id>       停止房间（从列表移除，并尽力踢掉媒体服务器上的流）
    GET    /api/streams/config          推流地址配置（WebRTC / RTMP）

平台不做媒体转发：房间是「登记 + 状态同步」，真实推流由外部媒体服务器
（SRS / ZLMediaKit）承担，见 app/forward_stream.py。
"""
import re

from flask import Blueprint, Response, g, jsonify, request
from sqlalchemy import select

from .. import forward_stream as fs
from ..auth import login_required
from ..extensions import db
from ..models import ForwardStream, record_audit, iso_utc

streams_bp = Blueprint("streams", __name__)

ROOM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _room_dict(row: ForwardStream) -> dict:
    return {
        "id": row.id,
        "room_id": row.room_id,
        "status": row.status,                 # waiting / live / stopped
        "source_ip": row.source_ip or "",
        "bitrate": row.bitrate or 0,          # bps（客户端按 /1000 显示 kbps）
        "created_at": iso_utc(row.created_at),
        "updated_at": iso_utc(row.updated_at),
    }


@streams_bp.get("/streams")
@login_required
def list_streams():
    # 配了媒体服务器就刷新真实状态；没配则保持 waiting
    try:
        fs.sync_media_state()
    except Exception:  # noqa: BLE001  媒体服务器抖动不应让接口失败
        db.session.rollback()
    rows = (db.session.execute(
        select(ForwardStream).order_by(ForwardStream.created_at.desc())
    ).scalars().all())
    return jsonify([_room_dict(r) for r in rows])


@streams_bp.post("/streams")
@login_required
def create_stream():
    data = request.get_json(silent=True) or {}
    room_id = str(data.get("room_id") or "").strip()
    if not room_id:
        return jsonify(error="缺少房间号"), 400
    if not ROOM_ID_RE.match(room_id):
        return jsonify(error="房间号只能包含字母/数字/下划线/短横线，最长 64 个字符"), 400

    row = db.session.execute(
        select(ForwardStream).where(ForwardStream.room_id == room_id)
    ).scalar_one_or_none()
    created = row is None
    if created:
        row = ForwardStream(room_id=room_id, status="waiting",
                            created_by=getattr(g.current_user, "id", None))
        db.session.add(row)
    else:
        # 已存在则重新激活（可能之前已停止/掉线）
        row.status = "waiting"
        row.bitrate = 0
        row.source_ip = ""
        row.stopped_at = None
    db.session.commit()
    record_audit(g.current_user.id, "create_stream", "forward_stream", row.id,
                 {"room_id": room_id})
    body = _room_dict(row)
    body["config"] = fs.stream_config(request)
    return jsonify(body), (201 if created else 200)


@streams_bp.delete("/streams/<room_id>")
@login_required
def stop_stream(room_id):
    row = db.session.execute(
        select(ForwardStream).where(ForwardStream.room_id == room_id)
    ).scalar_one_or_none()
    if row is None:
        return jsonify(error="房间不存在"), 404
    # 尽力通知媒体服务器断开该流（未配置媒体服务器时是空操作）
    try:
        fs.stop_media_stream(room_id)
    except Exception:  # noqa: BLE001
        pass
    db.session.delete(row)
    db.session.commit()
    record_audit(g.current_user.id, "stop_stream", "forward_stream", row.id,
                 {"room_id": room_id})
    return jsonify(ok=True, room_id=room_id, status="stopped")


@streams_bp.get("/streams/config")
@login_required
def get_stream_config():
    return jsonify(fs.stream_config(request))


# ---------------------------------------------------------------------------
# 极简播放页（给云手机浏览器用）
# ---------------------------------------------------------------------------
# 云手机自带浏览器是 Chromium 系：**不原生支持 HLS**（直接开 m3u8 是黑屏），
# 但有 WebRTC，所以用 WHEP 播放，延迟也更低。该页面不校验登录（云手机浏览器没有 token），
# 只暴露播放能力，且播放本身在媒体服务器侧就是开放的。
PLAYER_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>云手机摄像头预览</title>
<style>
  html,body{margin:0;height:100%;background:#000;overflow:hidden}
  video{width:100%;height:100%;object-fit:contain;background:#000}
  #msg{position:fixed;left:0;right:0;top:0;color:#fff;font:13px/1.5 sans-serif;padding:8px 10px;background:rgba(0,0,0,.55)}
</style></head>
<body>
<video id="v" autoplay muted playsinline></video>
<div id="msg">正在连接…</div>
<script>
const WHEP = "__WHEP__", HLS = "__HLS__", ROOM = "__ROOM__";
const v = document.getElementById("v"), msg = document.getElementById("msg");
function say(t){ msg.textContent = t; msg.style.display = t ? "block" : "none"; }
async function playWhep(){
  const pc = new RTCPeerConnection();
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.addTransceiver("audio", { direction: "recvonly" });
  pc.ontrack = (e) => { v.srcObject = e.streams[0]; say(""); };
  pc.onconnectionstatechange = () => { if (["failed","disconnected"].indexOf(pc.connectionState) >= 0) say("连接中断：" + pc.connectionState); };
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((r) => { if (pc.iceGatheringState === "complete") return r(); pc.onicegatheringstatechange = () => { if (pc.iceGatheringState === "complete") r(); }; setTimeout(r, 3000); });
  const res = await fetch(WHEP, { method: "POST", headers: { "Content-Type": "application/sdp" }, body: pc.localDescription.sdp });
  if (!res.ok) throw new Error("WHEP " + res.status);
  await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
}
function diag(){
  const ms = (window.MediaSource && MediaSource.isTypeSupported) ? MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E"') : false;
  const caps = (window.RTCRtpReceiver && RTCRtpReceiver.getCapabilities) ? RTCRtpReceiver.getCapabilities("video") : null;
  const cs = caps ? caps.codecs.map((c) => c.mimeType.replace("video/", "")).join(",") : "n/a";
  return "H264:typeSupported=" + ms + " | WebRTC解码器=" + cs;
}
// 移动端浏览器可能拦截自动播放：失败时给一个大按钮，点一下即可播放
const go = document.createElement("button");
go.textContent = "点击播放";
go.style.cssText = "position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:9;padding:14px 28px;font:16px sans-serif;border:0;border-radius:26px;background:#14b8a6;color:#fff";
go.onclick = () => { go.remove(); v.play().catch((e) => say("播放失败：" + e.message)); };
function tryPlay(){
  const p = v.play();
  if (p && p.catch) p.catch(() => { if (!document.body.contains(go)) document.body.appendChild(go); });
}
v.addEventListener("playing", () => { if (v.videoWidth > 0) say(""); if (document.body.contains(go)) go.remove(); });
(async () => {
  say("连接中…");
  try {
    await playWhep();
    tryPlay();
    setTimeout(() => { if (!v.videoWidth) { say(""); if (!document.body.contains(go)) document.body.appendChild(go); } }, 2500);
  } catch (e) {
    say("播放失败：" + e.message + " | " + diag() + (HLS ? " | 备用 HLS：" + HLS : ""));
  }
})();
</script></body></html>"""


@streams_bp.get("/streams/player/<room_id>")
def stream_player(room_id):
    whep = fs.whep_url(room_id, request)
    hls = fs.hls_url(room_id, request)
    if not whep and not hls:
        return jsonify(error="未配置媒体服务器播放地址（STREAM_WEBRTC_BASE / STREAM_HLS_BASE）"), 501
    body = (PLAYER_HTML
            .replace("__WHEP__", whep)
            .replace("__HLS__", hls)
            .replace("__ROOM__", room_id))
    return Response(body, mimetype="text/html")
