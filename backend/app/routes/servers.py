"""服务器管理：管理员自助登记承载云手机的服务器，通过 SSH 发现并导入其中的云手机。

接口（均 require_role admin）：
    GET    /servers                 列表
    POST   /servers/test            测试 SSH 连通（不入库）
    POST   /servers/discover        连接服务器并发现云手机（不入库）
    POST   /servers                 新增服务器（可选同时导入发现的云手机）
    POST   /servers/<id>/rediscover 对已保存服务器重新扫描
    POST   /servers/<id>/import     把发现的云手机批量导入设备库
    PATCH  /servers/<id>            修改服务器信息
    DELETE /servers/<id>            删除服务器（不删已导入设备）
"""
from datetime import datetime
import subprocess

from flask import request, jsonify, g, Blueprint
from sqlalchemy import func

from ..extensions import db
from ..models import Device, Server, record_audit
from ..auth import require_role, require_perm
from .. import ssh_probe, relay

servers_bp = Blueprint("servers", __name__)


def _utcnow():
    return datetime.utcnow()


def _extract_params(data, base=None):
    """从请求体提取 SSH 参数（缺省回落已保存服务器的配置）。"""
    b = base or {}
    return {
        "host": (data.get("host") or b.get("host") or "").strip(),
        "port": int(data.get("port") or b.get("port") or 22),
        "username": (data.get("username") or b.get("username") or "root").strip(),
        "auth_type": (data.get("auth_type") or b.get("auth_type") or "password").strip(),
        "secret": data.get("secret") if data.get("secret") is not None else (b.get("secret") or ""),
        "container_prefix": (data.get("container_prefix") or b.get("container_prefix") or "redroid").strip(),
        "adb_port_start": int(data.get("adb_port_start") or b.get("adb_port_start") or 5555),
        "adb_port_end": int(data.get("adb_port_end") or b.get("adb_port_end") or 5699),
    }


def _local_adb_serials():
    """本机 adb 当前能看到的 serial 集合；取不到返回 None（表示未知，不参与判断）。"""
    try:
        from .. import orchestrator
        out = subprocess.run([orchestrator._adb_path(), "devices"],
                             capture_output=True, timeout=15)
        txt = (out.stdout or b"").decode("utf-8", "ignore")
        return {line.split("\t")[0] for line in txt.splitlines()
                if line.strip().endswith("\tdevice")}
    except Exception:  # noqa: BLE001
        return None


def _is_attached(d, existing, local_serials):
    """判定这台设备是否真的已经接入并在用。

    只看"库里有没有同 serial"是不够的：库里可能留着旧的 USB serial 记录
    （例如设备原先插在另一台服务器上，后来搬走了），那条记录本机根本连不上、
    一直是离线。这种情况应该允许用新的中继地址**重新接入**，而不是跳过。
    """
    for s in (d.get("local_serial") or "", d["serial"]):
        if not s or s not in existing:
            continue
        if local_serials is None or s in local_serials:
            return True
    return False


def _attach_relays(result, params):
    """给发现结果里本机连不上的设备建立 SSH 中继，填上 local_serial。

    服务器上的云手机本机常常连不上（USB 号，或 192.168.99.x 这类服务器内网段）。
    中继之后平台就能用 127.0.0.1:<端口> 正常 adb connect。
    """
    if not result.get("online"):
        return result
    for d in result.get("devices", []):
        if d.get("access") == "direct":
            d["local_serial"] = d["serial"]
            continue
        try:
            lp = relay.ensure_relay(
                params["host"], params["username"], params.get("secret") or "",
                d.get("target_host"), d.get("target_port"),
                ssh_port=params.get("port") or 22,
                auth_type=params.get("auth_type") or "password",
            )
            d["local_serial"] = f"127.0.0.1:{lp}"
            d["relay_port"] = lp
        except Exception as e:  # noqa: BLE001 - 单台中继失败不影响其它设备
            d["local_serial"] = ""
            d["relay_error"] = str(e)[:120]
    return result


@servers_bp.get("/servers")
@require_perm("server.view")
def list_servers():
    """服务器列表。linked_devices = 该服务器已接入设备库的云手机数量（用于删除时提示）。"""
    rows = db.session.query(Server).order_by(Server.id).all()
    counts = {}
    if rows:
        q = (db.session.query(Device.ip, func.count(Device.id))
             .filter(Device.ip.in_([r.host for r in rows]))
             .group_by(Device.ip).all())
        counts = {ip: n for ip, n in q}
    return jsonify([{**r.to_dict(), "linked_devices": counts.get(r.host, 0)} for r in rows])


@servers_bp.post("/servers/test")
@require_perm("server.manage")
def test_server():
    """测试 SSH 连通性，body 给完整连接参数即可。"""
    data = request.get_json(silent=True) or {}
    params = _extract_params(data)
    if not params["host"]:
        return jsonify(error="请填写服务器地址"), 400
    ok, info = ssh_probe.test_connection(params)
    info["ok"] = ok
    return jsonify(info)


@servers_bp.post("/servers/discover")
@require_perm("server.manage")
def discover_phones():
    """连接远程服务器并发现云手机（不写库）。

    body: SSH 参数 + {"probe_models": true}，或 {"server_id": N} 复用已保存凭据。
    返回：{online, hostname, docker, source, devices:[{serial,name,container,status,port,model,...}], message}
    """
    data = request.get_json(silent=True) or {}
    server = None
    if data.get("server_id"):
        server = db.session.get(Server, int(data["server_id"]))
        if server is None:
            return jsonify(error="服务器不存在"), 404
    params = _extract_params(data, server.to_dict(with_secret=True) if server else None)
    if not params["host"]:
        return jsonify(error="请填写服务器地址"), 400

    result = ssh_probe.probe_server(params, probe_models=bool(data.get("probe_models", True)))

    _attach_relays(result, params)

    # 标记哪些已经在用（库中同 serial **且** 本机 adb 真的连得上）
    existing = {d.serial for d in db.session.query(Device.serial).all()}
    local_serials = _local_adb_serials()
    for d in result.get("devices", []):
        d["already_imported"] = _is_attached(d, existing, local_serials)
        # 库里有同名但连不上的旧记录 → 提示可以重新接入
        d["reattach"] = (
            not d["already_imported"]
            and (d["serial"] in existing or (d.get("local_serial") or "") in existing)
        )

    # 刷新已保存服务器的状态
    if server is not None:
        server.status = "online" if result.get("online") else "offline"
        server.last_checked = _utcnow()
        server.device_count = len(result.get("devices", []))
        server.last_error = "" if result.get("online") else (result.get("message") or "")
        db.session.commit()

    return jsonify(result)


@servers_bp.post("/servers")
@require_perm("server.manage")
def create_server():
    """新增服务器。body 带 import_devices（发现的 serial 列表）可一并导入云手机。"""
    cur = g.current_user
    data = request.get_json(silent=True) or {}
    host = (data.get("host") or "").strip()
    if not host:
        return jsonify(error="请填写服务器地址"), 400
    name = (data.get("name") or host).strip()

    dup = db.session.query(Server).filter(Server.host == host).first()
    if dup:
        return jsonify(error=f"服务器 {host} 已存在（{dup.name}）"), 409

    params = _extract_params(data)
    s = Server(
        name=name,
        host=host,
        port=params["port"],
        username=params["username"],
        auth_type=params["auth_type"],
        secret=params["secret"],
        container_prefix=params["container_prefix"],
        adb_port_start=params["adb_port_start"],
        adb_port_end=params["adb_port_end"],
        note=(data.get("note") or "").strip(),
        created_by=cur.id,
        created_at=_utcnow(),
    )

    imported = {"ok": 0, "skipped": 0}
    if data.get("discover"):
        result = ssh_probe.probe_server(params, probe_models=False)
        s.status = "online" if result.get("online") else "offline"
        s.last_checked = _utcnow()
        s.device_count = len(result.get("devices", []))
        s.last_error = "" if result.get("online") else (result.get("message") or "")

    db.session.add(s)
    db.session.commit()

    devices = data.get("import_devices") or []
    if devices:
        imported = _do_import(devices, s.host, cur.id)

    record_audit(cur.id, "create_server", "server", s.id,
                 {"host": host, "imported": imported["ok"]})
    return jsonify({**s.to_dict(), "imported": imported}), 201


@servers_bp.post("/servers/<int:sid>/rediscover")
@require_perm("server.manage")
def rediscover_server(sid):
    """对已保存服务器重新 SSH 扫描，更新在线状态与云手机数量。"""
    s = db.session.get(Server, sid)
    if s is None:
        return jsonify(error="服务器不存在"), 404
    params = _extract_params({}, s.to_dict(with_secret=True))
    result = ssh_probe.probe_server(params, probe_models=bool(
        (request.get_json(silent=True) or {}).get("probe_models", True)))
    _attach_relays(result, params)
    existing = {d.serial for d in db.session.query(Device.serial).all()}
    local_serials = _local_adb_serials()
    for d in result.get("devices", []):
        d["already_imported"] = _is_attached(d, existing, local_serials)
        d["reattach"] = (
            not d["already_imported"]
            and (d["serial"] in existing or (d.get("local_serial") or "") in existing)
        )
    s.status = "online" if result.get("online") else "offline"
    s.last_checked = _utcnow()
    s.device_count = len(result.get("devices", []))
    s.last_error = "" if result.get("online") else (result.get("message") or "")
    db.session.commit()
    return jsonify({**result, "server": s.to_dict()})


@servers_bp.post("/servers/<int:sid>/import")
@require_perm("server.manage")
def import_from_server(sid):
    """把发现的云手机批量导入设备库。

    body: {"devices": [{serial, name, model, android, container, status}, ...]}
    """
    cur = g.current_user
    s = db.session.get(Server, sid)
    if s is None:
        return jsonify(error="服务器不存在"), 404
    data = request.get_json(silent=True) or {}
    devices = data.get("devices") or []
    if not devices:
        return jsonify(error="请先勾选要导入的云手机"), 400
    result = _do_import(devices, s.host, cur.id)
    s.device_count = db.session.query(Device).filter(
        Device.ip == s.host, Device.backend == "redroid").count()
    db.session.commit()
    record_audit(cur.id, "import_server_devices", "server", sid,
                 {"host": s.host, **result})
    return jsonify(result)


def _do_import(items, host, user_id):
    """按 serial 幂等导入 Device（已存在则跳过；连不上的旧记录则重新接入）。"""
    import json as _json

    ok = 0
    skipped = 0
    reattached = 0
    added = []
    for it in items:
        serial = (it.get("serial") or "").strip()
        if not serial:
            continue
        exists = db.session.query(Device).filter(Device.serial == serial).first()
        if exists:
            skipped += 1
            continue
        # 库里留着同设备但连不上的旧记录（如旧的 USB serial）→ 就地更新为可连地址，
        # 而不是新插一条重复的。
        old = None
        origin = (it.get("origin_serial") or "").strip()
        if origin and origin != serial:
            old = db.session.query(Device).filter(Device.serial == origin).first()
        if old is not None:
            old.serial = serial
            old.name = (it.get("name") or old.name or serial).strip()[:64]
            old.ip = host or old.ip
            # 原先走 ssh_bridge（远程代执行），现在本机 adb 经中继能直连了，
            # 改回 redroid，否则控制接口会报"ssh_bridge control 未实现"。
            old.backend = "redroid"
            old.status = "running" if (it.get("status") or "running") == "running" else "stopped"
            old.last_seen = _utcnow()
            fp = {}
            try:
                fp = _json.loads(old.fingerprint or "{}")
            except Exception:  # noqa: BLE001
                fp = {}
            if it.get("model"):
                fp["model"] = it["model"]
            if it.get("android"):
                fp["android"] = it["android"]
            # 记下服务器侧的稳定地址（192.168.99.x:5555）：本机中继端口每次重启都会变，
            # 只有它能让程序在重启后把设备重新接回来。
            if origin:
                fp["remote_serial"] = origin
            old.fingerprint = _json.dumps(fp, ensure_ascii=False)
            reattached += 1
            added.append(serial)
            continue
        # 经 SSH 中继接入的设备 serial 是 127.0.0.1:<端口>，那只是本机回环地址，
        # 真正的归属是服务器本身，所以 ip 记服务器 host（用于按服务器筛选）。
        ip = host or (serial.split(":")[0] if ":" in serial else "")
        model = (it.get("model") or "").strip()
        android = (it.get("android") or "").strip()
        fp = {}
        if model:
            fp["model"] = model
        if android:
            fp["android"] = android
        if it.get("container"):
            fp["container"] = it["container"]
        if it.get("origin_serial"):
            fp["remote_serial"] = it["origin_serial"]
        elif not serial.startswith("127.0.0.1"):
            fp["remote_serial"] = serial
        import json as _json
        d = Device(
            # 服务器导入的设备属于平台公共池，显式置空，避免被默认企业回填吞掉
            enterprise_id=None,
            name=(it.get("name") or serial).strip()[:64],
            serial=serial,
            backend="redroid",
            status="running" if (it.get("status") or "running") == "running" else "stopped",
            ip=ip,
            fingerprint=_json.dumps(fp, ensure_ascii=False),
            created_by=user_id,
            created_at=_utcnow(),
            last_seen=_utcnow(),
        )
        db.session.add(d)
        added.append(serial)
        ok += 1
    db.session.commit()
    # 导入后立刻连一次：经中继接入的设备 serial 是本机回环地址，
    # 平台不会自动 connect，不连的话设备会一直显示离线。
    for serial in added:
        _adb_connect(serial)
    return {"ok": ok, "skipped": skipped, "reattached": reattached, "serials": added}


def _adb_connect(serial):
    """尽力连一次 adb（失败不影响导入结果）。"""
    if not serial.startswith("127.0.0.1:"):
        return
    try:
        from .. import orchestrator
        subprocess.run([orchestrator._adb_path(), "connect", serial],
                       capture_output=True, timeout=12)
    except Exception:  # noqa: BLE001
        pass


@servers_bp.patch("/servers/<int:sid>")
@require_perm("server.manage")
def update_server(sid):
    cur = g.current_user
    s = db.session.get(Server, sid)
    if s is None:
        return jsonify(error="服务器不存在"), 404
    data = request.get_json(silent=True) or {}
    for field in ("name", "host", "username", "auth_type", "secret",
                  "container_prefix", "note"):
        if field in data and data[field] is not None:
            setattr(s, field, (data[field] or "").strip() if isinstance(data[field], str) else data[field])
    if "port" in data and data["port"]:
        s.port = int(data["port"])
    if "adb_port_start" in data and data["adb_port_start"]:
        s.adb_port_start = int(data["adb_port_start"])
    if "adb_port_end" in data and data["adb_port_end"]:
        s.adb_port_end = int(data["adb_port_end"])
    db.session.commit()
    record_audit(cur.id, "update_server", "server", sid, {"name": s.name})
    return jsonify(s.to_dict())


@servers_bp.delete("/servers/<int:sid>")
@require_perm("server.manage")
def delete_server(sid):
    """删除服务器记录。

    默认只移除服务器，已导入的云手机保留在设备库；
    带 ?remove_devices=1 时连同该服务器下的云手机一起删除。
    """
    cur = g.current_user
    remove_devices = (request.args.get("remove_devices") or "").lower() in ("1", "true", "yes")
    s = db.session.get(Server, sid)
    if s is None:
        return jsonify(error="服务器不存在"), 404

    removed = 0
    if remove_devices:
        devs = db.session.query(Device).filter(Device.ip == s.host).all()
        for d in devs:
            db.session.delete(d)      # device_logs 由 cascade 自动清理
            removed += 1

    info = {"host": s.host, "name": s.name, "removed_devices": removed}
    db.session.delete(s)
    db.session.commit()
    record_audit(cur.id, "delete_server", "server", sid, info)
    return jsonify(ok=True, removed_devices=removed)
